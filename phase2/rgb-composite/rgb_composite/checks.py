"""Numeric QA checks for the narrowband RGB triplet pipeline (Phase 13 R-28).

Three hardware-free, colorblind-safe, deterministic checks — each returns a
generic CheckResult(name, passed, deltas) that JSON round-trips losslessly via
the default JsonContract mixin.

  check_registration     — cv2.phaseCorrelate sub-pixel alignment (G vs R, B vs R)
  check_base_neutrality  — max pairwise base_rgb deviation
  check_frame_anomaly    — per-channel base_rgb + uniformity_cv deviation vs baseline

Channel convention (locked project-wide): R=0, G=1, B=2.
Channels are array indices only — no chroma-based or color-vision-dependent
logic anywhere (SC-4).

Test oracle note (Pitfall 2): the SC-1 unit test stacks the SAME scene 3x
then np.rolls one channel to guarantee correlated content and a known shift.
Real C-41 frames share cross-channel scene structure (edges, grain) so
production correlation is meaningful — but real-frame validation and threshold
tuning are deferred to M2 plug-in day. Do not overclaim beyond the synthetic
oracle scope.

No new dependencies: cv2 (4.13.0) + numpy (2.4.0) + stdlib only.
"""
from __future__ import annotations

import math

import cv2
import numpy as np

from c41_core.contracts import BaseRegionDescriptor, CheckResult


# ---------------------------------------------------------------------------
# check_registration
# ---------------------------------------------------------------------------

def check_registration(
    triplet: np.ndarray,
    threshold: float = 1.0,
    window: np.ndarray | None = None,
) -> CheckResult:
    """Sub-pixel registration check using cv2.phaseCorrelate.

    Computes G-vs-R and B-vs-R 2D cross-correlation on float32 slices of the
    HxWx3 triplet (R = channel-index-0 reference).  PASSes when the maximum
    pairwise shift magnitude < threshold.

    Default threshold=1.0 px (10x the ~0.1 px phaseCorrelate noise floor on
    realistic image sizes).  The np.roll test oracle uses integer shifts with
    NO Hanning window (Pitfall 4): np.roll is a circular shift and phaseCorrelate
    without a window recovers integer circular shifts exactly; adding a Hanning
    window introduces ~0.03–0.07 px error on circular content.

    Fail-closed guards run BEFORE any phaseCorrelate call (Pitfall 1):
      - non-ndarray → TypeError (avoids confusing C-level assertion errors)
      - ndim != 3 or shape[2] != 3 → ValueError (hard HxWx3 contract)
      - size == 0 → ValueError (Pitfall 5: phaseCorrelate on a 0-row array
        silently returns garbage instead of raising)
      - non-floating dtype → ValueError
      - any non-finite value → ValueError

    Parameters
    ----------
    triplet   : HxWx3 float array — the three co-registered channel images.
    threshold : PASS if max pair shift magnitude < this, pixels (default 1.0).
    window    : optional Hanning window (same HxW) for non-circular real frames;
                default None (no window — correct for circular np.roll oracle).

    Returns
    -------
    CheckResult with name="registration", passed bool, and deltas:
      g_vs_r_dx, g_vs_r_dy : G-vs-R shift in pixels (x=col, y=row).
      b_vs_r_dx, b_vs_r_dy : B-vs-R shift in pixels.
    Positive dx = G/B shifted RIGHT of R; positive dy = shifted BELOW R.
    Magnitude in deltas intentionally omitted — caller can compute if needed.
    """
    # --- Fail-closed guards (ALL before any cv2 call — Pitfall 1 & 5) ---
    if not isinstance(triplet, np.ndarray):
        raise TypeError(f"triplet must be np.ndarray, got {type(triplet).__name__}")
    if triplet.ndim != 3 or triplet.shape[2] != 3:
        raise ValueError(f"triplet must be HxWx3, got shape {triplet.shape}")
    if triplet.size == 0:
        raise ValueError("triplet must not be empty")  # Pitfall 5
    if not np.issubdtype(triplet.dtype, np.floating):
        raise ValueError(f"triplet must be float dtype, got {triplet.dtype}")
    if not np.all(np.isfinite(triplet)):
        raise ValueError("triplet contains non-finite values")

    # --- Extract 2D float32 slices (phaseCorrelate requires float32 or float64) ---
    ch0 = triplet[:, :, 0].astype(np.float32)   # R — reference channel
    ch1 = triplet[:, :, 1].astype(np.float32)   # G
    ch2 = triplet[:, :, 2].astype(np.float32)   # B

    # --- Cross-correlation (NO Hanning window by default — Pitfall 4) ---
    (g_dx, g_dy), _ = cv2.phaseCorrelate(ch0, ch1, window)
    (b_dx, b_dy), _ = cv2.phaseCorrelate(ch0, ch2, window)

    max_mag = max(math.hypot(g_dx, g_dy), math.hypot(b_dx, b_dy))
    return CheckResult(
        name="registration",
        passed=max_mag < threshold,
        deltas={
            "g_vs_r_dx": float(g_dx),
            "g_vs_r_dy": float(g_dy),
            "b_vs_r_dx": float(b_dx),
            "b_vs_r_dy": float(b_dy),
        },
    )


# ---------------------------------------------------------------------------
# check_base_neutrality
# ---------------------------------------------------------------------------

def check_base_neutrality(
    descriptor: BaseRegionDescriptor,
    tolerance: float = 500.0,
) -> CheckResult:
    """Pairwise base_rgb channel deviation check.

    PASSes when max(|R-G|, |R-B|, |G-B|) < tolerance raw counts.

    BaseRegionDescriptor.__post_init__ already validates base_rgb (finite,
    >= 0, exactly 3 elements), so no extra guards are needed here.

    Default tolerance=500.0 raw counts (~0.76% of the 16-bit range), matching
    the phase-13 research threshold rationale for C-41 base neutrality.

    Parameters
    ----------
    descriptor : BaseRegionDescriptor with base_rgb = (R, G, B) channel means.
    tolerance  : PASS if all pairwise deviations < this (default 500.0 counts).

    Returns
    -------
    CheckResult with name="base_neutrality", passed bool, and deltas:
      rg_dev : |R - G|
      rb_dev : |R - B|
      gb_dev : |G - B|
    """
    R, G, B = descriptor.base_rgb
    rg_dev = abs(R - G)
    rb_dev = abs(R - B)
    gb_dev = abs(G - B)
    return CheckResult(
        name="base_neutrality",
        passed=max(rg_dev, rb_dev, gb_dev) < tolerance,
        deltas={"rg_dev": rg_dev, "rb_dev": rb_dev, "gb_dev": gb_dev},
    )


# ---------------------------------------------------------------------------
# check_frame_anomaly
# ---------------------------------------------------------------------------

def check_frame_anomaly(
    frame_descriptor: BaseRegionDescriptor,
    roll_baseline_descriptor: BaseRegionDescriptor,
    base_dev_threshold: float = 500.0,
    cv_dev_threshold: float = 5.0,
) -> CheckResult:
    """Per-channel base_rgb + uniformity_cv deviation vs the roll baseline.

    PASSes only when ALL four deviations are within their respective thresholds
    (logical AND — Pitfall 6).  A frame with OK base means but a blown
    uniformity_cv (e.g. rebate region shifted onto the image body) still FAILs.
    Using OR or max would silently pass anomalous frames.

    Default thresholds:
      base_dev_threshold = 500.0 raw counts per channel (~0.76% of 16-bit)
      cv_dev_threshold   = 5.0 percentage points (uniformity_cv range: 0–100%)

    Parameters
    ----------
    frame_descriptor        : BaseRegionDescriptor for the current frame.
    roll_baseline_descriptor: BaseRegionDescriptor for the roll baseline.
    base_dev_threshold      : per-channel base_rgb deviation limit (counts).
    cv_dev_threshold        : uniformity_cv deviation limit (pp).

    Returns
    -------
    CheckResult with name="frame_anomaly", passed bool, and deltas:
      r_base_dev : |frame R - baseline R|
      g_base_dev : |frame G - baseline G|
      b_base_dev : |frame B - baseline B|
      cv_dev     : |frame uniformity_cv - baseline uniformity_cv|
    """
    r_dev = abs(frame_descriptor.base_rgb[0] - roll_baseline_descriptor.base_rgb[0])
    g_dev = abs(frame_descriptor.base_rgb[1] - roll_baseline_descriptor.base_rgb[1])
    b_dev = abs(frame_descriptor.base_rgb[2] - roll_baseline_descriptor.base_rgb[2])
    cv_dev = abs(frame_descriptor.uniformity_cv - roll_baseline_descriptor.uniformity_cv)

    # Logical AND (Pitfall 6) — NOT OR, NOT max:
    # ALL four deviations must be within threshold for PASS.
    passed = (
        r_dev < base_dev_threshold
        and g_dev < base_dev_threshold
        and b_dev < base_dev_threshold
        and cv_dev < cv_dev_threshold
    )
    return CheckResult(
        name="frame_anomaly",
        passed=passed,
        deltas={
            "r_base_dev": r_dev,
            "g_base_dev": g_dev,
            "b_base_dev": b_dev,
            "cv_dev": cv_dev,
        },
    )
