#!/usr/bin/env python3
"""Quantitatively inspect a calibration triplet captured by `capture-calibration.sh`.

Reports per-channel:
  - Corner-to-center brightness falloff (%)
  - Saturation rate (% of pixels at or above clipping threshold)
  - Mean brightness as fraction of full scale
  - Implied tint drift across channels at the corners

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
    full_scale: int             # 65535 for uint16

    @property
    def mean_fraction(self) -> float:
        return self.mean_value / self.full_scale


def _load_demosaic(path: Path) -> np.ndarray:
    """Demosaic one cal ARW with the production DEMOSAIC_KWARGS."""
    # Lazy import — rawpy isn't required for tests that mock this out.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "phase2" / "rgb-composite"))
    from rgb_composite import demosaic_linear
    return demosaic_linear(path)


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

    return ChannelStats(
        channel=channel_label,
        mean_value=float(arr.mean()),
        center_value=center_value,
        corner_value=corner_value,
        falloff_pct=falloff_pct,
        saturation_pct=saturation_pct,
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
    if max_falloff_abs > FALLOFF_USABLE_PCT or tint_drift > TINT_DRIFT_FFC_PCT:
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
        "  ch | mean      | center     | corner     | falloff | saturated",
        "  ---|-----------|------------|------------|---------|----------",
    ]
    for s in stats:
        lines.append(
            f"  {s.channel:>2} | "
            f"{s.mean_value:9.0f} | "
            f"{s.center_value:10.0f} | "
            f"{s.corner_value:10.0f} | "
            f"{s.falloff_pct:6.1f}% | "
            f"{s.saturation_pct:7.2f}%"
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
