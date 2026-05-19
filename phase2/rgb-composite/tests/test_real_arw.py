"""Integration test against a real ARW file.

Closes the gap left by the unit tests that mock `demosaic_linear`: this
file actually runs rawpy/libraw on a real Sony ARW and verifies the
output is what PROJECT.md expects (16-bit linear ProPhoto-RGB, sensible
value range, the channel dimensions we'll feed into the compositor).

The test is opt-in. It runs if EITHER:
  - the env var `RGB_COMPOSITE_TEST_ARW` is set to a readable ARW path, OR
  - the canonical default fixture path exists:
      /Volumes/FilmscanWorkingDrive/2025Film To be Organized/Roll2/Negs/Roll2-070.ARW

Otherwise the test skips with a clear message. This keeps the test
suite green on CI/clean checkouts while still catching real bugs
locally (the user_wb tuple→list bug was found this way).
"""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest
import tifffile

from rgb_composite import composite_triplet, demosaic_linear


# Default fixture location — the operator's working drive. Override via
# the env var if you're testing elsewhere.
DEFAULT_FIXTURE = Path(
    "/Volumes/FilmscanWorkingDrive/2025Film To be Organized/Roll2/Negs/Roll2-070.ARW"
)


def _fixture_path() -> Path | None:
    env = os.environ.get("RGB_COMPOSITE_TEST_ARW")
    if env:
        p = Path(env)
        return p if p.is_file() else None
    return DEFAULT_FIXTURE if DEFAULT_FIXTURE.is_file() else None


pytestmark = pytest.mark.skipif(
    _fixture_path() is None,
    reason=(
        "No real ARW available. Set RGB_COMPOSITE_TEST_ARW=/path/to/sample.ARW "
        f"or drop a file at {DEFAULT_FIXTURE}"
    ),
)


@pytest.fixture(scope="module")
def real_arw() -> Path:
    p = _fixture_path()
    assert p is not None  # pytestmark already ensured this
    return p


@pytest.fixture(scope="module")
def demosaic_result(real_arw):
    """Cache the demosaic across tests — it's slow (~1 sec per call)."""
    return demosaic_linear(real_arw)


def test_demosaic_returns_3_channel_uint16(demosaic_result):
    """The most basic correctness invariant from PROJECT.md.

    `output_bps=16` + `output_color=ProPhoto` means we get a uint16
    HxWx3 array. If rawpy ever decides to give us float or 8-bit or
    HxWx4 we want to know immediately.
    """
    img = demosaic_result
    assert img.dtype == np.uint16, f"expected uint16, got {img.dtype}"
    assert img.ndim == 3, f"expected 3D array, got {img.ndim}D"
    assert img.shape[2] == 3, f"expected 3 channels, got {img.shape[2]}"


def test_demosaic_dimensions_match_a7cr(demosaic_result):
    """The a7CR's full-frame sensor is 9504x6336 active pixels (after the
    libraw crop). The raw image is 9600x6376 but `postprocess` trims
    margins. Any 60+ MP body should land in that ballpark.

    This protects against accidentally enabling cropping/downscaling
    in DEMOSAIC_KWARGS (e.g., `half_size=True`).
    """
    h, w, _ = demosaic_result.shape
    assert h > 6000 and h < 6400, f"unexpected height {h}"
    assert w > 9000 and w < 9700, f"unexpected width {w}"


def test_demosaic_values_are_linear_not_gamma_corrected(demosaic_result):
    """`gamma=(1,1)` + `no_auto_bright=True` produces a linear array
    that should occupy a relatively small fraction of the 0–65535 range
    (since linear has no gamma stretch). A gamma-corrected output
    would push the mean toward 32768.

    For a film negative scan: the mean is usually well under 20% of max
    because the film base + dye absorption keep the linear values low.
    """
    img = demosaic_result.astype(np.float64)
    overall_mean = img.mean()
    assert overall_mean < 65535 * 0.4, (
        f"mean {overall_mean:.0f} is too high for linear output — "
        "did gamma correction sneak in?"
    )


def test_demosaic_not_all_zero_not_all_max(demosaic_result):
    """Sanity guard against a totally broken decode."""
    img = demosaic_result
    assert img.max() > 1000, "decode looks empty"
    assert img.min() < 60000, "decode looks saturated"


def test_composite_pipeline_end_to_end(real_arw, tmp_path):
    """Run the full `composite_triplet` against three copies of the
    same ARW. The result is colorimetrically meaningless (it's not three
    different narrowband exposures), but it exercises:

      - rawpy decode against a real Bayer file × 3
      - dimension-mismatch check (passes because all three are the same file)
      - channel-from-correct-input stacking
      - tifffile write with ProPhoto colorspace tag
      - the sidecar writer

    Caught one bug already (user_wb tuple→list). Cheap, runs in ~3 sec.
    """
    out = tmp_path / "frame.tif"
    composite_triplet(real_arw, real_arw, real_arw, out)

    assert out.exists()
    sidecar = out.with_suffix(out.suffix + ".colorspace.txt")
    assert sidecar.exists()

    # Round-trip read
    tiff = tifffile.imread(out)
    assert tiff.dtype == np.uint16
    assert tiff.ndim == 3 and tiff.shape[2] == 3

    # When all three inputs are the same file, the composite's R channel
    # equals the source's R channel, G equals G, B equals B.
    src = demosaic_linear(real_arw)
    np.testing.assert_array_equal(tiff[..., 0], src[..., 0])
    np.testing.assert_array_equal(tiff[..., 1], src[..., 1])
    np.testing.assert_array_equal(tiff[..., 2], src[..., 2])
