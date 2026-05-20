"""Tests for scripts/inspect-calibration.py.

The script's `_load_demosaic` is the only non-pure-math step, and it's
monkeypatched in these tests. Everything else (patch sampling, falloff
computation, classification tiers) is pure numpy and runnable without
hardware.

Run from the repo root:
  python3 -m pytest scripts/test_inspect_calibration.py
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pytest


# Load the inspect-calibration.py module by file path (since its name has
# a hyphen and `import scripts.inspect_calibration` doesn't work cleanly).
_SCRIPT_PATH = Path(__file__).parent / "inspect-calibration.py"
_spec = importlib.util.spec_from_file_location("inspect_calibration", _SCRIPT_PATH)
inspect_calibration = importlib.util.module_from_spec(_spec)
sys.modules["inspect_calibration"] = inspect_calibration
_spec.loader.exec_module(inspect_calibration)


H, W = 256, 384  # large enough to give the 10% patch real area


def _vignetted(center: int, corner: int) -> np.ndarray:
    """Build an HxW uint16 cal channel with a radial falloff from
    `center` at the middle to `corner` at the four corners."""
    yy, xx = np.indices((H, W))
    cy, cx = (H - 1) / 2.0, (W - 1) / 2.0
    r = np.sqrt((yy - cy) ** 2 + (xx - cx) ** 2) / max(cy, cx)
    # Linear interpolation along r in [0, 1]
    val = center + (corner - center) * np.clip(r, 0.0, 1.0)
    return val.astype(np.uint16)


# ---------- measure_channel ----------

def test_measure_uniform_zero_falloff():
    arr = np.full((H, W), 40000, dtype=np.uint16)
    s = inspect_calibration.measure_channel(arr, "R")
    assert s.channel == "R"
    assert abs(s.falloff_pct) < 0.5
    assert s.saturation_pct == 0.0
    assert abs(s.mean_value - 40000) < 1


def test_measure_vignetted_reports_falloff():
    """50 000 center → 25 000 corner ≈ 50% falloff."""
    arr = _vignetted(center=50000, corner=25000)
    s = inspect_calibration.measure_channel(arr, "G")
    # Allow some slack — the corner patch is the four 10%-corners
    # averaged, not just the very-corner pixel. Expect ~40-50%.
    assert 35.0 < s.falloff_pct < 55.0
    assert s.saturation_pct == 0.0


def test_measure_saturation_detection():
    """Any channel with >1% pixels at full scale should report > 1% saturation."""
    arr = np.full((H, W), 50000, dtype=np.uint16)
    # Make 5% of pixels saturated
    n_sat = int(0.05 * H * W)
    flat = arr.flatten()
    flat[:n_sat] = 65535
    arr = flat.reshape(H, W)
    s = inspect_calibration.measure_channel(arr, "B")
    assert s.saturation_pct == pytest.approx(5.0, abs=0.5)


def test_measure_rejects_non_2d():
    with pytest.raises(ValueError, match="HxW"):
        inspect_calibration.measure_channel(np.zeros((H, W, 3), dtype=np.uint16), "R")


# ---------- classify ----------

def _stat(channel: str, falloff: float, saturation: float = 0.0, uniformity: float = 0.0):
    """Convenience to build a ChannelStats with only the fields classify() reads."""
    return inspect_calibration.ChannelStats(
        channel=channel,
        mean_value=40000.0,
        center_value=50000.0,
        corner_value=50000.0 * (1.0 - falloff / 100.0),
        falloff_pct=falloff,
        saturation_pct=saturation,
        uniformity_pct=uniformity,
        full_scale=65535,
    )


def test_classify_clean():
    """All channels < 15% falloff, drift < 3% → CLEAN."""
    stats = (_stat("R", 5.0), _stat("G", 6.0), _stat("B", 7.0))
    msg, rc = inspect_calibration.classify(stats)
    assert rc == 0
    assert "CLEAN" in msg


def test_classify_ffc_required_due_to_falloff():
    """Falloff between 15 and 30% → FFC required."""
    stats = (_stat("R", 20.0), _stat("G", 22.0), _stat("B", 21.0))
    msg, rc = inspect_calibration.classify(stats)
    assert rc == 0
    assert "OK with FFC" in msg


def test_classify_ffc_required_due_to_tint_drift():
    """Even with low max falloff, drift > 3% triggers the FFC-required tier."""
    stats = (_stat("R", 5.0), _stat("G", 6.0), _stat("B", 12.0))  # drift = 7
    msg, rc = inspect_calibration.classify(stats)
    assert rc == 0
    assert "OK with FFC" in msg


def test_classify_setup_problem_severe_falloff():
    """Any channel > 30% falloff → setup problem."""
    stats = (_stat("R", 35.0), _stat("G", 22.0), _stat("B", 21.0))
    msg, rc = inspect_calibration.classify(stats)
    assert rc == 1
    assert "FAIL" in msg


def test_classify_setup_problem_severe_tint_drift():
    """Tint drift > 10% → setup problem."""
    stats = (_stat("R", 5.0), _stat("G", 6.0), _stat("B", 20.0))  # drift = 15
    msg, rc = inspect_calibration.classify(stats)
    assert rc == 1
    assert "FAIL" in msg


def test_classify_saturation_overrides_other_metrics():
    """A saturated cal frame is unusable even if vignette is fine."""
    stats = (
        _stat("R", 5.0, saturation=2.0),  # >1% saturated
        _stat("G", 5.0),
        _stat("B", 5.0),
    )
    msg, rc = inspect_calibration.classify(stats)
    assert rc == 1
    assert "over-exposed" in msg


# ---------- main / file resolution ----------

def test_main_missing_dir_returns_2(tmp_path, capsys):
    rc = inspect_calibration.main([str(tmp_path / "nonexistent")])
    assert rc == 2
    err = capsys.readouterr().err
    assert "not a directory" in err or "missing" in err.lower()


def test_main_incomplete_cal_dir_returns_2(tmp_path, capsys):
    """Only R.ARW present; G and B missing → exit 2 with helpful message."""
    cal = tmp_path / "cal"
    cal.mkdir()
    (cal / "R.ARW").write_bytes(b"\x00")
    rc = inspect_calibration.main([str(cal)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "missing" in err.lower()


def test_main_happy_path_clean(tmp_path, monkeypatch, capsys):
    """Stub demosaic to return uniform arrays for all three channels.
    Result should be CLEAN, exit 0, with a printed table."""
    cal = tmp_path / "cal"
    cal.mkdir()
    for name in ("R.ARW", "G.ARW", "B.ARW"):
        (cal / name).write_bytes(b"\x00")

    def fake_demosaic(path):
        # Each cal's matching channel reads ~uniform; the others (crosstalk)
        # don't affect this script because it only measures the matching
        # channel via measure_channel(arr[..., n], "X").
        img = np.zeros((H, W, 3), dtype=np.uint16)
        ch = {"R": 0, "G": 1, "B": 2}[Path(path).stem.upper()]
        img[..., ch] = 40000
        return img

    monkeypatch.setattr(inspect_calibration, "_load_demosaic", fake_demosaic)

    rc = inspect_calibration.main([str(cal)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "CLEAN" in out
    # Per-channel table should list all three rows
    assert "  R |" in out
    assert "  G |" in out
    assert "  B |" in out


def test_main_setup_problem_returns_1(tmp_path, monkeypatch, capsys):
    """Stub demosaic with severe vignette → exit 1, FAIL message."""
    cal = tmp_path / "cal"
    cal.mkdir()
    for name in ("R.ARW", "G.ARW", "B.ARW"):
        (cal / name).write_bytes(b"\x00")

    def fake_demosaic(path):
        img = np.zeros((H, W, 3), dtype=np.uint16)
        ch = {"R": 0, "G": 1, "B": 2}[Path(path).stem.upper()]
        # Heavy vignette: center 50k, corner 10k → ~80% falloff
        img[..., ch] = _vignetted(center=50000, corner=10000)
        return img

    monkeypatch.setattr(inspect_calibration, "_load_demosaic", fake_demosaic)

    rc = inspect_calibration.main([str(cal)])
    assert rc == 1
    out = capsys.readouterr().out
    assert "FAIL" in out


# ---------- codex review additions ----------

def test_classify_detects_hotspot():
    """Corners BRIGHTER than center → must be flagged as a setup problem,
    not silently classified as clean. Per codex review."""
    # Build stats with NEGATIVE falloff (hotspot) — corner is 25% brighter
    stats = (
        inspect_calibration.ChannelStats(
            channel="R", mean_value=40000.0,
            center_value=40000.0, corner_value=50000.0,
            falloff_pct=-25.0,  # negative = hotspot
            saturation_pct=0.0, full_scale=65535,
        ),
        _stat("G", 5.0),
        _stat("B", 5.0),
    )
    msg, rc = inspect_calibration.classify(stats)
    assert rc == 1
    assert "hotspot" in msg.lower()


def test_classify_uniform_mild_hotspot_passes():
    """A uniform mild hotspot across all channels (corners slightly
    brighter, no per-channel drift) shouldn't trip the hotspot detector
    — only > FALLOFF_USABLE_PCT (15%) magnitude qualifies."""
    stats = tuple(
        inspect_calibration.ChannelStats(
            channel=ch, mean_value=40000.0,
            center_value=40000.0, corner_value=42000.0,
            falloff_pct=-5.0,         # uniform mild hotspot
            saturation_pct=0.0, full_scale=65535,
        )
        for ch in ("R", "G", "B")
    )
    msg, rc = inspect_calibration.classify(stats)
    # |falloff|=5 < 15, drift=0 → CLEAN, no hotspot warning
    assert rc == 0
    assert "hotspot" not in msg.lower()


def test_classify_severe_falloff_either_polarity_fails():
    """Whether the corner is much darker OR much brighter, >30% magnitude
    is a setup problem."""
    # Negative side
    stats_neg = (
        inspect_calibration.ChannelStats(
            channel="R", mean_value=40000.0,
            center_value=40000.0, corner_value=55000.0,
            falloff_pct=-35.0,
            saturation_pct=0.0, full_scale=65535,
        ),
        _stat("G", 5.0),
        _stat("B", 5.0),
    )
    msg_neg, rc_neg = inspect_calibration.classify(stats_neg)
    assert rc_neg == 1


# ---------- uniformity ----------

def test_uniformity_score_uniform_field_near_zero():
    """A perfectly uniform array has near-zero uniformity score."""
    arr = np.full((H, W), 40000, dtype=np.uint16)
    score = inspect_calibration.uniformity_score(arr)
    assert score < 0.5


def test_uniformity_score_patchy_field_nonzero():
    """A patchy field (sinusoidal variation) has non-zero uniformity score."""
    base = np.full((H, W), 40000.0, dtype=np.float32)
    # Add ~15% sinusoidal patchiness to make it cross the 3% boundary
    yy, xx = np.indices((H, W))
    patchy = base + 6000.0 * np.sin(yy / 20.0) * np.cos(xx / 20.0)
    arr = patchy.clip(0, 65535).astype(np.uint16)
    score = inspect_calibration.uniformity_score(arr)
    assert score > 3.0


def test_classify_uniformity_fail_exit_1():
    """All three channels uniformity > 8% → exit 1, FAIL message."""
    stats = (
        _stat("R", 5.0, uniformity=12.0),
        _stat("G", 5.0, uniformity=10.0),
        _stat("B", 5.0, uniformity=9.5),
    )
    msg, rc = inspect_calibration.classify(stats)
    assert rc == 1
    assert "FAIL" in msg
    # Either "uniformity" or "non-uniform" — the message wording
    # confirms the gate that fired
    assert "uniform" in msg.lower()


def test_classify_uniformity_warning_tier_exit_0():
    """One channel uniformity in 3-8% band → OK with FFC, exit 0."""
    stats = (
        _stat("R", 5.0, uniformity=5.0),  # in 3-8 warning band
        _stat("G", 5.0, uniformity=1.0),
        _stat("B", 5.0, uniformity=1.0),
    )
    msg, rc = inspect_calibration.classify(stats)
    assert rc == 0
    assert "OK with FFC" in msg


def test_classify_uniformity_independent_of_falloff():
    """Falloff and tint drift clean, but uniformity > 8% → still exit 1.

    This proves uniformity is an INDEPENDENT gate. If anyone refactors
    classify() to couple them, this test fails loudly.
    """
    stats = (
        _stat("R", 5.0, uniformity=15.0),  # falloff clean, uniformity FAIL
        _stat("G", 5.0, uniformity=0.5),
        _stat("B", 5.0, uniformity=0.5),
    )
    msg, rc = inspect_calibration.classify(stats)
    assert rc == 1
    assert "FAIL" in msg


# ---------- --json output ----------

def test_main_json_output_clean(tmp_path, monkeypatch, capsys):
    """--json flag emits a valid JSON object; uniform arrays → rc 0."""
    cal = tmp_path / "cal"
    cal.mkdir()
    for name in ("R.ARW", "G.ARW", "B.ARW"):
        (cal / name).write_bytes(b"\x00")

    def fake_demosaic(path):
        img = np.zeros((H, W, 3), dtype=np.uint16)
        ch = {"R": 0, "G": 1, "B": 2}[Path(path).stem.upper()]
        img[..., ch] = 40000
        return img

    monkeypatch.setattr(inspect_calibration, "_load_demosaic", fake_demosaic)
    rc = inspect_calibration.main([str(cal), "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert set(data["channels"].keys()) == {"R", "G", "B"}
    for ch_data in data["channels"].values():
        assert "falloff_pct" in ch_data
        assert "uniformity_pct" in ch_data
        assert ch_data["verdict"] in ("clean", "acceptable", "fail")
    assert data["overall"] in ("clean", "acceptable", "fail")


def test_main_json_shape_has_required_keys(tmp_path, monkeypatch, capsys):
    """Structural contract test: top-level and per-channel keys are all present.

    This is the contract CalibrationView's JSONDecoder depends on — if any
    key name changes, this test breaks before the Swift side breaks.
    """
    cal = tmp_path / "cal"
    cal.mkdir()
    for name in ("R.ARW", "G.ARW", "B.ARW"):
        (cal / name).write_bytes(b"\x00")

    def fake_demosaic(path):
        img = np.zeros((H, W, 3), dtype=np.uint16)
        ch = {"R": 0, "G": 1, "B": 2}[Path(path).stem.upper()]
        img[..., ch] = 40000
        return img

    monkeypatch.setattr(inspect_calibration, "_load_demosaic", fake_demosaic)
    inspect_calibration.main([str(cal), "--json"])
    out = capsys.readouterr().out
    data = json.loads(out)

    # Top-level keys
    assert "channels" in data
    assert "overall" in data

    # Per-channel keys — exactly the three required keys
    for ch in ("R", "G", "B"):
        ch_data = data["channels"][ch]
        assert set(ch_data.keys()) == {"falloff_pct", "uniformity_pct", "verdict"}


def test_json_overall_matches_classify_for_tint_drift(tmp_path, monkeypatch, capsys):
    """WR-01 regression: JSON overall must equal classify() even when tint drift
    triggers a FAIL but no individual channel exceeds per-channel fail thresholds.

    Example: R=2%, G=2%, B=20% falloff — each channel verdict is "clean" or
    "acceptable" individually, but classify() returns FAIL (rc=1) because
    tint_drift = 18% > TINT_DRIFT_BAD_PCT (10%). The JSON overall must be "fail".
    """
    cal = tmp_path / "cal"
    cal.mkdir()
    for name in ("R.ARW", "G.ARW", "B.ARW"):
        (cal / name).write_bytes(b"\x00")

    def fake_demosaic_tint_drift(path):
        """R center=40000,corner~39200 (~2% falloff); G similar; B center=40000,corner~32000 (~20% falloff)."""
        img = np.zeros((H, W, 3), dtype=np.uint16)
        stem = Path(path).stem.upper()
        ch = {"R": 0, "G": 1, "B": 2}[stem]
        if stem == "B":
            # ~20% falloff — individually "acceptable" but causes tint drift ~18%
            img[..., ch] = _vignetted(center=40000, corner=32000)
        else:
            # ~2% falloff — individually "clean"
            img[..., ch] = _vignetted(center=40000, corner=39200)
        return img

    monkeypatch.setattr(inspect_calibration, "_load_demosaic", fake_demosaic_tint_drift)

    rc = inspect_calibration.main([str(cal), "--json"])
    out = capsys.readouterr().out
    data = json.loads(out)

    # classify() returns rc=1 for tint drift > 10%; JSON overall must agree.
    assert rc == 1, f"Expected rc=1 for severe tint drift, got rc={rc}"
    assert data["overall"] == "fail", (
        f"JSON overall must be 'fail' when classify() returns rc=1 (tint drift), "
        f"got '{data['overall']}'. Per-channel: {data['channels']}"
    )

    # Verify the individual per-channel verdicts are NOT all "fail" — this confirms
    # the FAIL is coming from classify()'s tint-drift gate, not individual channels.
    per_channel_verdicts = [v["verdict"] for v in data["channels"].values()]
    assert "fail" not in per_channel_verdicts, (
        f"No individual channel should be 'fail' in this tint-drift scenario, "
        f"got {per_channel_verdicts}"
    )
