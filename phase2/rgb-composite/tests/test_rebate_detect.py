"""Tests for rgb_composite.rebate_detect — automatic and manual rebate detection.

No color discrimination: detection scores green-channel brightness only (NFR-11).
All tests are fully deterministic (NFR-11) and operate against the Phase 08
synthetic fixtures (make_c41_negative / make_rebate_strip) which have known,
planted rebate geometry.

Convention: H, W = 128, 192 (matches tests/test_ffc.py and fixture defaults).
"""
from __future__ import annotations

import numpy as np
import pytest

from c41_core import BaseRegionDescriptor, make_c41_negative, make_rebate_strip
from rgb_composite import detect_rebate, manual_picker


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

H, W = 128, 192
# Planted rebate bbox in make_c41_negative(height=128, rebate_height_frac=0.1):
#   rows 0 .. max(1, int(128 * 0.1)) - 1 = rows 0..11  (12 rows)
_REBATE_X = 0
_REBATE_Y = 0
_REBATE_W = W
_REBATE_H = max(1, int(H * 0.1))  # 12


# ---------------------------------------------------------------------------
# Private helper: bbox overlap fraction
# ---------------------------------------------------------------------------

def _overlap_fraction(ax: int, ay: int, aw: int, ah: int,
                       bx: int, by: int, bw: int, bh: int) -> float:
    """Intersection area / min(area_a, area_b).  Returns 0.0 if no overlap."""
    ix0, iy0 = max(ax, bx), max(ay, by)
    ix1, iy1 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    return (ix1 - ix0) * (iy1 - iy0) / min(aw * ah, bw * bh)


# ===========================================================================
# Task 2 tests: detect_rebate (auto-detection)
# ===========================================================================

def test_detect_rebate_finds_planted_rebate():
    """detect_rebate on make_c41_negative must overlap the planted top-rows rebate >= 0.5."""
    img = make_c41_negative(height=H, width=W, seed=42)
    desc = detect_rebate(img)
    overlap = _overlap_fraction(
        desc.x, desc.y, desc.w, desc.h,
        _REBATE_X, _REBATE_Y, _REBATE_W, _REBATE_H,
    )
    assert overlap >= 0.5, (
        f"Detected bbox ({desc.x},{desc.y},{desc.w},{desc.h}) overlaps planted rebate "
        f"({_REBATE_X},{_REBATE_Y},{_REBATE_W},{_REBATE_H}) by only {overlap:.3f} < 0.5"
    )


def test_descriptor_source_and_schema_version():
    """detect_rebate returns source='auto' and schema_version=1."""
    img = make_c41_negative(height=H, width=W, seed=42)
    desc = detect_rebate(img)
    assert desc.source == "auto", f"Expected source='auto', got {desc.source!r}"
    assert desc.schema_version == 1, f"Expected schema_version=1, got {desc.schema_version}"


def test_uniformity_cv_in_range():
    """detect_rebate descriptor.uniformity_cv must be in [0.0, 100.0]."""
    img = make_c41_negative(height=H, width=W, seed=42)
    desc = detect_rebate(img)
    assert isinstance(desc.uniformity_cv, float), "uniformity_cv should be a float"
    assert 0.0 <= desc.uniformity_cv <= 100.0, (
        f"uniformity_cv={desc.uniformity_cv} is outside [0, 100]"
    )


def test_base_rgb_blue_less_than_red_and_green():
    """base_rgb: blue < red AND blue < green (Pitfall 4 — no-WB raw, green reads highest).

    NOTE: Do NOT assert red > green.  In no-WB ProPhoto, the Sony a7CR green
    channel has ~2x native sensitivity so G > R at Dmin.
    """
    img = make_c41_negative(height=H, width=W, seed=42)
    desc = detect_rebate(img)
    r, g, b = desc.base_rgb
    assert b < r, f"Expected blue ({b:.1f}) < red ({r:.1f})"
    assert b < g, f"Expected blue ({b:.1f}) < green ({g:.1f})"


def test_detect_on_uniform_rebate_strip():
    """detect_rebate on make_rebate_strip returns valid descriptor; base_rgb close to known base."""
    img = make_rebate_strip(height=H, width=W, seed=42)
    desc = detect_rebate(img)
    # Validate descriptor type and source
    assert isinstance(desc, BaseRegionDescriptor)
    assert desc.source == "auto"
    # base_rgb should be close to the planted values (within a few percent).
    # Known base: R=8930, G=12097, B=2952
    r, g, b = desc.base_rgb
    assert abs(r - 8930) / 8930 < 0.05, f"R base too far from 8930: got {r:.1f}"
    assert abs(g - 12097) / 12097 < 0.05, f"G base too far from 12097: got {g:.1f}"
    assert abs(b - 2952) / 2952 < 0.05, f"B base too far from 2952: got {b:.1f}"


def test_detect_rebate_deterministic():
    """Same input array produces identical descriptors across two calls (NFR-11)."""
    img = make_c41_negative(height=H, width=W, seed=42)
    desc1 = detect_rebate(img)
    desc2 = detect_rebate(img)
    assert desc1.x == desc2.x and desc1.y == desc2.y, "x,y not deterministic"
    assert desc1.w == desc2.w and desc1.h == desc2.h, "w,h not deterministic"
    assert np.allclose(desc1.base_rgb, desc2.base_rgb), "base_rgb not deterministic"
    assert np.isclose(desc1.uniformity_cv, desc2.uniformity_cv), "uniformity_cv not deterministic"


def test_no_color_discrimination():
    """detect_rebate succeeds on a grayscale image (R==G==B) without error.

    Proves no code path depends on channels differing (NFR-11, color-blindness).
    Uses np.broadcast_to to make a single-channel grayscale visible in all 3 channels.
    """
    # Create a grayscale image: all three channels identical (flat green channel value)
    gray_channel = np.full((H, W), 12097, dtype=np.uint16)
    img = np.stack([gray_channel, gray_channel, gray_channel], axis=-1)
    # Must not raise; any valid descriptor is acceptable
    desc = detect_rebate(img)
    assert isinstance(desc, BaseRegionDescriptor), "Should return BaseRegionDescriptor"
    assert desc.source == "auto"
    assert 0.0 <= desc.uniformity_cv <= 100.0


def test_detect_rebate_downsample_path():
    """detect_rebate on a 1600x2400 image exercises scale < 1.0 downsample path (WR-02).

    max(1600, 2400) = 2400 > 1500, so scale = 1500/2400 = 0.625 < 1.0.
    The cv2.resize + bbox back-projection path must:
      - find the planted rebate (top 160 rows in make_c41_negative at rebate_height_frac=0.1)
      - return a bbox fully within the FULL-RESOLUTION 1600x2400 image bounds
      - overlap the planted rebate bbox by at least 0.5
    """
    BIG_H, BIG_W = 1600, 2400
    planted_rebate_h = max(1, int(BIG_H * 0.1))  # 160 rows
    img = make_c41_negative(height=BIG_H, width=BIG_W, seed=42)
    desc = detect_rebate(img)
    assert isinstance(desc, BaseRegionDescriptor)
    assert desc.source == "auto"
    # Bbox must be within FULL-RESOLUTION image (not the downsampled proxy)
    assert desc.x >= 0, f"x={desc.x} < 0"
    assert desc.y >= 0, f"y={desc.y} < 0"
    assert desc.w >= 1, f"w={desc.w} < 1"
    assert desc.h >= 1, f"h={desc.h} < 1"
    assert desc.x + desc.w <= BIG_W, (
        f"bbox right edge {desc.x + desc.w} exceeds full-res width {BIG_W}"
    )
    assert desc.y + desc.h <= BIG_H, (
        f"bbox bottom edge {desc.y + desc.h} exceeds full-res height {BIG_H}"
    )
    # Must find the planted rebate at the top of the image
    overlap = _overlap_fraction(
        desc.x, desc.y, desc.w, desc.h,
        0, 0, BIG_W, planted_rebate_h,
    )
    assert overlap >= 0.5, (
        f"Detected bbox ({desc.x},{desc.y},{desc.w},{desc.h}) overlaps planted rebate "
        f"(0,0,{BIG_W},{planted_rebate_h}) by only {overlap:.3f} < 0.5"
    )


def test_detect_rebate_all_zero_green_raises():
    """detect_rebate on all-zero green channel raises ValueError (IN-02 / CR-01 regression lock).

    Verifies the fail-closed guard added for CR-01: a degenerate all-zero green
    channel must raise ValueError with a message mentioning the green channel,
    not silently return a descriptor at (0, 0).
    """
    img = np.zeros((H, W, 3), dtype=np.uint16)
    with pytest.raises(ValueError, match="green channel"):
        detect_rebate(img)


# ===========================================================================
# Task 3 tests: manual_picker
# ===========================================================================

def test_manual_picker_center_click():
    """manual_picker at center returns source='manual', schema_version=1, bbox near center."""
    img = make_rebate_strip(height=H, width=W, seed=42)
    row, col = H // 2, W // 2
    desc = manual_picker(img, row=row, col=col)
    assert isinstance(desc, BaseRegionDescriptor), "Should return BaseRegionDescriptor"
    assert desc.source == "manual", f"Expected source='manual', got {desc.source!r}"
    assert desc.schema_version == 1, f"Expected schema_version=1, got {desc.schema_version}"
    # Bbox should be approximately centered near (col, row) — check containment
    assert desc.x <= col <= desc.x + desc.w, "col not inside bbox x-range"
    assert desc.y <= row <= desc.y + desc.h, "row not inside bbox y-range"


def test_manual_picker_same_type_as_auto():
    """manual_picker output is a BaseRegionDescriptor with identical field set as detect_rebate (SC-2)."""
    img = make_rebate_strip(height=H, width=W, seed=42)
    auto_desc = detect_rebate(img)
    manual_desc = manual_picker(img, row=H // 2, col=W // 2)
    # Both must be the same type
    assert type(auto_desc) is type(manual_desc), (
        f"Type mismatch: detect_rebate returns {type(auto_desc).__name__}, "
        f"manual_picker returns {type(manual_desc).__name__}"
    )
    # Both must have the same set of attributes
    import dataclasses
    auto_fields = {f.name for f in dataclasses.fields(auto_desc)}
    manual_fields = {f.name for f in dataclasses.fields(manual_desc)}
    assert auto_fields == manual_fields, (
        f"Field mismatch: auto has {auto_fields}, manual has {manual_fields}"
    )


def test_manual_picker_edge_clamping():
    """manual_picker at corner (0,0) and (H-1, W-1) returns valid clamped bbox (w>=1, h>=1)."""
    img = make_rebate_strip(height=H, width=W, seed=42)
    # Top-left corner
    desc_tl = manual_picker(img, row=0, col=0)
    assert desc_tl.w >= 1 and desc_tl.h >= 1, "Top-left corner: w or h is 0"
    assert desc_tl.x >= 0 and desc_tl.y >= 0, "Top-left corner: negative coords"
    assert desc_tl.x + desc_tl.w <= W, "Top-left corner: bbox extends past image width"
    assert desc_tl.y + desc_tl.h <= H, "Top-left corner: bbox extends past image height"
    # Bottom-right corner
    desc_br = manual_picker(img, row=H - 1, col=W - 1)
    assert desc_br.w >= 1 and desc_br.h >= 1, "Bottom-right corner: w or h is 0"
    assert desc_br.x >= 0 and desc_br.y >= 0, "Bottom-right corner: negative coords"
    assert desc_br.x + desc_br.w <= W, "Bottom-right: bbox extends past image width"
    assert desc_br.y + desc_br.h <= H, "Bottom-right: bbox extends past image height"


def test_manual_picker_oob_raises():
    """manual_picker raises ValueError for out-of-bounds (row, col) coordinates."""
    img = make_rebate_strip(height=H, width=W, seed=42)
    with pytest.raises(ValueError):
        manual_picker(img, row=-1, col=0)
    with pytest.raises(ValueError):
        manual_picker(img, row=H, col=0)  # == H is out of bounds
    with pytest.raises(ValueError):
        manual_picker(img, row=0, col=-1)
    with pytest.raises(ValueError):
        manual_picker(img, row=0, col=W)  # == W is out of bounds


def test_manual_picker_deterministic():
    """Two manual_picker calls on the same (img, row, col) return identical descriptors (NFR-11)."""
    img = make_rebate_strip(height=H, width=W, seed=42)
    desc1 = manual_picker(img, row=H // 4, col=W // 4)
    desc2 = manual_picker(img, row=H // 4, col=W // 4)
    assert desc1.x == desc2.x and desc1.y == desc2.y, "x,y not deterministic"
    assert desc1.w == desc2.w and desc1.h == desc2.h, "w,h not deterministic"
    assert np.allclose(desc1.base_rgb, desc2.base_rgb), "base_rgb not deterministic"
    assert np.isclose(desc1.uniformity_cv, desc2.uniformity_cv), "uniformity_cv not deterministic"


def test_manual_picker_uniformity_cv_in_range():
    """manual_picker descriptor.uniformity_cv must be in [0.0, 100.0]."""
    img = make_rebate_strip(height=H, width=W, seed=42)
    desc = manual_picker(img, row=H // 2, col=W // 2)
    assert isinstance(desc.uniformity_cv, float), "uniformity_cv should be a float"
    assert 0.0 <= desc.uniformity_cv <= 100.0, (
        f"uniformity_cv={desc.uniformity_cv} is outside [0, 100]"
    )


def test_manual_picker_float_inbounds_truncates():
    """manual_picker with float in-bounds coords truncates and succeeds (WR-01).

    Phase 14 SwiftUI bridge may pass mouse-click coords as floats (e.g. 0.5).
    These must truncate to the corresponding int and return a valid descriptor,
    not crash with TypeError.
    """
    img = make_rebate_strip(height=H, width=W, seed=42)
    # 0.5 truncates to 0 — valid in-bounds coord
    desc = manual_picker(img, row=float(H // 2) + 0.7, col=float(W // 2) + 0.9)
    assert isinstance(desc, BaseRegionDescriptor), "Should return BaseRegionDescriptor"
    # The returned bbox should match the int-truncated coords
    desc_int = manual_picker(img, row=H // 2, col=W // 2)
    assert desc.x == desc_int.x and desc.y == desc_int.y, (
        "float truncated coord should produce same bbox as int coord"
    )


def test_manual_picker_float_oob_raises_value_error():
    """manual_picker with float out-of-bounds coord raises ValueError (WR-01).

    The bounds check runs on the ORIGINAL (pre-truncation) value, so both
    float(-1.0) and the negative-fractional -0.1 are caught before int() is
    called.  float(W) is also out-of-bounds (W is not a valid col index).
    """
    img = make_rebate_strip(height=H, width=W, seed=42)
    # -1.0 is negative → ValueError caught pre-truncation
    with pytest.raises(ValueError):
        manual_picker(img, row=-1.0, col=0)
    # float(W) truncates to W: out-of-bounds (W is not a valid col index) → ValueError
    with pytest.raises(ValueError):
        manual_picker(img, row=0, col=float(W))


# ===========================================================================
# NFR-15 peer-review fix tests
# ===========================================================================

def test_detect_rebate_center_backprojection_formula():
    """FIX 1: back-projection uses per-axis ratios + center offset (NFR-15).

    Regression guard for two bugs in the old formula:
    (a) ``best_y_s / scale`` treats the scored pixel as TOP-LEFT of the window;
        correct formula is ``(best_y_s + 0.5) * ry - win_h/2`` (center offset).
    (b) Single ``scale`` was used for both axes; correct is per-axis
        ``ry = H / new_h``, ``rx = W / new_w``.

    Test method: build a synthetic uint16 image with a bright stripe at rows
    200..299 (not at y=0) and use a custom ``long_edge_target`` to force the
    downsampled ``best_y_s > 0``.  When the best window is not clamped to the
    top edge, the center-offset formula gives a DIFFERENT ``y`` than the old
    top-left formula.  We compute both expected values and assert the result
    matches the center-offset (correct) formula.
    """
    import cv2
    from scipy.ndimage import uniform_filter as _uf

    H, W = 500, 1600
    long_edge_target = 400
    stripe_y0, stripe_y1 = 200, 300

    # Build synthetic image: dark body, bright uniform stripe at rows 200..299.
    rng = np.random.default_rng(99)
    img = np.zeros((H, W, 3), dtype=np.uint16)
    body = rng.normal(3000, 100, (H, W, 3)).clip(0, 65535).astype(np.uint16)
    img[:] = body
    img[stripe_y0:stripe_y1, :, :] = (
        rng.normal(40000, 30, (stripe_y1 - stripe_y0, W, 3)).clip(0, 65535).astype(np.uint16)
    )

    desc = detect_rebate(img, long_edge_target=long_edge_target)

    # Replicate score map to find best_y_s and compute BOTH expected y values.
    scale = min(1.0, long_edge_target / max(H, W))
    new_h, new_w = int(H * scale), int(W * scale)
    ry = H / float(new_h)
    rx = W / float(new_w)
    green_s = cv2.resize(
        img[:, :, 1], (new_w, new_h), interpolation=cv2.INTER_AREA
    ).astype(np.float32)
    win = max(3, int(min(new_h, new_w) * 0.05))
    mean_map = _uf(green_s, size=win, mode="nearest").astype(np.float64)
    mean_sq = _uf((green_s ** 2).astype(np.float64), size=win, mode="nearest")
    var_map = np.maximum(mean_sq - mean_map ** 2, 0.0)
    cv_map = (np.sqrt(var_map) / np.maximum(mean_map, 1.0)).astype(np.float32)
    lap = cv2.Laplacian(green_s.clip(0, 65535).astype(np.uint16), cv2.CV_64F)
    detail_map = _uf(np.abs(lap).astype(np.float32), size=win, mode="nearest")
    b = (mean_map.astype(np.float32) / max(float(mean_map.max()), 1e-9)).clip(0, 1)
    c = (cv_map / max(float(cv_map.max()), 1e-9)).clip(0, 1)
    d = (detail_map / max(float(detail_map.max()), 1e-9)).clip(0, 1)
    score = b - c - d
    best_y_s, _ = np.unravel_index(int(np.argmax(score)), score.shape)

    # Center-offset formula (FIX 1 — correct):
    win_h = max(1, int(round(win * ry)))
    cy_correct = (best_y_s + 0.5) * ry
    y_correct = max(0, min(H - win_h, int(round(cy_correct - win_h / 2.0))))

    # Old formula (wrong — top-left placed at scored center):
    y_wrong = max(0, min(H - 1, int(round(best_y_s / scale))))

    # The bright stripe is at rows 200..299 so best_y_s > 0: both formulas
    # differ (the fixture is designed so the stripe is not at the image edge).
    assert best_y_s > 0, (
        "best_y_s==0 means the stripe is at the image top; test fixture would not "
        "distinguish the two formulas (both are clamped to y=0)"
    )
    assert y_correct != y_wrong, (
        f"y_correct={y_correct} == y_wrong={y_wrong}; formulas do not differ for "
        f"best_y_s={best_y_s} — choose a different fixture"
    )

    assert desc.y == y_correct, (
        f"desc.y={desc.y} does not match center-offset formula y={y_correct}; "
        f"old top-left formula would give y={y_wrong}.  "
        f"Center-offset back-projection may not be applied."
    )
    # Bbox must remain within image bounds
    assert desc.x >= 0 and desc.y >= 0
    assert desc.x + desc.w <= W
    assert desc.y + desc.h <= H


def test_detect_rebate_float32_input_raises_value_error():
    """FIX 2: float32 input raises ValueError (dtype guard, NFR-15).

    detect_rebate expects uint16 (demosaic_linear output).  A float32 array
    with the same shape must be rejected with a clear ValueError before any
    computation, not silently produce garbage via uint16 clipping.
    """
    img_f32 = np.ones((H, W, 3), dtype=np.float32) * 0.5
    with pytest.raises(ValueError, match="uint16"):
        detect_rebate(img_f32)


def test_manual_picker_negative_fractional_oob_raises():
    """FIX 3: manual_picker rejects negative fractional coords pre-truncation (NFR-15).

    row=-0.1 truncates to 0 under int(), which is in-bounds — the old code
    would silently accept it.  After the fix, the bounds check runs on the
    original float value so -0.1 is correctly rejected.

    Also confirms that a positive in-bounds float (5.7) is still accepted and
    truncates to the corresponding integer (5), returning a valid descriptor.
    """
    img = make_rebate_strip(height=H, width=W, seed=42)

    # -0.1 must raise even though int(-0.1) == 0
    with pytest.raises(ValueError):
        manual_picker(img, row=-0.1, col=0)

    # -0.9 must also raise (int(-0.9) == 0 truncates toward zero in Python)
    with pytest.raises(ValueError):
        manual_picker(img, row=0, col=-0.9)

    # In-bounds positive float: 5.7 → truncates to 5, must succeed
    desc = manual_picker(img, row=5.7, col=10.3)
    assert isinstance(desc, BaseRegionDescriptor)
    # Must match the integer-truncated call
    desc_int = manual_picker(img, row=5, col=10)
    assert desc.x == desc_int.x and desc.y == desc_int.y and \
           desc.w == desc_int.w and desc.h == desc_int.h
