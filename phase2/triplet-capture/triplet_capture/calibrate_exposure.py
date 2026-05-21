"""Per-channel exposure auto-calibration for the Config-B capture loop (Phase 12 R-27).

Drives the Orchestrator to capture a dark frame (LED levels 0), measure
per-channel black levels, then independently bisects the integer LED level
[1, 255] for each channel until the rebate's p99 (black-subtracted,
raw-linear) reaches ~95% of the 16-bit clip ceiling.  Returns a
``CalibrationResult`` with three ``ChannelCalibration`` records.

Design notes
------------
- REUSES ``Orchestrator.capture_triplet(retake=True)`` via the
  ``sony_capture_runner=`` injection seam (NFR-14 — do NOT bypass
  the Orchestrator abstraction).
- Hardware-free in tests: inject ``demosaic_factory`` (a callable that
  accepts the Orchestrator and returns a ``Callable[[Path], np.ndarray]``);
  the closure binds to the LIVE Orchestrator so ``orch.settings.level_*``
  is current at demosaic call time.  No rawpy is imported in that path
  (Pitfall 5).
- Warmup sleep is injectable (default ``time.sleep``); tests pass
  ``lambda _: None`` for instant, deterministic runs (NFR-12).
- ``retake=True`` so the Orchestrator frame counter does not advance on
  calibration captures — they are not part of the roll sequence (Pitfall 4).
- Black-subtracted ROI is ``np.clip(..., 0, None)`` before percentile
  (Pitfall 3) to prevent negative values distorting p99.
- Channel index LOCKED: R=0, G=1, B=2 (project convention; Pitfall 2).
- Bisection on integer [1, 255] terminates in ≤8 steps; MAX_ITER=10
  guarantees bounded execution (T-12-01).
- Non-convergence (e.g. blue maxed at 255, still under target) records
  ``best_level_so_far = 255`` and sets ``clip_fraction`` for the wizard
  (Phase 14) to surface numerically; ``gain > 0`` always holds (T-12-02).
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from c41_core import ChannelCalibration, CalibrationResult
from c41_core.contracts import BaseRegionDescriptor
from .orchestrator import Orchestrator, CaptureSettings

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

_CLIP_CEILING: int = 65535
_SATURATION_VALUE: int = 64000  # reuse inspect-calibration.py threshold for clip_fraction
_CH_IDX: dict[str, int] = {"R": 0, "G": 1, "B": 2}
_CH_LEVEL_FIELD: dict[str, str] = {"R": "level_r", "G": "level_g", "B": "level_b"}


def calibrate_exposure(
    scanlight,
    settings: CaptureSettings,
    base_region: BaseRegionDescriptor,
    *,
    ffc_cal_dir: str = "",
    max_iterations: int = 10,
    target_fraction: float = 0.95,
    tolerance: int = 500,
    warmup_s: float = 2.0,
    sony_capture_runner: Optional[Callable[[str, Path, int], int]] = None,
    sleep: Callable[[float], None] = time.sleep,
    demosaic_factory: Optional[Callable[["Orchestrator"], Callable[[Path], np.ndarray]]] = None,
) -> CalibrationResult:
    """Auto-tune each LED channel's exposure referenced to the rebate histogram.

    Drives the Config-B Orchestrator loop to:
      1. Capture a dark frame (all LEDs at 0) to measure per-channel black levels.
      2. For each channel (R, G, B) independently, bisect the integer LED level
         [1, 255] until the rebate's p99 (black-subtracted) reaches ~95% of the
         16-bit clip ceiling.
      3. Assemble and return a CalibrationResult.

    Blue ends up highest automatically because the orange mask attenuates blue
    ~2-4 stops.  No by-eye color judgment is performed.

    Args:
        scanlight:         Scanlight instance (already connected).
        settings:          CaptureSettings; used for Orchestrator construction.
                           LED levels are mutated during calibration via
                           update_settings() but the original settings object
                           is not modified (dataclasses.replace semantics).
        base_region:       BaseRegionDescriptor for the rebate strip ROI.
                           Produced by Phase 09 detect_rebate / manual picker.
        ffc_cal_dir:       Path reference for FFC calibration data (stored in
                           CalibrationResult; not used internally).
        max_iterations:    Upper bound on bisection iterations per channel
                           (default 10; bisection on [0,255] needs ≤8 to
                           converge to 1-count precision).
        target_fraction:   Target rebate p99 as fraction of clip ceiling
                           (default 0.95 → 62258 counts).
        tolerance:         Convergence tolerance in raw counts (default 500,
                           ~0.8% of full scale).
        warmup_s:          Seconds to sleep after Orchestrator construction
                           before the dark frame.  Inject sleep=lambda _: None
                           for instant tests.
        sony_capture_runner: Injected runner for the Orchestrator (same kwarg
                           as Orchestrator.__init__).  Tests pass a stub.
        sleep:             Injectable sleep callable; default time.sleep.
                           Tests pass lambda _: None for instant warmup.
        demosaic_factory:  Optional factory callable — called as
                           ``demosaic_factory(orch)`` after Orchestrator
                           construction to produce a ``Callable[[Path], np.ndarray]``
                           demosaic function that closes over the LIVE
                           ``orch.settings``.  Tests inject this seam for
                           hardware-free operation.  When None, the default
                           ``_demosaic_cal_frame`` from ``rgb_composite.ffc``
                           is used via lazy import (so rawpy is never touched
                           when a factory is injected — Pitfall 5).

    Returns:
        CalibrationResult with three ChannelCalibration records (r/g/b),
        the passed base_region, and ffc_cal_dir.

    Raises:
        RuntimeError: if any capture (dark frame or calibration frame) fails.
        ValueError: if max_iterations < 1, or if a channel produces no signal
            after black-level subtraction (dark frame brighter than signal or
            black_level miscalibrated).
    """
    # Guard: caller must allow at least one iteration
    if max_iterations < 1:
        raise ValueError(f"max_iterations must be >= 1, got {max_iterations}")

    # Derive the numeric target
    target: int = int(target_fraction * _CLIP_CEILING)

    # ------------------------------------------------------------------
    # STEP 0: Warmup sleep + construct ONE Orchestrator (NFR-14 pattern)
    # ------------------------------------------------------------------
    sleep(warmup_s)

    orch = Orchestrator(
        scanlight,
        settings,
        sony_capture_runner=sony_capture_runner,
        sleep=sleep,
    )

    # ------------------------------------------------------------------
    # STEP 0b: Resolve demosaic seam (Pitfall 5 — lazy import)
    # ------------------------------------------------------------------
    if demosaic_factory is not None:
        dem = demosaic_factory(orch)
    else:
        def dem(path: Path) -> np.ndarray:  # pragma: no cover
            from rgb_composite.ffc import _demosaic_cal_frame
            return _demosaic_cal_frame(path)

    # ------------------------------------------------------------------
    # STEP 1: Dark frame — measure per-channel black levels
    # Level 0 is valid for CaptureSettings (0 <= v <= 255); all LEDs off.
    # ------------------------------------------------------------------
    orch.update_settings(level_r=0, level_g=0, level_b=0)
    dark_result = orch.capture_triplet(retake=True)
    if not dark_result.success:
        raise RuntimeError(f"dark-frame capture failed: {dark_result.error}")

    black_level: dict[str, float] = {}
    for ch, ch_idx in _CH_IDX.items():
        dark_img = dem(dark_result.files[ch])                        # HxWx3 uint16
        dark_ch = dark_img[..., ch_idx].astype(np.float64)           # HxW
        black_level[ch] = float(np.mean(dark_ch))

    # ------------------------------------------------------------------
    # STEP 2: Per-channel bisection calibration loop
    # ------------------------------------------------------------------
    ch_cals: dict[str, ChannelCalibration] = {}

    for ch in ("R", "G", "B"):
        ch_idx = _CH_IDX[ch]
        level_field = _CH_LEVEL_FIELD[ch]
        other_fields = {_CH_LEVEL_FIELD[c]: 0 for c in ("R", "G", "B") if c != ch}

        lo: int = 1       # 0 reserved for dark frame
        hi: int = 255
        probe_level: int = (lo + hi) // 2   # current bisection probe (not the best-seen)
        best_p99: float = 0.0
        best_level_so_far: int = probe_level
        converged: bool = False
        final_roi_raw: Optional[np.ndarray] = None
        best_final_roi_raw: Optional[np.ndarray] = None

        for _iteration in range(max_iterations):
            # Light only this channel; keep roll_name unchanged (Pitfall 4)
            orch.update_settings(**{level_field: probe_level, **other_fields})

            result = orch.capture_triplet(retake=True)
            if not result.success:
                raise RuntimeError(
                    f"calibration capture failed for channel {ch}: {result.error}"
                )

            img = dem(result.files[ch])   # HxWx3 uint16

            # Extract ROI for the lit channel only (Pitfall 2 — index ch_idx)
            # Slice order: [rows, cols] = [y:y+h, x:x+w] (Pitfall 6)
            roi_raw = img[
                base_region.y : base_region.y + base_region.h,
                base_region.x : base_region.x + base_region.w,
                ch_idx,
            ]   # HxW uint16

            # Black-level subtraction, clip at 0 before percentile (Pitfall 3)
            roi_f32 = np.clip(
                roi_raw.astype(np.float32) - black_level[ch], 0.0, None
            )

            p99 = float(np.percentile(roi_f32, 99))

            # Track best-effort for non-convergence path
            if abs(p99 - target) < abs(best_p99 - target):
                best_p99 = p99
                best_level_so_far = probe_level
                best_final_roi_raw = roi_raw.copy()

            final_roi_raw = roi_raw  # last iteration's raw ROI

            # Convergence check
            if abs(p99 - target) <= tolerance:
                converged = True
                break

            # Bisection step
            if p99 < target:
                lo = probe_level + 1   # need more light
            else:
                hi = probe_level - 1   # too bright, back off

            if lo > hi:
                break   # search space exhausted (e.g. maxed at 255, still under target)

            probe_level = (lo + hi) // 2

        # ------------------------------------------------------------------
        # Post-loop: resolve converged level and compute clip_fraction
        # ------------------------------------------------------------------
        # Fail-closed: if every probe produced p99==0.0 the dark frame was brighter
        # than the calibration signal — return an error rather than a silent garbage
        # result (WR-02).
        if best_p99 == 0.0:
            raise ValueError(
                f"channel {ch}: rebate signal is zero after black subtraction at every "
                f"probe level — dark frame (black_level={black_level[ch]:.1f}) is "
                f"brighter than the calibration signal, or black_level is miscalibrated"
            )

        # Always use best_level_so_far for both the converged and non-convergence
        # paths (WR-01/WR-04: symmetric branches, probe_level != best_level_so_far
        # if convergence criterion were ever loosened).
        converged_level = best_level_so_far
        clip_roi = best_final_roi_raw if best_final_roi_raw is not None else final_roi_raw

        # clip_fraction: fraction of RAW (pre-subtraction) rebate pixels at or above
        # saturation threshold (near 0 for non-convergence / never-reached-target case)
        if clip_roi is not None:
            clip_fraction = float(np.mean(clip_roi >= _SATURATION_VALUE))
        else:
            clip_fraction = 0.0

        # Clamp to [0, 1] for safety (np.mean output should be in range, but guard anyway)
        clip_fraction = float(np.clip(clip_fraction, 0.0, 1.0))

        # gain = TARGET / max(converged_level, 1) — always > 0 (T-12-02)
        gain = float(target) / max(converged_level, 1)

        ch_cals[ch] = ChannelCalibration(
            channel=ch,  # type: ignore[arg-type]
            led_level=converged_level,
            black_level=black_level[ch],
            gain=gain,
            clip_fraction=clip_fraction,
        )

    # ------------------------------------------------------------------
    # STEP 3: Assemble and return CalibrationResult
    # ------------------------------------------------------------------
    return CalibrationResult(
        r=ch_cals["R"],
        g=ch_cals["G"],
        b=ch_cals["B"],
        base_region=base_region,
        ffc_cal_dir=ffc_cal_dir,
    )
