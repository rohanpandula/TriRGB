"""Auto-positive rendering for an already-captured RGB-lit triplet.

This module is the bridge between the operator-friendly workflow ("pick the
three R/G/B files") and the existing narrowband RGB composite/inversion code.
It accepts three RAW/TIFF inputs in any order, reads which channel each file is
from its filename (an R/G/B or red/green/blue suffix — never from image color,
so the assignment stays deterministic and colorblind-safe per NFR-11), keeps
only the matching sensor channel from each exposure, then renders a positive
TIFF using the same density-based inversion used by the scan pipeline.
"""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

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
)


CHANNEL_NAMES = ("R", "G", "B")
TIFF_SUFFIXES = {".tif", ".tiff"}
RAW_SUFFIXES = {
    ".arw", ".raw", ".dng", ".nef", ".cr2", ".cr3", ".raf", ".rw2", ".orf",
}
LOOK_SETTINGS: dict[str, dict[str, float]] = {
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


# Filename tokens that name a channel. Single letter or full color word, so
# both the canonical scan naming (Frame001_R.ARW) and friendly names
# (scan-green.tif) work. This is the ONLY thing channel assignment looks at —
# never image color (NFR-11: the operator is colorblind; no code branches on
# the R/G/B color content of a frame).
_CHANNEL_TOKENS: dict[str, str] = {
    "r": "R", "red": "R",
    "g": "G", "green": "G",
    "b": "B", "blue": "B",
}


def _channel_from_filename(path: Path) -> str | None:
    """Return 'R'/'G'/'B' from the final filename token, or None if unknown.

    The channel must be the LAST ``_ - . space``-delimited token of the stem
    (e.g. ``Frame001_R``, ``a-green``, ``B``). Embedded matches (``BlueRidge``)
    never count — only the trailing token — so the read is unambiguous.
    """
    tokens = [t for t in re.split(r"[ _.\-]+", Path(path).stem) if t]
    if not tokens:
        return None
    return _CHANNEL_TOKENS.get(tokens[-1].lower())


def detect_rgb_assignment(paths: Sequence[Path]) -> list[AssignedFile]:
    """Assign three input paths to R/G/B from their FILENAMES (no color reading).

    Each filename's final token must name its channel — a single letter
    ``R``/``G``/``B`` or the word ``red``/``green``/``blue`` (case-insensitive),
    delimited by ``_ - . space`` (e.g. ``Frame001_R.ARW``, ``scan-green.tif``,
    ``B.ARW``). Assignment is therefore deterministic and makes no color-content
    decision (NFR-11). Ambiguous, duplicate, or missing channel roles raise an
    actionable ``TripletPositiveError`` instead of guessing — a wrong guess
    would silently swap channels in the rendered positive.
    """
    if len(paths) != 3:
        raise TripletPositiveError(f"expected exactly 3 inputs, got {len(paths)}")

    by_channel: dict[str, Path] = {}
    for raw_path in paths:
        path = Path(raw_path)
        channel = _channel_from_filename(path)
        if channel is None:
            raise TripletPositiveError(
                f"{path.name}: cannot tell which channel this is from the filename. "
                "Name each file so its last part is the channel — R/G/B or "
                "red/green/blue (e.g. Frame001_R.ARW, scan-green.tif, B.ARW). "
                "The positive renderer never guesses channels from image color."
            )
        if channel in by_channel:
            raise TripletPositiveError(
                f"two files map to channel {channel}: "
                f"{by_channel[channel].name} and {path.name}. "
                "Provide exactly one R, one G, and one B file."
            )
        by_channel[channel] = path

    missing = [ch for ch in CHANNEL_NAMES if ch not in by_channel]
    if missing:
        raise TripletPositiveError(
            f"missing channel file(s): {', '.join(missing)}. "
            "Provide one file per channel, named R/G/B (or red/green/blue)."
        )

    # score/confidence are kept for the report/UI contract; assignment is by
    # filename, so confidence is a constant 1.0 (a deterministic, explicit role)
    # rather than a measured color ratio.
    return [
        AssignedFile(
            channel=channel,
            channel_index=index,
            path=str(by_channel[channel]),
            score=1.0,
            confidence=1.0,
        )
        for index, channel in enumerate(CHANNEL_NAMES)
    ]


def build_composite(assignments: Sequence[AssignedFile]) -> np.ndarray:
    """Build a channel-isolated RGB negative composite from detected files."""
    channels: list[np.ndarray] = []
    expected_shape: tuple[int, int] | None = None
    for assignment in sorted(assignments, key=lambda item: item.channel_index):
        img = load_linear_rgb(Path(assignment.path))
        shape = img.shape[:2]
        if expected_shape is None:
            expected_shape = shape
        elif shape != expected_shape:
            raise TripletPositiveError(
                "input dimensions do not match: "
                f"expected {expected_shape}, got {shape} for {Path(assignment.path).name}"
            )
        channels.append(img[..., assignment.channel_index])
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


def apply_render_look(positive: np.ndarray, *, curve_amount: float) -> np.ndarray:
    """Apply a simple luminance-safe S-curve to a rendered positive."""
    amount = max(0.0, min(float(curve_amount), 1.0))
    if amount <= 0.0:
        return positive
    work = positive.astype(np.float32) / 65535.0
    # Smoothstep is monotonic, keeps 0/1 pinned, and steepens the midtones.
    curved = work * work * (3.0 - 2.0 * work)
    work = work * (1.0 - amount) + curved * amount
    np.clip(work, 0.0, 1.0, out=work)
    return np.rint(work * 65535.0).astype(np.uint16)


def render_triplet_preview(
    paths: Sequence[Path],
    preview_path: Path,
    *,
    max_dimension: int = 1600,
    look: str = "standard",
    patch_size: int = 256,
) -> TripletPreviewResult:
    """Render a small positive preview for visual base-patch selection."""
    preview_path.parent.mkdir(parents=True, exist_ok=True)
    clean_paths = [Path(path).expanduser().resolve() for path in paths]
    for path in clean_paths:
        if not path.exists():
            raise TripletPositiveError(f"input not found: {path}")

    assignments = detect_rgb_assignment(clean_paths)
    composite = build_composite(assignments)
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
    )
    positive = apply_render_look(positive, curve_amount=look_settings["curve"])

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

    assignments = detect_rgb_assignment(clean_paths)
    composite = build_composite(assignments)
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
    positive, meta = auto_positive_from_composite(
        composite,
        descriptor,
        display_black_percentile=look_settings["black"],
        display_white_percentile=look_settings["white"],
        tone_gamma=effective_gamma,
    )
    positive = apply_render_look(positive, curve_amount=look_settings["curve"])
    meta["look"] = look_key
    meta["look_curve_amount"] = look_settings["curve"]

    composite_path = out_dir / f"{stem}_negative-composite.tif"
    positive_path = out_dir / f"{stem}_positive.tif"
    report_path = out_dir / f"{stem}_report.json"

    tifffile.imwrite(
        composite_path,
        composite,
        photometric="rgb",
        description="channel-isolated RGB negative composite from imported triplet",
        metadata=None,
    )
    tifffile.imwrite(
        positive_path,
        positive,
        photometric="rgb",
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
    args = parser.parse_args(argv)

    try:
        if args.preview_out is not None:
            result = render_triplet_preview(
                _parse_paths(args.inputs),
                Path(args.preview_out).expanduser(),
                max_dimension=args.preview_max,
                look=args.look,
                patch_size=args.base_patch,
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
            )
    except Exception as exc:
        print(f"scanlight-triplet-positive: {type(exc).__name__}: {exc}", file=__import__("sys").stderr)
        return 1

    print(json.dumps(_result_to_dict(result), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
