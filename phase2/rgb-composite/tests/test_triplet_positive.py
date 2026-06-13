from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image
import tifffile

import rgb_composite.triplet_positive as triplet_positive
from rgb_composite.triplet_positive import (
    RAW_SUFFIXES,
    TripletPositiveError,
    apply_render_look,
    detect_rgb_assignment,
    load_linear_rgb,
    render_triplet_preview,
    render_triplet_positive,
)


def _write_rgb_dominant(path: Path, values: tuple[int, int, int]) -> None:
    yy, xx = np.indices((80, 96), dtype=np.float32)
    texture = 0.85 + 0.3 * ((xx + yy) / float(xx.max() + yy.max()))
    img = np.zeros((80, 96, 3), dtype=np.uint16)
    for channel, value in enumerate(values):
        img[..., channel] = np.clip(np.rint(value * texture), 0, 65535).astype(np.uint16)
    tifffile.imwrite(path, img, photometric="rgb")


def test_load_linear_rgb_accepts_raf_raw_files(monkeypatch, tmp_path):
    raf = tmp_path / "DSCF0001.RAF"
    expected = np.zeros((4, 5, 3), dtype=np.uint16)
    calls: list[Path] = []

    def fake_demosaic(path: Path) -> np.ndarray:
        calls.append(path)
        return expected

    monkeypatch.setattr(triplet_positive, "demosaic_linear", fake_demosaic)

    assert ".raf" in RAW_SUFFIXES
    assert load_linear_rgb(raf) is expected
    assert calls == [raf]


def test_detect_rgb_assignment_orders_by_channel_energy_regardless_of_input_order(tmp_path):
    # Generic camera names; assignment is by per-channel signal energy, not names.
    red = tmp_path / "first.tif"
    green = tmp_path / "second.tif"
    blue = tmp_path / "third.tif"
    _write_rgb_dominant(red, (52000, 4000, 900))
    _write_rgb_dominant(green, (3500, 50000, 4500))
    _write_rgb_dominant(blue, (1000, 3500, 47000))

    # Input order shuffled; energy still sorts them to R, G, B.
    assignments = detect_rgb_assignment([green, blue, red])

    assert [item.channel for item in assignments] == ["R", "G", "B"]
    assert Path(assignments[0].path).name == "first.tif"
    assert Path(assignments[1].path).name == "second.tif"
    assert Path(assignments[2].path).name == "third.tif"
    # Energy-based confidence reflects the dominance ratio, not a constant.
    assert all(item.confidence > 1.5 for item in assignments)


def test_detect_rgb_assignment_ignores_filenames_and_uses_channel_energy(tmp_path):
    # File literally named "..._R" but its pixels are BLUE-dominant: energy wins,
    # the name is ignored. This is the whole point of the colorblind-safe rule.
    misnamed = tmp_path / "Roll001_Frame001_R.tif"
    other_a = tmp_path / "Roll001_Frame002.tif"
    other_b = tmp_path / "Roll001_Frame003.tif"
    _write_rgb_dominant(misnamed, (900, 3500, 47000))   # blue-dominant
    _write_rgb_dominant(other_a, (52000, 4000, 900))    # red-dominant
    _write_rgb_dominant(other_b, (3500, 50000, 4500))   # green-dominant

    assignments = detect_rgb_assignment([misnamed, other_a, other_b])
    by_name = {Path(a.path).name: a.channel for a in assignments}

    assert by_name["Roll001_Frame001_R.tif"] == "B"  # named R, but energy says B


def test_detect_rgb_assignment_auto_detects_unlabeled_files_from_channel_data(tmp_path):
    red = tmp_path / "DSC00448.tif"
    green = tmp_path / "DSC00449.tif"
    blue = tmp_path / "DSC00450.tif"
    _write_rgb_dominant(red, (52000, 4000, 900))
    _write_rgb_dominant(green, (3500, 50000, 4500))
    _write_rgb_dominant(blue, (1000, 3500, 47000))

    assignments = detect_rgb_assignment([green, blue, red])

    assert [item.channel for item in assignments] == ["R", "G", "B"]
    assert Path(assignments[0].path).name == "DSC00448.tif"
    assert Path(assignments[1].path).name == "DSC00449.tif"
    assert Path(assignments[2].path).name == "DSC00450.tif"
    assert all(item.confidence >= 1.5 for item in assignments)


def test_detect_rgb_assignment_rejects_ambiguous_unlabeled_files(tmp_path):
    files = []
    for name in ("frame001.tif", "frame002.tif", "frame003.tif"):
        path = tmp_path / name
        _write_rgb_dominant(path, (1000, 1000, 1000))
        files.append(path)

    with pytest.raises(TripletPositiveError, match="no clear single-channel dominance"):
        detect_rgb_assignment(files)


def test_detect_rgb_assignment_rejects_two_frames_dominant_in_same_channel(tmp_path):
    a = tmp_path / "a.tif"
    b = tmp_path / "b.tif"
    c = tmp_path / "c.tif"
    _write_rgb_dominant(a, (52000, 4000, 900))   # red-dominant
    _write_rgb_dominant(b, (50000, 3500, 1200))  # also red-dominant (conflict)
    _write_rgb_dominant(c, (1000, 3500, 47000))  # blue-dominant

    with pytest.raises(TripletPositiveError, match="strongest in channel R"):
        detect_rgb_assignment([a, b, c])


def test_detect_rgb_assignment_rejects_low_signal_frame(tmp_path):
    # Clear dominance RATIO (R >> G,B) but tiny absolute signal: the signal gate
    # (AUTO_ASSIGN_MIN_SIGNAL) must reject it rather than trust a near-dark frame.
    dim = tmp_path / "dim.tif"
    g = tmp_path / "g.tif"
    b = tmp_path / "b.tif"
    _write_rgb_dominant(dim, (200, 20, 15))      # R-dominant but far too dim
    _write_rgb_dominant(g, (3500, 50000, 4500))
    _write_rgb_dominant(b, (1000, 3500, 47000))

    with pytest.raises(TripletPositiveError, match="no clear single-channel dominance"):
        detect_rgb_assignment([dim, g, b])


def test_detect_rgb_assignment_is_deterministic(tmp_path):
    # NFR-11: same input -> same output, no RNG, no by-eye color decision.
    red = tmp_path / "1.tif"
    green = tmp_path / "2.tif"
    blue = tmp_path / "3.tif"
    _write_rgb_dominant(red, (52000, 4000, 900))
    _write_rgb_dominant(green, (3500, 50000, 4500))
    _write_rgb_dominant(blue, (1000, 3500, 47000))

    first = detect_rgb_assignment([blue, red, green])
    second = detect_rgb_assignment([blue, red, green])

    assert [(a.channel, a.path) for a in first] == [(a.channel, a.path) for a in second]


def test_render_triplet_positive_writes_composite_positive_and_report(tmp_path):
    red = tmp_path / "red.tif"
    green = tmp_path / "green.tif"
    blue = tmp_path / "blue.tif"
    _write_rgb_dominant(red, (52000, 4000, 900))
    _write_rgb_dominant(green, (3500, 50000, 4500))
    _write_rgb_dominant(blue, (1000, 3500, 47000))

    result = render_triplet_positive(
        [blue, red, green],
        tmp_path / "out",
        stem="frame001",
        patch_size=32,
    )

    composite = tifffile.imread(result.composite_path)
    positive = tifffile.imread(result.positive_path)

    assert composite.shape == (80, 96, 3)
    assert positive.shape == (80, 96, 3)
    assert composite.dtype == np.uint16
    assert positive.dtype == np.uint16
    # F8: verify zlib+predictor compression (matches archival composite writer).
    # tifffile reports zlib compression as ADOBE_DEFLATE (compression id 8).
    with tifffile.TiffFile(result.positive_path) as positive_tiff:
        assert positive_tiff.pages[0].compression.name == "ADOBE_DEFLATE"
    with tifffile.TiffFile(result.composite_path) as composite_tiff:
        assert composite_tiff.pages[0].compression.name == "ADOBE_DEFLATE"
    assert Path(result.report_path).exists()
    # Crosstalk channels are discarded: each composite channel is exactly the
    # same-index channel of the matching LED exposure (selected by energy).
    assert np.array_equal(composite[..., 0], tifffile.imread(red)[..., 0])
    assert np.array_equal(composite[..., 1], tifffile.imread(green)[..., 1])
    assert np.array_equal(composite[..., 2], tifffile.imread(blue)[..., 2])


def test_render_triplet_positive_accepts_manual_base_region(tmp_path):
    red = tmp_path / "red.tif"
    green = tmp_path / "green.tif"
    blue = tmp_path / "blue.tif"
    _write_rgb_dominant(red, (52000, 4000, 900))
    _write_rgb_dominant(green, (3500, 50000, 4500))
    _write_rgb_dominant(blue, (1000, 3500, 47000))

    result = render_triplet_positive(
        [red, green, blue],
        tmp_path / "out",
        stem="manual-base",
        base_region=(0, 0, 32, 40),
        look="flat",
    )

    assert result.base_region["source"] == "manual"
    assert result.base_region["x"] == 0
    assert result.base_region["y"] == 0
    assert result.base_region["w"] == 32
    assert result.base_region["h"] == 40
    assert result.positive_meta["look"] == "flat"


def test_render_triplet_positive_accepts_filmic_look(tmp_path):
    red = tmp_path / "red.tif"
    green = tmp_path / "green.tif"
    blue = tmp_path / "blue.tif"
    _write_rgb_dominant(red, (18000, 1500, 900))
    _write_rgb_dominant(green, (1200, 17000, 1500))
    _write_rgb_dominant(blue, (900, 1500, 16000))

    result = render_triplet_positive(
        [red, green, blue],
        tmp_path / "out",
        stem="filmic",
        look="filmic",
    )

    positive = tifffile.imread(result.positive_path)

    assert positive.shape == (80, 96, 3)
    assert positive.dtype == np.uint16
    assert result.positive_meta["look"] == "filmic"
    # Polarity: the dim (low-texture) top-left corner of the negative renders
    # brighter than the bright (high-texture) bottom-right corner of the positive.
    assert float(positive[:20, :20, :].mean()) > float(positive[-20:, -20:, :].mean())


def test_render_triplet_positive_whole_frame_base_mode_records_source(tmp_path):
    red = tmp_path / "red.tif"
    green = tmp_path / "green.tif"
    blue = tmp_path / "blue.tif"
    _write_rgb_dominant(red, (18000, 1500, 900))
    _write_rgb_dominant(green, (1200, 17000, 1500))
    _write_rgb_dominant(blue, (900, 1500, 16000))

    result = render_triplet_positive(
        [red, green, blue],
        tmp_path / "out",
        stem="wholeframe",
        look="standard",
        base_mode="whole_frame",
    )

    assert result.positive_meta["base_mode"] == "whole_frame"
    assert result.positive_meta["base_source"] == "whole_frame"


def test_render_triplet_positive_manual_base_region_honored_under_auto(tmp_path):
    """A manual base box is trusted (patch) even under the default 'auto' mode."""
    red = tmp_path / "red.tif"
    green = tmp_path / "green.tif"
    blue = tmp_path / "blue.tif"
    _write_rgb_dominant(red, (18000, 1500, 900))
    _write_rgb_dominant(green, (1200, 17000, 1500))
    _write_rgb_dominant(blue, (900, 1500, 16000))

    result = render_triplet_positive(
        [red, green, blue],
        tmp_path / "out",
        stem="manual-auto",
        base_region=(0, 0, 16, 80),  # an operator-drawn base box
        look="standard",  # base_mode defaults to "auto"
    )

    assert result.base_region["source"] == "manual"
    assert result.positive_meta["base_source"] == "patch"


def test_render_look_adds_midtones_without_moving_endpoints():
    gradient = np.linspace(0, 65535, 17, dtype=np.uint16)
    img = np.stack([gradient, gradient, gradient], axis=-1).reshape(1, 17, 3)

    curved = apply_render_look(img, curve_amount=1.0)

    assert int(curved[0, 0, 0]) == 0
    assert int(curved[0, -1, 0]) == 65535
    assert int(curved[0, 4, 0]) < int(img[0, 4, 0])
    assert int(curved[0, 12, 0]) > int(img[0, 12, 0])


def test_render_triplet_preview_writes_scaled_png(tmp_path):
    red = tmp_path / "red.tif"
    green = tmp_path / "green.tif"
    blue = tmp_path / "blue.tif"
    _write_rgb_dominant(red, (52000, 4000, 900))
    _write_rgb_dominant(green, (3500, 50000, 4500))
    _write_rgb_dominant(blue, (1000, 3500, 47000))

    result = render_triplet_preview(
        [red, green, blue],
        tmp_path / "preview.png",
        max_dimension=40,
        patch_size=32,
    )

    with Image.open(result.preview_path) as image:
        assert image.mode == "RGB"
        preview = np.asarray(image)
    assert preview.dtype == np.uint8
    assert preview.shape[2] == 3
    assert max(preview.shape[:2]) <= 40
    assert result.full_width == 96
    assert result.full_height == 80
    assert result.preview_width == preview.shape[1]
    assert result.preview_height == preview.shape[0]


def test_render_triplet_positive_loads_each_file_exactly_once(monkeypatch, tmp_path):
    """F4: each input file is demosaiced exactly once through render_triplet_positive.

    The scoring pass (detect_rgb_assignment) and the composite-build pass
    (build_composite) share a cache keyed by resolved path, so the 6-demosaic
    old behaviour is cut to 3.
    """
    red = tmp_path / "red.tif"
    green = tmp_path / "green.tif"
    blue = tmp_path / "blue.tif"
    _write_rgb_dominant(red, (52000, 4000, 900))
    _write_rgb_dominant(green, (3500, 50000, 4500))
    _write_rgb_dominant(blue, (1000, 3500, 47000))

    load_calls: list[str] = []
    original_load = triplet_positive.load_linear_rgb

    def counting_load(path: Path) -> np.ndarray:
        load_calls.append(str(path))
        return original_load(path)

    monkeypatch.setattr(triplet_positive, "load_linear_rgb", counting_load)

    render_triplet_positive(
        [red, green, blue],
        tmp_path / "out",
        stem="loadcount",
    )

    # Each of the three files must have been loaded exactly once.
    assert len(load_calls) == 3, (
        f"expected 3 load_linear_rgb calls (one per file), got {len(load_calls)}: "
        + ", ".join(load_calls)
    )
    assert sorted(load_calls) == sorted(str(p.resolve()) for p in [red, green, blue]), (
        "loaded paths don't match the input paths"
    )
