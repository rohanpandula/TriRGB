"""Write a 16-bit narrowband-RGB composite as a Linear DNG.

Why Linear DNG, not TIFF
------------------------
Lightroom and Capture One treat Linear DNG as RAW — you get the full
Develop module: parametric Temperature/Tint, Highlights/Shadows recovery,
Camera Profile selection, tone curve, HSL. They treat TIFF as a flat
raster and grey out half of those controls.

For the narrowband-RGB scanning pipeline, this matters most when handing
to NLP: a tricky frame (mixed light, severely under-exposed) gets a
chance at pre-NLP latitude that a TIFF won't give you.

What "Linear DNG" means
-----------------------
A DNG with `PhotometricInterpretation = LinearRaw (34892)` and three
samples per pixel — i.e. already-demosaiced, linear, but tagged with
enough metadata (ColorMatrix1, CalibrationIlluminant1, AsShotNeutral,
BlackLevel/WhiteLevel, DNGVersion) that LR recognises it as a DNG and
runs it through the RAW pipeline instead of the rendered-image pipeline.

The data we write IS the linear ProPhoto-RGB composite, byte-for-byte the
same as the TIFF output. The only difference is the wrapper.

Color space
-----------
We declare the camera-native color space as ProPhoto-RGB (D50). That
means `ColorMatrix1` = XYZ_D50 → ProPhoto and `ForwardMatrix1` = ProPhoto
→ XYZ_D50. `CalibrationIlluminant1` = 23 (D50). LR will treat the
on-disk values as raw "ProPhoto" sensor data, then convert to its working
space (also ProPhoto) — essentially identity through the matrix path.

`AsShotNeutral` = (1, 1, 1) because the data is already in a balanced
working space, not in a sensor's native non-neutral RGB. This means LR's
default Temperature/Tint will appear neutral, but you can still slide them
to make creative adjustments before running NLP. (NLP's docs recommend a
flat develop state before conversion, which is exactly what this gives.)
"""
from __future__ import annotations

from pathlib import Path
from typing import Sequence, Union

import numpy as np
import tifffile


# Standard ProPhoto-RGB (Romm RGB) D50 primaries → XYZ_D50.
# Source: ICC spec / standard color FAQ. Used as ForwardMatrix1.
_PROPHOTO_TO_XYZ_D50 = np.array(
    [
        [0.7976749, 0.1351917, 0.0313534],
        [0.2880402, 0.7118741, 0.0000857],
        [0.0000000, 0.0000000, 0.8252100],
    ],
    dtype=np.float64,
)

# Inverse, XYZ_D50 → ProPhoto-RGB. Used as ColorMatrix1.
_XYZ_TO_PROPHOTO_D50 = np.linalg.inv(_PROPHOTO_TO_XYZ_D50)

# Denominator for SRATIONAL encoding. 10000 → ~4 decimal digits of
# precision per coefficient. The matrices above are themselves only
# ~7-digit precise so this is well below their noise floor.
_SRATIONAL_DENOM = 10000

# DNG TIFF tag numbers (subset we use).
_TAG = {
    # Standard TIFF tags written explicitly even though tifffile may
    # default them — strict DNG validators flag missing values.
    "NewSubFileType": 254,       # 0 = primary image
    "Orientation": 274,          # 1 = top-left (no rotation/flip)
    # DNG-specific tags.
    "DNGVersion": 50706,
    "DNGBackwardVersion": 50707,
    "UniqueCameraModel": 50708,
    "BlackLevel": 50714,
    "WhiteLevel": 50717,
    "ColorMatrix1": 50721,
    "AsShotNeutral": 50728,
    "BaselineExposure": 50730,
    "CalibrationIlluminant1": 50778,
    "ProfileName": 50936,
    "ProfileEmbedPolicy": 50941,
    "ForwardMatrix1": 50964,
}

# TIFF datatype codes (per TIFF 6.0 spec § 2).
_DT = {
    "BYTE": 1,
    "ASCII": 2,
    "SHORT": 3,    # uint16
    "LONG": 4,     # uint32
    "RATIONAL": 5,
    "SRATIONAL": 10,
    "SLONG": 9,    # int32 (used for BaselineExposure, technically SRATIONAL)
}

# DNG-spec CalibrationIlluminant code for D50.
_ILLUMINANT_D50 = 23

# DNG-spec photometric interpretation for linear, already-demosaiced data.
_PHOTOMETRIC_LINEAR_RAW = 34892


def _matrix_to_srational_pairs(m: np.ndarray) -> tuple[tuple[int, int], ...]:
    """Flatten 3x3 → 9 (num, den) tuples for an SRATIONAL[9] tag."""
    flat = np.asarray(m, dtype=np.float64).flatten()
    return tuple((int(round(v * _SRATIONAL_DENOM)), _SRATIONAL_DENOM) for v in flat)


def _signed_rational(value: float, denom: int = _SRATIONAL_DENOM) -> tuple[int, int]:
    return (int(round(value * denom)), denom)


def build_dng_extratags(
    *,
    camera_model: str = "Scanlight v4 Narrowband-RGB Composite",
    profile_name: str = "Linear ProPhoto Composite",
    baseline_exposure: float = 0.0,
) -> list[tuple]:
    """Build the DNG-specific `extratags` list for `tifffile.imwrite`.

    Composable for tests — `write_linear_dng` just uses defaults.
    """
    color_matrix = _matrix_to_srational_pairs(_XYZ_TO_PROPHOTO_D50)
    forward_matrix = _matrix_to_srational_pairs(_PROPHOTO_TO_XYZ_D50)
    return [
        # NewSubFileType = 0 (primary image, not preview/mask/etc.)
        (_TAG["NewSubFileType"], _DT["LONG"], 1, 0, True),
        # Orientation = 1 (top-left, no rotation). tifffile defaults to
        # this, but Adobe's DNG Validator flags missing tags.
        (_TAG["Orientation"], _DT["SHORT"], 1, 1, True),
        # DNGVersion = 1.4.0.0
        (_TAG["DNGVersion"], _DT["BYTE"], 4, bytes([1, 4, 0, 0]), True),
        # DNGBackwardVersion = 1.2.0.0 (oldest LR that should handle this)
        (_TAG["DNGBackwardVersion"], _DT["BYTE"], 4, bytes([1, 2, 0, 0]), True),
        # UniqueCameraModel — surfaced in LR's "Camera" metadata
        (_TAG["UniqueCameraModel"], _DT["ASCII"], 0, camera_model, True),
        # BlackLevel = 0 per channel
        (_TAG["BlackLevel"], _DT["LONG"], 3, (0, 0, 0), True),
        # WhiteLevel = 65535 per channel
        (_TAG["WhiteLevel"], _DT["LONG"], 3, (65535, 65535, 65535), True),
        # ColorMatrix1 — XYZ_D50 → ProPhoto (camera native)
        (_TAG["ColorMatrix1"], _DT["SRATIONAL"], 9, color_matrix, True),
        # ForwardMatrix1 — ProPhoto → XYZ_D50 (LR uses this in the
        # XYZ-based render path; without it, LR falls back to ColorMatrix
        # inversion which is less accurate)
        (_TAG["ForwardMatrix1"], _DT["SRATIONAL"], 9, forward_matrix, True),
        # AsShotNeutral = (1, 1, 1) — data is already balanced
        (
            _TAG["AsShotNeutral"],
            _DT["RATIONAL"],
            3,
            ((1, 1), (1, 1), (1, 1)),
            True,
        ),
        # BaselineExposure = 0.0
        (
            _TAG["BaselineExposure"],
            _DT["SRATIONAL"],
            1,
            (_signed_rational(baseline_exposure),),
            True,
        ),
        # CalibrationIlluminant1 = D50
        (_TAG["CalibrationIlluminant1"], _DT["SHORT"], 1, _ILLUMINANT_D50, True),
        # ProfileName
        (_TAG["ProfileName"], _DT["ASCII"], 0, profile_name, True),
        # ProfileEmbedPolicy = 0 (allow embedding by other software)
        (_TAG["ProfileEmbedPolicy"], _DT["LONG"], 1, 0, True),
    ]


def write_linear_dng(
    out_path: Union[str, Path],
    composite: np.ndarray,
    *,
    description: str = "",
    software: str = "rgb-composite",
    camera_model: str = "Scanlight v4 Narrowband-RGB Composite",
) -> Path:
    """Write a HxWx3 uint16 array as a Linear DNG.

    Args:
        out_path: where to write. Parent dirs created as needed.
        composite: HxWx3 uint16 array of linear ProPhoto-RGB values.
        description: ImageDescription tag content.
        software: Software tag content.
        camera_model: value for the DNG `UniqueCameraModel` tag (50708).
            Default identifies this as a Scanlight composite. Set to
            `"Sony ILCE-7CR"` to make Lightroom offer Sony camera
            profiles (Cobalt Spectre, Adobe Standard, etc.) in the
            Profile dropdown. The data on disk is unchanged either way
            — only LR's interpretation differs.

    Returns:
        Path to the written DNG.

    Raises:
        ValueError: if `composite` is the wrong shape or dtype.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if composite.ndim != 3 or composite.shape[2] != 3:
        raise ValueError(
            f"expected HxWx3 array for DNG output, got shape {composite.shape}"
        )
    if composite.dtype != np.uint16:
        composite = composite.astype(np.uint16)

    extratags = build_dng_extratags(camera_model=camera_model)

    tifffile.imwrite(
        str(out_path),
        composite,
        photometric=_PHOTOMETRIC_LINEAR_RAW,
        planarconfig="contig",
        # No compression — DNGs are typically uncompressed or use lossless
        # JPEG. Plain uncompressed is the most universally readable.
        compression=None,
        software=software,
        description=description,
        extratags=extratags,
        # Suppress tifffile's own ImageJ/OME-style metadata so we don't
        # confuse DNG-aware readers with extra JSON in ImageDescription.
        metadata=None,
    )
    return out_path


def read_linear_dng_tags(path: Union[str, Path]) -> dict:
    """Read back the DNG-relevant tags. Used by tests and diagnostics."""
    path = Path(path)
    out: dict[str, object] = {}
    with tifffile.TiffFile(str(path)) as tf:
        page = tf.pages[0]
        for name, tagnum in _TAG.items():
            tag = page.tags.get(tagnum)
            if tag is not None:
                out[name] = tag.value
        # Also surface PhotometricInterpretation so callers can sanity-check.
        photo = page.tags.get("PhotometricInterpretation")
        if photo is not None:
            out["PhotometricInterpretation"] = int(photo.value)
    return out
