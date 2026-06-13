"""Auto-positive rendering for an already-captured RGB-lit triplet.

This module is the bridge between the operator-friendly workflow ("pick the
three RGB-lit files") and the existing narrowband RGB composite/inversion code.
It accepts three RAW/TIFF inputs in any order and decides which is R, G, and B
by measured per-channel signal energy — which narrowband LED dominated each
exposure — NOT by file names. File names are never consulted: the camera's
generic names (``DSC00448.ARW``) are accepted as-is, and assignment is
colorblind-safe (NFR-11) because the software reads the sensor signal rather
than asking anyone to judge color. It keeps only the matching sensor channel
from each exposure, then renders a positive TIFF using the same density-based
inversion used by the scan pipeline.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence, TypedDict

import numpy as np
from PIL import Image
import tifffile

from c41_core.contracts import BaseRegionDescriptor

from .composite import (
    DEFAULT_POSITIVE_BLACK_PERCENTILE,
    DEFAULT_POSITIVE_WHITE_PERCENTILE,
    DEFAULT_POSITIVE_TONE_GAMMA,
    auto_positive_from_composite,
    demosaic_linear,
    hd_sigmoid_tone,
)


CHANNEL_NAMES = ("R", "G", "B")
AUTO_ASSIGN_MIN_CONFIDENCE = 1.5
AUTO_ASSIGN_MIN_SIGNAL = 256.0
TIFF_SUFFIXES = {".tif", ".tiff"}
RAW_SUFFIXES = {
    ".arw", ".raw", ".dng", ".nef", ".cr2", ".cr3", ".raf", ".rw2", ".orf",
}
class LookSettings(TypedDict, total=False):
    black: float
    white: float
    gamma: float
    curve: float
    curve_type: str
    contrast: float
    pivot: float


LOOK_SETTINGS: dict[str, LookSettings] = {
    # The old workprint behavior: deliberately gentle and editable.
    "flat": {
        "black": DEFAULT_POSITIVE_BLACK_PERCENTILE,
        "white": DEFAULT_POSITIVE_WHITE_PERCENTILE,
        "gamma": DEFAULT_POSITIVE_TONE_GAMMA,
        "curve": 0.0,
    },
    # Better default starting point: slightly deeper black/white stretch and a
    # mild S-curve after density inversion.
    "standard": {
        "black": 0.7,
        "white": 99.82,
        "gamma": 0.82,
        "curve": 0.62,
    },
    "punchy": {
        "black": 1.0,
        "white": 99.9,
        "gamma": 0.9,
        "curve": 0.78,
    },
    "filmic": {
        "black": 0.7,
        "white": 99.82,
        "gamma": 0.82,
        "curve_type": "sigmoid",
        "contrast": 5.0,
        "pivot": 0.5,
    },
}


@dataclass(frozen=True)
class AssignedFile:
    channel: str
    channel_index: int
    path: str
    score: float
    confidence: float


@dataclass(frozen=True)
class TripletPositiveResult:
    positive_path: str
    composite_path: str
    report_path: str
    mapping: list[AssignedFile]
    base_region: dict[str, Any]
    positive_meta: dict[str, Any]


@dataclass(frozen=True)
class TripletPreviewResult:
    preview_path: str
    full_width: int
    full_height: int
    preview_width: int
    preview_height: int
    mapping: list[AssignedFile]
    auto_base_region: dict[str, Any]


class TripletPositiveError(RuntimeError):
    """Raised when a triplet cannot be processed safely."""


def load_linear_rgb(path: Path) -> np.ndarray:
    """Load a TIFF/RAW image as HxWx3 uint16 linear-ish RGB.

    RAW files go through the same rawpy demosaic settings used by
    ``rgb-composite``. TIFF files are assumed to be imported RGB data; 8-bit
    TIFFs are promoted to 16-bit so the rest of the pipeline has one contract.
    """
    suffix = path.suffix.lower()
    if suffix in TIFF_SUFFIXES:
        return _load_tiff_rgb(path)
    if suffix in RAW_SUFFIXES:
        return demosaic_linear(path)
    raise TripletPositiveError(
        f"{path.name}: unsupported file type {path.suffix!r}; choose TIFF or RAW files"
    )


def _load_tiff_rgb(path: Path) -> np.ndarray:
    arr = tifffile.imread(path)
    if arr.ndim == 4 and arr.shape[0] == 1:
        arr = arr[0]
    if arr.ndim == 3 and arr.shape[0] in (3, 4) and arr.shape[-1] not in (3, 4):
        arr = np.moveaxis(arr, 0, -1)
    if arr.ndim == 2:
        arr = np.repeat(arr[..., None], 3, axis=2)
    if arr.ndim != 3 or arr.shape[2] < 3:
        raise TripletPositiveError(f"{path.name}: expected an RGB TIFF, got shape {arr.shape}")
    return _to_uint16_rgb(arr[..., :3])


def _to_uint16_rgb(arr: np.ndarray) -> np.ndarray:
    if arr.dtype == np.uint16:
        return np.ascontiguousarray(arr)
    if arr.dtype == np.uint8:
        return (arr.astype(np.uint16) * np.uint16(257))
    if np.issubdtype(arr.dtype, np.unsignedinteger):
        max_value = float(np.iinfo(arr.dtype).max)
        return np.rint(arr.astype(np.float32) * (65535.0 / max_value)).astype(np.uint16)
    if np.issubdtype(arr.dtype, np.floating):
        work = arr.astype(np.float32)
        finite = work[np.isfinite(work)]
        if finite.size == 0:
            raise TripletPositiveError("floating-point TIFF contains no finite pixels")
        scale = 65535.0 if float(np.nanmax(finite)) <= 1.5 else 1.0
        np.clip(work * scale, 0.0, 65535.0, out=work)
        return np.rint(work).astype(np.uint16)
    raise TripletPositiveError(f"unsupported TIFF dtype {arr.dtype}")


def _channel_scores_from_image(
    path: Path,
    cache: dict[str, np.ndarray],
) -> dict[str, float]:
    """Return robust per-channel signal scores used to assign the channel.

    The score is a high percentile of each channel after subtracting a low
    percentile pedestal. That makes the comparison insensitive to black level,
    hot pixels, and small dark borders while preserving the narrowband LED
    dominance signal that identifies which R/G/B exposure this frame is.

    Note: a clipped or perfectly flat dominant channel has near-zero spread and
    can score low (pedestal ≈ peak). Properly exposed narrowband frames have
    scene texture in the lit channel, so this is robust in practice; a corrupt
    or clipped exposure fails closed via the signal/confidence gates in
    ``detect_rgb_assignment`` rather than being silently mis-assigned.

    Args:
        path: Resolved path to the RAW/TIFF file.
        cache: Dict keyed by str(path) — loaded arrays are stored here so
            ``build_composite`` can reuse them without a second demosaic (F4).
    """
    key = str(path)
    img = cache.get(key)
    if img is None:
        img = load_linear_rgb(path)
        cache[key] = img

    scores: dict[str, float] = {}
    for index, channel in enumerate(CHANNEL_NAMES):
        plane = img[..., index].astype(np.float32, copy=False)

        # F7: stride-8 subsample for percentile computation when the plane is
        # large (> 4_000_000 elements — a 61 MP sensor has ~20 M per channel).
        # A stride-8 sample (~315k elements) is statistically identical at
        # p1/p99.9 for any real narrowband-LED exposure. Fixed stride is
        # deterministic (NFR-11). Small test fixtures fall through unchanged.
        work = plane[::8, ::8] if plane.size > 4_000_000 else plane

        pedestal = float(np.percentile(work, 1.0))
        signal = np.maximum(work - pedestal, 0.0)
        scores[channel] = float(np.percentile(signal, 99.9))
    return scores


def detect_rgb_assignment(
    paths: Sequence[Path],
    _image_cache: dict[str, np.ndarray] | None = None,
) -> list[AssignedFile]:
    """Assign three input paths to R/G/B by measured per-channel signal energy.

    Channel roles are decided by which narrowband LED dominated each exposure
    (the sensor channel with the most signal) — **never** by file names. This is
    robust to generic camera names (``DSC00448.ARW``) and colorblind-safe
    (NFR-11): the software reads the sensor signal, the operator never judges
    color, the result is deterministic, and a triplet that is ambiguous (no
    clear dominant channel) or conflicting (two frames strongest in the same
    channel) fails closed with an actionable error rather than silently swapping
    channels.

    Args:
        paths: Exactly three RAW/TIFF paths.
        _image_cache: Optional dict for caching loaded arrays across the
            detect→build pipeline (F4). Pass the same dict to
            ``build_composite`` to avoid re-demosaicing the same files.
            If None, a fresh dict is created internally and discarded.
    """
    if len(paths) != 3:
        raise TripletPositiveError(f"expected exactly 3 inputs, got {len(paths)}")

    if _image_cache is None:
        _image_cache = {}

    by_channel: dict[str, AssignedFile] = {}
    for raw_path in paths:
        path = Path(raw_path)
        scores = _channel_scores_from_image(path, _image_cache)
        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        best_channel, best_score = ranked[0]
        second_score = max(score for _ch, score in ranked[1:])
        confidence = best_score / max(second_score, 1.0)
        score_text = ", ".join(f"{ch}={scores[ch]:.0f}" for ch in CHANNEL_NAMES)

        if best_score < AUTO_ASSIGN_MIN_SIGNAL or confidence < AUTO_ASSIGN_MIN_CONFIDENCE:
            raise TripletPositiveError(
                f"{path.name}: no clear single-channel dominance "
                f"({score_text}; confidence {confidence:.2f}x, need "
                f"≥{AUTO_ASSIGN_MIN_CONFIDENCE:g}x). Each frame must be a single "
                "narrowband R/G/B exposure — check the per-channel illumination."
            )
        if best_channel in by_channel:
            raise TripletPositiveError(
                f"two frames both read strongest in channel {best_channel}: "
                f"{Path(by_channel[best_channel].path).name} and {path.name} "
                f"({score_text}). Provide one R-lit, one G-lit, and one B-lit frame."
            )

        by_channel[best_channel] = AssignedFile(
            channel=best_channel,
            channel_index=CHANNEL_NAMES.index(best_channel),
            path=str(path),
            score=best_score,
            confidence=confidence,
        )

    missing = [ch for ch in CHANNEL_NAMES if ch not in by_channel]
    if missing:
        raise TripletPositiveError(
            f"could not identify channel(s) {', '.join(missing)} from the triplet "
            "by signal energy. Each frame must be a single narrowband R/G/B exposure."
        )

    return [by_channel[ch] for ch in CHANNEL_NAMES]


def build_composite(
    assignments: Sequence[AssignedFile],
    _image_cache: dict[str, np.ndarray] | None = None,
) -> np.ndarray:
    """Build a channel-isolated RGB negative composite from detected files.

    Args:
        assignments: Ordered R/G/B assignment list from ``detect_rgb_assignment``.
        _image_cache: Optional dict keyed by str(path) — loaded arrays from the
            scoring pass in ``detect_rgb_assignment`` (F4). When provided, each
            file is demosaiced exactly once across the full detect→build pipeline.
            After extracting the needed channel, the full array is deleted from
            the cache to release memory promptly.
    """
    if _image_cache is None:
        _image_cache = {}

    channels: list[np.ndarray] = []
    expected_shape: tuple[int, int] | None = None
    for assignment in sorted(assignments, key=lambda item: item.channel_index):
        path = Path(assignment.path)
        key = str(path)
        img = _image_cache.get(key)
        if img is None:
            img = load_linear_rgb(path)
        shape = img.shape[:2]
        if expected_shape is None:
            expected_shape = shape
        elif shape != expected_shape:
            raise TripletPositiveError(
                "input dimensions do not match: "
                f"expected {expected_shape}, got {shape} for {path.name}"
            )
        # .copy() so we can del the full array (frees ~350MB per RAW).
        channels.append(img[..., assignment.channel_index].copy())
        # Remove from cache: the full HxWx3 is no longer needed.
        _image_cache.pop(key, None)
        del img
    return np.stack(channels, axis=-1)


def auto_base_region(
    composite: np.ndarray,
    *,
    patch_size: int = 256,
) -> BaseRegionDescriptor:
    """Find a likely clear-film/base patch near the image edges."""
    if composite.ndim != 3 or composite.shape[2] != 3:
        raise TripletPositiveError(f"composite must be HxWx3, got {composite.shape}")
    h, w = composite.shape[:2]
    size = max(32, min(int(patch_size), h, w))
    step = max(16, size // 2)
    margin_x = max(size, int(round(w * 0.18)))
    margin_y = max(size, int(round(h * 0.18)))

    candidates: set[tuple[int, int]] = set()
    for y in range(0, max(1, h - size + 1), step):
        candidates.add((0, y))
        candidates.add((max(0, w - size), y))
    for x in range(0, max(1, w - size + 1), step):
        candidates.add((x, 0))
        candidates.add((x, max(0, h - size)))

    # Add a margin-grid fallback so a clear film strip just inside a border can
    # win over the absolute frame edge.
    for y in range(0, min(h - size + 1, margin_y), step):
        for x in range(0, max(1, w - size + 1), step):
            if x < margin_x or x > w - margin_x - size:
                candidates.add((x, y))
    for y in range(max(0, h - margin_y - size), max(1, h - size + 1), step):
        for x in range(0, max(1, w - size + 1), step):
            if x < margin_x or x > w - margin_x - size:
                candidates.add((x, y))

    best: tuple[float, int, int, tuple[float, float, float], float] | None = None
    for x, y in candidates:
        patch = composite[y:y + size, x:x + size, :]
        if patch.shape[0] != size or patch.shape[1] != size:
            continue
        base_rgb = tuple(float(v) for v in np.percentile(patch.reshape(-1, 3), 98.0, axis=0))
        gray = patch.astype(np.float32).mean(axis=2)
        gray_mean = float(gray.mean())
        if gray_mean <= 1.0:
            continue
        uniformity_cv = float(gray.std() / gray_mean * 100.0)
        clip_fraction = float(np.mean(patch >= 65535))
        floor_fraction = float(np.mean(patch <= 0))
        geometric_signal = float(np.prod(np.maximum(base_rgb, 1.0)) ** (1.0 / 3.0))
        score = geometric_signal - (geometric_signal * uniformity_cv * 0.02)
        score -= geometric_signal * clip_fraction * 8.0
        score -= geometric_signal * floor_fraction * 0.5
        if best is None or score > best[0]:
            best = (score, x, y, base_rgb, uniformity_cv)

    if best is None:
        raise TripletPositiveError("could not locate a usable film-base patch near the frame edge")

    _score, x, y, base_rgb, uniformity_cv = best
    return BaseRegionDescriptor(
        x=int(x),
        y=int(y),
        w=int(size),
        h=int(size),
        base_rgb=base_rgb,
        uniformity_cv=max(0.0, min(uniformity_cv, 100.0)),
        source="auto",
    )


def manual_base_region(
    composite: np.ndarray,
    region: tuple[int, int, int, int],
) -> BaseRegionDescriptor:
    """Measure film base from a user-supplied full-resolution crop."""
    if composite.ndim != 3 or composite.shape[2] != 3:
        raise TripletPositiveError(f"composite must be HxWx3, got {composite.shape}")
    h, w = composite.shape[:2]
    x, y, rw, rh = (int(v) for v in region)
    if rw <= 0 or rh <= 0:
        raise TripletPositiveError(f"manual base patch must have positive width/height, got {rw}x{rh}")
    x0 = max(0, min(x, w - 1))
    y0 = max(0, min(y, h - 1))
    x1 = max(x0 + 1, min(x + rw, w))
    y1 = max(y0 + 1, min(y + rh, h))
    patch = composite[y0:y1, x0:x1, :]
    base_rgb = tuple(float(v) for v in np.percentile(patch.reshape(-1, 3), 98.0, axis=0))
    gray = patch.astype(np.float32).mean(axis=2)
    gray_mean = float(gray.mean())
    uniformity_cv = float(gray.std() / max(gray_mean, 1.0) * 100.0)
    return BaseRegionDescriptor(
        x=x0,
        y=y0,
        w=x1 - x0,
        h=y1 - y0,
        base_rgb=base_rgb,
        uniformity_cv=max(0.0, min(uniformity_cv, 100.0)),
        source="manual",
    )


def apply_render_look(
    positive: np.ndarray,
    *,
    curve_amount: float,
    curve_type: str = "smoothstep",
    contrast: float = 5.0,
    pivot: float = 0.5,
) -> np.ndarray:
    """Apply the selected monotonic render-look curve to a positive image."""
    normalized_curve_type = curve_type.lower()
    if normalized_curve_type == "smoothstep":
        amount = max(0.0, min(float(curve_amount), 1.0))
        if amount <= 0.0:
            return positive
        work = positive.astype(np.float32) / 65535.0
        # Smoothstep is monotonic, keeps 0/1 pinned, and steepens the midtones.
        curved = work * work * (3.0 - 2.0 * work)
        work = work * (1.0 - amount) + curved * amount
        np.clip(work, 0.0, 1.0, out=work)
        return np.rint(work * 65535.0).astype(np.uint16)

    if normalized_curve_type == "sigmoid":
        work = positive.astype(np.float32) / 65535.0
        work = hd_sigmoid_tone(work, contrast=contrast, pivot=pivot)
        np.clip(work, 0.0, 1.0, out=work)
        return np.rint(work * 65535.0).astype(np.uint16)

    raise ValueError(f"unknown render look curve_type {curve_type!r}")


def render_triplet_preview(
    paths: Sequence[Path],
    preview_path: Path,
    *,
    max_dimension: int = 1600,
    look: str = "standard",
    patch_size: int = 256,
    base_mode: str = "auto",
) -> TripletPreviewResult:
    """Render a small positive preview for visual base-patch selection."""
    preview_path.parent.mkdir(parents=True, exist_ok=True)
    clean_paths = [Path(path).expanduser().resolve() for path in paths]
    for path in clean_paths:
        if not path.exists():
            raise TripletPositiveError(f"input not found: {path}")

    # F4: shared cache so each file is demosaiced exactly once (detect + build).
    _cache: dict[str, np.ndarray] = {}
    assignments = detect_rgb_assignment(clean_paths, _image_cache=_cache)
    composite = build_composite(assignments, _image_cache=_cache)
    descriptor = auto_base_region(composite, patch_size=patch_size)
    look_key = look.lower()
    if look_key not in LOOK_SETTINGS:
        raise TripletPositiveError(
            f"look must be one of {', '.join(sorted(LOOK_SETTINGS))}, got {look!r}"
        )
    look_settings = LOOK_SETTINGS[look_key]
    positive, _meta = auto_positive_from_composite(
        composite,
        descriptor,
        display_black_percentile=look_settings["black"],
        display_white_percentile=look_settings["white"],
        tone_gamma=look_settings["gamma"],
        base_mode=base_mode,
    )
    positive = apply_render_look(
        positive,
        curve_amount=float(look_settings.get("curve", 1.0)),
        curve_type=str(look_settings.get("curve_type", "smoothstep")),
        contrast=float(look_settings.get("contrast", 5.0)),
        pivot=float(look_settings.get("pivot", 0.5)),
    )

    full_h, full_w = positive.shape[:2]
    max_dim = max(16, int(max_dimension))
    stride = max(1, int(np.ceil(max(full_w, full_h) / max_dim)))
    preview = positive[::stride, ::stride, :]
    preview8 = np.rint(preview.astype(np.float32) / 257.0).clip(0, 255).astype(np.uint8)
    Image.fromarray(preview8).save(preview_path)

    return TripletPreviewResult(
        preview_path=str(preview_path),
        full_width=int(full_w),
        full_height=int(full_h),
        preview_width=int(preview8.shape[1]),
        preview_height=int(preview8.shape[0]),
        mapping=assignments,
        auto_base_region={
            "x": descriptor.x,
            "y": descriptor.y,
            "w": descriptor.w,
            "h": descriptor.h,
            "base_rgb": list(descriptor.base_rgb),
            "uniformity_cv": descriptor.uniformity_cv,
            "source": descriptor.source,
        },
    )


def render_triplet_positive(
    paths: Sequence[Path],
    out_dir: Path,
    *,
    stem: str | None = None,
    patch_size: int = 256,
    tone_gamma: float | None = None,
    look: str = "standard",
    base_region: tuple[int, int, int, int] | None = None,
    base_mode: str = "auto",
) -> TripletPositiveResult:
    """Process three files and write negative composite, positive TIFF, report."""
    out_dir.mkdir(parents=True, exist_ok=True)
    clean_paths = [Path(path).expanduser().resolve() for path in paths]
    for path in clean_paths:
        if not path.exists():
            raise TripletPositiveError(f"input not found: {path}")

    if stem is None:
        joined = "_".join(path.stem for path in clean_paths)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        stem = f"{joined}_{timestamp}"

    # F4: shared cache so each file is demosaiced exactly once (detect + build).
    _cache: dict[str, np.ndarray] = {}
    assignments = detect_rgb_assignment(clean_paths, _image_cache=_cache)
    composite = build_composite(assignments, _image_cache=_cache)
    descriptor = (
        manual_base_region(composite, base_region)
        if base_region is not None
        else auto_base_region(composite, patch_size=patch_size)
    )
    look_key = look.lower()
    if look_key not in LOOK_SETTINGS:
        raise TripletPositiveError(
            f"look must be one of {', '.join(sorted(LOOK_SETTINGS))}, got {look!r}"
        )
    look_settings = LOOK_SETTINGS[look_key]
    effective_gamma = float(tone_gamma) if tone_gamma is not None else look_settings["gamma"]
    # A manual base_region produces a descriptor with source="manual", which
    # auto_positive_from_composite never auto-overrides — so passing base_mode
    # straight through honors an operator-drawn box without a special case here.
    positive, meta = auto_positive_from_composite(
        composite,
        descriptor,
        display_black_percentile=look_settings["black"],
        display_white_percentile=look_settings["white"],
        tone_gamma=effective_gamma,
        base_mode=base_mode,
    )
    positive = apply_render_look(
        positive,
        curve_amount=float(look_settings.get("curve", 1.0)),
        curve_type=str(look_settings.get("curve_type", "smoothstep")),
        contrast=float(look_settings.get("contrast", 5.0)),
        pivot=float(look_settings.get("pivot", 0.5)),
    )
    meta["look"] = look_key
    meta["look_curve_amount"] = float(look_settings.get("curve", 1.0))

    composite_path = out_dir / f"{stem}_negative-composite.tif"
    positive_path = out_dir / f"{stem}_positive.tif"
    report_path = out_dir / f"{stem}_report.json"

    # F8: zlib+predictor compression matches the archival writer in composite.py.
    # ~361 MB uncompressed → ~150 MB compressed for a typical 61 MP frame.
    tifffile.imwrite(
        composite_path,
        composite,
        photometric="rgb",
        compression="zlib",
        predictor=True,
        description="channel-isolated RGB negative composite from imported triplet",
        metadata=None,
    )
    tifffile.imwrite(
        positive_path,
        positive,
        photometric="rgb",
        compression="zlib",
        predictor=True,
        description="auto positive render from channel-isolated RGB triplet",
        metadata=None,
    )

    result = TripletPositiveResult(
        positive_path=str(positive_path),
        composite_path=str(composite_path),
        report_path=str(report_path),
        mapping=assignments,
        base_region={
            "x": descriptor.x,
            "y": descriptor.y,
            "w": descriptor.w,
            "h": descriptor.h,
            "base_rgb": list(descriptor.base_rgb),
            "uniformity_cv": descriptor.uniformity_cv,
            "source": descriptor.source,
        },
        positive_meta=_jsonable(meta),
    )
    report_path.write_text(json.dumps(_result_to_dict(result), indent=2), encoding="utf-8")
    return result


def _jsonable(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return value


def _result_to_dict(result: TripletPositiveResult) -> dict[str, Any]:
    data = asdict(result)
    data["mapping"] = [asdict(item) for item in result.mapping]
    return _jsonable(data)


def _parse_paths(values: Iterable[str]) -> list[Path]:
    return [Path(value).expanduser() for value in values]


def _parse_base_region(value: str | None) -> tuple[int, int, int, int] | None:
    if value is None or value.strip() == "":
        return None
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("base region must be x,y,w,h")
    try:
        x, y, w, h = (int(part) for part in parts)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("base region must contain integers") from exc
    if w <= 0 or h <= 0:
        raise argparse.ArgumentTypeError("base region width and height must be > 0")
    return x, y, w, h


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scanlight-triplet-positive",
        description="Auto-detect R/G/B triplet files and render a positive TIFF.",
    )
    parser.add_argument("inputs", nargs=3, help="Three RAW/TIFF files from one RGB-lit frame")
    parser.add_argument("--out-dir", required=True, help="Directory for composite, positive, and report")
    parser.add_argument("--stem", default=None, help="Output filename stem")
    parser.add_argument("--base-patch", type=int, default=256, help="Auto d-min patch size in pixels")
    parser.add_argument(
        "--preview-out",
        default=None,
        help="Write only a small PNG preview for visual base-patch selection.",
    )
    parser.add_argument(
        "--preview-max",
        type=int,
        default=1600,
        help="Maximum preview image dimension when --preview-out is used.",
    )
    parser.add_argument(
        "--base-region",
        type=_parse_base_region,
        default=None,
        help="Manual full-resolution film-base crop as x,y,w,h. Overrides --base-patch.",
    )
    parser.add_argument(
        "--look",
        choices=sorted(LOOK_SETTINGS),
        default="standard",
        help="Automatic tone curve starting point.",
    )
    parser.add_argument(
        "--tone-gamma",
        type=float,
        default=None,
        help="Override the selected look's tone gamma.",
    )
    parser.add_argument(
        "--base-mode",
        choices=("patch", "whole_frame", "auto"),
        default="auto",
        help=(
            "Clear-film base estimation. 'patch' uses the rebate box; "
            "'whole_frame' uses the frame's per-channel high percentile (best for "
            "full-bleed scans with no rebate); 'auto' (default) falls back to "
            "whole-frame only when the picked patch is non-uniform."
        ),
    )
    args = parser.parse_args(argv)

    try:
        if args.preview_out is not None:
            result = render_triplet_preview(
                _parse_paths(args.inputs),
                Path(args.preview_out).expanduser(),
                max_dimension=args.preview_max,
                look=args.look,
                patch_size=args.base_patch,
                base_mode=args.base_mode,
            )
        else:
            result = render_triplet_positive(
                _parse_paths(args.inputs),
                Path(args.out_dir).expanduser(),
                stem=args.stem,
                patch_size=args.base_patch,
                tone_gamma=args.tone_gamma,
                look=args.look,
                base_region=args.base_region,
                base_mode=args.base_mode,
            )
    except Exception as exc:
        print(f"scanlight-triplet-positive: {type(exc).__name__}: {exc}", file=__import__("sys").stderr)
        return 1

    print(json.dumps(_result_to_dict(result), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
