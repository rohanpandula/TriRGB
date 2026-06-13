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
import json
import subprocess
from typing import Callable

import numpy as np
import pytest

from c41_core import ChannelCalibration, CalibrationResult
from c41_core.contracts import BaseRegionDescriptor
from triplet_capture.orchestrator import CaptureSettings, Orchestrator

# RED: import calibrate_exposure — this module does not exist yet.
# Collection will fail here until Task 2 creates it.
from triplet_capture.calibrate_exposure import (
    _MAX_CLIP_FRACTION,
    _PWM_MIN,
    _adjacent_shutter,
    _attempt_score,
    _candidate_shutters,
    _exposure_status,
    _next_probe_level,
    calibrate_exposure,
)


# ---------------------------------------------------------------------------
# SCALE constants — the oracle for SC-3 / SC-4
# ---------------------------------------------------------------------------

# TARGET = int(0.85 * (65535 - BLACK_OFFSET)) = 55487
# SCALE chosen so convergence levels are BY CONSTRUCTION:
#   R → ~180,  G → ~160,  B → ~230   (blue highest → SC-3 holds)
BLACK_OFFSET: float = 256.0
TARGET = int(0.85 * (65535 - BLACK_OFFSET))


def shutter_seconds(label: str) -> float:
    if "/" in label:
        num, den = label.split("/", 1)
        return float(num) / float(den)
    return float(label)


SCALE: dict[str, float] = {
    "R": (TARGET - BLACK_OFFSET) / 180.0,   # ≈ 344.5  → level_R* ≈ 180
    "G": (TARGET - BLACK_OFFSET) / 160.0,   # ≈ 387.5  → level_G* ≈ 160
    "B": (TARGET - BLACK_OFFSET) / 230.0,   # ≈ 269.6  → level_B* ≈ 230
}

# Non-convergence scale: make blue require level > 255 even at the slowest
# shutter candidate to force non-convergence path.
# p99.9 = level * SCALE_NO_CONV_B + BLACK_OFFSET
# At 1s the calibration fake applies a 4x shutter factor relative to 1/4s,
# so needs level ~500 even at the slowest candidate.
SCALE_NO_CONV: dict[str, float] = {
    "R": SCALE["R"],
    "G": SCALE["G"],
    "B": (TARGET - BLACK_OFFSET) / 2000.0,
}

SCALE_SHUTTER_COARSE: dict[str, float] = {
    "R": SCALE["R"],
    "G": SCALE["G"],
    "B": (TARGET - BLACK_OFFSET) / 340.0,  # clearly too dim at 1/4s, clean at 1/2s
}

SCALE_FAST_SHUTTER_PREFERRED: dict[str, float] = {
    # At 1/4s this solves near LED 128. At 1/6s it solves near LED 190,
    # which is the intended preference when both are target-safe.
    "R": (TARGET - BLACK_OFFSET) / 128.0,
    "G": (TARGET - BLACK_OFFSET) / 128.0,
    "B": (TARGET - BLACK_OFFSET) / 128.0,
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

    def shutter_seconds(label: str) -> float:
        if "/" in label:
            num, den = label.split("/", 1)
            return float(num) / float(den)
        return float(label)

    def demosaic_fn(path: Path) -> np.ndarray:
        # Read LIVE LED levels from the orchestrator settings — NOT from calls.
        level_r = orch.settings.level_r
        level_g = orch.settings.level_g
        level_b = orch.settings.level_b

        levels = {"R": level_r, "G": level_g, "B": level_b}

        # Build HxWx3 float array; each channel is level * scale + black_offset
        img = np.zeros((_IMG_H, _IMG_W, 3), dtype=np.float32)
        for ch, ch_idx in CH_IDX.items():
            shutter = getattr(orch.settings, f"shutter_{ch.lower()}") or "1/4"
            shutter_factor = shutter_seconds(shutter) / shutter_seconds("1/4")
            brightness = levels[ch] * scale[ch] * shutter_factor + black_offset
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
        trigger_mode="sdk",  # sdk mode (also the dataclass default; no ied_inbox needed)
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
            trigger_mode="sdk",  # sdk mode (also the dataclass default; no ied_inbox needed)
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
        assert ch_cal.shutter_speed, f"shutter_speed should be recorded for {ch_cal.channel}"

    assert cal.base_region.base_rgb == pytest.approx((cal.r.p99, cal.g.p99, cal.b.p99))
    assert cal.base_region.base_rgb != make_base_region().base_rgb


def test_shutter_speed_is_coarse_control_when_led_would_max(tmp_path):
    """If a channel would need LED >255 at the current shutter, calibration
    slows the shutter only when max light is still clearly below target."""
    cal, _ = _run_calibrate(tmp_path, scale=SCALE_SHUTTER_COARSE)

    assert cal.b.shutter_speed == "1/3"
    assert _PWM_MIN <= cal.b.led_level <= 255
    assert cal.b.exposure_status == "target"


def test_fast_shutter_high_led_preferred_when_target_safe(tmp_path):
    """When a fast shutter can hit target safely, solve LED as the fine trim."""
    cal, _ = _run_calibrate(tmp_path, scale=SCALE_FAST_SHUTTER_PREFERRED)

    for ch_cal in (cal.r, cal.g, cal.b):
        assert ch_cal.shutter_speed == "1/6"
        assert 180 <= ch_cal.led_level <= 255
        assert ch_cal.exposure_status == "target"
        assert abs(ch_cal.p99 - ch_cal.target) <= 500


def test_fast_shutter_under_at_max_falls_back_to_slower_target(tmp_path):
    """Regression: do not leave progress at "trying LED 255" when probing fast shutters.

    Real calibration used to step through multiple slow/hot captures after a
    fast probe. The analytic policy should jump straight to the fastest viable
    shutter with a computed LED trim.
    """
    runner, _ = make_runner()

    def demosaic_factory(orch: Orchestrator) -> Callable[[Path], np.ndarray]:
        ch_idx = {"R": 0, "G": 1, "B": 2}
        base_scale = (TARGET - BLACK_OFFSET) / 128.0

        def shutter_seconds(label: str) -> float:
            if "/" in label:
                num, den = label.split("/", 1)
                return float(num) / float(den)
            return float(label)

        def demosaic_fn(_path: Path) -> np.ndarray:
            img = np.zeros((_IMG_H, _IMG_W, 3), dtype=np.float32)
            for ch, idx in ch_idx.items():
                level = getattr(orch.settings, f"level_{ch.lower()}")
                shutter = getattr(orch.settings, f"shutter_{ch.lower()}") or "1/4"
                factor = shutter_seconds(shutter) / shutter_seconds("1/4")
                signal = level * base_scale * factor
                if shutter == "1/8":
                    signal = min(signal, TARGET - 6000)
                img[:, :, idx] = signal + BLACK_OFFSET
            return np.clip(img, 0, 65535).astype(np.uint16)

        return demosaic_fn

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
            trigger_mode="sdk",  # sdk mode (also the dataclass default; no ied_inbox needed)
        ),
        base_region=make_base_region(),
        ffc_cal_dir="",
        max_iterations=10,
        warmup_s=0.0,
        sony_capture_runner=runner,
        sleep=lambda _: None,
        demosaic_factory=demosaic_factory,
    )

    for ch_cal in (cal.r, cal.g, cal.b):
        assert ch_cal.shutter_speed == "1/6"
        assert 180 <= ch_cal.led_level <= 255
        assert ch_cal.exposure_status == "target"
        assert abs(ch_cal.p99 - ch_cal.target) <= 500

    events = [
        json.loads(line)
        for line in (tmp_path / "scan_log.jsonl").read_text().splitlines()
    ]
    assert any(event["event"] == "calibration_channel_complete" for event in events)
    assert events[-1]["event"] == "calibration_complete"


def test_clipped_slow_verify_tries_intermediate_bracket_shutter(tmp_path):
    """If the solved slow shutter clips but the faster retry is dim, try
    Sony's intermediate third-stop shutters before declaring a clean ceiling.

    This matches the real G-channel trace: 1/4 clipped, 1/8 at LED 255 was
    clean but far under target, and 1/5 was the brightest untried bracket step.
    """
    runner, _ = make_runner()
    bracket_scale = TARGET / 229.0

    def demosaic_factory(orch: Orchestrator) -> Callable[[Path], np.ndarray]:
        ch_idx = {"R": 0, "G": 1, "B": 2}

        def demosaic_fn(_path: Path) -> np.ndarray:
            img = np.full((_IMG_H, _IMG_W, 3), BLACK_OFFSET, dtype=np.float32)
            for ch, idx in ch_idx.items():
                level = getattr(orch.settings, f"level_{ch.lower()}")
                shutter = getattr(orch.settings, f"shutter_{ch.lower()}") or "1/4"
                factor = shutter_seconds(shutter) / shutter_seconds("1/4")
                if level == 0:
                    signal = 0.0
                elif shutter == "1/4" and level >= 220:
                    signal = 65535.0
                else:
                    signal = float(level) * bracket_scale * factor
                img[:, :, idx] = signal + BLACK_OFFSET
            return np.clip(img, 0, 65535).astype(np.uint16)

        return demosaic_fn

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
            trigger_mode="sdk",  # sdk mode (also the dataclass default; no ied_inbox needed)
        ),
        base_region=make_base_region(),
        ffc_cal_dir="",
        max_iterations=10,
        warmup_s=0.0,
        sony_capture_runner=runner,
        sleep=lambda _: None,
        demosaic_factory=demosaic_factory,
    )

    for ch_cal in (cal.r, cal.g, cal.b):
        assert ch_cal.shutter_speed == "1/5"
        assert ch_cal.led_level == 255
        assert ch_cal.exposure_status == "clip_limited"
        assert ch_cal.p99 >= ch_cal.target * 0.85
        assert ch_cal.p99 < ch_cal.target

    events = [
        json.loads(line)
        for line in (tmp_path / "scan_log.jsonl").read_text().splitlines()
    ]
    bracket_events = [
        event for event in events if event["event"] == "calibration_bracket_refine"
    ]
    assert len(bracket_events) == 3
    assert {event["next_shutter"] for event in bracket_events} == {"1/5"}


def test_saved_stock_seed_can_complete_channel_in_one_capture(tmp_path):
    """A clean saved stock recipe should be used as the first probe.

    Repeat-stock calibration should not start over at the generic probe when
    the stored RGB/shutter recipe still lands in the target band.
    """
    runner, calls = make_runner()

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
            trigger_mode="sdk",
        ),
        base_region=make_base_region(),
        ffc_cal_dir="",
        max_iterations=10,
        warmup_s=0.0,
        sony_capture_runner=runner,
        sleep=lambda _: None,
        demosaic_factory=demosaic_factory,
        seed_recipe={
            "R": (180, "1/4"),
            "G": (160, "1/4"),
            "B": (230, "1/4"),
        },
    )

    assert cal.r.exposure_status == "target"
    assert cal.g.exposure_status == "target"
    assert cal.b.exposure_status == "target"
    assert len(calls) == 4  # one dark frame + one seed capture per channel

    events = [
        json.loads(line)
        for line in (tmp_path / "scan_log.jsonl").read_text().splitlines()
    ]
    probes = [event for event in events if event["event"] == "calibration_probe"]
    assert [event["phase"] for event in probes] == ["seed", "seed", "seed"]
    assert not any(event["event"] == "calibration_solve" for event in events)


def test_clipped_seed_retries_one_stop_faster_before_led_floor(tmp_path):
    """If the seed clips, retry a faster shutter before dropping to LED floor.

    This matches the real failure mode where a hard-clipped old recipe was
    backed down at the same shutter and reported as clip-limited.
    """
    runner, _ = make_runner()

    def demosaic_factory(orch: Orchestrator):
        ch_idx = {"R": 0, "G": 1, "B": 2}

        def demosaic_fn(_path: Path) -> np.ndarray:
            img = np.full((_IMG_H, _IMG_W, 3), BLACK_OFFSET, dtype=np.float32)
            if all(getattr(orch.settings, f"level_{ch.lower()}") == 0 for ch in ("R", "G", "B")):
                return img.astype(np.uint16)
            for ch, idx in ch_idx.items():
                level = getattr(orch.settings, f"level_{ch.lower()}")
                shutter = getattr(orch.settings, f"shutter_{ch.lower()}") or "1/4"
                if ch == "R" and level == 200 and shutter == "1/4":
                    img[:, :, idx] = 65535
                elif ch == "R" and level == 200 and shutter == "1/8":
                    img[:, :, idx] = TARGET + BLACK_OFFSET
                else:
                    img[:, :, idx] = TARGET + BLACK_OFFSET
            return np.clip(img, 0, 65535).astype(np.uint16)

        return demosaic_fn

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
            trigger_mode="sdk",
        ),
        base_region=make_base_region(),
        ffc_cal_dir="",
        max_iterations=10,
        warmup_s=0.0,
        sony_capture_runner=runner,
        sleep=lambda _: None,
        demosaic_factory=demosaic_factory,
        seed_recipe={
            "R": (200, "1/4"),
            "G": (160, "1/4"),
            "B": (230, "1/4"),
        },
    )

    assert cal.r.exposure_status == "target"
    assert cal.r.shutter_speed == "1/8"
    assert cal.r.led_level == 200

    events = [
        json.loads(line)
        for line in (tmp_path / "scan_log.jsonl").read_text().splitlines()
    ]
    r_probes = [
        event for event in events
        if event["event"] == "calibration_probe" and event["channel"] == "R"
    ]
    assert [event["phase"] for event in r_probes[:2]] == ["seed", "seed-faster"]
    assert not any(event["phase"] == "seed-floor" for event in r_probes)


def test_clipped_histogram_probe_is_not_selected_over_safe_probe():
    """A target-matching but clipped histogram must lose to a safe exposure.

    This protects real calibration from choosing an overexposed rebate just
    because its p99 is numerically close to the target.
    """
    clipped = {
        "led_level": 160,
        "p99": TARGET,
        "clip_fraction": _MAX_CLIP_FRACTION + 0.02,
    }
    safe = {
        "led_level": 150,
        "p99": TARGET - 8000,
        "clip_fraction": 0.0,
    }

    assert _attempt_score(safe, TARGET) < _attempt_score(clipped, TARGET)


def test_sensor_clipped_probe_is_not_selected_over_safe_probe(tmp_path):
    """Source-Bayer clipping must beat a demosaiced p99 that looks safe.

    Real a7CR red probes can clip the Bayer red photosites while rawpy's
    ProPhoto output channel stays below the demosaiced saturation threshold.
    """
    runner, calls = make_runner()

    def demosaic_factory(orch: Orchestrator):
        return make_calibration_demosaic(orch)

    def sensor_clip_factory(orch: Orchestrator):
        def sensor_clip(_path: Path, channel: str, _base_region: BaseRegionDescriptor) -> float:
            level = getattr(orch.settings, f"level_{channel.lower()}")
            if channel == "R" and level >= 180:
                return _MAX_CLIP_FRACTION + 0.25
            return 0.0

        return sensor_clip

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
            trigger_mode="sdk",  # sdk mode (also the dataclass default; no ied_inbox needed)
        ),
        base_region=make_base_region(),
        ffc_cal_dir="",
        max_iterations=10,
        warmup_s=0.0,
        sony_capture_runner=runner,
        sleep=lambda _: None,
        demosaic_factory=demosaic_factory,
        sensor_clip_factory=sensor_clip_factory,
    )

    assert calls
    assert cal.r.led_level < 180
    assert cal.r.clip_fraction <= _MAX_CLIP_FRACTION


def test_source_limited_channel_reports_brightest_clean_setting(tmp_path):
    """If source RAW clips before p99 can reach target, report the clean ceiling.

    This is the real camera behavior seen with red/green: forcing the target
    would clip source photosites, so the correct outcome is the brightest clean
    bracket value, not a misleading "under" pass.
    """
    runner, _ = make_runner()

    def demosaic_factory(orch: Orchestrator):
        return make_calibration_demosaic(orch)

    def sensor_clip_factory(orch: Orchestrator):
        def sensor_clip(_path: Path, channel: str, _base_region: BaseRegionDescriptor) -> float:
            level = getattr(orch.settings, f"level_{channel.lower()}")
            if channel == "R" and level >= 150:
                return _MAX_CLIP_FRACTION + 0.25
            return 0.0

        return sensor_clip

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
            trigger_mode="sdk",  # sdk mode (also the dataclass default; no ied_inbox needed)
        ),
        base_region=make_base_region(),
        ffc_cal_dir="",
        max_iterations=10,
        warmup_s=0.0,
        sony_capture_runner=runner,
        sleep=lambda _: None,
        demosaic_factory=demosaic_factory,
        sensor_clip_factory=sensor_clip_factory,
    )

    assert cal.r.exposure_status == "source_limited"
    assert cal.r.led_level < 150
    assert cal.r.clip_fraction <= _MAX_CLIP_FRACTION


def test_roughly_two_percent_low_is_target_band():
    """A practical target band avoids labeling 98% as UNDER in the UI."""
    assert _exposure_status(
        p99=TARGET * 0.98,
        target=TARGET,
        clip_fraction=0.0,
        tolerance=2000,
    ) == "target"


def test_clipped_probe_backs_off_by_bracket_instead_of_single_step():
    """Real-run regression: sensor clipping can leave demosaiced p99 below target.

    The old solver used ``probe_level - 1`` in that case, which turned one
    overshoot into many 20-second RAW captures.
    """
    next_level = _next_probe_level(
        probe_level=214,
        p99=56231.0,
        target=TARGET,
        clip_fraction=_MAX_CLIP_FRACTION + 0.39,
        clean_lower_bound=166,
        clipped_upper_bound=214,
    )

    assert next_level == 190


def test_under_probe_respects_known_clipped_upper_bound():
    """After a clipped probe, under-target estimates should bisect the bracket."""
    next_level = _next_probe_level(
        probe_level=190,
        p99=54000.0,
        target=TARGET,
        clip_fraction=0.0,
        clean_lower_bound=190,
        clipped_upper_bound=214,
    )

    assert next_level == 195


def test_supported_shutter_candidates_keep_intermediate_sony_steps():
    """Use Sony's full writable list, not only the coarse fallback ladder."""
    candidates = _candidate_shutters(
        "1/40",
        ["1/40", "1/30", "1/25", "1/20", "1/15", "1/13",
         "1/10", "1/8", "1/6", "1/5", "1/4", "1/3", "0.4", "1/2", "1", "2"],
    )

    assert "1/6" in candidates
    assert "1/3" in candidates
    assert "0.4" in candidates
    assert "2" not in candidates


def test_clipped_max_light_probe_does_not_step_slower():
    """Once a max-light shutter probe clips, trim that shutter instead of slowing."""
    candidates = _candidate_shutters("1/40")

    assert _adjacent_shutter(
        current="1/2",
        candidates=candidates,
        led_level=255,
        best_p99=52000.0,
        clip_fraction=_MAX_CLIP_FRACTION + 0.1,
        target=TARGET,
        tolerance=2000,
        converged=False,
    ) is None


def test_calibration_probe_logs_histogram_clip_metrics(tmp_path):
    """Progress logs include upper-histogram and clipping data for UX/debugging."""
    _cal, _calls = _run_calibrate(tmp_path)

    probes = [
        json.loads(line)
        for line in (tmp_path / "scan_log.jsonl").read_text().splitlines()
        if '"event": "calibration_probe"' in line
    ]

    assert probes, "expected calibration_probe events in scan_log.jsonl"
    first = probes[0]
    assert "p99" in first
    assert "p999" in first
    assert "clip_fraction" in first
    assert "output_clip_fraction" in first
    assert "sensor_clip_fraction" in first
    assert "max_clip_fraction" in first
    assert "exposure_status" in first
    assert 0.0 <= first["clip_fraction"] <= 1.0


def test_sdk_calibration_requires_writable_shutter_before_dark_frame(tmp_path, monkeypatch):
    """If the camera is in A/P/Auto, Sony reports shutter writable=no. Fail
    before any dark-frame capture so A-mode metering does not choose a 30s dark.
    """
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout="shutterSpeed current=30 raw=19660810 writable=no\n",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    def demosaic_factory(orch: Orchestrator):
        return make_calibration_demosaic(orch)

    orch = Orchestrator(
        FakeScanlight(),
        CaptureSettings(
            roll_name="CalibExposure",
            frame_number=1,
            output_folder=tmp_path,
            level_r=128,
            level_g=128,
            level_b=128,
            settle_ms=0,
            trigger_mode="sdk",
            sony_capture_path="/bin/sony-capture",
        ),
        sleep=lambda _: None,
    )

    with pytest.raises(RuntimeError, match="camera mode dial to M"):
        calibrate_exposure(
            scanlight=FakeScanlight(),
            settings=orch.settings,
            base_region=make_base_region(),
            ffc_cal_dir="",
            max_iterations=10,
            warmup_s=0.0,
            sleep=lambda _: None,
            demosaic_factory=demosaic_factory,
            orchestrator=orch,
        )

    assert any("--list-shutter-speeds" in cmd for cmd in calls)
    assert not any("--out" in cmd for cmd in calls)


def test_dark_frame_failure_restores_original_levels(tmp_path):
    """A failed dark-frame capture must not leave the shared orchestrator at RGB=0."""
    calls: list[tuple[str, Path, int]] = []

    def runner(channel: str, out_path: Path, timeout_s: int) -> int:
        calls.append((channel, out_path, timeout_s))
        return 1

    settings = CaptureSettings(
        roll_name="CalibExposure",
        frame_number=1,
        output_folder=tmp_path,
        level_r=111,
        level_g=122,
        level_b=133,
        shutter_r="1/8",
        shutter_g="1/15",
        shutter_b="1/30",
        settle_ms=0,
        trigger_mode="sdk",  # sdk mode (also the dataclass default; no ied_inbox needed)
    )
    orch = Orchestrator(
        FakeScanlight(),
        settings,
        sony_capture_runner=runner,
        sleep=lambda _: None,
    )

    def demosaic_factory(orchestrator: Orchestrator) -> Callable[[Path], np.ndarray]:
        return make_calibration_demosaic(orchestrator)

    with pytest.raises(RuntimeError, match="dark-frame capture failed"):
        calibrate_exposure(
            scanlight=FakeScanlight(),
            settings=settings,
            base_region=make_base_region(),
            warmup_s=0.0,
            sleep=lambda _: None,
            demosaic_factory=demosaic_factory,
            orchestrator=orch,
        )

    assert calls
    assert orch.settings.level_r == 111
    assert orch.settings.level_g == 122
    assert orch.settings.level_b == 133
    assert orch.settings.shutter_r == "1/8"
    assert orch.settings.shutter_g == "1/15"
    assert orch.settings.shutter_b == "1/30"


def test_blue_led_level_highest(tmp_path):
    """SC-3: orange-mask attenuation forces blue to use at least as much exposure.

    In the analytic SDK policy, the mask can show up as slower shutter rather
    than strictly higher LED.  Assert on total exposure product instead.
    """
    cal, _ = _run_calibrate(tmp_path)

    b_exposure = cal.b.led_level * shutter_seconds(cal.b.shutter_speed)
    r_exposure = cal.r.led_level * shutter_seconds(cal.r.shutter_speed)
    g_exposure = cal.g.led_level * shutter_seconds(cal.g.shutter_speed)

    assert b_exposure >= r_exposure
    assert b_exposure >= g_exposure
    assert shutter_seconds(cal.b.shutter_speed) > shutter_seconds(cal.r.shutter_speed)
    assert shutter_seconds(cal.b.shutter_speed) > shutter_seconds(cal.g.shutter_speed)


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
    """The runner call count is bounded by single-channel calibration captures.

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
            trigger_mode="sdk",  # sdk mode (also the dataclass default; no ied_inbox needed)
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

    # Upper bound for the default shutter plan: one dark RAW plus one RAW per
    # channel per bisection iteration. The real hardware used to do full triplets
    # here, which made calibration slow enough for the Swift HTTP request to time out.
    max_calls = 1 + 3 * max_iterations
    assert len(calls) <= max_calls, (
        f"runner called {len(calls)} times, exceeds ceiling of {max_calls}"
    )
    assert len(calls) > 0, "runner must have been called at least once"


def test_calibration_uses_single_dark_and_single_channel_probes(tmp_path):
    """Real-run regression: calibration must not shoot full R/G/B triplets for
    the dark frame or for each per-channel probe.
    """
    runner, calls = make_runner()

    def demosaic_factory(orch: Orchestrator):
        return make_calibration_demosaic(orch)

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
            trigger_mode="sdk",  # sdk mode (also the dataclass default; no ied_inbox needed)
        ),
        base_region=make_base_region(),
        ffc_cal_dir="",
        max_iterations=1,
        warmup_s=0.0,
        sony_capture_runner=runner,
        sleep=lambda _: None,
        demosaic_factory=demosaic_factory,
    )

    assert calls[0][0] == "R"
    assert calls[0][1].name.endswith("_Cal_Dark.ARW")
    assert {"R", "G", "B"}.issubset({channel for channel, _path, _timeout in calls[1:]})
    for channel, path, _timeout in calls[1:]:
        assert path.name.endswith(f"_Cal_{channel}.ARW")


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
            trigger_mode="sdk",  # sdk mode (also the dataclass default; no ied_inbox needed)
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
    proving the Orchestrator/IED-backed path is driven, not bypassed.
    """
    _, calls = _run_calibrate(tmp_path)

    assert len(calls) > 0, (
        "runner was never called — Orchestrator/IED-backed path was bypassed"
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
                trigger_mode="sdk",  # sdk mode (also the dataclass default; no ied_inbox needed)
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
                trigger_mode="sdk",  # sdk mode (also the dataclass default; no ied_inbox needed)
            ),
            base_region=make_base_region(),
            ffc_cal_dir="",
            max_iterations=10,
            warmup_s=0.0,
            sony_capture_runner=runner,
            sleep=lambda _: None,
            demosaic_factory=demosaic_factory,
        )


def test_near_zero_signal_raises(tmp_path):
    """FIX-1 regression: near-zero signal (p99 < _MIN_SIGNAL_P99 = 10 counts) also
    raises ValueError, not just exact-zero.

    Simulates ADC offset / hot-pixel residual that leaves p99 slightly above 0
    (e.g. 5 counts) on every probe — still below the noise-floor threshold and
    therefore indistinguishable from no optical signal.
    """
    runner, _ = make_runner()

    # p99 = 5 counts at every probe: above 0 but below _MIN_SIGNAL_P99 (10).
    NEAR_ZERO_SIGNAL = 5.0

    def demosaic_factory(orch: Orchestrator) -> Callable[[Path], np.ndarray]:
        def _near_zero_demosaic(path: Path) -> np.ndarray:
            # Uniform image at NEAR_ZERO_SIGNAL; after black_level=0 subtraction
            # the p99 is exactly NEAR_ZERO_SIGNAL.
            img = np.full((_IMG_H, _IMG_W, 3), NEAR_ZERO_SIGNAL, dtype=np.float32)
            return np.clip(img, 0, 65535).astype(np.uint16)
        return _near_zero_demosaic

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
                trigger_mode="sdk",  # sdk mode (also the dataclass default; no ied_inbox needed)
            ),
            base_region=make_base_region(),
            ffc_cal_dir="",
            max_iterations=10,
            warmup_s=0.0,
            sony_capture_runner=runner,
            sleep=lambda _: None,
            demosaic_factory=demosaic_factory,
        )


def test_non_convergence_logs_warning(tmp_path, caplog):
    """FIX-2 regression: when bisection maxes out and stays under target, a WARNING
    is emitted naming the channel and explaining it could not reach the exposure target.

    The function must still return a valid CalibrationResult (not raise).
    """
    import logging

    runner, _ = make_runner()

    def demosaic_factory(orch: Orchestrator):
        return make_calibration_demosaic(orch, scale=SCALE_NO_CONV)

    with caplog.at_level(logging.WARNING, logger="triplet_capture.calibrate_exposure"):
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
                trigger_mode="sdk",  # sdk mode (also the dataclass default; no ied_inbox needed)
            ),
            base_region=make_base_region(),
            ffc_cal_dir="",
            max_iterations=10,
            warmup_s=0.0,
            sony_capture_runner=runner,
            sleep=lambda _: None,
            demosaic_factory=demosaic_factory,
        )

    # Must still return a valid result
    assert isinstance(cal, CalibrationResult), "non-convergence must return CalibrationResult, not raise"

    # A WARNING must have been logged for the blue channel (the one that can't converge)
    warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        "B" in msg and ("255" in msg or "max LED" in msg or "exposure target" in msg)
        for msg in warning_messages
    ), (
        f"Expected a WARNING about channel B failing to reach target at level 255. "
        f"Got warnings: {warning_messages}"
    )
