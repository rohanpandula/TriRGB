"""Tests for `rgb_composite.composite`.

Real RAW decode requires rawpy + actual ARW files, which we don't ship in
the repo. Instead we patch `demosaic_linear` to return synthetic 16-bit
linear arrays — the rest of the pipeline (channel selection, stacking,
TIFF write, dimension sanity check) is purely numpy / tifffile and is
fully exercisable without hardware.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import tifffile

import rgb_composite.composite as composite_mod
from rgb_composite import composite_triplet, DEMOSAIC_KWARGS, DimensionMismatchError


# ---------- fixtures ----------

H, W = 64, 96  # tiny to keep tests fast


def make_uniform(red: int, green: int, blue: int) -> np.ndarray:
    """HxWx3 uint16 array filled with fixed per-channel values."""
    img = np.zeros((H, W, 3), dtype=np.uint16)
    img[..., 0] = red
    img[..., 1] = green
    img[..., 2] = blue
    return img


@pytest.fixture
def demosaics_stub(monkeypatch):
    """Patch demosaic_linear to return distinct, fingerprintable arrays.

    The composite logic should take channel 0 from the R-lit array, channel 1
    from the G-lit, channel 2 from the B-lit. We give each input distinct
    per-channel values so we can prove that exact selection happened.
    """
    calls: list[str] = []

    arrays = {
        # Under red light: red channel hot, others near zero (sensor crosstalk).
        "r.ARW": make_uniform(red=50000, green=200, blue=50),
        # Under green light: green channel hot, others near zero.
        "g.ARW": make_uniform(red=300, green=48000, blue=400),
        # Under blue light: blue channel hot, others near zero.
        "b.ARW": make_uniform(red=80, green=600, blue=42000),
    }

    def fake_demosaic(path):
        name = Path(path).name
        calls.append(name)
        return arrays[name].copy()

    monkeypatch.setattr(composite_mod, "demosaic_linear", fake_demosaic)
    return arrays, calls


# ---------- tests ----------

def test_composite_takes_correct_channel_from_each_input(demosaics_stub, tmp_path):
    arrays, _calls = demosaics_stub
    out = tmp_path / "frame001.tif"

    composite_triplet(
        tmp_path / "r.ARW", tmp_path / "g.ARW", tmp_path / "b.ARW", out
    )

    tiff = tifffile.imread(out)
    assert tiff.shape == (H, W, 3)
    assert tiff.dtype == np.uint16

    # The composite should be:
    #   R channel = R-lit demosaic's channel 0 (50000)
    #   G channel = G-lit demosaic's channel 1 (48000)
    #   B channel = B-lit demosaic's channel 2 (42000)
    # If endianness or channel-selection was wrong, these would show the
    # crosstalk values from the wrong input.
    assert np.all(tiff[..., 0] == 50000)
    assert np.all(tiff[..., 1] == 48000)
    assert np.all(tiff[..., 2] == 42000)


def test_composite_does_not_invert(demosaics_stub, tmp_path):
    """Critical: this stage must NOT invert. Inversion is downstream.

    If someone "fixes" the composite by subtracting from max, this test
    catches it — our R-lit input had R=50000, so the composite R must be
    50000, not 65535 - 50000 = 15535.
    """
    out = tmp_path / "frame.tif"
    composite_triplet(
        tmp_path / "r.ARW", tmp_path / "g.ARW", tmp_path / "b.ARW", out
    )
    tiff = tifffile.imread(out)
    assert tiff[0, 0, 0] == 50000  # not 15535


def test_dimension_mismatch_raises(monkeypatch, tmp_path):
    """If film moved between captures, abort with a clear error."""
    arrays = {
        "r.ARW": make_uniform(40000, 0, 0),
        "g.ARW": np.zeros((H, W + 1, 3), dtype=np.uint16),  # one column wider
        "b.ARW": make_uniform(0, 0, 30000),
    }
    monkeypatch.setattr(
        composite_mod,
        "demosaic_linear",
        lambda p: arrays[Path(p).name].copy(),
    )
    with pytest.raises(DimensionMismatchError) as exc:
        composite_triplet(
            tmp_path / "r.ARW", tmp_path / "g.ARW", tmp_path / "b.ARW",
            tmp_path / "frame.tif",
        )
    # Message should mention the shapes so the operator can see what diverged
    assert "shape mismatch" in str(exc.value).lower()


def test_demosaic_kwargs_match_project_md():
    """PROJECT.md mandates specific rawpy parameters; lock them down.

    If anyone changes these silently — say, flips use_camera_wb to True — the
    entire color pipeline downstream of this is wrong but visually plausible.
    """
    assert DEMOSAIC_KWARGS["gamma"] == (1, 1)
    assert DEMOSAIC_KWARGS["no_auto_bright"] is True
    assert DEMOSAIC_KWARGS["output_bps"] == 16
    assert DEMOSAIC_KWARGS["use_camera_wb"] is False
    # `user_wb` is a list, not a tuple — rawpy 0.27 rejects tuples here.
    assert DEMOSAIC_KWARGS["user_wb"] == [1.0, 1.0, 1.0, 1.0]
    assert isinstance(DEMOSAIC_KWARGS["user_wb"], list)
    import rawpy
    assert DEMOSAIC_KWARGS["output_color"] == rawpy.ColorSpace.ProPhoto


def test_sidecar_written_by_default(demosaics_stub, tmp_path):
    out = tmp_path / "frame.tif"
    composite_triplet(
        tmp_path / "r.ARW", tmp_path / "g.ARW", tmp_path / "b.ARW", out
    )
    sidecar = out.with_suffix(out.suffix + ".colorspace.txt")
    assert sidecar.exists()
    text = sidecar.read_text()
    assert "linear ProPhoto-RGB" in text
    assert "NOT INVERTED" in text


def test_sidecar_can_be_disabled(demosaics_stub, tmp_path):
    out = tmp_path / "frame.tif"
    composite_triplet(
        tmp_path / "r.ARW", tmp_path / "g.ARW", tmp_path / "b.ARW", out,
        write_sidecar=False,
    )
    sidecar = out.with_suffix(out.suffix + ".colorspace.txt")
    assert not sidecar.exists()
    assert out.exists()


def test_tiff_metadata_has_colorspace_tag(demosaics_stub, tmp_path):
    out = tmp_path / "frame.tif"
    composite_triplet(
        tmp_path / "r.ARW", tmp_path / "g.ARW", tmp_path / "b.ARW", out
    )
    with tifffile.TiffFile(out) as tf:
        descr = tf.pages[0].tags.get("ImageDescription")
        assert descr is not None
        assert "ProPhoto" in descr.value


def test_creates_parent_directory(demosaics_stub, tmp_path):
    nested = tmp_path / "a" / "b" / "c"
    out = nested / "frame.tif"
    assert not nested.exists()
    composite_triplet(
        tmp_path / "r.ARW", tmp_path / "g.ARW", tmp_path / "b.ARW", out
    )
    assert out.exists()


def test_cli_smoke(demosaics_stub, tmp_path, capsys):
    """End-to-end CLI exercise — argparse + composite + exit code."""
    out = tmp_path / "out.tif"
    rc = composite_mod.main(
        [
            "--r", str(tmp_path / "r.ARW"),
            "--g", str(tmp_path / "g.ARW"),
            "--b", str(tmp_path / "b.ARW"),
            "--out", str(out),
        ]
    )
    assert rc == 0
    assert out.exists()
    captured = capsys.readouterr()
    assert str(out) in captured.out


def test_cli_dimension_mismatch_exits_nonzero(monkeypatch, tmp_path, capsys):
    arrays = {
        "r.ARW": make_uniform(40000, 0, 0),
        "g.ARW": np.zeros((H + 5, W, 3), dtype=np.uint16),
        "b.ARW": make_uniform(0, 0, 30000),
    }
    monkeypatch.setattr(
        composite_mod,
        "demosaic_linear",
        lambda p: arrays[Path(p).name].copy(),
    )
    rc = composite_mod.main(
        [
            "--r", str(tmp_path / "r.ARW"),
            "--g", str(tmp_path / "g.ARW"),
            "--b", str(tmp_path / "b.ARW"),
            "--out", str(tmp_path / "out.tif"),
        ]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "shape mismatch" in err.lower()


# ---------- FFC integration ----------

def _setup_ffc_cal(tmp_path: Path, monkeypatch) -> Path:
    """Create an FFC cal dir + stub demosaic_linear for BOTH cal frames
    and per-frame triplets.

    Cal frames are uniform across the field (no vignette) so FFC maps
    end up ≈ 1.0 — meaning applying FFC is functionally a no-op and the
    composite values should equal the un-FFCed reference.
    """
    from rgb_composite import clear_ffc_cache

    cal = tmp_path / "calibration"
    cal.mkdir()
    (cal / "R.ARW").write_bytes(b"\x00")
    (cal / "G.ARW").write_bytes(b"\x00")
    (cal / "B.ARW").write_bytes(b"\x00")

    # Cal frames: uniform full-field at scanning brightness, with the
    # matching channel dominant (everything else is crosstalk).
    cal_arrays = {
        "R.ARW": make_uniform(red=45000, green=200, blue=80),
        "G.ARW": make_uniform(red=180, green=42000, blue=200),
        "B.ARW": make_uniform(red=100, green=250, blue=38000),
    }
    # Frame triplet: distinct so we can prove correct channel selection.
    frame_arrays = {
        "r.ARW": make_uniform(red=30000, green=200, blue=50),
        "g.ARW": make_uniform(red=300, green=28000, blue=400),
        "b.ARW": make_uniform(red=80, green=600, blue=22000),
    }

    def fake_demosaic(path):
        name = Path(path).name
        if name in cal_arrays:
            return cal_arrays[name].copy()
        if name in frame_arrays:
            return frame_arrays[name].copy()
        raise FileNotFoundError(f"unknown test fixture: {name}")

    monkeypatch.setattr(composite_mod, "demosaic_linear", fake_demosaic)
    clear_ffc_cache()
    return cal


def test_composite_with_ffc_uniform_cal_is_passthrough(monkeypatch, tmp_path):
    """A uniform cal frame produces unity FFC maps, so the composite
    should equal the no-FFC composite."""
    from rgb_composite import clear_ffc_cache

    cal = _setup_ffc_cal(tmp_path, monkeypatch)
    out = tmp_path / "frame.tif"

    composite_triplet(
        tmp_path / "r.ARW", tmp_path / "g.ARW", tmp_path / "b.ARW", out,
        ffc_calibration_dir=cal,
    )

    tiff = tifffile.imread(out)
    # Interior pixels — FFC smoothing has tiny edge effects but core is
    # within ~1% of the original.
    interior = tiff[10:-10, 10:-10, :]
    assert abs(int(interior[..., 0].mean()) - 30000) < 500
    assert abs(int(interior[..., 1].mean()) - 28000) < 500
    assert abs(int(interior[..., 2].mean()) - 22000) < 500
    clear_ffc_cache()


def test_composite_ffc_sidecar_mentions_calibration(monkeypatch, tmp_path):
    cal = _setup_ffc_cal(tmp_path, monkeypatch)
    out = tmp_path / "frame.tif"
    composite_triplet(
        tmp_path / "r.ARW", tmp_path / "g.ARW", tmp_path / "b.ARW", out,
        ffc_calibration_dir=cal,
    )
    sidecar = out.with_suffix(out.suffix + ".colorspace.txt").read_text()
    assert "ffc_calibration" in sidecar
    assert str(cal) in sidecar


def test_composite_ffc_shape_mismatch_raises(monkeypatch, tmp_path):
    """Cal at one size, frame at another → CalibrationError."""
    from rgb_composite import CalibrationError, clear_ffc_cache

    cal = tmp_path / "calibration"
    cal.mkdir()
    (cal / "R.ARW").write_bytes(b"\x00")
    (cal / "G.ARW").write_bytes(b"\x00")
    (cal / "B.ARW").write_bytes(b"\x00")

    # Cal at 64x96; frame at 80x96 (taller). FFC maps shape mismatch.
    cal_shape = (64, 96, 3)
    frame_shape = (80, 96, 3)

    def fake_demosaic(path):
        name = Path(path).name
        if name in {"R.ARW", "G.ARW", "B.ARW"}:
            return np.full(cal_shape, 40000, dtype=np.uint16)
        return np.full(frame_shape, 30000, dtype=np.uint16)

    monkeypatch.setattr(composite_mod, "demosaic_linear", fake_demosaic)
    clear_ffc_cache()
    try:
        with pytest.raises(CalibrationError, match="FFC shape"):
            composite_triplet(
                tmp_path / "r.ARW", tmp_path / "g.ARW", tmp_path / "b.ARW",
                tmp_path / "out.tif",
                ffc_calibration_dir=cal,
            )
    finally:
        clear_ffc_cache()


# ---------- output format ----------

def test_format_dng_writes_dng_only(demosaics_stub, tmp_path):
    out = tmp_path / "frame.tif"
    result = composite_triplet(
        tmp_path / "r.ARW", tmp_path / "g.ARW", tmp_path / "b.ARW", out,
        output_format="dng",
    )
    # Suffix swapped to .dng
    assert result == tmp_path / "frame.dng"
    assert result.exists()
    # TIFF was NOT written
    assert not out.exists()


def test_format_both_writes_both_outputs(demosaics_stub, tmp_path):
    out = tmp_path / "frame.tif"
    result = composite_triplet(
        tmp_path / "r.ARW", tmp_path / "g.ARW", tmp_path / "b.ARW", out,
        output_format="both",
    )
    # Returns the TIFF path
    assert result == out
    assert out.exists()
    assert (tmp_path / "frame.dng").exists()


def test_format_dng_pixels_match_tiff(demosaics_stub, tmp_path):
    """Same input → DNG and TIFF must contain bit-identical pixels."""
    out = tmp_path / "frame.tif"
    composite_triplet(
        tmp_path / "r.ARW", tmp_path / "g.ARW", tmp_path / "b.ARW", out,
        output_format="both",
    )
    tiff = tifffile.imread(out)
    dng = tifffile.imread(str(tmp_path / "frame.dng"))
    np.testing.assert_array_equal(tiff, dng)


def test_invalid_format_raises(demosaics_stub, tmp_path):
    with pytest.raises(ValueError, match="output_format must be"):
        composite_triplet(
            tmp_path / "r.ARW", tmp_path / "g.ARW", tmp_path / "b.ARW",
            tmp_path / "out.tif",
            output_format="jpg",
        )


def test_cli_format_dng(demosaics_stub, tmp_path, capsys):
    rc = composite_mod.main(
        [
            "--r", str(tmp_path / "r.ARW"),
            "--g", str(tmp_path / "g.ARW"),
            "--b", str(tmp_path / "b.ARW"),
            "--out", str(tmp_path / "out.tif"),
            "--format", "dng",
        ]
    )
    assert rc == 0
    assert (tmp_path / "out.dng").exists()
    captured = capsys.readouterr().out
    assert "out.dng" in captured


def test_cli_ffc_calibration_flag(monkeypatch, tmp_path, capsys):
    cal = _setup_ffc_cal(tmp_path, monkeypatch)
    rc = composite_mod.main(
        [
            "--r", str(tmp_path / "r.ARW"),
            "--g", str(tmp_path / "g.ARW"),
            "--b", str(tmp_path / "b.ARW"),
            "--out", str(tmp_path / "out.tif"),
            "--ffc-calibration", str(cal),
        ]
    )
    assert rc == 0
    sidecar = (tmp_path / "out.tif").with_suffix(".tif.colorspace.txt").read_text()
    assert "ffc_calibration" in sidecar


def test_cli_calibration_error_exits_nonzero(monkeypatch, tmp_path, capsys):
    """Missing R.ARW in cal dir → exit 1 with a clear error."""
    cal = tmp_path / "calibration"
    cal.mkdir()
    # Only G.ARW present; R and B missing.
    (cal / "G.ARW").write_bytes(b"\x00")
    # Need a working demosaic stub for the frame paths even though we
    # should bail before reaching them.
    monkeypatch.setattr(
        composite_mod,
        "demosaic_linear",
        lambda p: make_uniform(0, 0, 0),
    )
    from rgb_composite import clear_ffc_cache
    clear_ffc_cache()
    rc = composite_mod.main(
        [
            "--r", str(tmp_path / "r.ARW"),
            "--g", str(tmp_path / "g.ARW"),
            "--b", str(tmp_path / "b.ARW"),
            "--out", str(tmp_path / "out.tif"),
            "--ffc-calibration", str(cal),
        ]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "calibration" in err.lower()
    clear_ffc_cache()
