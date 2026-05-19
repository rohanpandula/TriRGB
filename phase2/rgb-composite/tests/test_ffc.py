"""Tests for `rgb_composite.ffc`."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

import rgb_composite.ffc as ffc_mod
import rgb_composite.composite as composite_mod
from rgb_composite import (
    CalibrationError,
    FFCMaps,
    apply_ffc_to_channel,
    clear_ffc_cache,
    compute_ffc_map,
    load_ffc_maps,
)


H, W = 128, 192  # Small enough to keep tests fast, big enough for FFC math


# ---------- compute_ffc_map ----------

def test_uniform_cal_produces_unity_map():
    """A perfectly flat cal frame → multiplier map ≈ 1.0 everywhere."""
    cal = np.full((H, W), 40000, dtype=np.uint16)
    fmap = compute_ffc_map(cal)
    assert fmap.shape == (H, W)
    assert fmap.dtype == np.float32
    # Tolerance: smoothing introduces tiny edge effects but the bulk
    # should be at 1.0.
    interior = fmap[10:-10, 10:-10]
    assert np.allclose(interior, 1.0, atol=0.02)


def test_vignetted_cal_lifts_corners():
    """A center-bright, corner-dim cal → multiplier rises at corners."""
    yy, xx = np.indices((H, W))
    cy, cx = (H - 1) / 2.0, (W - 1) / 2.0
    # cos^4-ish falloff: corners ~50% as bright as center
    r = np.sqrt((yy - cy) ** 2 + (xx - cx) ** 2) / max(cy, cx)
    cal = (50000 * (1.0 - 0.5 * r ** 2)).clip(0, 65535).astype(np.uint16)

    fmap = compute_ffc_map(cal)
    # Center near 1.0
    center = fmap[H // 2, W // 2]
    assert 0.9 <= center <= 1.1, f"center multiplier {center} not near 1.0"
    # Corner clearly lifted (the vignette is ~50% so multiplier should
    # be ~2.0 unless smoothing or clipping intervenes)
    corner = fmap[0, 0]
    assert corner > 1.3, f"corner multiplier {corner} should lift > 1.3"


def test_dark_cal_raises():
    """A dark cal frame (scanlight off, wrong channel) is unusable."""
    cal = np.full((H, W), 100, dtype=np.uint16)  # ~0.15% of full-scale
    with pytest.raises(CalibrationError, match="below the"):
        compute_ffc_map(cal)


def test_saturated_cal_raises():
    """A cal frame with too many clipped pixels can't be calibrated
    accurately — the reference brightness is the clipped value, not the
    true center brightness, so center vignette stays uncorrected.
    """
    cal = np.full((H, W), 65535, dtype=np.uint16)  # all saturated
    with pytest.raises(CalibrationError, match="over-exposed"):
        compute_ffc_map(cal)


def test_lightly_saturated_cal_still_accepted():
    """A small saturated patch (<1% of pixels) is tolerable — the
    smoothing washes it out. The threshold lets operators keep mostly-
    good cal frames instead of requiring re-shoots for a single hot spot."""
    cal = np.full((H, W), 50000, dtype=np.uint16)
    # Punch a 5x5 saturated patch — well under 1% of an 128x192 frame.
    cal[60:65, 90:95] = 65535
    fmap = compute_ffc_map(cal)
    assert fmap.shape == (H, W)


def test_multiplier_clipping():
    """Extreme dark corners get clipped to the max multiplier."""
    cal = np.full((H, W), 50000, dtype=np.uint16)
    # Punch a near-zero hole that would otherwise blow up the multiplier
    cal[0:20, 0:20] = 100
    fmap = compute_ffc_map(cal)
    # Even at the dark patch, multiplier is clipped (default 3.0)
    assert fmap.max() <= 3.0 + 1e-3


def test_wrong_dimensions_raises():
    with pytest.raises(ValueError, match="expected HxW"):
        compute_ffc_map(np.zeros((H, W, 3), dtype=np.uint16))


# ---------- apply_ffc_to_channel ----------

def test_apply_ffc_corrects_vignette():
    """Apply FFC computed from a vignetted cal back to that cal —
    result should be approximately flat."""
    yy, xx = np.indices((H, W))
    cy, cx = (H - 1) / 2.0, (W - 1) / 2.0
    r = np.sqrt((yy - cy) ** 2 + (xx - cx) ** 2) / max(cy, cx)
    cal = (50000 * (1.0 - 0.4 * r ** 2)).clip(0, 65535).astype(np.uint16)

    fmap = compute_ffc_map(cal)
    corrected = apply_ffc_to_channel(cal, fmap)

    # The corrected cal should be much flatter than the original. We
    # measure flatness by comparing the corner-vs-center brightness ratio.
    orig_corner = cal[5:25, 5:25].mean()
    orig_center = cal[H // 2 - 10:H // 2 + 10, W // 2 - 10:W // 2 + 10].mean()
    orig_ratio = orig_corner / orig_center

    corr_corner = corrected[5:25, 5:25].mean()
    corr_center = corrected[H // 2 - 10:H // 2 + 10, W // 2 - 10:W // 2 + 10].mean()
    corr_ratio = corr_corner / corr_center

    assert orig_ratio < 0.85, "test setup: original was supposed to be vignetted"
    assert corr_ratio > 0.95, (
        f"corrected corner/center ratio {corr_ratio:.3f} should be near 1.0 "
        f"(original was {orig_ratio:.3f})"
    )


def test_apply_ffc_preserves_shape_and_dtype():
    data = np.full((H, W), 30000, dtype=np.uint16)
    fmap = np.full((H, W), 1.5, dtype=np.float32)
    out = apply_ffc_to_channel(data, fmap)
    assert out.shape == (H, W)
    assert out.dtype == np.uint16
    # 30000 * 1.5 = 45000
    assert out[0, 0] == 45000


def test_apply_ffc_clips_overflow():
    """50000 * 2.0 = 100000 → must clip to uint16 max (65535), not wrap."""
    data = np.full((H, W), 50000, dtype=np.uint16)
    fmap = np.full((H, W), 2.0, dtype=np.float32)
    out = apply_ffc_to_channel(data, fmap)
    assert out.max() == 65535
    # Sanity: not wrapped to a small value
    assert out.min() == 65535


def test_apply_ffc_shape_mismatch_raises():
    data = np.zeros((H, W), dtype=np.uint16)
    fmap = np.ones((H + 1, W), dtype=np.float32)
    with pytest.raises(ValueError, match="FFC shape mismatch"):
        apply_ffc_to_channel(data, fmap)


# ---------- load_ffc_maps ----------

@pytest.fixture
def cal_dir_with_stubbed_demosaic(tmp_path, monkeypatch):
    """Set up an FFC cal directory and stub `demosaic_linear` to return
    distinguishable per-channel arrays so we can prove the right channel
    of each cal frame is picked.
    """
    cal = tmp_path / "calibration"
    cal.mkdir()
    # The files just need to exist; the stub returns synthetic data.
    (cal / "R.ARW").write_bytes(b"\x00")
    (cal / "G.ARW").write_bytes(b"\x00")
    (cal / "B.ARW").write_bytes(b"\x00")

    def make_cal(red_val, green_val, blue_val):
        img = np.zeros((H, W, 3), dtype=np.uint16)
        img[..., 0] = red_val
        img[..., 1] = green_val
        img[..., 2] = blue_val
        return img

    fakes = {
        "R.ARW": make_cal(45000, 200, 100),
        "G.ARW": make_cal(150, 42000, 200),
        "B.ARW": make_cal(80, 250, 38000),
    }

    def fake_demosaic(path):
        return fakes[Path(path).name].copy()

    monkeypatch.setattr(composite_mod, "demosaic_linear", fake_demosaic)
    # ffc.py imports it lazily as a side effect; clear cache between tests
    clear_ffc_cache()
    yield cal
    clear_ffc_cache()


def test_load_ffc_maps_picks_matching_channel(cal_dir_with_stubbed_demosaic):
    cal = cal_dir_with_stubbed_demosaic
    maps = load_ffc_maps(cal)
    assert isinstance(maps, FFCMaps)
    assert maps.shape == (H, W)
    # Each map was built from a different cal frame — they're all uniform
    # arrays at the dominant channel value (45000, 42000, 38000 respectively),
    # so all maps should be approximately unity.
    for fmap in (maps.r, maps.g, maps.b):
        interior = fmap[10:-10, 10:-10]
        assert np.allclose(interior, 1.0, atol=0.02)


def test_load_ffc_maps_caches(cal_dir_with_stubbed_demosaic, monkeypatch):
    """Second call with the same path should hit the LRU cache."""
    cal = cal_dir_with_stubbed_demosaic
    call_count = {"n": 0}
    original = composite_mod.demosaic_linear

    def counting_demosaic(p):
        call_count["n"] += 1
        return original(p)

    monkeypatch.setattr(composite_mod, "demosaic_linear", counting_demosaic)

    load_ffc_maps(cal)
    n1 = call_count["n"]
    load_ffc_maps(cal)
    n2 = call_count["n"]
    assert n2 == n1, "second load_ffc_maps call should be cached"


def test_load_ffc_maps_missing_dir_raises(tmp_path):
    with pytest.raises(CalibrationError, match="not a directory"):
        load_ffc_maps(tmp_path / "does-not-exist")


def test_load_ffc_maps_missing_files_raises(tmp_path):
    cal = tmp_path / "calibration"
    cal.mkdir()
    # Only put R.ARW; G and B are missing
    (cal / "R.ARW").write_bytes(b"\x00")
    with pytest.raises(CalibrationError, match="missing files for channel"):
        load_ffc_maps(cal)


def test_load_ffc_maps_dimension_mismatch_raises(tmp_path, monkeypatch):
    cal = tmp_path / "calibration"
    cal.mkdir()
    (cal / "R.ARW").write_bytes(b"\x00")
    (cal / "G.ARW").write_bytes(b"\x00")
    (cal / "B.ARW").write_bytes(b"\x00")

    def fake_demosaic(path):
        name = Path(path).name
        if name == "R.ARW":
            return np.full((H, W, 3), 40000, dtype=np.uint16)
        if name == "G.ARW":
            return np.full((H + 5, W, 3), 40000, dtype=np.uint16)
        return np.full((H, W, 3), 40000, dtype=np.uint16)

    monkeypatch.setattr(composite_mod, "demosaic_linear", fake_demosaic)
    clear_ffc_cache()
    try:
        with pytest.raises(CalibrationError, match="calibration shape mismatch"):
            load_ffc_maps(cal)
    finally:
        clear_ffc_cache()


def test_load_ffc_maps_case_insensitive_extension(tmp_path, monkeypatch):
    """`r.arw` (lowercase) should be picked up just like `R.ARW`."""
    cal = tmp_path / "calibration"
    cal.mkdir()
    (cal / "R.arw").write_bytes(b"\x00")
    (cal / "G.arw").write_bytes(b"\x00")
    (cal / "B.arw").write_bytes(b"\x00")

    monkeypatch.setattr(
        composite_mod,
        "demosaic_linear",
        lambda p: np.full((H, W, 3), 40000, dtype=np.uint16),
    )
    clear_ffc_cache()
    try:
        maps = load_ffc_maps(cal)
        assert maps.shape == (H, W)
    finally:
        clear_ffc_cache()
