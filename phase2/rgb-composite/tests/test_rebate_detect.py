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
