"""Integration tests: triplet-capture → rgb-composite → batch-composite.

Requirement: R-15 — end-to-end synthetic-ARW integration test suite.

These tests exercise the real `rgb_composite.composite_triplet` and the real
`batch_composite.composite_roll`, with only `demosaic_linear` monkeypatched to
return synthetic uint16 channel-dominant arrays.  This catches contract drift at
the rgb-composite ↔ batch-composite seam — the boundary that neither package's
unit tests can see because each one stubs the other.

Six tests cover:
  1. Single-frame triplet → TIFF channel-selection contract
  2. 36-frame roll → all composited, channel-by-channel verification on 3 samples
  3. FFC applied when calibration dir is provided (pixel sanity + sidecar proof)
  4. Empty roll directory → exit code 2 (batch_mod.main empty-roll guard)
  5. DNG output → required tags present (DNGVersion, PhotometricInterpretation,
     ColorMatrix1, AsShotNeutral, UniqueCameraModel)
  6. dng_camera_model override → UniqueCameraModel round-trips correctly

Critical invariant: every `composite_roll` call passes `workers=1` so the
inline code path is used — process-pool workers run in separate processes and
cannot see the monkeypatched `demosaic_linear` from the parent.
See phase2/batch-composite/batch_composite/batch.py:254-264 for the inline path.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import tifffile

from rgb_composite import composite_triplet, read_linear_dng_tags
from batch_composite import composite_roll
import batch_composite.batch as batch_mod

from tests.integration.conftest import H, W, DOMINANT, CROSSTALK


# ---------------------------------------------------------------------------
# Module-level helpers (not fixtures — used by multiple tests)
# ---------------------------------------------------------------------------

def _decode_rationals_flat(raw) -> list[float]:
    """Convert a flat (num, den, num, den, ...) sequence to floats.

    tifffile returns RATIONAL/SRATIONAL tags as a flat sequence of
    alternating numerators and denominators.  Adapted from
    phase2/rgb-composite/tests/test_dng.py::_decode_rationals.
    """
    if hasattr(raw, "tolist"):
        raw = raw.tolist()
    flat = list(raw)
    if len(flat) % 2 != 0:
        raise AssertionError(f"odd-length rational sequence: {flat}")
    return [flat[i] / flat[i + 1] for i in range(0, len(flat), 2)]


def _to_str(v) -> str:
    """Defensively decode a DNG tag value that may be bytes or str."""
    if isinstance(v, bytes):
        return v.rstrip(b"\x00").decode()
    return str(v)


# ---------------------------------------------------------------------------
# Test 1: single-frame triplet → TIFF channel-selection contract
# ---------------------------------------------------------------------------

def test_single_frame_triplet_to_composite_tiff(
    synthetic_demosaic, roll_directory_factory, tmp_path
):
    """Channel 0 of composite = R-lit channel 0, Ch 1 = G-lit ch 1, Ch 2 = B-lit ch 2."""
    roll_dir = roll_directory_factory(roll_name="RollIntTest", n_frames=1)
    r_path = roll_dir / "RollIntTest_Frame001_R.ARW"
    g_path = roll_dir / "RollIntTest_Frame001_G.ARW"
    b_path = roll_dir / "RollIntTest_Frame001_B.ARW"

    out = tmp_path / "single.tif"
    composite_triplet(r_path, g_path, b_path, out)

    tiff = tifffile.imread(out)
    assert tiff.shape == (H, W, 3), f"expected shape ({H}, {W}, 3), got {tiff.shape}"
    assert tiff.dtype == np.uint16, f"expected uint16, got {tiff.dtype}"

    # Channel-selection contract: R-lit's dominant channel 0 → composite channel 0,
    # G-lit's dominant channel 1 → composite channel 1,
    # B-lit's dominant channel 2 → composite channel 2.
    assert tiff[0, 0, 0] == DOMINANT, (
        f"R channel got {tiff[0,0,0]} not {DOMINANT}; channel selection drift?"
    )
    assert tiff[0, 0, 1] == DOMINANT, (
        f"G channel got {tiff[0,0,1]} not {DOMINANT}; channel selection drift?"
    )
    assert tiff[0, 0, 2] == DOMINANT, (
        f"B channel got {tiff[0,0,2]} not {DOMINANT}; channel selection drift?"
    )
    assert np.all(tiff[..., 0] == DOMINANT)
    assert np.all(tiff[..., 1] == DOMINANT)
    assert np.all(tiff[..., 2] == DOMINANT)

    # Stub was called exactly 3 times (once per channel)
    assert len(synthetic_demosaic) == 3, (
        f"expected 3 demosaic calls, got {len(synthetic_demosaic)}"
    )


# ---------------------------------------------------------------------------
# Test 2: 36-frame roll — all composited, channel-selection spot-checked
# ---------------------------------------------------------------------------

def test_full_roll_36_frames(synthetic_demosaic, roll_directory_factory):
    """36 complete triplets → 36 composites; channel-selection contract on frames 1, 18, 36."""
    roll_dir = roll_directory_factory(roll_name="RollIntTest", n_frames=36)

    result = composite_roll(roll_dir, workers=1)

    assert len(result.composited) == 36, (
        f"expected 36 composited frames, got {len(result.composited)}"
    )
    assert len(result.skipped) == 0, f"unexpected skipped: {result.skipped}"
    assert len(result.failed) == 0, f"unexpected failures: {result.failed}"

    # All 36 composite files must exist with the right shape.
    composites_dir = roll_dir / "composites"
    for n in range(1, 37):
        tif_path = composites_dir / f"RollIntTest_Frame{n:03d}.tif"
        assert tif_path.exists(), f"missing composite: {tif_path.name}"
        tiff = tifffile.imread(tif_path)
        assert tiff.shape == (H, W, 3), (
            f"Frame{n:03d}: expected shape ({H},{W},3), got {tiff.shape}"
        )

    # Full channel-selection assertion on three spread-out sample frames.
    for sample_frame in (1, 18, 36):
        tiff = tifffile.imread(
            composites_dir / f"RollIntTest_Frame{sample_frame:03d}.tif"
        )
        assert tiff[0, 0, 0] == DOMINANT, (
            f"Frame{sample_frame:03d}: R channel got {tiff[0,0,0]} not {DOMINANT}"
        )
        assert tiff[0, 0, 1] == DOMINANT, (
            f"Frame{sample_frame:03d}: G channel got {tiff[0,0,1]} not {DOMINANT}"
        )
        assert tiff[0, 0, 2] == DOMINANT, (
            f"Frame{sample_frame:03d}: B channel got {tiff[0,0,2]} not {DOMINANT}"
        )

    # Every frame triggered exactly 3 demosaic calls.
    assert len(synthetic_demosaic) == 36 * 3, (
        f"expected {36 * 3} demosaic calls, got {len(synthetic_demosaic)}"
    )


# ---------------------------------------------------------------------------
# Test 3: FFC applied when calibration provided (pixel sanity + sidecar proof)
# ---------------------------------------------------------------------------

def test_ffc_applied_when_calibration_provided(
    synthetic_demosaic, roll_directory_factory, cal_triplet_factory
):
    """Uniform cal → near-identity FFC; sidecar mentions ffc_calibration + cal path."""
    roll_dir = roll_directory_factory(roll_name="RollIntTest", n_frames=2)
    cal = cal_triplet_factory()

    result = composite_roll(roll_dir, workers=1, ffc_calibration_dir=cal)

    assert len(result.composited) == 2, (
        f"expected 2 composited frames, got {len(result.composited)}"
    )
    assert len(result.failed) == 0, f"unexpected failures: {result.failed}"

    # Pixel sanity: uniform cal → unity FFC maps → interior pixel mean ≈ DOMINANT.
    # Take the interior to avoid FFC smoothing edge effects.
    tif_path = roll_dir / "composites" / "RollIntTest_Frame001.tif"
    tiff = tifffile.imread(tif_path)
    interior = tiff[10:-10, 10:-10, :]
    assert abs(int(interior[..., 0].mean()) - DOMINANT) < 500, (
        f"R channel interior mean {interior[...,0].mean():.0f} deviates from DOMINANT {DOMINANT}"
    )
    assert abs(int(interior[..., 1].mean()) - DOMINANT) < 500, (
        f"G channel interior mean {interior[...,1].mean():.0f} deviates from DOMINANT {DOMINANT}"
    )
    assert abs(int(interior[..., 2].mean()) - DOMINANT) < 500, (
        f"B channel interior mean {interior[...,2].mean():.0f} deviates from DOMINANT {DOMINANT}"
    )

    # Sidecar proof: the FFC code path ran (not just a lucky pixel coincidence).
    sidecar_path = tif_path.with_suffix(tif_path.suffix + ".colorspace.txt")
    assert sidecar_path.exists(), f"sidecar missing: {sidecar_path}"
    sidecar_text = sidecar_path.read_text()
    assert "ffc_calibration" in sidecar_text, (
        "sidecar does not contain 'ffc_calibration' — FFC code path may not have run"
    )
    assert str(cal) in sidecar_text, (
        f"sidecar does not reference cal dir {cal}"
    )


# ---------------------------------------------------------------------------
# Test 4: empty roll directory → exit code 2
# ---------------------------------------------------------------------------

def test_empty_roll_dir_returns_nonzero(tmp_path, capsys):
    """batch_mod.main on a directory with no matching ARWs returns exit 2."""
    empty = tmp_path / "no_arws"
    empty.mkdir()
    # Add an irrelevant file to prove the regex filter works (not just empty dir).
    (empty / "stray.txt").write_text("not an ARW")

    rc = batch_mod.main([str(empty), "--workers", "1"])

    assert rc == 2, f"expected exit 2 (empty-roll guard), got {rc}"
    stderr = capsys.readouterr().err
    assert "no frames matching" in stderr, (
        f"expected 'no frames matching' in stderr, got: {stderr!r}"
    )


# ---------------------------------------------------------------------------
# Test 5: DNG output → required tags present
# ---------------------------------------------------------------------------

def test_dng_output_has_required_tags(synthetic_demosaic, roll_directory_factory):
    """composite_roll with output_format='dng' produces a Linear DNG with required tags."""
    roll_dir = roll_directory_factory(n_frames=1)

    result = composite_roll(roll_dir, workers=1, output_format="dng")

    assert len(result.composited) == 1, (
        f"expected 1 composited DNG, got {len(result.composited)}"
    )
    dng_path = result.composited[0]
    assert dng_path.suffix == ".dng", f"expected .dng suffix, got {dng_path.suffix}"
    assert dng_path.exists(), f"DNG file missing: {dng_path}"

    tags = read_linear_dng_tags(dng_path)

    # DNGVersion: major byte must be 1, minor >= 2
    assert "DNGVersion" in tags, "DNGVersion tag missing from DNG output"
    v = tags["DNGVersion"]
    if isinstance(v, (bytes, bytearray)):
        v = tuple(v)
    elif hasattr(v, "tolist"):
        v = tuple(v.tolist())
    else:
        v = tuple(v)
    assert v[0] == 1, f"DNGVersion major must be 1, got {v}"
    assert v[1] >= 2, f"DNGVersion minor must be >= 2, got {v}"

    # PhotometricInterpretation must be 34892 (LinearRaw)
    assert tags.get("PhotometricInterpretation") == 34892, (
        f"PhotometricInterpretation expected 34892, got {tags.get('PhotometricInterpretation')}"
    )

    # ColorMatrix1 must exist and have length 18 (9 SRATIONAL pairs flattened)
    assert "ColorMatrix1" in tags, "ColorMatrix1 tag missing"
    cm1 = tags["ColorMatrix1"]
    if hasattr(cm1, "tolist"):
        cm1 = cm1.tolist()
    assert len(list(cm1)) == 18, (
        f"ColorMatrix1 expected 18 elements (9 SRATIONAL pairs), got {len(list(cm1))}"
    )

    # AsShotNeutral: should decode to approximately [1.0, 1.0, 1.0]
    assert "AsShotNeutral" in tags, "AsShotNeutral tag missing"
    asn_values = _decode_rationals_flat(tags["AsShotNeutral"])
    assert asn_values == pytest.approx([1.0, 1.0, 1.0]), (
        f"AsShotNeutral expected [1.0, 1.0, 1.0], got {asn_values}"
    )

    # UniqueCameraModel: default is the Scanlight identifier
    assert "UniqueCameraModel" in tags, "UniqueCameraModel tag missing"
    model = _to_str(tags["UniqueCameraModel"])
    assert model == "Scanlight v4 Narrowband-RGB Composite", (
        f"UniqueCameraModel expected 'Scanlight v4 Narrowband-RGB Composite', got {model!r}"
    )


# ---------------------------------------------------------------------------
# Test 6: camera-model override
# ---------------------------------------------------------------------------

def test_camera_model_override_for_cobalt(synthetic_demosaic, roll_directory_factory):
    """dng_camera_model='Sony ILCE-7CR' flows through to DNG UniqueCameraModel tag."""
    roll_dir = roll_directory_factory(n_frames=1)

    result = composite_roll(
        roll_dir,
        workers=1,
        output_format="dng",
        dng_camera_model="Sony ILCE-7CR",
    )

    assert len(result.composited) == 1
    dng_path = result.composited[0]
    assert dng_path.exists()

    tags = read_linear_dng_tags(dng_path)
    assert "UniqueCameraModel" in tags, "UniqueCameraModel tag missing"
    model = _to_str(tags["UniqueCameraModel"])
    assert model == "Sony ILCE-7CR", (
        f"UniqueCameraModel expected 'Sony ILCE-7CR', got {model!r}; "
        "dng_camera_model passthrough between batch_composite and rgb_composite broke?"
    )
