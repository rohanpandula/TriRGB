"""Tests for calibrate_exposure — hardware-free via stub runner + closure-over-orch demosaic.

Mirrors the stub patterns from test_capture_flats.py:
  - FakeScanlight: duck-type for set_color/off
  - make_runner: writes a plausible-sized fake RAW file; returns (runner, calls)
  - settings fixture: settle_ms=0 so no real sleep in tests
  - make_calibration_demosaic: returns a closure over orch.settings so
    per-channel brightness SCALES with the live LED level (not parsed from calls)

STUB MODEL (load-bearing — encodes the SC-3/SC-4 oracle):
  p99 = level * SCALE[ch] + BLACK_OFFSET
  where SCALE is chosen so convergence targets are:
    R → level ≈ 180, G → level ≈ 160, B → level ≈ 230
  This guarantees SC-3 analytically: B converges highest.

All tests are fully hardware-free (NFR-12): no rawpy, no real hardware.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

import numpy as np
import pytest

from c41_core import ChannelCalibration, CalibrationResult
from c41_core.contracts import BaseRegionDescriptor
from triplet_capture.orchestrator import CaptureSettings, Orchestrator

# RED: import calibrate_exposure — this module does not exist yet.
# Collection will fail here until Task 2 creates it.
from triplet_capture.calibrate_exposure import calibrate_exposure


# ---------------------------------------------------------------------------
# SCALE constants — the oracle for SC-3 / SC-4
# ---------------------------------------------------------------------------

# TARGET = int(0.95 * 65535) = 62258
# SCALE chosen so convergence levels are BY CONSTRUCTION:
#   R → ~180,  G → ~160,  B → ~230   (blue highest → SC-3 holds)
TARGET = int(0.95 * 65535)          # 62258
BLACK_OFFSET: float = 256.0

SCALE: dict[str, float] = {
    "R": (TARGET - BLACK_OFFSET) / 180.0,   # ≈ 344.5  → level_R* ≈ 180
    "G": (TARGET - BLACK_OFFSET) / 160.0,   # ≈ 387.5  → level_G* ≈ 160
    "B": (TARGET - BLACK_OFFSET) / 230.0,   # ≈ 269.6  → level_B* ≈ 230
}

# Non-convergence scale: make blue require level > 255 to force non-convergence path.
# p99 = level * SCALE_NO_CONV_B + BLACK_OFFSET; target = 62258
# SCALE_NO_CONV_B * 255 + 256 = only ~32000 → well below target at max LED
SCALE_NO_CONV: dict[str, float] = {
    "R": SCALE["R"],
    "G": SCALE["G"],
    "B": (TARGET - BLACK_OFFSET) / 500.0,  # needs level ~500 → clips at 255
}

# Image dimensions to match the base_region fixture
_IMG_H = 128
_IMG_W = 192


# ---------------------------------------------------------------------------
# Stubs (mirror test_capture_flats.py patterns)
# ---------------------------------------------------------------------------

class FakeScanlight:
    """Minimal duck-type covering set_color/off used by the Orchestrator."""

    def __init__(self):
        self.calls: list[tuple] = []

    def set_color(self, r=0, g=0, b=0, w=0, save=False):
        self.calls.append(("set_color", r, g, b, w, save))

    def off(self):
        self.calls.append(("off",))


def make_runner(success_size: int = 70 * 1024 * 1024):
    """Returns (runner, calls). Runner writes a fake RAW and returns 0."""
    calls: list[tuple[str, Path, int]] = []

    def runner(channel: str, out_path: Path, timeout_s: int) -> int:
        calls.append((channel, out_path, timeout_s))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"\x00" * success_size)
        return 0

    return runner, calls


def make_calibration_demosaic(
    orch: Orchestrator,
    *,
    black_offset: float = BLACK_OFFSET,
    scale: dict[str, float] | None = None,
) -> Callable[[Path], np.ndarray]:
    """Factory that returns a demosaic closure over orch.settings.

    At call time, reads orch.settings.level_r/g/b (LIVE — update_settings
    runs BEFORE the runner in the Orchestrator, so the level is current;
    Pitfall 1 — do NOT parse scanlight.calls).

    Brightness model: p99 ≈ level * SCALE[ch] + black_offset
    SCALE chosen so all three channels converge within [0,255] and blue
    ends highest (SC-3 holds analytically).

    Args:
        orch: the Orchestrator instance the loop drives.
        black_offset: uniform dark-frame value per channel.
        scale: per-channel slope {R, G, B}. Defaults to module SCALE.
    """
    if scale is None:
        scale = SCALE

    CH_IDX = {"R": 0, "G": 1, "B": 2}
    CLIP = 65535

    def demosaic_fn(path: Path) -> np.ndarray:
        # Read LIVE LED levels from the orchestrator settings — NOT from calls.
        level_r = orch.settings.level_r
        level_g = orch.settings.level_g
        level_b = orch.settings.level_b

        levels = {"R": level_r, "G": level_g, "B": level_b}

        # Build HxWx3 float array; each channel is level * scale + black_offset
        img = np.zeros((_IMG_H, _IMG_W, 3), dtype=np.float32)
        for ch, ch_idx in CH_IDX.items():
            brightness = levels[ch] * scale[ch] + black_offset
            img[:, :, ch_idx] = brightness

        # Add small deterministic noise seeded from LED levels (NFR-11)
        rng = np.random.default_rng(level_r * 1000 + level_g * 100 + level_b)
        noise = rng.normal(0.0, 20.0, size=(_IMG_H, _IMG_W, 3)).astype(np.float32)
        return np.clip(img + noise, 0, CLIP).astype(np.uint16)

    return demosaic_fn


def make_base_region() -> BaseRegionDescriptor:
    """Return a BaseRegionDescriptor covering (most of) the 128x192 test frame."""
    return BaseRegionDescriptor(
        x=4, y=4, w=184, h=120,
        base_rgb=(8930.0, 12097.0, 2952.0),
        uniformity_cv=1.5,
        source="auto",
    )


@pytest.fixture
def settings(tmp_path):
    """CaptureSettings with settle_ms=0 and a stable roll_name to avoid resets."""
    return CaptureSettings(
        roll_name="CalibExposure",
        frame_number=1,
        output_folder=tmp_path,
        level_r=128,
        level_g=128,
        level_b=128,
        settle_ms=0,
    )


def _run_calibrate(tmp_path, *, scale=None) -> tuple[CalibrationResult, list]:
    """Helper: run calibrate_exposure with the closure-based demosaic factory."""
    runner, calls = make_runner()

    def demosaic_factory(orch: Orchestrator):
        return make_calibration_demosaic(orch, scale=scale)

    cal = calibrate_exposure(
        scanlight=FakeScanlight(),
        settings=CaptureSettings(
            roll_name="CalibExposure",
            frame_number=1,
            output_folder=tmp_path,
            level_r=128,
            level_g=128,
            level_b=128,
            settle_ms=0,
        ),
        base_region=make_base_region(),
        ffc_cal_dir="",
        max_iterations=10,
        warmup_s=0.0,
        sony_capture_runner=runner,
        sleep=lambda _: None,
        demosaic_factory=demosaic_factory,
    )
    return cal, calls


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_calibrate_exposure_returns_calibration_result(tmp_path):
    """calibrate_exposure returns a valid CalibrationResult with 3 ChannelCalibration.

    Verifies: channel literals, gain > 0, led_level in [0, 255].
    """
    cal, _ = _run_calibrate(tmp_path)

    assert isinstance(cal, CalibrationResult)
    assert isinstance(cal.r, ChannelCalibration)
    assert isinstance(cal.g, ChannelCalibration)
    assert isinstance(cal.b, ChannelCalibration)

    assert cal.r.channel == "R"
    assert cal.g.channel == "G"
    assert cal.b.channel == "B"

    for ch_cal in (cal.r, cal.g, cal.b):
        assert ch_cal.gain > 0, f"gain must be > 0, got {ch_cal.gain} for {ch_cal.channel}"
        assert 0 <= ch_cal.led_level <= 255, (
            f"led_level out of range: {ch_cal.led_level} for {ch_cal.channel}"
        )


def test_blue_led_level_highest(tmp_path):
    """SC-3: orange-mask attenuation forces blue LED level above R and G."""
    cal, _ = _run_calibrate(tmp_path)

    assert cal.b.led_level > cal.r.led_level, (
        f"SC-3 fail: blue led_level={cal.b.led_level} must exceed "
        f"red led_level={cal.r.led_level}"
    )
    assert cal.b.led_level > cal.g.led_level, (
        f"SC-3 fail: blue led_level={cal.b.led_level} must exceed "
        f"green led_level={cal.g.led_level}"
    )


def test_black_subtraction_shifts_histogram(tmp_path):
    """SC-4: per-channel black-level subtraction shifts p99 by exactly the known offset.

    Tests the numeric property directly: p99(roi) - p99(clip(roi - BLACK_OFFSET, 0)) == BLACK_OFFSET
    for a roi well above BLACK_OFFSET (so clipping at 0 does not eat the shift).
    """
    BRIGHT = 30000.0
    roi_raw = np.full((32, 32), BRIGHT, dtype=np.float32)

    p99_before = float(np.percentile(roi_raw, 99))
    p99_after = float(np.percentile(np.clip(roi_raw - BLACK_OFFSET, 0, None), 99))

    assert abs((p99_before - p99_after) - BLACK_OFFSET) < 1.0, (
        f"SC-4 fail: expected shift={BLACK_OFFSET}, "
        f"got shift={p99_before - p99_after:.2f}"
    )

    # End-to-end: verify that the CalibrationResult.black_level values
    # match the known BLACK_OFFSET from the dark-frame stub.
    cal, _ = _run_calibrate(tmp_path)
    for ch_cal in (cal.r, cal.g, cal.b):
        assert abs(ch_cal.black_level - BLACK_OFFSET) < 50.0, (
            f"black_level for {ch_cal.channel} should be near {BLACK_OFFSET}, "
            f"got {ch_cal.black_level:.2f}"
        )


def test_calibration_result_json_roundtrip(tmp_path):
    """CalibrationResult.from_json(cal.to_json()) == cal (JSON round-trip)."""
    cal, _ = _run_calibrate(tmp_path)

    restored = CalibrationResult.from_json(cal.to_json())
    assert restored == cal, (
        "JSON round-trip failed: original and restored CalibrationResult differ"
    )


def test_loop_terminates_bounded(tmp_path):
    """The runner call count is bounded: <= 3 (dark frame) + 3 channels * max_iterations * 3.

    Also verifies the function returns (does not hang).
    """
    runner, calls = make_runner()
    max_iterations = 10

    def demosaic_factory(orch: Orchestrator):
        return make_calibration_demosaic(orch)

    cal = calibrate_exposure(
        scanlight=FakeScanlight(),
        settings=CaptureSettings(
            roll_name="CalibExposure",
            frame_number=1,
            output_folder=tmp_path,
            level_r=128,
            level_g=128,
            level_b=128,
            settle_ms=0,
        ),
        base_region=make_base_region(),
        ffc_cal_dir="",
        max_iterations=max_iterations,
        warmup_s=0.0,
        sony_capture_runner=runner,
        sleep=lambda _: None,
        demosaic_factory=demosaic_factory,
    )

    assert isinstance(cal, CalibrationResult)

    # Upper bound: 1 dark-frame triplet (3 calls) + 3 channels * max_iterations triplets * 3 channels-per-triplet
    max_calls = 3 + 3 * max_iterations * 3
    assert len(calls) <= max_calls, (
        f"runner called {len(calls)} times, exceeds ceiling of {max_calls}"
    )
    assert len(calls) > 0, "runner must have been called at least once"


def test_non_convergence_records_flag(tmp_path):
    """With a scale so blue cannot reach target at level 255:
    cal.b.led_level == 255 and cal.b.clip_fraction is a finite float in [0, 1].
    The function still returns a valid CalibrationResult (fail-closed numeric).
    """
    runner, _ = make_runner()

    def demosaic_factory(orch: Orchestrator):
        return make_calibration_demosaic(orch, scale=SCALE_NO_CONV)

    cal = calibrate_exposure(
        scanlight=FakeScanlight(),
        settings=CaptureSettings(
            roll_name="CalibExposure",
            frame_number=1,
            output_folder=tmp_path,
            level_r=128,
            level_g=128,
            level_b=128,
            settle_ms=0,
        ),
        base_region=make_base_region(),
        ffc_cal_dir="",
        max_iterations=10,
        warmup_s=0.0,
        sony_capture_runner=runner,
        sleep=lambda _: None,
        demosaic_factory=demosaic_factory,
    )

    assert isinstance(cal, CalibrationResult), "must return valid CalibrationResult on non-convergence"
    assert cal.b.led_level == 255, (
        f"non-convergence path: blue led_level should be 255, got {cal.b.led_level}"
    )
    import math
    assert math.isfinite(cal.b.clip_fraction), "clip_fraction must be finite"
    assert 0.0 <= cal.b.clip_fraction <= 1.0, (
        f"clip_fraction out of range: {cal.b.clip_fraction}"
    )


def test_determinism(tmp_path):
    """NFR-11: two identical calls produce equal CalibrationResult."""
    cal1, _ = _run_calibrate(tmp_path)
    # Second call: new tmp_path sub-folder to avoid file conflicts
    cal2, _ = _run_calibrate(tmp_path)

    assert cal1 == cal2, (
        "Determinism fail: two identical calibrate_exposure calls produced different results"
    )


def test_orchestrator_reused(tmp_path):
    """NFR-14: the injected runner is invoked (calls list non-empty),
    proving the Orchestrator/Config-B path is driven, not bypassed.
    """
    _, calls = _run_calibrate(tmp_path)

    assert len(calls) > 0, (
        "runner was never called — Orchestrator/Config-B path was bypassed"
    )


def test_hardware_free(tmp_path):
    """Every test runs with the demosaic seam injected and a stub runner.

    rawpy is never imported (suite passes without rawpy installed).
    NFR-12, Pitfall 5.

    This test verifies the module can be loaded and run without rawpy by
    confirming no import of rawpy occurs when demosaic_factory is injected.
    """
    import sys

    # rawpy must NOT be imported anywhere in the test path
    assert "rawpy" not in sys.modules, (
        "rawpy was imported in the test path — hardware-free invariant violated"
    )

    # Run calibration with factory injected — no rawpy should be triggered
    cal, _ = _run_calibrate(tmp_path)
    assert isinstance(cal, CalibrationResult)

    # Confirm rawpy still not imported after the run
    assert "rawpy" not in sys.modules, (
        "rawpy was imported during calibrate_exposure with demosaic_factory injected"
    )


def test_max_iterations_zero_raises(tmp_path):
    """WR-03/IN-02: max_iterations=0 must raise ValueError, not silently return junk.

    Regression gate: ensures the max_iterations guard at function entry fires
    before any Orchestrator construction or capture call.
    """
    runner, calls = make_runner()

    def demosaic_factory(orch: Orchestrator) -> Callable[[Path], np.ndarray]:
        return make_calibration_demosaic(orch)

    with pytest.raises(ValueError, match="max_iterations"):
        calibrate_exposure(
            scanlight=FakeScanlight(),
            settings=CaptureSettings(
                roll_name="CalibExposure",
                frame_number=1,
                output_folder=tmp_path,
                level_r=128,
                level_g=128,
                level_b=128,
                settle_ms=0,
            ),
            base_region=make_base_region(),
            ffc_cal_dir="",
            max_iterations=0,
            warmup_s=0.0,
            sony_capture_runner=runner,
            sleep=lambda _: None,
            demosaic_factory=demosaic_factory,
        )

    # Guard fires before any capture — runner must NOT have been called
    assert len(calls) == 0, (
        "runner was called despite max_iterations=0 — guard must fire before capture"
    )


def test_zero_signal_raises(tmp_path):
    """WR-02: if the dark frame is brighter than the calibration signal on any channel,
    calibrate_exposure must raise ValueError, not silently return led_level=128.

    Simulates a 'too-bright dark frame' scenario by making the demosaic return a
    uniformly-black image (all zeros), so after black-level subtraction every ROI
    is zero and p99 == 0.0 at every probe level.
    """
    runner, _ = make_runner()

    def demosaic_factory(orch: Orchestrator) -> Callable[[Path], np.ndarray]:
        # Return a closure that always yields an all-zero image regardless of LED level.
        # This simulates black_level >= signal at every probe (WR-02 scenario).
        def _zero_demosaic(path: Path) -> np.ndarray:
            return np.zeros((_IMG_H, _IMG_W, 3), dtype=np.uint16)
        return _zero_demosaic

    with pytest.raises(ValueError, match="rebate signal is zero"):
        calibrate_exposure(
            scanlight=FakeScanlight(),
            settings=CaptureSettings(
                roll_name="CalibExposure",
                frame_number=1,
                output_folder=tmp_path,
                level_r=128,
                level_g=128,
                level_b=128,
                settle_ms=0,
            ),
            base_region=make_base_region(),
            ffc_cal_dir="",
            max_iterations=10,
            warmup_s=0.0,
            sony_capture_runner=runner,
            sleep=lambda _: None,
            demosaic_factory=demosaic_factory,
        )
