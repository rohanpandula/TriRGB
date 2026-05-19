"""Tests for `rgb_composite.dng` — Linear DNG output."""
from __future__ import annotations

import numpy as np
import pytest

from rgb_composite import build_dng_extratags, read_linear_dng_tags, write_linear_dng


H, W = 64, 96


def make_composite(r: int, g: int, b: int) -> np.ndarray:
    img = np.zeros((H, W, 3), dtype=np.uint16)
    img[..., 0] = r
    img[..., 1] = g
    img[..., 2] = b
    return img


def test_write_linear_dng_creates_file(tmp_path):
    out = tmp_path / "frame.dng"
    composite = make_composite(50000, 48000, 42000)
    result = write_linear_dng(out, composite)
    assert result == out
    assert out.exists()
    assert out.stat().st_size > 0


def test_dng_has_dng_version_tag(tmp_path):
    """LR identifies a DNG by the DNGVersion tag — must be present
    and start with major version 1.x.
    """
    out = tmp_path / "frame.dng"
    write_linear_dng(out, make_composite(10000, 20000, 30000))
    tags = read_linear_dng_tags(out)
    assert "DNGVersion" in tags
    # tifffile returns bytes for BYTE-typed tags
    v = tags["DNGVersion"]
    if isinstance(v, (bytes, bytearray)):
        v = tuple(v)
    elif isinstance(v, (list, tuple)) and len(v) == 4:
        v = tuple(v)
    assert v[0] == 1, f"DNGVersion major must be 1, got {v}"
    assert v[1] >= 2, f"DNGVersion minor must be >= 2, got {v}"


def test_dng_photometric_is_linear_raw(tmp_path):
    """PhotometricInterpretation must be 34892 (LinearRaw) — this is what
    tells LR to treat the file as a RAW (3-sample LinearRaw flavor)."""
    out = tmp_path / "frame.dng"
    write_linear_dng(out, make_composite(20000, 25000, 30000))
    tags = read_linear_dng_tags(out)
    assert tags["PhotometricInterpretation"] == 34892


def test_dng_white_and_black_levels(tmp_path):
    out = tmp_path / "frame.dng"
    write_linear_dng(out, make_composite(20000, 25000, 30000))
    tags = read_linear_dng_tags(out)
    # Both are per-channel
    bl = tags["BlackLevel"]
    wl = tags["WhiteLevel"]
    if hasattr(bl, "tolist"):
        bl = bl.tolist()
    if hasattr(wl, "tolist"):
        wl = wl.tolist()
    assert tuple(bl) == (0, 0, 0)
    assert tuple(wl) == (65535, 65535, 65535)


def test_dng_calibration_illuminant_is_d50(tmp_path):
    out = tmp_path / "frame.dng"
    write_linear_dng(out, make_composite(10000, 10000, 10000))
    tags = read_linear_dng_tags(out)
    assert tags["CalibrationIlluminant1"] == 23  # D50


def _decode_rationals(raw) -> list[float]:
    """tifffile returns rational tags as a flat tuple (num, den, num, den, ...).
    Pair them up and divide.
    """
    if hasattr(raw, "tolist"):
        raw = raw.tolist()
    flat = list(raw)
    if len(flat) % 2 != 0:
        raise AssertionError(f"odd-length rational sequence: {flat}")
    return [flat[i] / flat[i + 1] for i in range(0, len(flat), 2)]


def test_dng_as_shot_neutral_is_unity(tmp_path):
    """Data is already balanced — AsShotNeutral should be (1, 1, 1)."""
    out = tmp_path / "frame.dng"
    write_linear_dng(out, make_composite(30000, 30000, 30000))
    tags = read_linear_dng_tags(out)
    values = _decode_rationals(tags["AsShotNeutral"])
    assert values == pytest.approx([1.0, 1.0, 1.0])


def test_dng_color_matrix_round_trip(tmp_path):
    """ColorMatrix1 should round-trip with ~4 digits of precision
    (we encode at denom=10000). The first element of XYZ_D50→ProPhoto is
    ~1.3459."""
    out = tmp_path / "frame.dng"
    write_linear_dng(out, make_composite(10000, 10000, 10000))
    tags = read_linear_dng_tags(out)
    matrix_flat = _decode_rationals(tags["ColorMatrix1"])
    assert len(matrix_flat) == 9
    # First element: XYZ→ProPhoto[0][0] = 1.3459...
    assert matrix_flat[0] == pytest.approx(1.3459, abs=1e-3)
    # Last element: XYZ→ProPhoto[2][2] = 1.2118...
    assert matrix_flat[8] == pytest.approx(1.2118, abs=1e-3)
    # The zero entries in row 2 (XYZ_Z only depends on B for ProPhoto-D50)
    assert matrix_flat[6] == pytest.approx(0.0, abs=1e-3)
    assert matrix_flat[7] == pytest.approx(0.0, abs=1e-3)


def test_dng_roundtrip_pixel_values(tmp_path):
    """The pixel data must be preserved byte-for-byte through the write."""
    import tifffile

    out = tmp_path / "frame.dng"
    composite = make_composite(40000, 35000, 25000)
    # Make it non-uniform so we'd catch shape/order/transpose bugs
    composite[10:20, 30:50, 0] = 60000
    composite[50:60, 60:80, 1] = 55000
    write_linear_dng(out, composite)

    back = tifffile.imread(str(out))
    assert back.shape == (H, W, 3)
    assert back.dtype == np.uint16
    np.testing.assert_array_equal(back, composite)


def test_dng_creates_parent_directory(tmp_path):
    out = tmp_path / "nested" / "deep" / "frame.dng"
    write_linear_dng(out, make_composite(1, 2, 3))
    assert out.exists()


def test_dng_rejects_wrong_shape(tmp_path):
    out = tmp_path / "frame.dng"
    with pytest.raises(ValueError, match="HxWx3"):
        write_linear_dng(out, np.zeros((H, W), dtype=np.uint16))
    with pytest.raises(ValueError, match="HxWx3"):
        write_linear_dng(out, np.zeros((H, W, 4), dtype=np.uint16))


def test_dng_accepts_other_dtypes_by_casting(tmp_path):
    """Pass float — function should cast to uint16."""
    out = tmp_path / "frame.dng"
    composite = make_composite(30000, 30000, 30000).astype(np.float32)
    write_linear_dng(out, composite)
    import tifffile
    back = tifffile.imread(str(out))
    assert back.dtype == np.uint16


def test_build_dng_extratags_has_all_expected_tags():
    """Lock down the tag set — accidentally dropping ColorMatrix1 would
    silently produce DNGs that LR opens with wrong colors."""
    expected_tag_numbers = {
        254,    # NewSubFileType (added per codex review for strict DNG validators)
        274,    # Orientation
        50706,  # DNGVersion
        50707,  # DNGBackwardVersion
        50708,  # UniqueCameraModel
        50714,  # BlackLevel
        50717,  # WhiteLevel
        50721,  # ColorMatrix1
        50964,  # ForwardMatrix1
        50728,  # AsShotNeutral
        50730,  # BaselineExposure
        50778,  # CalibrationIlluminant1
        50936,  # ProfileName
        50941,  # ProfileEmbedPolicy
    }
    tags = build_dng_extratags()
    tag_numbers = {t[0] for t in tags}
    assert expected_tag_numbers <= tag_numbers, (
        f"missing DNG tags: {expected_tag_numbers - tag_numbers}"
    )


def test_dng_has_orientation_and_subfiletype(tmp_path):
    """Codex review flag: DNG validators expect Orientation and
    NewSubFileType even though TIFF defaults work in most readers."""
    import tifffile
    out = tmp_path / "frame.dng"
    write_linear_dng(out, make_composite(10000, 10000, 10000))
    with tifffile.TiffFile(str(out)) as tf:
        page = tf.pages[0]
        assert page.tags.get("Orientation") is not None
        assert int(page.tags["Orientation"].value) == 1
        assert page.tags.get("NewSubfileType") is not None
        assert int(page.tags["NewSubfileType"].value) == 0


# ---------- --camera-model flag ----------

def test_dng_default_camera_model(tmp_path):
    """Default identifies the file as a Scanlight composite."""
    out = tmp_path / "frame.dng"
    write_linear_dng(out, make_composite(10000, 10000, 10000))
    tags = read_linear_dng_tags(out)
    assert tags["UniqueCameraModel"] == "Scanlight v4 Narrowband-RGB Composite"


def test_dng_override_camera_model_for_lr_profile_picker(tmp_path):
    """Setting camera_model='Sony ILCE-7CR' is the path for Cobalt DCP
    A/B tests — LR matches camera profiles by model name, so a Sony-
    identified DNG gets the Sony profile dropdown."""
    out = tmp_path / "frame.dng"
    write_linear_dng(
        out, make_composite(10000, 10000, 10000),
        camera_model="Sony ILCE-7CR",
    )
    tags = read_linear_dng_tags(out)
    assert tags["UniqueCameraModel"] == "Sony ILCE-7CR"
