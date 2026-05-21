"""Flat-field capture loop for radiometric FFC (Phase 10 R-26).

Drives the Config-B Orchestrator N times to capture blank-light flat frames,
averages them in linear space per channel, computes the uniformity-improvement
metric, and returns a FlatFieldResult.

Design notes
------------
- REUSES ``Orchestrator.capture_triplet()`` via the ``sony_capture_runner=``
  injection seam (NFR-14 — do NOT bypass the Orchestrator abstraction).
- Hardware-free in tests: inject a stub runner (like ``make_runner`` in
  test_orchestrator.py) and a fake ``demosaic_fn`` that returns synthetic
  arrays from c41_core.fixtures.  No rawpy is imported in that path (Pitfall 5).
- Warmup sleep is injectable (default ``time.sleep``); tests pass
  ``lambda _: None`` for instant, deterministic runs (NFR-12).
- ``capture_triplet(retake=True)`` is used so the Orchestrator frame counter
  does not advance on each flat — flats are not part of the roll sequence.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from c41_core import ChannelCalibration, FlatFieldResult
from .orchestrator import Orchestrator, CaptureSettings


def _cv(channel_hw: np.ndarray) -> float:
    """Coefficient of variation using the production _box_filter_2d.

    Imported lazily to avoid pulling in the full rgb_composite stack at
    module load time (matches the lazy-import pattern in _demosaic_cal_frame).
    """
    from rgb_composite.ffc import _box_filter_2d
    h, w = channel_hw.shape
    kernel = max(3, int(min(h, w) * 0.05))
    smoothed = _box_filter_2d(channel_hw.astype(np.float32), kernel)
    mean_val = float(np.mean(smoothed))
    return float(np.std(smoothed)) / max(mean_val, 1.0)


def capture_flats(
    scanlight,
    settings: CaptureSettings,
    black_levels: tuple[ChannelCalibration, ChannelCalibration, ChannelCalibration],
    *,
    n_frames: int = 8,
    warmup_s: float = 5.0,
    working_brightness: Optional[int] = None,
    flat_data_path: str = "",
    sony_capture_runner: Optional[Callable[[str, Path, int], int]] = None,
    sleep: Callable[[float], None] = time.sleep,
    demosaic_fn: Optional[Callable[[Path], np.ndarray]] = None,
) -> FlatFieldResult:
    """Capture N blank-light flat frames and return a FlatFieldResult.

    Reuses ``Orchestrator.capture_triplet()`` (Config-B loop) so all
    the existing inbox/quarantine/retry logic is inherited rather than
    reimplemented (NFR-14).

    Args:
        scanlight:        Scanlight instance (already connected).
        settings:         CaptureSettings; used verbatim — caller sets the
                          LED levels / output_folder / settle_ms etc.
        black_levels:     3-tuple of ChannelCalibration, indexed [0]=R [1]=G [2]=B.
                          Each ``.black_level`` is recorded in FlatFieldResult.
        n_frames:         Number of flat frames to capture and average.
        warmup_s:         Seconds to sleep after LED on before the first capture.
                          In tests, pass ``sleep=lambda _: None`` to skip.
        working_brightness: LED level recorded in FlatFieldResult.  Defaults
                          to ``settings.level_g`` if None.
        flat_data_path:   Path/id string for the caller to reference where the
                          averaged flat is (or will be) stored on disk.
        sony_capture_runner: Injected runner for the Orchestrator (same kwarg
                          as ``Orchestrator.__init__``).  Tests pass a stub.
        sleep:            Injectable sleep callable; default ``time.sleep``.
                          Tests pass ``lambda _: None`` for instant warmup.
        demosaic_fn:      Injectable demosaic callable; default lazily imports
                          ``_demosaic_cal_frame`` from ``rgb_composite.ffc``
                          (which pulls rawpy only when called — not at import).
                          Tests inject a fake that returns synthetic arrays so
                          rawpy is never touched in the test path (Pitfall 5).

    Returns:
        FlatFieldResult with n_frames_averaged, black levels, working_brightness,
        and uniformity_improvement (CV ratio on the G channel).

    Raises:
        ValueError: if n_frames < 1.
        RuntimeError: if any individual flat capture fails.
    """
    if n_frames < 1:
        raise ValueError(f"n_frames must be >= 1, got {n_frames}")

    # 1. Warmup sleep (noop in tests via injected sleep=lambda _: None)
    sleep(warmup_s)

    # 2. Construct one Orchestrator reusing the existing Config-B path.
    #    Pass sleep through so settle delays are also noop in tests.
    orch = Orchestrator(
        scanlight,
        settings,
        sony_capture_runner=sony_capture_runner,
        sleep=sleep,
    )

    # 3. Resolve demosaic: use the injected fn if provided, else lazy-import
    #    _demosaic_cal_frame.  This ensures rawpy is never pulled in when
    #    demosaic_fn is set (hardware-free tests never touch rawpy).
    if demosaic_fn is not None:
        dem = demosaic_fn
    else:
        def dem(path: Path) -> np.ndarray:
            from rgb_composite.ffc import _demosaic_cal_frame
            return _demosaic_cal_frame(path)

    # 4. Capture N frames and accumulate per-channel sums in float64.
    #    Accumulators are initialised lazily on the first frame once shapes
    #    are known (avoids needing to know HxW before capturing).
    sums: list[Optional[np.ndarray]] = [None, None, None]   # per channel index
    first_channels: list[Optional[np.ndarray]] = [None, None, None]  # for CV(single)

    for i in range(n_frames):
        # retake=True so the frame counter does not advance — flat frames
        # are not part of the roll sequence.
        result = orch.capture_triplet(retake=True)
        if not result.success:
            raise RuntimeError(f"flat capture {i} failed: {result.error}")

        for ch_idx, ch in enumerate("RGB"):
            img = dem(result.files[ch])          # HxWx3 uint16
            channel_data = img[..., ch_idx].astype(np.float64)
            if sums[ch_idx] is None:
                sums[ch_idx] = channel_data.copy()
                first_channels[ch_idx] = img[..., ch_idx].copy()
            else:
                sums[ch_idx] += channel_data  # type: ignore[operator]

    # 5. Compute per-channel averages; reassemble HxWx3 float32 averaged flat.
    h, w = sums[0].shape  # type: ignore[union-attr]
    avg_channels = [
        (sums[ch].astype(np.float32) / n_frames)  # type: ignore[union-attr]
        for ch in range(3)
    ]

    # 6. Uniformity-improvement metric on the G channel (index 1).
    #    CV(single_frame) / CV(averaged) — higher is better (Pitfall 6: floor 1e-9).
    single_cv = _cv(first_channels[1])  # type: ignore[arg-type]
    avg_cv = _cv(avg_channels[1])
    uniformity_improvement = single_cv / max(avg_cv, 1e-9)

    # 7. Resolve working brightness.
    if working_brightness is None:
        working_brightness = settings.level_g

    # 8. Construct and return FlatFieldResult.
    return FlatFieldResult(
        flat_data_path=flat_data_path,
        n_frames_averaged=n_frames,
        warmup_s=warmup_s,
        black_level_r=float(black_levels[0].black_level),
        black_level_g=float(black_levels[1].black_level),
        black_level_b=float(black_levels[2].black_level),
        working_brightness=working_brightness,
        uniformity_improvement=float(uniformity_improvement),
    )
