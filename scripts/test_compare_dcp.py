"""Tests for scripts/compare-dcp.py.

The six tests here cover the full control flow of compare-dcp.py without
ever actually running rgb-composite or parsing real DNG files:

  1. test_invokes_composite_twice_with_correct_camera_models
       Both invocations happen; model strings correct.
  2. test_missing_triplet_returns_2
       Empty dir → exit 2, error on stderr.
  3. test_subprocess_failure_returns_1_and_surfaces_stderr
       Subprocess returns non-zero → exit 1, stderr surfaced.
  4. test_partial_failure_surfaces_which_model_failed
       First call passes, second fails → exit 1, model B identified in stderr.
  5. test_report_written_to_input_dir_on_success
       Success path → report file written, contains DIFF and SAME rows.
  6. test_diff_tags_pure_function
       _diff_tags() pure-function unit test: SAME, DIFF, and one-sided keys.

Why subprocess is stubbed:
  compare-dcp.py shells out to rgb-composite which requires rawpy/libraw and
  real ARW files.  By monkeypatching subprocess.run (and read_linear_dng_tags)
  we get sub-second tests that run on any machine without hardware.

Run from the repo root:
  python3 -m pytest scripts/test_compare_dcp.py
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Module loader — mirrors test_inspect_calibration.py pattern
# ---------------------------------------------------------------------------
_SCRIPT_PATH = Path(__file__).parent / "compare-dcp.py"
_spec = importlib.util.spec_from_file_location("compare_dcp", _SCRIPT_PATH)
compare_dcp = importlib.util.module_from_spec(_spec)
sys.modules["compare_dcp"] = compare_dcp
_spec.loader.exec_module(compare_dcp)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _make_triplet(d: Path, prefix: str = "Roll_Frame001") -> None:
    """Write minimal stub R/G/B ARW files so _find_triplet can locate them."""
    for ch in ("R", "G", "B"):
        (d / f"{prefix}_{ch}.ARW").write_bytes(b"\x00")


def _fake_tags_a() -> dict:
    return {
        "UniqueCameraModel": compare_dcp.MODEL_SCANLIGHT,
        "ColorMatrix1": (1, 2, 3, 4, 5, 6, 7, 8, 9),
        "ProfileName": "Linear ProPhoto",
        "DNGVersion": b"\x01\x04\x00\x00",
        "PhotometricInterpretation": 34892,
    }


def _fake_tags_b() -> dict:
    tags = _fake_tags_a()
    tags["UniqueCameraModel"] = compare_dcp.MODEL_SONY
    return tags


# ---------------------------------------------------------------------------
# Test 1: both invocations with correct camera-model strings
# ---------------------------------------------------------------------------

def _make_fake_run(monkeypatch_or_None=None):
    """Return a fake subprocess.run that touches the --out file so os.path.getsize works."""
    def fake_run(args, **kwargs):
        # Find --out <path> in the args list and touch that file.
        try:
            out_idx = args.index("--out")
            out_path = Path(args[out_idx + 1])
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(b"\x00" * 100)
        except (ValueError, IndexError):
            pass
        return MagicMock(returncode=0, stdout="", stderr="")
    return MagicMock(side_effect=fake_run)


def test_invokes_composite_twice_with_correct_camera_models(tmp_path, monkeypatch):
    """Two subprocess.run calls happen with the two camera-model strings."""
    _make_triplet(tmp_path)

    mock_run = _make_fake_run()
    # compare-dcp does `import subprocess; subprocess.run(...)` so we patch the
    # subprocess module reference that compare_dcp holds.
    monkeypatch.setattr(compare_dcp.subprocess, "run", mock_run)
    # Stub the DNG reader so we don't try to parse the b"\x00" temp outputs.
    tag_calls = {"n": 0}

    def fake_read_tags(p):
        tag_calls["n"] += 1
        return _fake_tags_a() if tag_calls["n"] == 1 else _fake_tags_b()

    monkeypatch.setattr(compare_dcp, "read_linear_dng_tags", fake_read_tags)

    rc = compare_dcp.main([str(tmp_path)])
    assert mock_run.call_count == 2

    args_a = mock_run.call_args_list[0][0][0]
    args_b = mock_run.call_args_list[1][0][0]
    assert "Scanlight v4" in " ".join(args_a), f"Model A not in args: {args_a}"
    assert "Sony ILCE-7CR" in " ".join(args_b), f"Model B not in args: {args_b}"
    assert rc == 0


# ---------------------------------------------------------------------------
# Test 2: missing triplet → exit 2
# ---------------------------------------------------------------------------

def test_missing_triplet_returns_2(tmp_path, capsys):
    """Empty directory → exit 2, error mentions triplet/missing/not found."""
    empty = tmp_path / "empty"
    empty.mkdir()
    rc = compare_dcp.main([str(empty)])
    assert rc == 2
    err = capsys.readouterr().err.lower()
    assert "triplet" in err or "missing" in err or "not found" in err


# ---------------------------------------------------------------------------
# Test 3: subprocess failure → exit 1, stderr surfaced
# ---------------------------------------------------------------------------

def test_subprocess_failure_returns_1_and_surfaces_stderr(tmp_path, monkeypatch, capsys):
    """First subprocess call fails → exit 1, its stderr appears in our stderr."""
    _make_triplet(tmp_path)
    mock_run = MagicMock(return_value=MagicMock(
        returncode=1, stdout="", stderr="rawpy: bad file"
    ))
    monkeypatch.setattr(compare_dcp.subprocess, "run", mock_run)

    rc = compare_dcp.main([str(tmp_path)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "rawpy: bad file" in err


# ---------------------------------------------------------------------------
# Test 4: partial failure — first succeeds, second fails
# ---------------------------------------------------------------------------

def test_partial_failure_surfaces_which_model_failed(tmp_path, monkeypatch, capsys):
    """First call passes, second (Sony ILCE-7CR) fails → exit 1, stderr identifies model B."""
    _make_triplet(tmp_path)
    call_count = {"n": 0}

    def fake_run(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return MagicMock(returncode=0, stdout="", stderr="")
        return MagicMock(returncode=1, stdout="", stderr="oom in rgb-composite")

    monkeypatch.setattr(compare_dcp.subprocess, "run", fake_run)
    # DNG reader won't be called (second subprocess failed) but stub just in case.
    monkeypatch.setattr(compare_dcp, "read_linear_dng_tags", lambda p: _fake_tags_a())

    rc = compare_dcp.main([str(tmp_path)])
    assert rc == 1
    err = capsys.readouterr().err
    # Must surface context identifying model B or the underlying error.
    assert "Sony ILCE-7CR" in err or "oom in rgb-composite" in err


# ---------------------------------------------------------------------------
# Test 5: report written to input_dir on success
# ---------------------------------------------------------------------------

def test_report_written_to_input_dir_on_success(tmp_path, monkeypatch):
    """Success path → report file written, contains DIFF row (UniqueCameraModel)
    and SAME row (ColorMatrix1)."""
    _make_triplet(tmp_path)
    mock_run = _make_fake_run()
    monkeypatch.setattr(compare_dcp.subprocess, "run", mock_run)

    tag_calls = {"n": 0}

    def fake_read_tags(p):
        tag_calls["n"] += 1
        return _fake_tags_a() if tag_calls["n"] == 1 else _fake_tags_b()

    monkeypatch.setattr(compare_dcp, "read_linear_dng_tags", fake_read_tags)

    rc = compare_dcp.main([str(tmp_path)])
    assert rc == 0

    report = tmp_path / ".compare-dcp-report.txt"
    assert report.exists(), "report file not written to input_dir"
    body = report.read_text()

    assert "Scanlight v4" in body
    assert "Sony ILCE-7CR" in body
    # UniqueCameraModel differs → DIFF row; ColorMatrix1 same → SAME row.
    assert "DIFF" in body
    assert "SAME" in body


# ---------------------------------------------------------------------------
# Test 6: _diff_tags pure-function unit test
# ---------------------------------------------------------------------------

def test_diff_tags_handles_same_diff_and_missing_keys():
    """_diff_tags() produces correct SAME/DIFF rows; one-sided keys → DIFF."""
    tags_a = {
        "UniqueCameraModel": "A",
        "ColorMatrix1": (1, 2, 3),
        "OnlyA": "x",
    }
    tags_b = {
        "UniqueCameraModel": "B",
        "ColorMatrix1": (1, 2, 3),
        "OnlyB": "y",
    }
    rows = compare_dcp._diff_tags(tags_a, tags_b)
    # Map tag → result for easy assertion.
    result = {row[0]: row[-1] for row in rows}

    assert result["UniqueCameraModel"] == "DIFF"
    assert result["ColorMatrix1"] == "SAME"
    # Keys present in only one side should appear and be marked DIFF.
    assert "OnlyA" in result
    assert "OnlyB" in result
    assert result["OnlyA"] == "DIFF"
    assert result["OnlyB"] == "DIFF"
