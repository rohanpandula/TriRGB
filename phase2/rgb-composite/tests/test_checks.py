"""Tests for rgb_composite.checks — SC-1..SC-4 numeric QA checks (Phase 13 R-28).

SC-1: check_registration — aligned PASS, np.roll-shifted G FAIL with recovered shift
SC-2: check_base_neutrality — neutral PASS, diverged FAIL
SC-3: check_frame_anomaly — ok PASS, bad FAIL, blown-CV-only FAIL (AND rule / Pitfall 6)
SC-4: no hue/saturation/perceptual color-name logic in checks.py (grep oracle)
SC-5: CheckResult JSON round-trip via rgb_composite re-export (cross-check)

Test oracle note (Pitfall 2): make_c41_negative has DECORRELATED per-channel content
(R mean ~8930, G mean ~12097 — different scene statistics). Direct R-vs-G phaseCorrelate
on the raw fixture gives garbage (resp~0.05). The SC-1 oracle stacks the SAME scene 3x
then np.rolls one channel to guarantee correlated content and a measurable, known shift.
"""
from __future__ import annotations

import subprocess
import sys

import numpy as np
import pytest

from c41_core.contracts import BaseRegionDescriptor, CheckResult
from c41_core.fixtures import make_c41_negative
from rgb_composite.checks import (
    check_base_neutrality,
    check_frame_anomaly,
    check_registration,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_brd(base_rgb: tuple[float, float, float], cv: float = 2.0) -> BaseRegionDescriptor:
    """Return a minimal valid BaseRegionDescriptor for neutrality/anomaly tests."""
    return BaseRegionDescriptor(
        x=0, y=0, w=10, h=10,
        base_rgb=base_rgb,
        uniformity_cv=cv,
        source="auto",
    )


def _make_aligned_triplet() -> tuple[np.ndarray, np.ndarray]:
    """Return (triplet, scene) for the aligned SC-1 oracle.

    Stack the same R-channel scene 3x so all channels share identical content.
    channel index: R=0, G=1, B=2 (locked project convention).
    """
    img = make_c41_negative(128, 192, seed=42)
    scene = img[:, :, 0].astype(np.float32)
    triplet = np.stack([scene, scene, scene], axis=-1)
    return triplet, scene


# ---------------------------------------------------------------------------
# SC-1: check_registration — aligned PASS, shifted FAIL + recovered shift
# ---------------------------------------------------------------------------

def test_registration_aligned_passes():
    """SC-1: aligned triplet (same scene 3x) → check_registration passes."""
    triplet, scene = _make_aligned_triplet()
    result = check_registration(triplet, threshold=1.0)
    assert result.passed is True, "aligned triplet must pass"
    assert result.name == "registration"
    assert abs(result.deltas["g_vs_r_dx"]) < 0.05, "aligned: dx must be near 0"
    assert abs(result.deltas["g_vs_r_dy"]) < 0.05, "aligned: dy must be near 0"


def test_registration_shifted_fails_with_recovered_shift():
    """SC-1: np.roll-shifted G channel → FAIL, recovered shift within 0.05 px.

    N_y=5, N_x=3: roll G channel by 5 rows (axis=0) and 3 cols (axis=1).
    phaseCorrelate with NO Hanning window recovers the integer roll exactly
    (circular shift ↔ circular correlation — Pitfall 4).
    Stack SAME scene 3x, roll ONE channel (Pitfall 2 — raw fixture R-vs-G is garbage).
    """
    triplet, scene = _make_aligned_triplet()
    N_y, N_x = 5, 3
    g_shifted = np.roll(np.roll(scene, N_y, axis=0), N_x, axis=1)
    # Replace G channel (index 1) with the shifted version
    triplet_shifted = np.stack([scene, g_shifted, scene], axis=-1)
    result = check_registration(triplet_shifted, threshold=1.0)
    assert not result.passed, "shifted G must fail"
    assert abs(result.deltas["g_vs_r_dx"] - N_x) < 0.05, (
        f"recovered dx={result.deltas['g_vs_r_dx']:.4f} should be near {N_x}"
    )
    assert abs(result.deltas["g_vs_r_dy"] - N_y) < 0.05, (
        f"recovered dy={result.deltas['g_vs_r_dy']:.4f} should be near {N_y}"
    )


# ---------------------------------------------------------------------------
# SC-2: check_base_neutrality — neutral PASS, diverged FAIL
# ---------------------------------------------------------------------------

def test_base_neutrality_neutral_passes():
    """SC-2: R=G=B → passed, all pairwise deviations 0.0."""
    descriptor = _make_brd(base_rgb=(5000.0, 5000.0, 5000.0))
    result = check_base_neutrality(descriptor)
    assert result.passed is True
    assert result.name == "base_neutrality"
    assert result.deltas["rg_dev"] == 0.0
    assert result.deltas["rb_dev"] == 0.0
    assert result.deltas["gb_dev"] == 0.0


def test_base_neutrality_diverged_fails():
    """SC-2: diverged base_rgb → NOT passed, correct pairwise deltas."""
    descriptor = _make_brd(base_rgb=(1000.0, 2000.0, 500.0))
    result = check_base_neutrality(descriptor)
    assert not result.passed
    # R=1000, G=2000, B=500
    assert abs(result.deltas["rg_dev"] - 1000.0) < 1e-6   # |R-G|
    assert abs(result.deltas["rb_dev"] - 500.0) < 1e-6    # |R-B|
    assert abs(result.deltas["gb_dev"] - 1500.0) < 1e-6   # |G-B|


# ---------------------------------------------------------------------------
# SC-3: check_frame_anomaly — ok PASS, bad FAIL, blown-CV-only FAIL (AND rule)
# ---------------------------------------------------------------------------

def test_frame_anomaly_ok_passes():
    """SC-3: frame within all thresholds vs baseline → passed."""
    # Baseline from make_brd default: base=(8930,12097,2952), cv=2.0
    baseline = _make_brd(base_rgb=(8930.0, 12097.0, 2952.0), cv=2.0)
    # Frame: small deviations well within 500 raw counts / 5 pp
    frame = _make_brd(base_rgb=(8980.0, 12067.0, 2972.0), cv=2.1)
    result = check_frame_anomaly(frame, baseline)
    assert result.passed is True
    assert result.name == "frame_anomaly"


def test_frame_anomaly_bad_fails():
    """SC-3: frame with large base AND cv deviation → NOT passed."""
    baseline = _make_brd(base_rgb=(8930.0, 12097.0, 2952.0), cv=2.0)
    # R deviates >500, cv deviates >5 pp
    frame = _make_brd(base_rgb=(9930.0, 10597.0, 3452.0), cv=8.5)
    result = check_frame_anomaly(frame, baseline)
    assert not result.passed
    # r_base_dev = |9930 - 8930| = 1000
    assert abs(result.deltas["r_base_dev"] - 1000.0) < 1e-6
    # cv_dev = |8.5 - 2.0| = 6.5
    assert abs(result.deltas["cv_dev"] - 6.5) < 1e-6


def test_frame_anomaly_blown_cv_only_fails():
    """SC-3 / Pitfall 6: OK base means but blown uniformity_cv → still FAILS (AND rule).

    A logical OR would pass this frame — prove the AND is enforced.
    baseline cv=2.0, frame cv=9.0 → cv_dev=7.0 > cv_dev_threshold=5.0
    base_rgb identical → all base devs = 0.0 < base_dev_threshold=500.0
    PASS requires ALL four within threshold, so cv_dev violation → FAIL.
    """
    baseline = _make_brd(base_rgb=(8930.0, 12097.0, 2952.0), cv=2.0)
    frame = _make_brd(base_rgb=(8930.0, 12097.0, 2952.0), cv=9.0)  # same base, blown cv
    result = check_frame_anomaly(frame, baseline)
    assert not result.passed, (
        "blown cv_dev must FAIL even when base means are within tolerance (AND rule / Pitfall 6)"
    )
    assert result.deltas["r_base_dev"] == 0.0
    assert result.deltas["g_base_dev"] == 0.0
    assert result.deltas["b_base_dev"] == 0.0
    assert abs(result.deltas["cv_dev"] - 7.0) < 1e-6


# ---------------------------------------------------------------------------
# Guard rejection: non-HxWx3 and empty arrays (Pitfall 1 & 5)
# ---------------------------------------------------------------------------

def test_registration_rejects_3d_violations():
    """Pitfall 1 & 5: malformed triplet is rejected BEFORE phaseCorrelate.

    Three cases:
      - 2D array (ndim != 3) → hits the ndim guard
      - HxWx2 array (shape[2] != 3) → hits the channel-count guard
      - valid-shape-but-empty np.zeros((0,5,3)) → hits the size==0 guard (Pitfall 5)
        phaseCorrelate silently returns garbage on a 0-row array; the size guard must
        fire BEFORE any cv2 call.
    """
    scene = make_c41_negative(32, 48, seed=7)[:, :, 0].astype(np.float32)

    # Case 1: 2D array
    with pytest.raises(ValueError):
        check_registration(scene)  # ndim=2, not 3

    # Case 2: HxWx2 (wrong channel count)
    two_ch = np.stack([scene, scene], axis=-1)
    with pytest.raises(ValueError):
        check_registration(two_ch)

    # Case 3: valid shape (HxWx3) but size==0 — the actual Pitfall 5 guard
    empty = np.zeros((0, 5, 3), dtype=np.float32)
    with pytest.raises(ValueError):
        check_registration(empty)


def test_registration_rejects_non_ndarray():
    """Pitfall 1: non-ndarray input raises TypeError before any numpy call."""
    with pytest.raises(TypeError):
        check_registration([[1.0, 2.0, 3.0]])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# SC-5 cross-check: CheckResult re-exported from rgb_composite round-trips
# ---------------------------------------------------------------------------

def test_check_result_roundtrip_via_rgb_composite():
    """SC-5 cross-check: CheckResult re-exported from rgb_composite is the same type.

    Confirms the re-export in rgb_composite/__init__.py is wired correctly and
    that from_json(to_json()) produces an equal object.
    """
    from rgb_composite import CheckResult as CheckResultFromPkg

    # Use check_base_neutrality to produce a real result
    descriptor = _make_brd(base_rgb=(5000.0, 5000.0, 5000.0))
    result = check_base_neutrality(descriptor)

    # Confirm it IS a CheckResult (the re-export points to the real class)
    assert isinstance(result, CheckResultFromPkg)

    # Round-trip
    restored = CheckResultFromPkg.from_json(result.to_json())
    assert restored == result
    assert isinstance(restored.deltas, dict)


# ---------------------------------------------------------------------------
# SC-4 grep oracle: no color-name / perceptual logic in checks.py
# ---------------------------------------------------------------------------

def test_sc4_no_perceptual_color_logic():
    """SC-4: checks.py must contain NO hue/saturation/perceptual/color-name logic.

    Channels are array indices R=0/G=1/B=2 only — no color-vision-dependent code.
    """
    import os
    checks_path = os.path.join(
        os.path.dirname(__file__),
        "..", "rgb_composite", "checks.py",
    )
    checks_path = os.path.normpath(checks_path)
    result = subprocess.run(
        ["grep", "-niE", r"hue|saturation|perceptual|colou?r[- ]name", checks_path],
        capture_output=True, text=True,
    )
    assert result.returncode == 1, (  # 1 = no match (clean); 0 = SC-4 violation; 2 = grep error
        f"SC-4 violation or grep error (rc={result.returncode}): {result.stdout or result.stderr}"
    )
