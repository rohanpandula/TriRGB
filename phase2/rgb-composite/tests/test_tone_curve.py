from __future__ import annotations

import numpy as np

from rgb_composite.composite import hd_sigmoid_tone


def test_hd_sigmoid_tone_is_monotonic_with_pinned_endpoints_and_range():
    ramp = np.linspace(0.0, 1.0, 10_001, dtype=np.float32)

    out = hd_sigmoid_tone(ramp, contrast=5.0, pivot=0.5)

    assert out.dtype == np.float32
    assert out.shape == ramp.shape
    assert np.all(np.diff(out.astype(np.float64)) >= 0.0)
    assert float(out[0]) == 0.0
    assert float(out[-1]) == 1.0
    assert float(out.min()) >= 0.0
    assert float(out.max()) <= 1.0


def test_hd_sigmoid_tone_is_deterministic():
    ramp = np.linspace(0.0, 1.0, 4096, dtype=np.float32)

    out1 = hd_sigmoid_tone(ramp, contrast=4.5, pivot=0.45)
    out2 = hd_sigmoid_tone(ramp, contrast=4.5, pivot=0.45)

    np.testing.assert_array_equal(out1, out2)


def test_hd_sigmoid_tone_near_zero_contrast_is_identity():
    ramp = np.linspace(0.0, 1.0, 257, dtype=np.float32)

    out = hd_sigmoid_tone(ramp, contrast=1e-5, pivot=0.5)

    np.testing.assert_allclose(out, ramp, rtol=0.0, atol=0.0)


def test_hd_sigmoid_tone_larger_contrast_has_steeper_midtone_slope():
    x = np.array([0.49, 0.51], dtype=np.float32)

    low = hd_sigmoid_tone(x, contrast=1.0, pivot=0.5)
    high = hd_sigmoid_tone(x, contrast=6.0, pivot=0.5)

    assert float(high[1] - high[0]) > float(low[1] - low[0])


def test_hd_sigmoid_tone_clamps_out_of_range_pivot_without_nan():
    ramp = np.linspace(0.0, 1.0, 1024, dtype=np.float32)

    for pivot in (-10.0, 10.0):
        out = hd_sigmoid_tone(ramp, contrast=50.0, pivot=pivot)
        assert np.all(np.isfinite(out))
        assert float(out[0]) == 0.0
        assert float(out[-1]) == 1.0
        assert float(out.min()) >= 0.0
        assert float(out.max()) <= 1.0
