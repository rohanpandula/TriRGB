"""Tests for scripts/classify-arw.py.

The hard parts (rawpy decode, file I/O) are isolated in `_load_demosaic`,
which we monkeypatch. The classification math runs on synthetic 3-channel
arrays that simulate what each narrowband LED actually produces on the
Sony Bayer sensor.
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

# scripts/classify-arw.py is hyphen-named — load it via importlib.
_SCRIPT_PATH = Path(__file__).resolve().parent / "classify-arw.py"
_spec = importlib.util.spec_from_file_location("classify_arw", _SCRIPT_PATH)
classify_arw = importlib.util.module_from_spec(_spec)
sys.modules["classify_arw"] = classify_arw
_spec.loader.exec_module(classify_arw)


# ---------- synthetic-array fixtures ----------

def _r_lit_frame(h: int = 64, w: int = 64) -> np.ndarray:
    """Simulate an R-lit Bayer demosaic: strong R, near-noise G/B."""
    rng = np.random.default_rng(42)
    img = np.zeros((h, w, 3), dtype=np.float32)
    img[..., 0] = 0.5 + 0.05 * rng.standard_normal((h, w))  # strong red
    img[..., 1] = 0.02 + 0.005 * rng.standard_normal((h, w))  # ~noise
    img[..., 2] = 0.02 + 0.005 * rng.standard_normal((h, w))  # ~noise
    return np.clip(img, 0, 1)


def _g_lit_frame(h: int = 64, w: int = 64) -> np.ndarray:
    rng = np.random.default_rng(43)
    img = np.zeros((h, w, 3), dtype=np.float32)
    img[..., 0] = 0.02 + 0.005 * rng.standard_normal((h, w))
    img[..., 1] = 0.5 + 0.05 * rng.standard_normal((h, w))
    img[..., 2] = 0.02 + 0.005 * rng.standard_normal((h, w))
    return np.clip(img, 0, 1)


def _b_lit_frame(h: int = 64, w: int = 64) -> np.ndarray:
    rng = np.random.default_rng(44)
    img = np.zeros((h, w, 3), dtype=np.float32)
    img[..., 0] = 0.02 + 0.005 * rng.standard_normal((h, w))
    img[..., 1] = 0.02 + 0.005 * rng.standard_normal((h, w))
    img[..., 2] = 0.5 + 0.05 * rng.standard_normal((h, w))
    return np.clip(img, 0, 1)


def _white_lit_frame(h: int = 64, w: int = 64) -> np.ndarray:
    """White-light leak — all three channels roughly equal. Should fall
    out as 'ambiguous' since no channel dominates."""
    rng = np.random.default_rng(45)
    img = 0.4 + 0.05 * rng.standard_normal((h, w, 3)).astype(np.float32)
    return np.clip(img, 0, 1)


# ---------- core classification logic ----------

def test_r_lit_array_classified_as_R():
    c = classify_arw.classify_channels(_r_lit_frame(), file_label="r.ARW")
    assert c.label == "R"
    assert c.confidence == "high"
    assert c.red_dominance > 10
    assert c.green_dominance < 1
    assert c.blue_dominance < 1


def test_g_lit_array_classified_as_G():
    c = classify_arw.classify_channels(_g_lit_frame(), file_label="g.ARW")
    assert c.label == "G"
    assert c.confidence == "high"
    assert c.green_dominance > 10


def test_b_lit_array_classified_as_B():
    c = classify_arw.classify_channels(_b_lit_frame(), file_label="b.ARW")
    assert c.label == "B"
    assert c.confidence == "high"
    assert c.blue_dominance > 10


def test_white_lit_array_is_ambiguous():
    """A roughly-equal-channel frame (white light, no light, mixed exposure)
    should not be force-classified. The caller decides what to do."""
    c = classify_arw.classify_channels(_white_lit_frame(), file_label="w.ARW")
    assert c.label == "ambiguous"
    assert c.confidence == "ambiguous"
    # Dominance ratios should all sit near 1.0 (no clear winner)
    assert c.red_dominance < 2
    assert c.green_dominance < 2
    assert c.blue_dominance < 2


def test_threshold_tuneable():
    """A weak-LED rig (low dominance ratio) can still be classified if
    the operator lowers the threshold."""
    # Build a weakly-R-lit frame: R only ~3x stronger than G/B.
    rng = np.random.default_rng(46)
    img = np.zeros((32, 32, 3), dtype=np.float32)
    img[..., 0] = 0.3 + 0.02 * rng.standard_normal((32, 32))
    img[..., 1] = 0.1 + 0.02 * rng.standard_normal((32, 32))
    img[..., 2] = 0.1 + 0.02 * rng.standard_normal((32, 32))
    img = np.clip(img, 0, 1)

    # Default threshold (5.0) → ambiguous (R/G ratio is only ~3)
    c_strict = classify_arw.classify_channels(img, file_label="weak.ARW", threshold=5.0)
    assert c_strict.label == "ambiguous"

    # Loose threshold (2.0) → R-lit
    c_loose = classify_arw.classify_channels(img, file_label="weak.ARW", threshold=2.0)
    assert c_loose.label == "R"


# ---------- CLI / end-to-end via monkeypatched demosaic ----------

def test_cli_r_lit_exits_zero_with_human_output(capsys, tmp_path):
    fake_arw = tmp_path / "Frame001_R.ARW"
    fake_arw.write_bytes(b"")  # existence-only; demosaic is monkeypatched

    with patch.object(classify_arw, "_load_demosaic", return_value=_r_lit_frame()):
        rc = classify_arw.main([str(fake_arw)])

    assert rc == 0
    out = capsys.readouterr().out
    assert out.startswith("R-lit")
    assert "red_dominance" in out


def test_cli_white_lit_exits_one(capsys, tmp_path):
    fake_arw = tmp_path / "Frame001_W.ARW"
    fake_arw.write_bytes(b"")

    with patch.object(classify_arw, "_load_demosaic", return_value=_white_lit_frame()):
        rc = classify_arw.main([str(fake_arw)])

    # exit 1 = ambiguous (no clear LED winner). Caller can decide whether
    # to skip the frame, fall back to filename grouping, or fail loudly.
    assert rc == 1
    out = capsys.readouterr().out
    assert out.startswith("ambiguous-lit")


def test_cli_missing_file_exits_two(capsys, tmp_path):
    rc = classify_arw.main([str(tmp_path / "nonexistent.ARW")])
    assert rc == 2
    err = capsys.readouterr().err
    assert "not found" in err


def test_cli_json_output(capsys, tmp_path):
    fake_arw = tmp_path / "Frame001_G.ARW"
    fake_arw.write_bytes(b"")

    with patch.object(classify_arw, "_load_demosaic", return_value=_g_lit_frame()):
        rc = classify_arw.main([str(fake_arw), "--json"])

    assert rc == 0
    out = capsys.readouterr().out.strip()
    obj = json.loads(out)
    assert obj["classification"] == "G"
    assert obj["confidence"] == "high"
    assert obj["green_dominance"] > 10
    assert "means" in obj
    assert {"r", "g", "b"}.issubset(obj["means"].keys())


def test_dominance_rejects_non_3channel_array():
    """Programmer error — should raise, not silently return garbage."""
    bad = np.zeros((32, 32), dtype=np.float32)
    with pytest.raises(ValueError):
        classify_arw.channel_dominance(bad)
