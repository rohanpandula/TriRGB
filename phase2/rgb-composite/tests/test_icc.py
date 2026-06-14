"""Tests for rgb_composite.icc and ICC embedding in the writers.

Verifies the hand-built ProPhoto ICC profiles are structurally valid, usable by
a real CMM (ImageCms), and that the pipeline writers embed the right profile:
linear ProPhoto on the archival composite, ProPhoto (2.2 display) on the
rendered positive and its preview (so on-screen preview matches the export).
"""
from __future__ import annotations

import io
import struct
from pathlib import Path

import numpy as np
import pytest
import tifffile
from PIL import Image, ImageCms

from rgb_composite.dng import _PROPHOTO_TO_XYZ_D50
from rgb_composite.icc import (
    PROPHOTO_G22_ICC,
    PROPHOTO_LINEAR_ICC,
    build_prophoto_icc,
)
from rgb_composite.triplet_positive import (
    render_triplet_positive,
    render_triplet_preview,
)

ICC_PROFILE_TAG = 34675


# --- helpers ---------------------------------------------------------------


def _tiff_icc(path) -> bytes | None:
    with tifffile.TiffFile(path) as tf:
        tag = tf.pages[0].tags.get(ICC_PROFILE_TAG)
        return bytes(tag.value) if tag is not None else None


def _png_icc(path) -> bytes | None:
    with Image.open(path) as img:
        return img.info.get("icc_profile")


def _tag_table(icc: bytes) -> dict[bytes, tuple[int, int]]:
    """Decode the ICC tag table → {signature: (offset, size)}."""
    count = struct.unpack(">I", icc[128:132])[0]
    table: dict[bytes, tuple[int, int]] = {}
    for i in range(count):
        off = 132 + i * 12
        sig = icc[off : off + 4]
        offset, size = struct.unpack(">II", icc[off + 4 : off + 12])
        table[sig] = (offset, size)
    return table


def _write_rgb_dominant(path: Path, values: tuple[int, int, int]) -> None:
    yy, xx = np.indices((80, 96), dtype=np.float32)
    texture = 0.85 + 0.3 * ((xx + yy) / float(xx.max() + yy.max()))
    img = np.zeros((80, 96, 3), dtype=np.uint16)
    for channel, value in enumerate(values):
        img[..., channel] = np.clip(
            np.rint(value * texture), 0, 65535
        ).astype(np.uint16)
    tifffile.imwrite(path, img, photometric="rgb")


def _triplet(tmp_path: Path) -> list[Path]:
    red, green, blue = (tmp_path / n for n in ("red.tif", "green.tif", "blue.tif"))
    _write_rgb_dominant(red, (52000, 4000, 900))
    _write_rgb_dominant(green, (3500, 50000, 4500))
    _write_rgb_dominant(blue, (1000, 3500, 47000))
    return [red, green, blue]


# --- profile structure -----------------------------------------------------


@pytest.mark.parametrize("icc", [PROPHOTO_LINEAR_ICC, PROPHOTO_G22_ICC])
def test_profile_header_is_rgb_xyz_display(icc):
    assert struct.unpack(">I", icc[0:4])[0] == len(icc)  # size field == actual
    assert icc[12:16] == b"mntr"  # display device class
    assert icc[16:20] == b"RGB "  # data color space
    assert icc[20:24] == b"XYZ "  # profile connection space
    assert icc[36:40] == b"acsp"  # ICC file signature


@pytest.mark.parametrize("icc", [PROPHOTO_LINEAR_ICC, PROPHOTO_G22_ICC])
def test_profile_parses_and_builds_transform(icc):
    """A real CMM can load it and build a working transform to sRGB."""
    prof = ImageCms.ImageCmsProfile(io.BytesIO(icc))
    ImageCms.buildTransform(prof, ImageCms.createProfile("sRGB"), "RGB", "RGB")


def test_colorants_match_prophoto_matrix():
    """rXYZ/gXYZ/bXYZ are the columns of the ProPhoto→XYZ(D50) matrix."""
    tags = _tag_table(PROPHOTO_LINEAR_ICC)
    for col, sig in enumerate((b"rXYZ", b"gXYZ", b"bXYZ")):
        off = tags[sig][0]
        xyz = (
            np.array(struct.unpack(">iii", PROPHOTO_LINEAR_ICC[off + 8 : off + 20]))
            / 65536.0
        )
        np.testing.assert_allclose(xyz, _PROPHOTO_TO_XYZ_D50[:, col], atol=2e-4)


def test_linear_trc_is_identity():
    tags = _tag_table(PROPHOTO_LINEAR_ICC)
    off = tags[b"rTRC"][0]
    assert PROPHOTO_LINEAR_ICC[off : off + 4] == b"curv"
    assert struct.unpack(">I", PROPHOTO_LINEAR_ICC[off + 8 : off + 12])[0] == 0


def test_display_trc_encodes_gamma_2_2():
    tags = _tag_table(PROPHOTO_G22_ICC)
    off = tags[b"rTRC"][0]
    assert struct.unpack(">I", PROPHOTO_G22_ICC[off + 8 : off + 12])[0] == 1
    gamma = struct.unpack(">H", PROPHOTO_G22_ICC[off + 12 : off + 14])[0] / 256.0
    assert gamma == pytest.approx(2.2, abs=0.01)


def test_linear_and_display_profiles_differ():
    assert PROPHOTO_LINEAR_ICC != PROPHOTO_G22_ICC


def test_build_is_reproducible():
    """No wall-clock in the profile → identical bytes for identical args."""
    raw = build_prophoto_icc.__wrapped__  # bypass lru_cache
    assert raw(2.2, "Example") == raw(2.2, "Example")


# --- embedding mechanism ---------------------------------------------------


def test_tifffile_iccprofile_roundtrips(tmp_path):
    out = tmp_path / "x.tif"
    tifffile.imwrite(
        out, np.zeros((8, 8, 3), np.uint16), iccprofile=PROPHOTO_LINEAR_ICC
    )
    assert _tiff_icc(out) == PROPHOTO_LINEAR_ICC


# --- integration: the real writers embed the right profiles ----------------


def test_render_triplet_positive_embeds_profiles(tmp_path):
    result = render_triplet_positive(
        _triplet(tmp_path), tmp_path / "out", stem="frame", patch_size=32
    )
    # Archival composite is linear; rendered positive carries a display curve.
    assert _tiff_icc(result.composite_path) == PROPHOTO_LINEAR_ICC
    assert _tiff_icc(result.positive_path) == PROPHOTO_G22_ICC


def test_exported_positive_is_color_managed(tmp_path):
    """The positive opens as a real color-managed image (not raw bytes)."""
    result = render_triplet_positive(
        _triplet(tmp_path), tmp_path / "out", stem="frame", patch_size=32
    )
    icc = _tiff_icc(result.positive_path)
    assert icc is not None
    ImageCms.ImageCmsProfile(io.BytesIO(icc))


def test_preview_carries_same_profile_as_positive(tmp_path):
    """Preview and positive share one profile → WYSIWYG."""
    result = render_triplet_preview(
        _triplet(tmp_path), tmp_path / "preview.png", max_dimension=40, patch_size=32
    )
    assert _png_icc(result.preview_path) == PROPHOTO_G22_ICC
