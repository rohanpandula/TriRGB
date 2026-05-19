#!/usr/bin/env python3
"""classify-arw.py — Detect which Scanlight LED illuminated an ARW frame.

For narrowband-RGB scanning, each shot is lit by exactly one of R/G/B
LEDs. The Sony Bayer color-filter array rejects out-of-band wavelengths
with extreme ratios at narrowband sources (~665nm R, ~525nm G, ~455nm B),
so:

  - In an R-lit shot: R channel collects ~all signal; G and B see ~noise.
  - In a G-lit shot: G channel dominates.
  - In a B-lit shot: B channel dominates.

This holds for ANY film frame — even pure cyan dye, even an orange-mask
Portra, even an empty frame. The Bayer rejection at narrowband wavelengths
is so high that scene content doesn't budge the ratio noticeably.

Use cases:
  - Diagnostic: "I have a folder of ARWs with no naming convention — which
    is which?" Run this on each file, get R/G/B classification.
  - Self-validation: "Does my orchestrator's filename tag (Frame001_R)
    match the actual channel content?" Run this to catch mis-wired LEDs,
    file-rename bugs, or out-of-order arrivals.
  - Future-M3 integration: batch-composite --verify-grouping uses this
    function to cross-check filename-claims against channel content
    before committing to a composite that might silently swap channels.

Usage:
  python3 scripts/classify-arw.py path/to/shot.ARW
  python3 scripts/classify-arw.py path/to/shot.ARW --json
  python3 scripts/classify-arw.py path/to/shot.ARW --threshold 5.0

Output (human):
  R-lit (red_dominance=18.3, green_dominance=0.4, blue_dominance=0.3)

Output (--json):
  {"file":"shot.ARW","classification":"R","red_dominance":18.3,
   "green_dominance":0.4,"blue_dominance":0.3,"confidence":"high"}

Exit codes:
  0 — classified as one of R/G/B with confidence at or above threshold
  1 — ambiguous (no channel's dominance ratio cleared the threshold)
  2 — bad args or could not open / decode the ARW
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np


# Default dominance threshold. At 665nm narrowband R on Sony's Bayer CFA,
# the typical red-to-other-channel ratio is >15. At 525nm G, ~12. At 455nm
# B, ~10. 5.0 is a generous floor — if any channel doesn't clear 5x its
# next-strongest competitor, the file likely wasn't lit by a clean
# narrowband source (white-light leak, no light, mixed exposure).
DEFAULT_DOMINANCE_THRESHOLD = 5.0


@dataclass
class Classification:
    """Result of inspecting one ARW's per-channel content."""
    file: str
    label: str  # "R" | "G" | "B" | "ambiguous"
    red_dominance: float
    green_dominance: float
    blue_dominance: float
    confidence: str  # "high" | "marginal" | "ambiguous"
    means: tuple[float, float, float]  # raw mean per (R, G, B) channel


def _load_demosaic(path: Path) -> np.ndarray:
    """Demosaic one ARW with the production DEMOSAIC_KWARGS.

    Returns an HxWx3 float array in 0..1 range. Lazy-imports rawpy via the
    rgb_composite package so unit tests can monkeypatch this function
    without pulling in the raw decoder.
    """
    sys.path.insert(
        0, str(Path(__file__).resolve().parent.parent / "phase2" / "rgb-composite")
    )
    from rgb_composite import demosaic_linear
    return demosaic_linear(path)


def channel_dominance(rgb: np.ndarray) -> tuple[float, float, float]:
    """Return (red, green, blue) dominance ratios.

    Each ratio is mean(this_channel) / max(mean(other_two_channels), eps).
    A ratio of 10 means "this channel is 10x brighter than the brightest
    of the other two". On narrowband-lit frames the dominant channel
    reliably exceeds 10x; on white-lit or mixed frames all three ratios
    sit near 1.0.
    """
    if rgb.ndim != 3 or rgb.shape[-1] != 3:
        raise ValueError(f"expected HxWx3 array, got shape {rgb.shape}")
    means = (
        float(np.mean(rgb[..., 0])),
        float(np.mean(rgb[..., 1])),
        float(np.mean(rgb[..., 2])),
    )
    eps = 1e-9
    r_dom = means[0] / max(max(means[1], means[2]), eps)
    g_dom = means[1] / max(max(means[0], means[2]), eps)
    b_dom = means[2] / max(max(means[0], means[1]), eps)
    return r_dom, g_dom, b_dom


def classify_channels(
    rgb: np.ndarray,
    file_label: str,
    threshold: float = DEFAULT_DOMINANCE_THRESHOLD,
) -> Classification:
    """Pick the dominant channel from a demosaiced array.

    Returns the channel label ("R" | "G" | "B") whose dominance ratio is
    highest. If no channel clears `threshold`, returns label="ambiguous"
    so the caller can decide what to do (warn, fail, fall back to time
    or filename grouping).
    """
    r_dom, g_dom, b_dom = channel_dominance(rgb)
    means = (
        float(np.mean(rgb[..., 0])),
        float(np.mean(rgb[..., 1])),
        float(np.mean(rgb[..., 2])),
    )
    best = max(r_dom, g_dom, b_dom)
    if best < threshold:
        return Classification(
            file=file_label,
            label="ambiguous",
            red_dominance=r_dom,
            green_dominance=g_dom,
            blue_dominance=b_dom,
            confidence="ambiguous",
            means=means,
        )
    label = "R" if best == r_dom else ("G" if best == g_dom else "B")
    confidence = "high" if best >= threshold * 2.0 else "marginal"
    return Classification(
        file=file_label,
        label=label,
        red_dominance=r_dom,
        green_dominance=g_dom,
        blue_dominance=b_dom,
        confidence=confidence,
        means=means,
    )


def classify_arw(path: Path, threshold: float = DEFAULT_DOMINANCE_THRESHOLD) -> Classification:
    """Demosaic an ARW from disk and classify it. Convenience wrapper."""
    rgb = _load_demosaic(path)
    return classify_channels(rgb, file_label=str(path), threshold=threshold)


def _print_human(c: Classification) -> None:
    print(
        f"{c.label}-lit "
        f"(red_dominance={c.red_dominance:.2f}, "
        f"green_dominance={c.green_dominance:.2f}, "
        f"blue_dominance={c.blue_dominance:.2f}, "
        f"confidence={c.confidence})"
    )


def _print_json(c: Classification) -> None:
    obj = {
        "file": c.file,
        "classification": c.label,
        "red_dominance": round(c.red_dominance, 4),
        "green_dominance": round(c.green_dominance, 4),
        "blue_dominance": round(c.blue_dominance, 4),
        "confidence": c.confidence,
        "means": {
            "r": round(c.means[0], 6),
            "g": round(c.means[1], 6),
            "b": round(c.means[2], 6),
        },
    }
    print(json.dumps(obj, sort_keys=True))


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="classify-arw",
        description="Detect which Scanlight LED (R/G/B) illuminated an ARW.",
    )
    parser.add_argument("arw", type=Path, help="Path to an .ARW file.")
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_DOMINANCE_THRESHOLD,
        help=(
            "Minimum dominance ratio for a positive classification. "
            f"Default {DEFAULT_DOMINANCE_THRESHOLD}. "
            "Lower if you have weak LEDs or heavy ND filtering."
        ),
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of human text.")
    args = parser.parse_args(argv)

    if not args.arw.exists():
        print(f"classify-arw: not found: {args.arw}", file=sys.stderr)
        return 2

    try:
        result = classify_arw(args.arw, threshold=args.threshold)
    except Exception as e:
        print(f"classify-arw: failed to decode {args.arw}: {e}", file=sys.stderr)
        return 2

    if args.json:
        _print_json(result)
    else:
        _print_human(result)

    return 0 if result.label != "ambiguous" else 1


if __name__ == "__main__":
    sys.exit(main())
