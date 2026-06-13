"""Flat-field capture loop for radiometric FFC (Phase 10 R-26).

Drives the IED-backed Orchestrator N times to capture blank-light flat frames,
keeps all N demosaiced frames to build a flat_stack (NxHxWx3 uint16), averages
them for the uniformity-improvement metric, and returns (flat_stack, FlatFieldResult).

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
- Memory note: keeping N full-res uint16 HxWx3 frames in memory is an
  acceptable one-time calibration cost (a 7CR frame ≈ 54 MP × 6 bytes × N ≈
  1.7 GB for N=8 — large but bounded; full memory optimisation deferred to M2).
  The caller can pass flat_data_path to persist the stack and free RAM promptly.
"""
from __future__ import annotations

import shutil
import tempfile
import time
from dataclasses import replace
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
) -> tuple[np.ndarray, FlatFieldResult]:
    """Capture N blank-light flat frames and return (flat_stack, FlatFieldResult).

    Reuses ``Orchestrator.capture_triplet()`` (IED-backed loop) so all
    the existing inbox/quarantine/retry logic is inherited rather than
    reimplemented (NFR-14).

    The returned ``flat_stack`` is the primary output: an NxHxWx3 uint16
    array that can be passed directly to ``apply_ffc_radiometric(raw,
    flat_stack, black_levels)``.  The averaging happens inside
    ``apply_ffc_radiometric`` (single averaging point, maximum precision).
    The ``FlatFieldResult`` carries metadata (n_frames, black levels, CV
    metric) for JSON serialisation and audit trail.

    Memory note: keeping N full-res frames in memory is an acceptable
    one-time calibration cost.  Full optimisation (streaming to disk during
    capture) is deferred to Milestone 2.  If ``flat_data_path`` is a
    non-empty string the stack is persisted to that path via ``np.save``
    (the caller can then load it with ``np.load`` and free the in-memory
    copy if needed).

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
        flat_data_path:   If non-empty, the flat_stack is saved to this path
                          via ``np.save``.  The path is also stored in
                          FlatFieldResult.flat_data_path for reference.
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
        (flat_stack, FlatFieldResult) where flat_stack is NxHxWx3 uint16 and
        FlatFieldResult carries n_frames_averaged, black levels,
        working_brightness, uniformity_improvement, and flat_data_path.

    Raises:
        ValueError: if n_frames < 1.
        RuntimeError: if any individual flat capture fails.
    """
    if n_frames < 1:
        raise ValueError(f"n_frames must be >= 1, got {n_frames}")

    # 1. Warmup sleep (noop in tests via injected sleep=lambda _: None)
    sleep(warmup_s)

    # Capture flats into an ISOLATED temp directory, NOT the roll's output
    # folder. capture_triplet(retake=True) names files {roll}_FrameNNN_{ch}.ARW
    # at the CURRENT frame number; writing those into the roll folder would
    # overwrite/leave junk in a real roll frame (flats are blank-light, not roll
    # frames). The ARWs are intermediate — demosaiced into `frames` in memory —
    # so the temp dir is removed afterward.
    frames: list[np.ndarray] = []                              # HxWx3 uint16, len = N
    sums: list[Optional[np.ndarray]] = [None, None, None]     # per channel float64 sum
    first_channels: list[Optional[np.ndarray]] = [None, None, None]  # for CV(single)

    flats_dir = Path(tempfile.mkdtemp(prefix="trirgb_flats_"))
    orch: Optional[Orchestrator] = None
    try:
        # 2. Construct one Orchestrator reusing the existing IED-backed path,
        #    pointed at the isolated flats dir. Pass sleep through (noop in tests).
        orch = Orchestrator(
            scanlight,
            replace(settings, output_folder=flats_dir),
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

        # 4. Capture N frames; keep all demosaiced HxWx3 uint16 frames in a list.
        for i in range(n_frames):
            # retake=True so the frame counter does not advance — flat frames
            # are not part of the roll sequence.
            result = orch.capture_triplet(retake=True)
            if not result.success:
                raise RuntimeError(f"flat capture {i} failed: {result.error}")

            # Accumulate the full HxWx3 frame for the flat_stack primary output.
            # Demosaic each channel file; note all three channels of a given frame
            # are demosaiced from separate R/G/B exposures, so we collect them into
            # one HxWx3 array aligned to the canonical channel order.
            frame_channels: list[np.ndarray] = []
            for ch_idx, ch in enumerate("RGB"):
                img = dem(result.files[ch])             # HxWx3 uint16
                channel_hw = img[..., ch_idx]           # HxW uint16 — matching channel
                frame_channels.append(channel_hw)

                # Accumulate for the CV metric (float64 for accuracy)
                channel_f64 = channel_hw.astype(np.float64)
                if sums[ch_idx] is None:
                    sums[ch_idx] = channel_f64.copy()
                    first_channels[ch_idx] = channel_hw.copy()
                else:
                    sums[ch_idx] += channel_f64  # type: ignore[operator]

            # Reconstruct an HxWx3 uint16 frame from the three matching channels.
            # Each ch_idx slot holds the radiometrically-correct channel from its
            # respective narrow-band exposure (R-lit→ch0, G-lit→ch1, B-lit→ch2).
            frame_hwx3 = np.stack(frame_channels, axis=-1)   # HxWx3 uint16
            frames.append(frame_hwx3)
    finally:
        # Close any persistent sony-capture session this Orchestrator opened
        # (sdk_persistent default + no injected runner) so a direct library
        # caller can't leak the --persist child / camera session. No-op when the
        # caller injected a runner (it owns the session). Then drop the temp ARWs.
        if orch is not None:
            orch.close()
        shutil.rmtree(flats_dir, ignore_errors=True)

    # 5. Stack all frames into the flat_stack primary output: NxHxWx3 uint16.
    flat_stack = np.stack(frames, axis=0)    # (N, H, W, 3)

    # 6. Build per-channel averaged flat for the CV metric.
    #    float32 is sufficient for the metric; the single authoritative
    #    averaging is done inside apply_ffc_radiometric (not here).
    h, w = sums[0].shape  # type: ignore[union-attr]
    avg_channels = [
        (sums[ch].astype(np.float32) / n_frames)  # type: ignore[union-attr]
        for ch in range(3)
    ]

    # 7. Uniformity-improvement metric on the G channel (index 1).
    #    CV(single_frame) / CV(averaged) — higher is better (Pitfall 6: floor 1e-9).
    single_cv = _cv(first_channels[1])  # type: ignore[arg-type]
    avg_cv = _cv(avg_channels[1])
    uniformity_improvement = single_cv / max(avg_cv, 1e-9)

    # 8. Resolve working brightness.
    if working_brightness is None:
        working_brightness = settings.level_g

    # 9. Persist flat_stack to disk if a path was provided.
    #    np.save creates an .npy file at the given path (appends .npy if absent).
    #    The caller can np.load it later and free the in-memory flat_stack.
    if flat_data_path:
        np.save(flat_data_path, flat_stack)

    # 10. Build FlatFieldResult metadata record.
    result_meta = FlatFieldResult(
        flat_data_path=flat_data_path,
        n_frames_averaged=n_frames,
        warmup_s=warmup_s,
        black_level_r=float(black_levels[0].black_level),
        black_level_g=float(black_levels[1].black_level),
        black_level_b=float(black_levels[2].black_level),
        working_brightness=working_brightness,
        uniformity_improvement=float(uniformity_improvement),
    )

    return flat_stack, result_meta
