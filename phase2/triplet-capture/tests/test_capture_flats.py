"""Tests for capture_flats — hardware-free via stub runner + fake demosaic.

Mirrors the stub patterns from test_orchestrator.py:
  - FakeScanlight: duck-type for set_color/off
  - make_runner: writes a plausible-sized fake RAW file; returns (runner, calls)
  - settings fixture: settle_ms=0 so no real sleep in tests
  - fake_demosaic: returns make_rebate_strip(...) ignoring path (no rawpy)

All tests are fully hardware-free (NFR-12).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from c41_core import ChannelCalibration, FlatFieldResult
from c41_core.fixtures import make_rebate_strip
from triplet_capture.capture_flats import capture_flats
from triplet_capture.orchestrator import CaptureSettings


# ---------------------------------------------------------------------------
# Stubs (mirror test_orchestrator.py patterns)
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
    """Returns (runner, calls).  Runner writes a fake RAW and returns 0."""
    calls: list[tuple[str, Path, int]] = []

    def runner(channel: str, out_path: Path, timeout_s: int) -> int:
        calls.append((channel, out_path, timeout_s))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"\x00" * success_size)
        return 0

    return runner, calls


def fake_demosaic(path: Path) -> np.ndarray:
    """Return a synthetic flat regardless of what path points to (no rawpy)."""
    return make_rebate_strip(height=128, width=192, seed=42)


@pytest.fixture
def settings(tmp_path):
    return CaptureSettings(
        roll_name="FlatCapture",
        frame_number=1,
        output_folder=tmp_path,
        level_r=200,
        level_g=180,
        level_b=160,
        settle_ms=0,  # no sleep in tests
    )


def _make_black_levels(
    bl_r: float = 250.0,
    bl_g: float = 255.0,
    bl_b: float = 240.0,
) -> tuple[ChannelCalibration, ChannelCalibration, ChannelCalibration]:
    return (
        ChannelCalibration(channel="R", led_level=200, black_level=bl_r, gain=1.0, clip_fraction=0.0),
        ChannelCalibration(channel="G", led_level=180, black_level=bl_g, gain=1.0, clip_fraction=0.0),
        ChannelCalibration(channel="B", led_level=160, black_level=bl_b, gain=1.0, clip_fraction=0.0),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_capture_flats_returns_flat_field_result(settings):
    """capture_flats returns a FlatFieldResult with correct fields."""
    runner, _ = make_runner()
    black_levels = _make_black_levels(250.0, 255.0, 240.0)

    result = capture_flats(
        scanlight=FakeScanlight(),
        settings=settings,
        black_levels=black_levels,
        n_frames=4,
        warmup_s=999.0,           # irrelevant — sleep is noop
        sony_capture_runner=runner,
        sleep=lambda _: None,     # test-fast warmup
        demosaic_fn=fake_demosaic,
    )

    assert isinstance(result, FlatFieldResult)
    assert result.n_frames_averaged == 4
    assert result.black_level_r == 250.0
    assert result.black_level_g == 255.0
    assert result.black_level_b == 240.0
    # working_brightness should default to settings.level_g when not provided
    assert result.working_brightness == settings.level_g
    # uniformity_improvement must be a finite, non-negative float
    assert isinstance(result.uniformity_improvement, float)
    assert result.uniformity_improvement >= 0.0
    import math
    assert math.isfinite(result.uniformity_improvement)
    # Must round-trip via JSON (FlatFieldResult contract)
    restored = FlatFieldResult.from_json(result.to_json())
    assert restored == result


def test_capture_flats_drives_loop_n_times(settings):
    """The Orchestrator runner is invoked n_frames * 3 times (R/G/B per frame)."""
    runner, calls = make_runner()
    n_frames = 6

    capture_flats(
        scanlight=FakeScanlight(),
        settings=settings,
        black_levels=_make_black_levels(),
        n_frames=n_frames,
        sony_capture_runner=runner,
        sleep=lambda _: None,
        demosaic_fn=fake_demosaic,
    )

    # Each frame requires R + G + B = 3 runner calls
    assert len(calls) == n_frames * 3, (
        f"expected {n_frames * 3} runner calls (n_frames={n_frames} × 3 channels), "
        f"got {len(calls)}"
    )
    # Confirm R/G/B channels are captured in the standard order
    channels_per_frame = [calls[i * 3: i * 3 + 3] for i in range(n_frames)]
    for frame_calls in channels_per_frame:
        assert [c[0] for c in frame_calls] == ["R", "G", "B"]


def test_capture_flats_hardware_free_and_warmup_injectable(settings):
    """Warmup sleep is injectable; no real ARW / rawpy path is touched."""
    runner, _ = make_runner()
    sleep_calls: list[float] = []

    def spy_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    warmup_val = 42.5
    capture_flats(
        scanlight=FakeScanlight(),
        settings=settings,
        black_levels=_make_black_levels(),
        n_frames=2,
        warmup_s=warmup_val,
        sony_capture_runner=runner,
        sleep=spy_sleep,         # records calls instead of sleeping
        demosaic_fn=fake_demosaic,  # synthetic arrays — no rawpy
    )

    # Warmup sleep was invoked with warmup_val (not a real sleep, just a record)
    assert len(sleep_calls) >= 1, "warmup sleep must be called at least once"
    assert sleep_calls[0] == warmup_val, (
        f"warmup sleep first call should be warmup_s={warmup_val}, got {sleep_calls[0]}"
    )
    # No real ARW was opened because fake_demosaic bypasses rawpy entirely
    # (verified by the fact that the test suite doesn't need rawpy installed)
