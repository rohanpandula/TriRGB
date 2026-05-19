"""Linear-demosaic three RAW exposures (R, G, B-lit) into one 16-bit TIFF/DNG.

Phase 2 / Deliverable 2B of the film scanner build — see `../../PROJECT.md`.

The pipeline:

  1. Demosaic each ARW linearly with rawpy/libraw (no gamma, no WB, no
     auto-bright, output_bps=16, output_color=ProPhoto).
  2. From the R-lit demosaic, take channel 0 (the red sensor data, which
     under the 665nm Scanlight LED is what carries the film's red record).
  3. From the G-lit demosaic, take channel 1.
  4. From the B-lit demosaic, take channel 2.
  5. (Optional) Per-channel Flat Field Correction using cached cal maps.
  6. Stack into HxWx3 uint16, save as 16-bit linear ProPhoto-RGB.

Output format is either TIFF (the legacy default), Linear DNG (treated as
RAW by Lightroom/Capture One — gets you parametric editing controls), or
both side-by-side.

No inversion happens here. The output is a positive-numbers representation
of a negative; FilmLab or NLP handles inversion downstream.
"""
from __future__ import annotations

from pathlib import Path
from typing import Mapping, Optional, Union

import numpy as np
import rawpy
import tifffile

from .dng import write_linear_dng
from .ffc import (
    CalibrationError,
    FFCMaps,
    apply_ffc_to_channel,
    load_ffc_maps,
)

# Exact PROJECT.md-mandated demosaic parameters. Centralized here so the
# Phase 3 native app can reference the same dict via Python interop, and so
# tests can verify rawpy is invoked with these values.
DEMOSAIC_KWARGS: Mapping[str, object] = {
    "gamma": (1, 1),
    "no_auto_bright": True,
    "output_bps": 16,
    "use_camera_wb": False,
    # rawpy validates `user_wb` as a list — passing a tuple raises
    # `Argument 'user_wb' has incorrect type` at decode time. Caught by
    # the real-ARW integration test, not by mocked unit tests.
    "user_wb": [1.0, 1.0, 1.0, 1.0],
    "output_color": rawpy.ColorSpace.ProPhoto,
}

# Allowed values for the `output_format` parameter.
OUTPUT_FORMATS = ("tiff", "dng", "both")


class DimensionMismatchError(ValueError):
    """Raised when the three input RAWs don't have matching dimensions.

    Almost always means the film physically shifted between captures.
    """


def demosaic_linear(raw_path: Union[str, Path]) -> np.ndarray:
    """Open one RAW and return an HxWx3 uint16 array in linear ProPhoto-RGB.

    Uses the exact parameters in `DEMOSAIC_KWARGS`. Kept as a small named
    function so tests can monkeypatch this single seam in Phase 2 work and
    the Phase 3 app can re-host the same parameters.
    """
    with rawpy.imread(str(raw_path)) as raw:
        return raw.postprocess(**DEMOSAIC_KWARGS)


def _select_channel(arr: np.ndarray, ch: int) -> np.ndarray:
    """Return one channel of an HxWx3 array as HxW, with sanity checks."""
    if arr.ndim != 3 or arr.shape[2] < 3:
        raise ValueError(f"expected HxWx3 array, got shape {arr.shape}")
    if not (0 <= ch < arr.shape[2]):
        raise ValueError(f"channel {ch} out of range for {arr.shape}")
    return arr[..., ch]


def _write_tiff(out_path: Path, composite: np.ndarray, description: str) -> None:
    """Write a 16-bit linear RGB TIFF with colorspace metadata embedded."""
    tifffile.imwrite(
        out_path,
        composite,
        photometric="rgb",
        compression="zlib",
        predictor=True,
        description=description,
        software="rgb-composite",
        metadata={"colorspace": "linear ProPhoto-RGB"},
    )


def _write_sidecar(
    out_path: Path,
    description: str,
    *,
    ffc_source: Optional[Path] = None,
) -> Path:
    """Write a .colorspace.txt sidecar next to the composite.

    FilmLab in particular reads sidecars more reliably than embedded tags.
    """
    sidecar = out_path.with_suffix(out_path.suffix + ".colorspace.txt")
    body = (
        "colorspace: linear ProPhoto-RGB\n"
        "bit_depth: 16\n"
        "gamma: 1.0\n"
        "white_point: D50\n"
        "primaries: ProPhoto-RGB (Romm RGB)\n"
        "inversion: NOT INVERTED — invert downstream in FilmLab or NLP\n"
        f"source: {description}\n"
    )
    if ffc_source is not None:
        body += f"ffc_calibration: {ffc_source}\n"
    sidecar.write_text(body)
    return sidecar


def _derive_dng_path(tiff_path: Path) -> Path:
    """Return the DNG sibling path for a TIFF output path."""
    return tiff_path.with_suffix(".dng")


def _maybe_apply_ffc(
    r_channel: np.ndarray,
    g_channel: np.ndarray,
    b_channel: np.ndarray,
    ffc_maps: Optional[FFCMaps],
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """If `ffc_maps` is set, apply each per-channel map. Otherwise pass through."""
    if ffc_maps is None:
        return r_channel, g_channel, b_channel
    return (
        apply_ffc_to_channel(r_channel, ffc_maps.r),
        apply_ffc_to_channel(g_channel, ffc_maps.g),
        apply_ffc_to_channel(b_channel, ffc_maps.b),
    )


DEFAULT_DNG_CAMERA_MODEL = "Scanlight v4 Narrowband-RGB Composite"


def composite_triplet(
    r_path: Union[str, Path],
    g_path: Union[str, Path],
    b_path: Union[str, Path],
    out_path: Union[str, Path],
    *,
    write_sidecar: bool = True,
    ffc_calibration_dir: Optional[Union[str, Path]] = None,
    output_format: str = "tiff",
    dng_camera_model: Optional[str] = None,
) -> Path:
    """Read three RAWs, composite into one 16-bit linear output, return the path.

    Args:
        r_path: ARW captured under red LED illumination.
        g_path: ARW captured under green LED illumination.
        b_path: ARW captured under blue LED illumination.
        out_path: Where to write the output. Parent dirs created as needed.
            For `output_format="tiff"`, must end in `.tif/.tiff`.
            For `output_format="dng"`, the suffix is swapped to `.dng`.
            For `output_format="both"`, the TIFF goes at `out_path` and the
            DNG sibling at `out_path.with_suffix(".dng")`.
        write_sidecar: If True (default), also write a `.colorspace.txt`
            sidecar describing the colorspace. The output itself also
            carries colorspace tags, but FilmLab reads sidecars more
            reliably than embedded tags.
        ffc_calibration_dir: If provided, a directory containing R.ARW,
            G.ARW, B.ARW blank-light captures. Per-channel FFC maps are
            computed once (cached) and applied to each frame's matching
            channel before stacking. Required for clean narrowband-RGB
            scans — see `ffc.py` and the README for context.
        output_format: One of "tiff" (legacy default), "dng" (Linear DNG —
            opens as RAW in Lightroom/Capture One), or "both".
        dng_camera_model: value for the DNG `UniqueCameraModel` tag. The
            default identifies the file as a Scanlight composite. Set to
            `"Sony ILCE-7CR"` to make Lightroom offer Sony camera
            profiles (Cobalt Spectre, Adobe Standard, etc.) in the
            Profile dropdown. Ignored for tiff-only output.

    Returns:
        Path to the primary output. For `output_format="both"`, that's the
        TIFF; the DNG sibling exists at `result.with_suffix(".dng")`.

    Raises:
        DimensionMismatchError: if the three RAWs don't share dimensions.
        ValueError: invalid `output_format`.
        CalibrationError: cal directory exists but is unusable.
        rawpy.LibRawError (subclasses): on RAW decode failures.
    """
    if output_format not in OUTPUT_FORMATS:
        raise ValueError(
            f"output_format must be one of {OUTPUT_FORMATS}, got {output_format!r}"
        )
    if dng_camera_model is None:
        dng_camera_model = DEFAULT_DNG_CAMERA_MODEL

    r_path = Path(r_path)
    g_path = Path(g_path)
    b_path = Path(b_path)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Load FFC maps up front so a bad cal directory fails before we burn
    # 10+ seconds on three RAW decodes. lru_cache makes the repeated calls
    # in batch-mode free.
    ffc_maps: Optional[FFCMaps] = None
    if ffc_calibration_dir is not None:
        ffc_maps = load_ffc_maps(Path(ffc_calibration_dir))

    r_img = demosaic_linear(r_path)
    g_img = demosaic_linear(g_path)
    b_img = demosaic_linear(b_path)

    if not (r_img.shape == g_img.shape == b_img.shape):
        raise DimensionMismatchError(
            f"shape mismatch: R={r_img.shape} G={g_img.shape} B={b_img.shape} — "
            "this typically means the film moved between captures; reshoot the frame"
        )

    if ffc_maps is not None and ffc_maps.shape != r_img.shape[:2]:
        raise CalibrationError(
            f"FFC shape {ffc_maps.shape} doesn't match frame shape "
            f"{r_img.shape[:2]} — recapture calibration at the same crop/zoom"
        )

    # Per PROJECT.md: take channel 0 from R-lit, channel 1 from G-lit,
    # channel 2 from B-lit. The other two channels of each demosaic are
    # cross-talk noise from the narrowband illumination and are discarded.
    r_ch = _select_channel(r_img, 0)
    g_ch = _select_channel(g_img, 1)
    b_ch = _select_channel(b_img, 2)

    # FFC, if calibration was provided.
    r_ch, g_ch, b_ch = _maybe_apply_ffc(r_ch, g_ch, b_ch, ffc_maps)

    composite = np.stack([r_ch, g_ch, b_ch], axis=-1)

    # Sanity check the dtype — postprocess(output_bps=16) returns uint16,
    # apply_ffc preserves that.
    if composite.dtype != np.uint16:
        composite = composite.astype(np.uint16)

    description = (
        f"narrowband-RGB composite from {r_path.name}, {g_path.name}, {b_path.name} "
        "(linear ProPhoto-RGB, 16-bit, NOT inverted"
        + (f", FFC from {ffc_maps.source.name}" if ffc_maps else "")
        + ")"
    )

    # Dispatch by format. We always write the requested file(s) and a
    # single sidecar; if both formats are written, the sidecar sits next
    # to the TIFF (the primary deliverable).
    primary: Path
    if output_format == "tiff":
        _write_tiff(out_path, composite, description)
        primary = out_path
    elif output_format == "dng":
        dng_path = _derive_dng_path(out_path)
        write_linear_dng(
            dng_path, composite, description=description,
            camera_model=dng_camera_model,
        )
        primary = dng_path
    else:  # "both"
        _write_tiff(out_path, composite, description)
        write_linear_dng(
            _derive_dng_path(out_path), composite, description=description,
            camera_model=dng_camera_model,
        )
        primary = out_path

    if write_sidecar:
        _write_sidecar(
            primary,
            description,
            ffc_source=ffc_maps.source if ffc_maps else None,
        )

    return primary


def main(argv=None) -> int:
    """`rgb-composite` CLI entrypoint."""
    import argparse
    import sys

    p = argparse.ArgumentParser(
        prog="rgb-composite",
        description=(
            "Composite three narrowband-RGB RAW exposures into a 16-bit "
            "linear ProPhoto-RGB output. Result is NOT inverted; downstream "
            "tools (FilmLab, NLP) perform the negative-to-positive step."
        ),
    )
    p.add_argument("--r", required=True, help="RAW captured under red illumination")
    p.add_argument("--g", required=True, help="RAW captured under green illumination")
    p.add_argument("--b", required=True, help="RAW captured under blue illumination")
    p.add_argument("--out", required=True, help="Output path (TIFF by default; suffix swapped to .dng for --format dng)")
    p.add_argument(
        "--no-sidecar",
        action="store_true",
        help="Skip writing the .colorspace.txt sidecar (output tags still embedded).",
    )
    p.add_argument(
        "--ffc-calibration",
        metavar="DIR",
        default=None,
        help=(
            "Directory containing R.ARW, G.ARW, B.ARW blank-light captures. "
            "Per-channel Flat Field Correction will be applied to each frame "
            "channel before stacking. Strongly recommended for narrowband-RGB "
            "scanning to remove wavelength-dependent vignette tint."
        ),
    )
    p.add_argument(
        "--format",
        choices=OUTPUT_FORMATS,
        default="tiff",
        help=(
            "Output format. 'tiff' = legacy 16-bit linear ProPhoto TIFF. "
            "'dng' = Linear DNG (opens as RAW in Lightroom/Capture One, with "
            "full Develop module). 'both' = write both side-by-side."
        ),
    )
    p.add_argument(
        "--camera-model",
        default=DEFAULT_DNG_CAMERA_MODEL,
        help=(
            "Value for the DNG UniqueCameraModel tag. Defaults to "
            f"{DEFAULT_DNG_CAMERA_MODEL!r}. Set to \"Sony ILCE-7CR\" if "
            "you want Lightroom to offer Sony camera profiles (Cobalt "
            "Spectre, Adobe Standard, etc.) in the Profile dropdown."
        ),
    )
    args = p.parse_args(argv)

    try:
        out = composite_triplet(
            args.r,
            args.g,
            args.b,
            args.out,
            write_sidecar=not args.no_sidecar,
            ffc_calibration_dir=args.ffc_calibration,
            output_format=args.format,
            dng_camera_model=args.camera_model,
        )
    except DimensionMismatchError as e:
        print(f"rgb-composite: {e}", file=sys.stderr)
        return 1
    except CalibrationError as e:
        print(f"rgb-composite: calibration error: {e}", file=sys.stderr)
        return 1
    except FileNotFoundError as e:
        print(f"rgb-composite: input not found: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # rawpy errors etc.
        print(f"rgb-composite: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    print(str(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
