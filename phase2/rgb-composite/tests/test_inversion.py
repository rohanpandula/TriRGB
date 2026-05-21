"""Tests for ``invert_composite`` in ``rgb_composite.composite``.

No code path in ``invert_composite`` or these tests requires color-vision /
by-eye color judgment — every assertion is a numeric comparison (NFR-11 /
SC-5).  All correctness properties are verified against integer/float values
derived from ``make_c41_negative`` with a fixed seed; no perceptual color
decisions are made anywhere.

Phase 11, plan 01 — RED wave (Task 1).  Tests are written before the
implementation exists; the import of ``invert_composite`` below will fail
with ``ImportError`` until Task 2 provides the implementation.
"""
from __future__ import annotations

import numpy as np
import pytest

from c41_core import BaseRegionDescriptor, InversionParams, make_c41_negative
from rgb_composite.composite import invert_composite


# ---------------------------------------------------------------------------
# Shared fixture helpers
# ---------------------------------------------------------------------------

H, W = 128, 192


def _make_fixture(seed: int = 42):
    """Build a synthetic C-41 negative and matching descriptor+params.

    Returns (img, descriptor, params) where:
      - img is HxWx3 uint16 from make_c41_negative
      - descriptor measures the top rebate strip mean per channel
      - params use base_g as base_target, black_point_*=0.0,
        white_point_*=base_target, tone_curve_id="linear", gamma=1.0

    Per RESEARCH.md pitfall 4: green reads HIGHEST in no-WB raw.  We set
    base_target = base_g (the dominant channel) so white_point > black_point
    is always satisfied (InversionParams CR-02 guard).
    """
    img = make_c41_negative(H, W, seed=seed)
    rebate_h = max(1, int(H * 0.1))

    base_r = float(img[:rebate_h, :, 0].mean())
    base_g = float(img[:rebate_h, :, 1].mean())
    base_b = float(img[:rebate_h, :, 2].mean())
    base_target = base_g  # arbitrary neutral gray (G is highest channel)

    descriptor = BaseRegionDescriptor(
        x=0, y=0, w=W, h=rebate_h,
        base_rgb=(base_r, base_g, base_b),
        uniformity_cv=1.0,
        source="manual",
    )
    params = InversionParams(
        base_target=base_target,
        black_point_r=0.0,
        black_point_g=0.0,
        black_point_b=0.0,
        white_point_r=base_target,
        white_point_g=base_target,
        white_point_b=base_target,
        tone_curve_id="linear",
        tone_curve_params=(),
        gamma=1.0,
    )
    return img, descriptor, params


# ---------------------------------------------------------------------------
# Test 1: polarity + base neutralization (SC-3)
# ---------------------------------------------------------------------------

def test_invert_composite_polarity_and_base_neutralization():
    """SC-3: dense orange body becomes lighter than the neutralized rebate.

    Two simultaneous assertions (RESEARCH.md SC-3, lines 486-536):
      Polarity    — for every channel, body mean > rebate mean by > 10 000
                    counts (dense negative → lighter positive).
      Neutralization — the three rebate-output channel means are within
                    500 counts of each other (base forced to R=G=B gray).

    All assertions are numeric comparisons — no color-vision path (NFR-11).
    RESEARCH.md verified the real gap ~22 k and inter-channel deviation ~167.
    """
    img, descriptor, params = _make_fixture(seed=42)
    rebate_h = max(1, int(H * 0.1))

    result = invert_composite(img, descriptor, params)

    rebate_out = result[:rebate_h, :, :]
    body_out = result[rebate_h:, :, :]

    # Polarity: dense body → lighter positive (>10 000 counts above rebate mean)
    for ch in range(3):
        body_mean = float(body_out[:, :, ch].mean())
        rebate_mean = float(rebate_out[:, :, ch].mean())
        assert body_mean > rebate_mean + 10_000, (
            f"channel {ch}: body mean ({body_mean:.0f}) not > "
            f"rebate mean ({rebate_mean:.0f}) + 10 000"
        )

    # Neutralization: rebate output channels are within 500 counts of each other
    rebate_means = [float(rebate_out[:, :, ch].mean()) for ch in range(3)]
    max_deviation = max(
        abs(rebate_means[i] - rebate_means[j])
        for i in range(3)
        for j in range(3)
    )
    assert max_deviation < 500, (
        f"rebate channel means not neutral: {[f'{m:.1f}' for m in rebate_means]}, "
        f"max_deviation={max_deviation:.0f} (threshold=500)"
    )


# ---------------------------------------------------------------------------
# Test 2: determinism (SC-2 / NFR-11)
# ---------------------------------------------------------------------------

def test_invert_composite_determinism():
    """SC-2: two calls with identical inputs must be bit-exact equal (no RNG)."""
    img, descriptor, params = _make_fixture(seed=42)

    out1 = invert_composite(img, descriptor, params)
    out2 = invert_composite(img, descriptor, params)

    np.testing.assert_array_equal(
        out1, out2,
        err_msg="invert_composite is not deterministic — two identical calls differ",
    )


# ---------------------------------------------------------------------------
# Test 3: output dtype and shape
# ---------------------------------------------------------------------------

def test_invert_composite_output_dtype_and_shape():
    """Output must be np.uint16 with shape (H, W, 3) — same as input."""
    img, descriptor, params = _make_fixture(seed=42)

    result = invert_composite(img, descriptor, params)

    assert result.dtype == np.uint16, (
        f"expected uint16 output, got {result.dtype}"
    )
    assert result.shape == img.shape, (
        f"output shape {result.shape} != input shape {img.shape}"
    )


# ---------------------------------------------------------------------------
# Test 4: no overflow (clip discipline)
# ---------------------------------------------------------------------------

def test_invert_composite_output_no_overflow():
    """Output values must be in [0, 65535] — no uint16 wrap-around."""
    img, descriptor, params = _make_fixture(seed=42)

    result = invert_composite(img, descriptor, params)

    assert int(result.min()) >= 0, (
        f"output contains values below 0: min={result.min()}"
    )
    assert int(result.max()) <= 65535, (
        f"output contains values above 65535: max={result.max()}"
    )


# ---------------------------------------------------------------------------
# Test 5: identity tone curve (SC-1)
# ---------------------------------------------------------------------------

def test_linear_tone_curve_is_identity():
    """SC-1: "linear" tone_curve_id applies no shaping beyond invert+scale.

    Equivalence check: the output of invert_composite with tone_curve_id="linear"
    must equal the output we compute manually with the same arithmetic (no
    tone shaping applied after the inversion formula).  This pins that the
    "linear" path is a true identity transform.

    All comparisons are numeric — no color-vision path (NFR-11 / SC-5).
    """
    # Use a very simple 1x1 pixel array so we can compute the expected value
    # exactly without floating-point ambiguity from large arrays.
    # One pixel at mid-gray relative to base_target.
    base_target = 10000.0
    mid_val = np.array([[[5000, 5000, 5000]]], dtype=np.uint16)  # shape (1,1,3)

    descriptor = BaseRegionDescriptor(
        x=0, y=0, w=1, h=1,
        base_rgb=(base_target, base_target, base_target),
        uniformity_cv=0.0,
        source="manual",
    )
    params = InversionParams(
        base_target=base_target,
        black_point_r=0.0,
        black_point_g=0.0,
        black_point_b=0.0,
        white_point_r=base_target,
        white_point_g=base_target,
        white_point_b=base_target,
        tone_curve_id="linear",
        tone_curve_params=(),
        gamma=1.0,
    )
    result = invert_composite(mid_val, descriptor, params)

    # Manual arithmetic (identity tone = no tone shaping):
    # Step 2: gain = base_target / base_target = 1.0 → work = 5000.0
    # Step 3: (10000 - 5000) / (10000 - 0) = 0.5 → clip → 0.5
    # Step 5: 0.5 * 65535 = 32767.5 → astype(uint16) = 32767
    expected_val = np.uint16(int(0.5 * 65535.0))  # truncation matches astype

    for ch in range(3):
        assert result[0, 0, ch] == expected_val, (
            f"channel {ch}: expected {expected_val}, got {result[0, 0, ch]}"
        )


# ---------------------------------------------------------------------------
# Test 6: non-"linear" tone_curve_id raises NotImplementedError
# ---------------------------------------------------------------------------

def test_invert_composite_unknown_tone_curve_raises():
    """Non-"linear" tone_curve_id must raise NotImplementedError (fail-closed)."""
    img, descriptor, _ = _make_fixture(seed=42)

    params_bad = InversionParams(
        base_target=float(img[:max(1, int(H * 0.1)), :, 1].mean()),
        black_point_r=0.0,
        black_point_g=0.0,
        black_point_b=0.0,
        white_point_r=float(img[:max(1, int(H * 0.1)), :, 1].mean()),
        white_point_g=float(img[:max(1, int(H * 0.1)), :, 1].mean()),
        white_point_b=float(img[:max(1, int(H * 0.1)), :, 1].mean()),
        tone_curve_id="s-curve",  # unsupported
        tone_curve_params=(),
        gamma=1.0,
    )

    with pytest.raises(NotImplementedError, match="s-curve"):
        invert_composite(img, descriptor, params_bad)


# ---------------------------------------------------------------------------
# Test 7: near-zero base_rgb raises ValueError
# ---------------------------------------------------------------------------

def test_invert_composite_rejects_near_zero_base():
    """A descriptor with a base_rgb channel below the threshold raises ValueError."""
    img = make_c41_negative(H, W, seed=42)

    # B channel set to 0.5 — well below _MIN_BASE_CHANNEL = 100.0
    bad_descriptor = BaseRegionDescriptor(
        x=0, y=0, w=W, h=max(1, int(H * 0.1)),
        base_rgb=(8929.0, 12096.0, 0.5),  # B near-zero
        uniformity_cv=1.0,
        source="manual",
    )
    params = InversionParams(
        base_target=12096.0,
        black_point_r=0.0,
        black_point_g=0.0,
        black_point_b=0.0,
        white_point_r=12096.0,
        white_point_g=12096.0,
        white_point_b=12096.0,
        tone_curve_id="linear",
        tone_curve_params=(),
        gamma=1.0,
    )

    with pytest.raises(ValueError, match="below the minimum threshold"):
        invert_composite(img, bad_descriptor, params)


# ---------------------------------------------------------------------------
# Test 8: wrong-shape triplet raises ValueError
# ---------------------------------------------------------------------------

def test_invert_composite_rejects_wrong_shape():
    """A non-HxWx3 array (e.g. HxW or HxWx2) raises ValueError matching 'HxWx3'."""
    _, descriptor, params = _make_fixture(seed=42)

    # 2D array (HxW)
    bad_2d = np.zeros((H, W), dtype=np.uint16)
    with pytest.raises(ValueError, match="HxWx3"):
        invert_composite(bad_2d, descriptor, params)

    # 3D array with wrong channel count
    bad_2ch = np.zeros((H, W, 2), dtype=np.uint16)
    with pytest.raises(ValueError, match="HxWx3"):
        invert_composite(bad_2ch, descriptor, params)
