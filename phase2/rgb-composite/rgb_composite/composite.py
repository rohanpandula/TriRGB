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

import json
import logging
from pathlib import Path
from typing import Any, Mapping, Optional, Union

import numpy as np
import rawpy
import tifffile

logger = logging.getLogger("rgb_composite")

from c41_core.contracts import BaseRegionDescriptor, InversionParams

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
DEFAULT_POSITIVE_DMIN_PERCENTILE = 98.0
DEFAULT_POSITIVE_BLACK_PERCENTILE = 0.1
DEFAULT_POSITIVE_WHITE_PERCENTILE = 99.2
DEFAULT_POSITIVE_TONE_GAMMA = 0.55
DEFAULT_POSITIVE_ANALYSIS_CROP_FRACTION = 0.03


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


def _write_positive_sidecar(
    out_path: Path,
    source_path: Path,
    description: str,
    meta: Mapping[str, Any],
) -> Path:
    """Write a short audit sidecar for an auto-positive render."""
    sidecar = out_path.with_suffix(out_path.suffix + ".colorspace.txt")
    body = (
        "colorspace: rendered positive RGB from linear ProPhoto-RGB composite\n"
        "bit_depth: 16\n"
        "inversion: AUTO POSITIVE\n"
        "model: density (-log transmission/base)\n"
        f"source_negative: {source_path.name}\n"
        f"source: {description}\n"
        f"frame_base_rgb: {meta.get('frame_base_rgb')}\n"
        f"profile_base_rgb: {meta.get('profile_base_rgb')}\n"
        f"dmin_percentile: {meta.get('dmin_percentile')}\n"
        f"display_black_percentile: {meta.get('display_black_percentile')}\n"
        f"display_white_percentile: {meta.get('display_white_percentile')}\n"
        f"tone_gamma: {meta.get('tone_gamma')}\n"
        f"input_clip_fraction: {meta.get('input_clip_fraction')}\n"
    )
    sidecar.write_text(body)
    return sidecar


def _derive_dng_path(tiff_path: Path) -> Path:
    """Return the DNG sibling path for a TIFF output path."""
    return tiff_path.with_suffix(".dng")


def _derive_positive_path(primary_path: Path) -> Path:
    """Return the positive TIFF sibling for a primary composite path."""
    return primary_path.with_name(primary_path.stem + "_positive.tif")


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


def _profile_json_to_descriptor(
    profile_json: Union[str, Path, Mapping[str, Any], BaseRegionDescriptor],
) -> BaseRegionDescriptor:
    """Parse app/CLI positive-profile input into a BaseRegionDescriptor.

    Accepted shapes:
      - BaseRegionDescriptor instance
      - JSON string containing either a base-region object or
        {"base_region": {...}}
      - path to a JSON file with either of the above
      - mapping with snake_case or Swift camelCase keys
    """
    if isinstance(profile_json, BaseRegionDescriptor):
        return profile_json

    raw: Any = profile_json
    if isinstance(profile_json, Path):
        raw = profile_json.read_text()
    elif isinstance(profile_json, str):
        candidate = Path(profile_json).expanduser()
        if candidate.exists():
            raw = candidate.read_text()
        else:
            raw = profile_json

    if isinstance(raw, str):
        data = json.loads(raw)
    elif isinstance(raw, Mapping):
        data = dict(raw)
    else:
        raise TypeError(f"positive profile must be JSON, mapping, or BaseRegionDescriptor; got {type(raw).__name__}")

    if "base_region" in data:
        data = data["base_region"]
    elif "baseRegion" in data:
        data = data["baseRegion"]

    base_rgb = data.get("base_rgb", data.get("baseRgb"))
    uniformity_cv = data.get("uniformity_cv", data.get("uniformityCv", 0.0))
    schema_version = data.get("schema_version", data.get("schemaVersion", 1))
    source = data.get("source", "manual")

    return BaseRegionDescriptor(
        x=int(data["x"]),
        y=int(data["y"]),
        w=int(data["w"]),
        h=int(data["h"]),
        base_rgb=tuple(float(v) for v in base_rgb),
        uniformity_cv=float(uniformity_cv),
        source=str(source),
        schema_version=int(schema_version),
    )


def _analysis_sample(
    arr: np.ndarray,
    crop_fraction: float = DEFAULT_POSITIVE_ANALYSIS_CROP_FRACTION,
) -> np.ndarray:
    """Central image sample used for automatic display levels.

    Cropping keeps sprockets, edge borders, and the selected rebate patch from
    dominating the display stretch. Very small arrays fall back to the whole
    image so tests and thumbnails still work.
    """
    h, w = arr.shape[:2]
    cf = max(0.0, min(float(crop_fraction), 0.45))
    y0 = int(round(h * cf))
    y1 = int(round(h * (1.0 - cf)))
    x0 = int(round(w * cf))
    x1 = int(round(w * (1.0 - cf)))
    if y1 <= y0 or x1 <= x0:
        return arr
    return arr[y0:y1, x0:x1, :]


def _frame_base_rgb(
    triplet: np.ndarray,
    descriptor: BaseRegionDescriptor,
) -> tuple[float, float, float]:
    """Measure d-min color from the selected box in this specific frame.

    Calibration stores the location and an initial base measurement, but scan
    composites can land at a different absolute brightness because shutter/LED
    settings changed. Re-reading the same box from the current composite keeps
    the inversion white point tied to the frame that is actually being flipped.
    """
    h, w = triplet.shape[:2]
    x0 = max(0, min(int(descriptor.x), w))
    y0 = max(0, min(int(descriptor.y), h))
    x1 = max(x0, min(int(descriptor.x + descriptor.w), w))
    y1 = max(y0, min(int(descriptor.y + descriptor.h), h))
    if x1 <= x0 or y1 <= y0:
        return tuple(float(v) for v in descriptor.base_rgb)

    patch = triplet[y0:y1, x0:x1, :].reshape(-1, 3).astype(np.float32)
    if patch.size == 0:
        return tuple(float(v) for v in descriptor.base_rgb)

    base = tuple(float(v) for v in np.percentile(patch, DEFAULT_POSITIVE_DMIN_PERCENTILE, axis=0))
    if any(v < _MIN_BASE_CHANNEL for v in base):
        return tuple(float(v) for v in descriptor.base_rgb)
    return base


def auto_positive_from_composite(
    triplet: np.ndarray,
    descriptor: BaseRegionDescriptor,
    *,
    dmin_percentile: float = DEFAULT_POSITIVE_DMIN_PERCENTILE,
    display_black_percentile: float = DEFAULT_POSITIVE_BLACK_PERCENTILE,
    display_white_percentile: float = DEFAULT_POSITIVE_WHITE_PERCENTILE,
    tone_gamma: float = DEFAULT_POSITIVE_TONE_GAMMA,
    analysis_crop_fraction: float = DEFAULT_POSITIVE_ANALYSIS_CROP_FRACTION,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Render a linear negative composite into a usable 16-bit positive.

    This automates the operator workflow:
      1. measure clear film/base from the saved region on this frame;
      2. normalize each channel to that base and convert to optical density;
      3. stretch black/white points from robust scene percentiles;
      4. apply a mild display curve so the preview is an editable workprint.

    The archival composite remains unchanged; this function produces a
    separate rendered TIFF for inspection/editing.
    """
    if triplet.ndim != 3 or triplet.shape[2] != 3:
        raise ValueError(f"triplet must be HxWx3, got shape {triplet.shape}")
    if triplet.dtype != np.uint16:
        raise ValueError(f"triplet dtype must be uint16, got {triplet.dtype}")
    if any(b < _MIN_BASE_CHANNEL for b in descriptor.base_rgb):
        raise ValueError(
            f"base_rgb {descriptor.base_rgb} has a channel below {_MIN_BASE_CHANNEL}; "
            "recapture or reselect the film-base calibration patch"
        )
    if tone_gamma <= 0:
        raise ValueError(f"tone_gamma must be > 0, got {tone_gamma}")

    profile_base_rgb = tuple(float(v) for v in descriptor.base_rgb)
    frame_base_rgb = _frame_base_rgb(triplet, descriptor)

    work = triplet.astype(np.float32)
    base = np.maximum(np.asarray(frame_base_rgb, dtype=np.float32), _MIN_BASE_CHANNEL)

    # Normalize to clear film/base, then convert transmittance to density:
    # high negative density becomes high positive brightness. This is the
    # physical model the linear subtract/invert path was missing.
    transmission = work / base.reshape(1, 1, 3)
    np.clip(transmission, 1e-5, 1.0, out=transmission)
    density = -np.log(transmission, dtype=np.float32)

    sample = _analysis_sample(density, analysis_crop_fraction)
    positive = np.empty_like(density, dtype=np.float32)
    display_levels: list[tuple[float, float]] = []

    for ch in range(3):
        values = sample[..., ch].reshape(-1)
        values = values[np.isfinite(values)]
        if values.size == 0:
            raise ValueError("positive render analysis sample contains no finite pixels")

        lo = float(np.percentile(values, display_black_percentile))
        hi = float(np.percentile(values, display_white_percentile))
        if hi - lo <= 1e-6:
            hi = lo + 1e-6
        positive[..., ch] = (density[..., ch] - lo) / (hi - lo)
        display_levels.append((lo, hi))

    np.clip(positive, 0.0, 1.0, out=positive)
    positive = np.power(positive, tone_gamma, dtype=np.float32)
    np.clip(positive, 0.0, 1.0, out=positive)

    meta: dict[str, Any] = {
        "frame_base_rgb": tuple(float(v) for v in frame_base_rgb),
        "profile_base_rgb": profile_base_rgb,
        "dmin_percentile": dmin_percentile,
        "display_levels": tuple(display_levels),
        "display_black_percentile": display_black_percentile,
        "display_white_percentile": display_white_percentile,
        "tone_gamma": tone_gamma,
        "analysis_crop_fraction": analysis_crop_fraction,
        "input_clip_fraction": tuple(
            float(np.mean(triplet[..., ch] >= 65535)) for ch in range(3)
        ),
    }
    return np.rint(positive * 65535.0).astype(np.uint16), meta


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
    positive_profile_json: Optional[Union[str, Path, Mapping[str, Any], BaseRegionDescriptor]] = None,
    positive_tone_gamma: float = DEFAULT_POSITIVE_TONE_GAMMA,
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
        positive_profile_json: Optional base-region profile JSON from the
            calibration wizard. When set, a sibling `<stem>_positive.tif` is
            rendered automatically while the primary linear negative remains
            unchanged.
        positive_tone_gamma: Display curve exponent for the auto-positive
            render. Lower values brighten midtones; ignored unless
            `positive_profile_json` is set.

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

    # Sanity check the dtype — postprocess(output_bps=16) returns uint16 and
    # apply_ffc preserves that. If something upstream ever changes (a rawpy
    # upgrade, an FFC path returning float), coerce but log loudly rather than
    # silently masking the regression; round floats so we don't bias-truncate.
    if composite.dtype != np.uint16:
        logger.warning(
            "composite dtype was %s, expected uint16 — coercing "
            "(check rawpy/FFC output)", composite.dtype,
        )
        if np.issubdtype(composite.dtype, np.floating):
            composite = np.rint(composite).astype(np.uint16)
        else:
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

    if positive_profile_json is not None:
        descriptor = _profile_json_to_descriptor(positive_profile_json)
        positive, positive_meta = auto_positive_from_composite(
            composite,
            descriptor,
            tone_gamma=positive_tone_gamma,
        )
        positive_path = _derive_positive_path(primary)
        positive_description = (
            f"auto-positive render from {primary.name}; d-min balanced from "
            f"base region x={descriptor.x} y={descriptor.y} w={descriptor.w} h={descriptor.h}"
        )
        _write_tiff(positive_path, positive, positive_description)
        if write_sidecar:
            _write_positive_sidecar(
                positive_path,
                primary,
                positive_description,
                positive_meta,
            )

    return primary


# ---------------------------------------------------------------------------
# Phase 11 inversion extensions (additive — NFR-14)
# ---------------------------------------------------------------------------

# Fail-closed guard for a near-zero base calibration value.  The smallest
# real base channel in practice is blue (~2952 for Sony a7CR no-WB).  100.0
# is well below any plausible real value, so only a calibration failure (e.g.
# a mis-detected dark region) would fall below it.
# InversionParams.__post_init__ already guarantees white_point > black_point
# per channel (CR-02), so no inversion-denominator guard is needed here.
_MIN_BASE_CHANNEL: float = 100.0


def _apply_tone_curve(
    data: np.ndarray,
    tone_curve_id: str,
    tone_curve_params: tuple,
) -> np.ndarray:
    """Apply a tone curve to HxWx3 float32 data in the range [0, 1].

    Phase 11 implements ONLY the ``"linear"`` (identity) tone curve.  The
    dispatch structure is present so a future phase can add an ``elif`` branch
    without touching Phase 11 logic.  gamma is NOT applied here — see
    ``invert_composite``.

    Args:
        data: HxWx3 float32 array, values in [0, 1] after Step 3 clip.
        tone_curve_id: Identifier string.  Only ``"linear"`` is supported.
        tone_curve_params: Tuple of float params (empty for ``"linear"``).

    Returns:
        The (possibly transformed) array.  For ``"linear"``, returns ``data``
        unchanged (identity — SC-1 monotonic tone satisfied).

    Raises:
        NotImplementedError: for any ``tone_curve_id`` other than ``"linear"``.
    """
    if tone_curve_id == "linear":
        return data  # identity — monotonic, SC-1 satisfied
    raise NotImplementedError(
        f"tone_curve_id {tone_curve_id!r} is not implemented; "
        'only "linear" is supported in Phase 11'
    )


def invert_composite(
    triplet: np.ndarray,
    descriptor: BaseRegionDescriptor,
    params: InversionParams,
) -> np.ndarray:
    """Invert a composited C-41 negative triplet to a finished positive.

    Returns a 16-bit linear ProPhoto-RGB ``uint16`` array (HxWx3), suitable
    for downstream tone-grading in Phase 14 (wizard) or Phase 15 (roll
    integration).

    Five-step pipeline (all in LINEAR space, pure float32 arithmetic):

      Step 0 — Validate: triplet shape must be HxWx3; each ``base_rgb``
               channel must be >= ``_MIN_BASE_CHANNEL``.
      Step 1 — Dtype: ``triplet.astype(np.float32)`` — uint16 → float32
               always allocates a fresh copy (no caller-array mutation).
      Step 2 — Neutralize: per-channel gain = ``base_target / base_rgb[ch]``
               forces the measured rebate base to ``base_target`` gray,
               simultaneously canceling the orange mask AND the baked-in
               white balance.
      Step 3 — Invert: ``(white[ch] - x) / (white[ch] - black[ch])`` then
               ``clip(0, 1)``.  The denominator is never zero — InversionParams
               CR-02 guarantees ``white > black`` per channel.
      Step 4 — Tone curve: dispatched through ``_apply_tone_curve``.  Phase 11
               implements ONLY ``"linear"`` (identity).  Non-``"linear"``
               tone_curve_id raises ``NotImplementedError`` (fail-closed).
      Step 5 — Encode: ``*= 65535``, ``clip(0, 65535)``,
               ``astype(np.uint16)``.

    NOTE (anti-pattern warning): the input ``triplet`` is ALREADY
    black-subtracted by ``apply_ffc_radiometric`` in ``ffc.py`` (see
    ffc.py lines 418-420: "Phase 11 inversion must NOT re-subtract
    black_level from this output").  ``InversionParams.black_point_*`` is
    the INVERSION SHADOW FLOOR for the ``(white-x)/(white-black)`` formula,
    NOT a second black subtraction.  Do not add any subtraction of a
    black-level constant from ``triplet`` or ``work`` before Step 3.

    NOTE (gamma deferred): ``params.gamma`` is NOT applied in Phase 11.
    The field exists in ``InversionParams`` for a future phase.  No
    ``np.power`` or gamma encoding is performed here.

    NOTE (no color-vision path — NFR-11 / SC-5): all operations are
    per-channel numeric transforms; no perceptual color decision is made
    anywhere in this function.

    Args:
        triplet: HxWx3 ``uint16`` array — the composited, FFC-corrected,
            black-subtracted narrowband-RGB frame from ``composite_triplet``
            (or ``make_c41_negative`` in tests).
        descriptor: ``BaseRegionDescriptor`` carrying ``base_rgb`` — the
            measured no-WB raw per-channel mean of the rebate (film base).
        params: ``InversionParams`` carrying black/white points, base_target,
            tone_curve_id, and gamma.

    Returns:
        HxWx3 ``np.uint16`` finished positive in 16-bit linear ProPhoto-RGB.

    Raises:
        ValueError: if ``triplet`` is not HxWx3, or any ``base_rgb`` channel
            is below ``_MIN_BASE_CHANNEL`` (possible calibration failure).
        NotImplementedError: if ``params.tone_curve_id`` is not ``"linear"``.
    """
    # Step 0: validate inputs (fail-closed — T-11-01, T-11-02)
    if triplet.ndim != 3 or triplet.shape[2] != 3:
        raise ValueError(
            f"triplet must be HxWx3, got shape {triplet.shape}"
        )
    if triplet.dtype != np.uint16:
        raise ValueError(
            f"triplet dtype must be uint16 (HxWx3 uint16 contract), got {triplet.dtype}; "
            "non-uint16 arrays (e.g. float with NaN) are rejected to prevent silent "
            "data corruption in astype(uint16)"
        )
    if any(b < _MIN_BASE_CHANNEL for b in descriptor.base_rgb):
        raise ValueError(
            f"base_rgb {descriptor.base_rgb} has a channel below the minimum "
            f"threshold {_MIN_BASE_CHANNEL}; possible calibration error — "
            "recapture the rebate base measurement"
        )
    # Hoist tone_curve_id check before any array allocation (IN-02): an
    # unsupported curve would otherwise waste a full-image neutralize+invert
    # before raising.  When a future phase adds a new curve, remove this
    # early check alongside the matching elif in _apply_tone_curve.
    if params.tone_curve_id != "linear":
        raise NotImplementedError(
            f"tone_curve_id {params.tone_curve_id!r} is not implemented; "
            'only "linear" is supported in Phase 11'
        )

    # Step 1: float32 workspace — dtype change uint16→float32 guarantees a
    # fresh allocation; caller's array is never mutated (Pitfall 3).
    work = triplet.astype(np.float32)

    # Step 2: neutralize — per-channel gain forces measured base to base_target
    # gray, canceling the orange mask AND the baked-in WB simultaneously.
    # (Pitfall 1 reminder: do NOT subtract a black level here — the triplet is
    # already black-subtracted by apply_ffc_radiometric; see ffc.py:418-420.)
    for ch, base_val in enumerate(descriptor.base_rgb):
        work[..., ch] *= params.base_target / base_val

    # Step 3: invert per channel — (white − x) / (white − black), then clip.
    # Unpack as explicit (r,g,b) tuples to avoid channel transposition (Pitfall 2).
    # No runtime white>black guard needed — InversionParams CR-02 guarantees it.
    black_pts = (params.black_point_r, params.black_point_g, params.black_point_b)
    white_pts = (params.white_point_r, params.white_point_g, params.white_point_b)
    for ch in range(3):
        work[..., ch] = (white_pts[ch] - work[..., ch]) / (white_pts[ch] - black_pts[ch])
    # Clip BEFORE tone curve so _apply_tone_curve always receives [0, 1] data
    # (Pitfall 5 — future gamma curves require valid domain).
    np.clip(work, 0.0, 1.0, out=work)

    # Step 4: tone curve dispatch (Phase 11: identity only).
    # gamma is NOT applied here — deferred per Phase 11 scope.
    work = _apply_tone_curve(work, params.tone_curve_id, params.tone_curve_params)

    # Step 5: scale to 16-bit and encode (matches ffc.py clip+cast discipline).
    work *= 65535.0
    np.clip(work, 0.0, 65535.0, out=work)
    return work.astype(np.uint16)


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
    p.add_argument(
        "--positive-profile-json",
        default=None,
        help=(
            "Base-region JSON from exposure calibration. When provided, "
            "rgb-composite also writes a sibling *_positive.tif rendered with "
            "d-min balance, inversion, automatic levels, and a display curve."
        ),
    )
    p.add_argument(
        "--positive-tone-gamma",
        type=float,
        default=DEFAULT_POSITIVE_TONE_GAMMA,
        help=(
            "Tone curve exponent for --positive-profile-json output "
            f"(default {DEFAULT_POSITIVE_TONE_GAMMA})."
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
            positive_profile_json=args.positive_profile_json,
            positive_tone_gamma=args.positive_tone_gamma,
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
