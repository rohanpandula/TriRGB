"""Tests for c41_core.fixtures — shape/dtype, determinism, realism, performance.

These tests confirm:
- make_c41_negative() and make_rebate_strip() return HxWx3 uint16 arrays
- same seed → same output (NFR-11 determinism)
- different seeds → different output
- load time <1s (NFR-13 performance gate)
- orange-mask realism: rebate brightest, blue density swing > red, no clip
- blue channel strongly attenuated relative to R and G (Pitfall 4 awareness:
  green reads highest in no-WB raw capture — blue << red AND blue << green)
"""
from __future__ import annotations

import time

import numpy as np
import pytest

from c41_core.fixtures import (
    DEFAULT_SEED,
    _BASE_B,
    _BASE_G,
    _BASE_R,
    make_c41_negative,
    make_rebate_strip,
)


# ---------------------------------------------------------------------------
# Shape and dtype
# ---------------------------------------------------------------------------

def test_make_c41_negative_default_shape_dtype():
    arr = make_c41_negative()
    assert arr.shape == (128, 192, 3), f"Expected (128, 192, 3), got {arr.shape}"
    assert arr.dtype == np.uint16, f"Expected uint16, got {arr.dtype}"


def test_make_c41_negative_custom_shape():
    arr = make_c41_negative(height=64, width=96)
    assert arr.shape == (64, 96, 3)
    assert arr.dtype == np.uint16


def test_make_rebate_strip_default_shape_dtype():
    arr = make_rebate_strip()
    assert arr.shape == (128, 192, 3)
    assert arr.dtype == np.uint16


# ---------------------------------------------------------------------------
# Determinism (NFR-11)
# ---------------------------------------------------------------------------

def test_make_c41_negative_determinism_same_seed():
    """Same seed must produce bit-exact identical arrays."""
    arr1 = make_c41_negative(seed=DEFAULT_SEED)
    arr2 = make_c41_negative(seed=DEFAULT_SEED)
    assert np.array_equal(arr1, arr2), "make_c41_negative(seed=42) must be deterministic"


def test_make_c41_negative_determinism_different_seeds():
    """Different seeds must produce different arrays."""
    arr1 = make_c41_negative(seed=42)
    arr2 = make_c41_negative(seed=99)
    assert not np.array_equal(arr1, arr2), "Different seeds must produce different outputs"


def test_make_rebate_strip_determinism():
    """make_rebate_strip must also be deterministic by seed."""
    arr1 = make_rebate_strip(seed=DEFAULT_SEED)
    arr2 = make_rebate_strip(seed=DEFAULT_SEED)
    assert np.array_equal(arr1, arr2)


# ---------------------------------------------------------------------------
# Load time <1s (NFR-13 performance gate)
# ---------------------------------------------------------------------------

def test_fixture_load_time():
    """A single make_c41_negative() call must complete in <1s."""
    t0 = time.perf_counter()
    make_c41_negative()
    elapsed = time.perf_counter() - t0
    assert elapsed < 1.0, f"make_c41_negative() took {elapsed:.3f}s (limit: 1s)"


def test_rebate_strip_load_time():
    t0 = time.perf_counter()
    make_rebate_strip()
    elapsed = time.perf_counter() - t0
    assert elapsed < 1.0, f"make_rebate_strip() took {elapsed:.3f}s (limit: 1s)"


# ---------------------------------------------------------------------------
# Orange-mask realism
# ---------------------------------------------------------------------------

def test_fixture_orange_mask_realism():
    """Core realism checks for make_c41_negative().

    Verifies:
    1. Rebate (top rows) is brighter than body in the R channel.
    2. Blue channel has a larger density swing (max/min ratio) than red.
       This models the orange-mask effect: blue has ~1.64D vs red ~0.50D.
    3. No pixel reaches 65535 (no clipping in 16-bit space).
    """
    arr = make_c41_negative(seed=42)
    rebate_h = max(1, int(128 * 0.1))

    # Rebate is the brightest region
    assert arr[:rebate_h, :, 0].mean() > arr[rebate_h:, :, 0].mean(), (
        "Rebate strip must be brighter than body in R channel"
    )

    # Blue density swing (max/min ratio) > red density swing
    b_max = float(arr[:, :, 2].max())
    b_min = float(max(arr[:, :, 2].min(), 1))  # guard against zero
    r_max = float(arr[:, :, 0].max())
    r_min = float(max(arr[:, :, 0].min(), 1))
    b_ratio = b_max / b_min
    r_ratio = r_max / r_min
    assert b_ratio > r_ratio, (
        f"Blue density swing ({b_ratio:.1f}) must exceed red density swing ({r_ratio:.1f})"
    )

    # No saturation: default bases (~12k) are far from the 16-bit ceiling.
    # The clip in make_c41_negative allows exactly 65535, so use <= 65534
    # to confirm the default fixture doesn't reach the saturation boundary.
    assert arr.max() <= 65534, (
        f"Default fixture should not reach 65535 (got {arr.max()}); "
        "suggests unrealistically high base or noise params"
    )


def test_fixture_blue_attenuation():
    """Blue channel must be strongly attenuated relative to R and G.

    The no-WB raw defaults are: R=8930, G=12097, B=2952. Green reads highest
    because of Sony sensor sensitivity (capture-space artifact, not the film).
    Blue must be significantly below BOTH red and green.

    NOTE: We assert base_b << base_r AND base_b << base_g, NOT base_r > base_g.
    (Pitfall 4: green > red in no-WB mode is CORRECT for a7CR — do not assert
    red dominance in raw space.)
    """
    # Check the module constants directly
    assert _BASE_B < _BASE_R, f"Blue base ({_BASE_B}) must be < red base ({_BASE_R})"
    assert _BASE_B < _BASE_G, f"Blue base ({_BASE_B}) must be < green base ({_BASE_G})"

    # Also check via array: body mean of blue << red and green
    arr = make_c41_negative(seed=42)
    rebate_h = max(1, int(128 * 0.1))
    body = arr[rebate_h:, :, :]
    mean_b = float(body[:, :, 2].mean())
    mean_r = float(body[:, :, 0].mean())
    mean_g = float(body[:, :, 1].mean())
    assert mean_b < mean_r, f"Blue body mean ({mean_b:.0f}) must be < red ({mean_r:.0f})"
    assert mean_b < mean_g, f"Blue body mean ({mean_b:.0f}) must be < green ({mean_g:.0f})"


def test_make_rebate_strip_uniform():
    """Rebate strip must have no density variation (uniform base, no density swing)."""
    arr = make_rebate_strip(seed=42)
    # Per-channel CV (noise/mean) should be very small — pure base + tiny noise
    # Use noise_sigma=50 default: CV = 50 / 8930 << 1%
    for ch_idx, base in enumerate([_BASE_R, _BASE_G, _BASE_B]):
        ch = arr[:, :, ch_idx].astype(np.float32)
        cv = ch.std() / max(ch.mean(), 1.0)
        assert cv < 0.05, f"Channel {ch_idx} CV={cv:.4f} suggests density variation (expected <5%)"


# ---------------------------------------------------------------------------
# FIX 4: Fixture param validation — misuse fails clearly
# ---------------------------------------------------------------------------

def test_make_c41_negative_rejects_zero_width():
    """FIX 4: zero width must raise ValueError, not produce a numpy broadcast error."""
    with pytest.raises(ValueError, match="width"):
        make_c41_negative(height=128, width=0)


def test_make_c41_negative_rejects_zero_height():
    """FIX 4: zero height must raise ValueError."""
    with pytest.raises(ValueError, match="height"):
        make_c41_negative(height=0, width=192)


def test_make_c41_negative_rejects_rebate_frac_above_one():
    """FIX 4: rebate_height_frac > 1 must raise ValueError (fraction out of range)."""
    with pytest.raises(ValueError, match="rebate_height_frac"):
        make_c41_negative(rebate_height_frac=1.5)


def test_make_c41_negative_rejects_rebate_frac_zero():
    """FIX 4: rebate_height_frac == 0 must raise ValueError (no rebate rows)."""
    with pytest.raises(ValueError, match="rebate_height_frac"):
        make_c41_negative(rebate_height_frac=0.0)


def test_make_rebate_strip_rejects_zero_width():
    """FIX 4: zero width on make_rebate_strip must raise ValueError."""
    with pytest.raises(ValueError, match="width"):
        make_rebate_strip(height=128, width=0)
