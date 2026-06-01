from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image
import tifffile

from rgb_composite.triplet_positive import (
    TripletPositiveError,
    apply_render_look,
    detect_rgb_assignment,
    render_triplet_preview,
    render_triplet_positive,
)


def _write_rgb(path: Path, values: tuple[int, int, int]) -> None:
    img = np.zeros((80, 96, 3), dtype=np.uint16)
    img[..., 0] = values[0]
    img[..., 1] = values[1]
    img[..., 2] = values[2]
    # Give the auto base finder a bright, low-texture strip on the edge.
    img[:, :16, 0] = max(values[0], 32000)
    img[:, :16, 1] = max(values[1], 32000)
    img[:, :16, 2] = max(values[2], 32000)
    tifffile.imwrite(path, img, photometric="rgb")


def test_detect_rgb_assignment_accepts_any_file_order(tmp_path):
    green = tmp_path / "a-green.tif"
    blue = tmp_path / "b-blue.tif"
    red = tmp_path / "c-red.tif"
    _write_rgb(green, (700, 50000, 1200))
    _write_rgb(blue, (200, 1800, 47000))
    _write_rgb(red, (52000, 1000, 800))

    assignments = detect_rgb_assignment([green, blue, red])

    # Assignment is by the filename token (red/green/blue), not image color, and
    # is independent of input order.
    assert [item.channel for item in assignments] == ["R", "G", "B"]
    assert Path(assignments[0].path).name == "c-red.tif"
    assert Path(assignments[1].path).name == "a-green.tif"
    assert Path(assignments[2].path).name == "b-blue.tif"
    # Deterministic filename role → constant confidence (no measured color ratio).
    assert all(item.confidence == 1.0 for item in assignments)


def test_detect_rgb_assignment_accepts_letter_suffixes(tmp_path):
    r = tmp_path / "Roll001_Frame001_R.tif"
    g = tmp_path / "Roll001_Frame001_G.tif"
    b = tmp_path / "Roll001_Frame001_B.tif"
    for path, values in ((r, (50000, 0, 0)), (g, (0, 50000, 0)), (b, (0, 0, 50000))):
        _write_rgb(path, values)

    assignments = detect_rgb_assignment([b, r, g])

    assert [item.channel for item in assignments] == ["R", "G", "B"]
    assert Path(assignments[0].path).name == "Roll001_Frame001_R.tif"


def test_detect_rgb_assignment_rejects_unlabeled_filenames(tmp_path):
    files = []
    for name in ("frame001.tif", "frame002.tif", "frame003.tif"):
        path = tmp_path / name
        _write_rgb(path, (1000, 1000, 1000))
        files.append(path)

    with pytest.raises(TripletPositiveError):
        detect_rgb_assignment(files)


def test_detect_rgb_assignment_rejects_duplicate_channel(tmp_path):
    files = []
    for name in ("scan_R.tif", "other_R.tif", "scan_G.tif"):
        path = tmp_path / name
        _write_rgb(path, (1000, 1000, 1000))
        files.append(path)

    with pytest.raises(TripletPositiveError):
        detect_rgb_assignment(files)


def test_render_triplet_positive_writes_composite_positive_and_report(tmp_path):
    red = tmp_path / "red.tif"
    green = tmp_path / "green.tif"
    blue = tmp_path / "blue.tif"
    _write_rgb(red, (52000, 1000, 800))
    _write_rgb(green, (700, 50000, 1200))
    _write_rgb(blue, (200, 1800, 47000))

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
    with tifffile.TiffFile(result.positive_path) as positive_tiff:
        assert positive_tiff.pages[0].compression.name == "NONE"
    with tifffile.TiffFile(result.composite_path) as composite_tiff:
        assert composite_tiff.pages[0].compression.name == "NONE"
    assert Path(result.report_path).exists()
    # Crosstalk channels are discarded: each composite channel comes from only
    # the same-index channel of the matching LED exposure.
    assert int(composite[24, 24, 0]) == 52000
    assert int(composite[24, 24, 1]) == 50000
    assert int(composite[24, 24, 2]) == 47000


def test_render_triplet_positive_accepts_manual_base_region(tmp_path):
    red = tmp_path / "red.tif"
    green = tmp_path / "green.tif"
    blue = tmp_path / "blue.tif"
    _write_rgb(red, (52000, 1000, 800))
    _write_rgb(green, (700, 50000, 1200))
    _write_rgb(blue, (200, 1800, 47000))

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
    _write_rgb(red, (52000, 1000, 800))
    _write_rgb(green, (700, 50000, 1200))
    _write_rgb(blue, (200, 1800, 47000))

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
