#!/usr/bin/env python3
"""Quantitatively inspect a calibration triplet captured by `capture-calibration.sh`.

Reports per-channel:
  - Corner-to-center brightness falloff (%)
  - Saturation rate (% of pixels at or above clipping threshold)
  - Mean brightness as fraction of full scale
  - Implied tint drift across channels at the corners
  - Per-channel uniformity (% stddev of smoothed cal / mean)

The decision points are documented in `docs/optical_dry_run.md` § "Per-
channel narrowband vignette inspection". This script is the quantitative
half of that section — eyeballing the ARWs is still recommended for
finding non-symmetric problems the numbers won't catch.

Usage:
  python3 scripts/inspect-calibration.py <cal-dir>
  python3 scripts/inspect-calibration.py ~/.scanlight/calibration/2026-05-19

The cal-dir must contain R.ARW, G.ARW, B.ARW (case-insensitive on
extension) as produced by `scripts/capture-calibration.sh`. Exit code 0
if all three channels pass the "usable" thresholds; 1 if any channel
exceeds the "redo cal" thresholds; 2 if files are missing or can't be
decoded.

This script is pure Python + numpy + rawpy. It does NOT modify the cal
directory. Safe to run repeatedly.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np


# Decision thresholds. See optical_dry_run.md for the policy.
FALLOFF_USABLE_PCT = 15.0       # below this: FFC is nice-to-have
FALLOFF_REQUIRED_PCT = 30.0     # above this: optics problem, don't scan
SATURATION_BAD_PCT = 1.0        # above this: cal frame is over-exposed
TINT_DRIFT_BAD_PCT = 10.0       # above this: wavelength-dependent vignette is severe
TINT_DRIFT_FFC_PCT = 3.0        # above this: FFC is required, not optional
# Uniformity = 100 * std(smoothed_cal_channel) / mean — patchy frames
# (fingerprint on diffuser, drifted LED, partial obstruction) have high
# uniformity scores even with low radial falloff.
UNIFORMITY_CLEAN_PCT = 3.0      # below this: frame is acceptably uniform
UNIFORMITY_FAIL_PCT = 8.0       # above this: frame is patchy, do not use for FFC

# Patch sizes for center and corner sampling, as a fraction of the
# shorter image dimension. Center = ~10% of frame; corners = same size.
_PATCH_FRAC = 0.10

# Saturation defined as values >= this. Matches the FFC module's guard
# (`_SATURATION_THRESHOLD = 64000` in `rgb_composite/ffc.py`).
_SATURATION_VALUE = 64000


@dataclass(frozen=True)
class ChannelStats:
    """Numeric summary of one demosaiced calibration channel."""
    channel: str
    mean_value: float           # 0–65535, mean brightness
    center_value: float         # mean of center patch
    corner_value: float         # mean of the four corner patches
    falloff_pct: float          # 100 × (1 - corner/center)
    saturation_pct: float       # fraction of pixels >= _SATURATION_VALUE
    uniformity_pct: float = 0.0  # 100 * std(smoothed) / mean — patchy → high
    full_scale: int = 65535     # 65535 for uint16

    @property
    def mean_fraction(self) -> float:
        return self.mean_value / self.full_scale


def _load_demosaic(path: Path) -> np.ndarray:
    """Demosaic one cal ARW with the production DEMOSAIC_KWARGS."""
    # Lazy import — rawpy isn't required for tests that mock this out.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "phase2" / "rgb-composite"))
    from rgb_composite import demosaic_linear
    return demosaic_linear(path)


def uniformity_score(channel: np.ndarray) -> float:
    """Return 100 * std(smoothed) / mean for a single-channel cal frame.

    Uses the same box-filter kernel that the FFC pipeline uses
    (`_box_filter_2d` from `rgb_composite.ffc`, kernel size =
    max(3, int(min(h, w) * 0.05)) — matching `_SMOOTH_KERNEL_FRAC`).

    A perfectly uniform frame returns ~0%.  A frame with patchy local
    variation (fingerprint on diffuser, partial obstruction, drifted LED)
    returns a value proportional to the variation even when the radial
    falloff appears acceptable.  Thresholds: < UNIFORMITY_CLEAN_PCT (3%)
    → clean; 3-8% → borderline (FFC required); > UNIFORMITY_FAIL_PCT
    (8%) → reject.

    Args:
        channel: HxW numpy array (any numeric dtype).

    Returns:
        Uniformity score as a float percentage (0–100+).

    Raises:
        ValueError: if `channel` is not a 2-D array.
    """
    if channel.ndim != 2:
        raise ValueError(f"expected HxW array, got shape {channel.shape}")
    h, w = channel.shape
    # Kernel size mirrors production _SMOOTH_KERNEL_FRAC = 0.05 in ffc.py.
    # We hard-code 0.05 here rather than importing the private constant so
    # that this script has no hard import dependency on rgb_composite at
    # module load time (matching the lazy-import pattern used by
    # _load_demosaic above).
    _RGB_COMPOSITE_PATH = Path(__file__).resolve().parent.parent / "phase2" / "rgb-composite"
    sys.path.insert(0, str(_RGB_COMPOSITE_PATH))
    from rgb_composite.ffc import _box_filter_2d  # noqa: PLC0415
    kernel = max(3, int(min(h, w) * 0.05))
    smoothed = _box_filter_2d(channel, kernel)
    mean_val = float(np.mean(smoothed))
    return 100.0 * float(np.std(smoothed)) / max(mean_val, 1.0)


def _resolve_cal_files(cal_dir: Path) -> tuple[Path, Path, Path]:
    """Locate R/G/B cal files in `cal_dir`. Mirrors `rgb_composite.ffc._resolve_cal_files`."""
    if not cal_dir.is_dir():
        raise FileNotFoundError(f"not a directory: {cal_dir}")
    candidates = {p.name.upper(): p for p in cal_dir.iterdir() if p.is_file()}
    found: dict[str, Path] = {}
    for ch in ("R", "G", "B"):
        for ext in (".ARW", ".arw"):
            key = (ch + ext).upper()
            if key in candidates:
                found[ch] = candidates[key]
                break
    missing = [ch for ch in ("R", "G", "B") if ch not in found]
    if missing:
        raise FileNotFoundError(
            f"missing R.ARW / G.ARW / B.ARW in {cal_dir} (missing: {','.join(missing)})"
        )
    return found["R"], found["G"], found["B"]


def measure_channel(arr: np.ndarray, channel_label: str) -> ChannelStats:
    """Compute brightness/falloff/saturation stats for one channel of a demosaic.

    `arr` is HxW (single channel — the caller picks which one to measure).
    """
    if arr.ndim != 2:
        raise ValueError(f"expected HxW array, got shape {arr.shape}")
    h, w = arr.shape
    full_scale = int(np.iinfo(arr.dtype).max) if np.issubdtype(arr.dtype, np.integer) else 65535
    patch = max(8, int(min(h, w) * _PATCH_FRAC))

    # Center patch.
    cy, cx = h // 2, w // 2
    half = patch // 2
    center = arr[cy - half:cy + half, cx - half:cx + half]
    center_value = float(center.mean())

    # Four corner patches, averaged.
    corners = [
        arr[:patch, :patch],
        arr[:patch, w - patch:],
        arr[h - patch:, :patch],
        arr[h - patch:, w - patch:],
    ]
    corner_value = float(np.mean([c.mean() for c in corners]))

    falloff_pct = 100.0 * (1.0 - corner_value / max(center_value, 1.0))
    saturation_pct = 100.0 * float((arr >= _SATURATION_VALUE).mean())
    uniformity_pct_val = uniformity_score(arr)

    return ChannelStats(
        channel=channel_label,
        mean_value=float(arr.mean()),
        center_value=center_value,
        corner_value=corner_value,
        falloff_pct=falloff_pct,
        saturation_pct=saturation_pct,
        uniformity_pct=uniformity_pct_val,
        full_scale=full_scale,
    )


def inspect_triplet(cal_dir: Path) -> tuple[ChannelStats, ChannelStats, ChannelStats]:
    """Demosaic R/G/B cal frames, measure the matching channel of each.

    Per project convention (rgb-composite): R-lit cal → channel 0,
    G-lit cal → channel 1, B-lit cal → channel 2. The "matching channel"
    rule preserves correctness when adding crosstalk-rejecting downstream
    steps.
    """
    r_path, g_path, b_path = _resolve_cal_files(cal_dir)
    r_img = _load_demosaic(r_path)
    g_img = _load_demosaic(g_path)
    b_img = _load_demosaic(b_path)
    if not (r_img.shape == g_img.shape == b_img.shape):
        raise ValueError(
            f"cal frame shape mismatch: R={r_img.shape} G={g_img.shape} B={b_img.shape}"
        )
    return (
        measure_channel(r_img[..., 0], "R"),
        measure_channel(g_img[..., 1], "G"),
        measure_channel(b_img[..., 2], "B"),
    )


def classify(stats: tuple[ChannelStats, ChannelStats, ChannelStats]) -> tuple[str, int]:
    """Return (decision_message, exit_code) given per-channel stats.

    Decision tiers (looser → tighter): "CLEAN", "OK with FFC", "FAIL —
    setup problem; do not scan".

    Falloff sign convention: positive = dimmer corners than center
    (normal vignette), negative = brighter corners than center (hotspot).
    Either polarity beyond the threshold is a problem — a hotspot signals
    that the scanlight isn't centered under the carrier or the diffuser
    has a defect. We bound the magnitude both ways.
    """
    falloffs = [s.falloff_pct for s in stats]
    abs_falloffs = [abs(f) for f in falloffs]
    saturations = [s.saturation_pct for s in stats]
    max_falloff_abs = max(abs_falloffs)
    # Tint drift = range across the per-channel falloff values (signed,
    # so a hotspot in R + normal vignette in B still shows up as drift).
    tint_drift = max(falloffs) - min(falloffs)
    has_hotspot = any(f < -FALLOFF_USABLE_PCT for f in falloffs)
    max_uniformity = max(s.uniformity_pct for s in stats)
    worst_uniformity_channel = max(stats, key=lambda s: s.uniformity_pct).channel

    if any(sat > SATURATION_BAD_PCT for sat in saturations):
        return (
            "FAIL — at least one channel is over-exposed (>1% pixels saturated). "
            "Lower the scanlight level and recapture the calibration.",
            1,
        )
    if has_hotspot:
        offenders = ",".join(
            s.channel for s in stats if s.falloff_pct < -FALLOFF_USABLE_PCT
        )
        return (
            f"FAIL — hotspot detected in channel(s) {offenders} (corner brighter "
            "than center). The scanlight isn't centered under the carrier or the "
            "diffuser is uneven. Re-seat the scanlight + holder, then re-cal.",
            1,
        )
    if max_falloff_abs > FALLOFF_REQUIRED_PCT or tint_drift > TINT_DRIFT_BAD_PCT:
        return (
            f"FAIL — vignette too severe (max |falloff| {max_falloff_abs:.1f}%, "
            f"tint drift {tint_drift:.1f}%). Fix the optics — lift the holder "
            "5–15mm off the diffuser, re-center the lens over the gate, or "
            "re-seat the scanlight under the carrier — and re-cal.",
            1,
        )
    if max_uniformity > UNIFORMITY_FAIL_PCT:
        return (
            f"FAIL — channel {worst_uniformity_channel} uniformity {max_uniformity:.1f}% "
            f"exceeds {UNIFORMITY_FAIL_PCT}% threshold (patchy cal frame; likely a "
            "fingerprint or partial obstruction on the diffuser, or a drifted LED). "
            "Clean optics, re-seat the Scanlight, and re-capture the cal triplet.",
            1,
        )
    if (max_falloff_abs > FALLOFF_USABLE_PCT or tint_drift > TINT_DRIFT_FFC_PCT
            or max_uniformity > UNIFORMITY_CLEAN_PCT):
        return (
            f"OK with FFC — moderate vignette (max |falloff| {max_falloff_abs:.1f}%, "
            f"tint drift {tint_drift:.1f}%). Pass this cal dir to "
            "rgb-composite via --ffc-calibration on every composite.",
            0,
        )
    return (
        f"CLEAN — minimal vignette (max |falloff| {max_falloff_abs:.1f}%, "
        f"tint drift {tint_drift:.1f}%). FFC is nice-to-have, not required. "
        "You can scan without it.",
        0,
    )


def format_report(stats: tuple[ChannelStats, ChannelStats, ChannelStats]) -> str:
    """Pretty-print the per-channel stats as a fixed-width table."""
    lines = [
        "  ch | mean      | center     | corner     | falloff | saturated | uniform",
        "  ---|-----------|------------|------------|---------|-----------|--------",
    ]
    for s in stats:
        lines.append(
            f"  {s.channel:>2} | "
            f"{s.mean_value:9.0f} | "
            f"{s.center_value:10.0f} | "
            f"{s.corner_value:10.0f} | "
            f"{s.falloff_pct:6.1f}% | "
            f"{s.saturation_pct:7.2f}% | "
            f"{s.uniformity_pct:6.2f}%"
        )
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="inspect-calibration",
        description=(
            "Inspect a Scanlight FFC calibration triplet (R.ARW, G.ARW, "
            "B.ARW) and report per-channel vignette severity, saturation, "
            "and tint drift. Decides whether the cal is clean, needs FFC, "
            "or indicates a real optical problem."
        ),
    )
    p.add_argument("cal_dir", type=Path,
                   help="Calibration directory written by capture-calibration.sh")
    args = p.parse_args(argv)

    try:
        stats = inspect_triplet(args.cal_dir)
    except FileNotFoundError as e:
        print(f"inspect-calibration: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"inspect-calibration: {type(e).__name__}: {e}", file=sys.stderr)
        return 2

    print(f"Calibration directory: {args.cal_dir}")
    print()
    print(format_report(stats))
    print()
    message, rc = classify(stats)
    print(message)
    return rc


if __name__ == "__main__":
    sys.exit(main())
