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


# ---------------------------------------------------------------------------
# Test 9: non-uint16 dtype raises ValueError (FIX 1 — fail-closed dtype guard)
# ---------------------------------------------------------------------------

def test_invert_composite_rejects_non_uint16_dtype():
    """A float32 (or NaN-containing) triplet must raise ValueError — fail-closed.

    A float32 array passes shape validation but would silently encode NaN as
    0 via astype(uint16).  The Step 0 dtype guard must catch this before any
    compute.  Matches detect_rebate Phase 09 dtype discipline.
    """
    _, descriptor, params = _make_fixture(seed=42)

    # Plain float32 triplet (correct shape, wrong dtype)
    float_triplet = np.zeros((H, W, 3), dtype=np.float32)
    with pytest.raises(ValueError, match="uint16"):
        invert_composite(float_triplet, descriptor, params)

    # NaN-containing float32 triplet — the primary silent-failure vector
    nan_triplet = np.full((H, W, 3), np.nan, dtype=np.float32)
    with pytest.raises(ValueError, match="uint16"):
        invert_composite(nan_triplet, descriptor, params)


# ---------------------------------------------------------------------------
# Test 10: nonzero black_point shifts the inversion floor (FIX 2 — pinning)
# ---------------------------------------------------------------------------

def test_invert_composite_nonzero_black_point_shifts_floor():
    """black_point_* is the inversion shadow FLOOR, NOT a second black subtraction.

    Construct a 1x1 pixel with a known value and nonzero black points, then
    assert the output matches the exact (white-x)/(white-black) formula with
    that floor.  This pins the behaviour for Phase 14/15 consumers:
    black_point shifts what maps to 0 in the positive, not what is subtracted
    from the raw scan.

    Formula (for each channel ch):
      step2: work = pixel * (base_target / base_rgb[ch])       (neutralize)
      step3: out_f = (white[ch] - work) / (white[ch] - black[ch])  then clip
      step5: uint16 = int(out_f * 65535)    (truncation, matches astype)

    Concrete values chosen so arithmetic is exact in float32:
      pixel = 8000, base_rgb = base_target = 10000.0
      => step2: work = 8000.0 * (10000/10000) = 8000.0
      black_point = 1000.0, white_point = 50000.0
      => step3: (50000 - 8000) / (50000 - 1000) = 42000 / 49000 ≈ 0.857142...
      => step5: int(0.857142... * 65535) = int(56173.469...) = 56173

    If black_point were incorrectly subtracted from the triplet FIRST:
      pixel - 1000 = 7000 → step3: (50000-7000)/(50000-1000) = 43000/49000
      ≈ 0.877551 → int(0.877551*65535) = 57497  ← different, would fail.
    """
    base_target = 10000.0
    black_point = 1000.0
    white_point = 50000.0
    pixel_val = 8000

    # 1x1x3 pixel, all channels identical for simplicity
    triplet = np.array([[[pixel_val, pixel_val, pixel_val]]], dtype=np.uint16)

    descriptor = BaseRegionDescriptor(
        x=0, y=0, w=1, h=1,
        base_rgb=(base_target, base_target, base_target),
        uniformity_cv=0.0,
        source="manual",
    )
    params = InversionParams(
        base_target=base_target,
        black_point_r=black_point,
        black_point_g=black_point,
        black_point_b=black_point,
        white_point_r=white_point,
        white_point_g=white_point,
        white_point_b=white_point,
        tone_curve_id="linear",
        tone_curve_params=(),
        gamma=1.0,
    )
    result = invert_composite(triplet, descriptor, params)

    # Manual arithmetic using (white-x)/(white-black) with nonzero black:
    # step2: work = 8000.0 * (10000/10000) = 8000.0  (no change — gain=1)
    # step3: (50000 - 8000) / (50000 - 1000) = 42000 / 49000
    out_f = (white_point - float(pixel_val)) / (white_point - black_point)
    expected = int(out_f * 65535.0)  # truncation matches astype(uint16)

    for ch in range(3):
        assert result[0, 0, ch] == expected, (
            f"channel {ch}: expected {expected} (black_point={black_point} "
            f"shifts floor via formula, NOT subtracted from triplet), "
            f"got {result[0, 0, ch]}"
        )

    # Confirm the result does NOT match the "incorrectly-subtracted" value
    wrong_work = float(pixel_val) - black_point  # 7000
    wrong_f = (white_point - wrong_work) / (white_point - black_point)
    wrong_expected = int(wrong_f * 65535.0)
    assert result[0, 0, 0] != wrong_expected, (
        "result matched the wrong 'black_point subtracted from triplet' value — "
        "black_point must only shift the inversion floor, not be subtracted"
    )


# ---------------------------------------------------------------------------
# Test 11: overflow/underflow is CLIPPED, not wrapped (FIX 3 — clip discipline)
# ---------------------------------------------------------------------------

def test_invert_composite_clips_not_wraps_on_overflow():
    """Pre-cast clipping must produce exactly 65535 (ceiling) and 0 (floor).

    The existing overflow test checks that output is uint16-bounded, but that
    is tautologically true for any uint16 array.  This test proves the
    pre-cast np.clip at Step 5 is doing real work by constructing inputs that
    drive pre-cast float values ABOVE 65535 and BELOW 0, then asserting the
    output is clamped — not wrapped or truncated-from-integer overflow.

    Scenario A (drives output toward white — clipped to 65535):
      A strongly UNDEREXPOSED (very low raw value) pixel relative to the base
      inverts to a large positive, which Step 5 scales above 65535.
      pixel = 100, base_rgb = base_target = 10000, black = 0, white = 50000
      step2: work = 100 * (10000/10000) = 100
      step3: (50000 - 100) / 50000 = 0.998 → clip → 0.998
      step5: 0.998 * 65535 = 65404 (well within, no overflow for this case)

      To force overflow above 65535, use white > 65535 (allowed by params
      since white_point is raw counts, not bounded to 65535):
      Use pixel=1, base=base_target=10000, black=0, white=70000
      step2: work = 1.0
      step3: (70000 - 1) / 70000 ≈ 0.99999 → step5: 0.99999*65535 ≈ 65534 OK

      Simpler: drive a pixel BELOW black_point so inversion > 1.0:
      pixel=500, black=1000, white=50000, base=base_target=10000
      step2: work = 500
      step3: (50000 - 500) / (50000 - 1000) = 49500/49000 ≈ 1.0102 > 1 → clips to 1
      step5: 1.0 * 65535 = 65535.0 → uint16 = 65535  ✓

    Scenario B (drives output toward black — clipped to 0):
      A pixel ABOVE white_point inverts to a negative fraction:
      pixel=55000, black=0, white=50000, base=base_target=10000
      step2: work = 55000
      step3: (50000 - 55000) / 50000 = -0.1 → clips to 0
      step5: 0 → uint16 = 0  ✓
    """
    base_target = 10000.0

    descriptor = BaseRegionDescriptor(
        x=0, y=0, w=1, h=1,
        base_rgb=(base_target, base_target, base_target),
        uniformity_cv=0.0,
        source="manual",
    )

    # --- Scenario A: pixel below black_point → inversion > 1 → clips to 65535 ---
    pixel_a = np.array([[[500, 500, 500]]], dtype=np.uint16)
    params_a = InversionParams(
        base_target=base_target,
        black_point_r=1000.0,
        black_point_g=1000.0,
        black_point_b=1000.0,
        white_point_r=50000.0,
        white_point_g=50000.0,
        white_point_b=50000.0,
        tone_curve_id="linear",
        tone_curve_params=(),
        gamma=1.0,
    )
    result_a = invert_composite(pixel_a, descriptor, params_a)

    # Verify pre-cast value would have been above 65535 without clip:
    # step3 result = 49500/49000 ≈ 1.0102 > 1 → step5 raw = 1.0102 * 65535 ≈ 66203
    # np.clip must clamp to 65535 before astype — NOT wrap to 66203 % 65536 = 667
    for ch in range(3):
        assert result_a[0, 0, ch] == 65535, (
            f"channel {ch}: expected 65535 (clipped), got {result_a[0, 0, ch]} — "
            "pre-cast value above 65535 must be CLIPPED, not wrapped/truncated"
        )

    # --- Scenario B: pixel above white_point → inversion < 0 → clips to 0 ---
    pixel_b = np.array([[[55000, 55000, 55000]]], dtype=np.uint16)
    params_b = InversionParams(
        base_target=base_target,
        black_point_r=0.0,
        black_point_g=0.0,
        black_point_b=0.0,
        white_point_r=50000.0,
        white_point_g=50000.0,
        white_point_b=50000.0,
        tone_curve_id="linear",
        tone_curve_params=(),
        gamma=1.0,
    )
    result_b = invert_composite(pixel_b, descriptor, params_b)

    # Verify pre-cast value would have been below 0 without clip:
    # step3 result = (50000-55000)/50000 = -0.1 → step5 raw = -0.1 * 65535 = -6553.5
    # np.clip must clamp to 0 before astype — NOT produce 65536 - 6553 = 58983
    for ch in range(3):
        assert result_b[0, 0, ch] == 0, (
            f"channel {ch}: expected 0 (clipped), got {result_b[0, 0, ch]} — "
            "pre-cast value below 0 must be CLIPPED, not wrapped/truncated"
        )
