"""Tests for `batch_composite.batch`.

We monkeypatch `rgb_composite.composite_triplet` at the module level so the
worker import inside `_composite_one` picks up our stub. To make the stub
shareable across the ProcessPool we pin `workers=1` (inline path) for the
correctness tests — that exercises the same `_composite_one` function plus
the discovery and skip logic but doesn't fork subprocesses (which can't see
monkeypatched modules anyway).
"""
from __future__ import annotations

from pathlib import Path

import pytest

import batch_composite.batch as bm
from batch_composite import FrameGroup, SkipReason, composite_roll, discover_frames


def touch(p: Path, content: bytes = b"") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return p


def make_triplet(d: Path, roll: str, frame: int) -> tuple[Path, Path, Path]:
    r = touch(d / f"{roll}_Frame{frame:03d}_R.ARW")
    g = touch(d / f"{roll}_Frame{frame:03d}_G.ARW")
    b = touch(d / f"{roll}_Frame{frame:03d}_B.ARW")
    return r, g, b


@pytest.fixture
def stub_composite(monkeypatch):
    """Replace the import target inside the worker with a stub that
    writes a marker file to the output path and returns it."""
    calls: list[tuple[Path, Path, Path, Path]] = []

    def fake_composite_triplet(r, g, b, out, **kwargs):
        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"<fake tiff>")
        calls.append((Path(r), Path(g), Path(b), out))
        return out

    # The worker imports rgb_composite lazily. Monkeypatch sys.modules
    # so when `_composite_one` does `from rgb_composite import composite_triplet`
    # it picks up our stub.
    import sys, types
    fake_mod = types.ModuleType("rgb_composite")
    fake_mod.composite_triplet = fake_composite_triplet
    monkeypatch.setitem(sys.modules, "rgb_composite", fake_mod)
    return calls


# ---------- discovery ----------

def test_discover_groups_by_frame_number(tmp_path):
    make_triplet(tmp_path, "Roll001", 1)
    make_triplet(tmp_path, "Roll001", 2)
    # Stray non-matching file
    touch(tmp_path / "scan_log.jsonl", b"")

    groups = discover_frames(tmp_path)
    assert len(groups) == 2
    assert groups[0].frame_number == 1
    assert groups[1].frame_number == 2
    assert all(g.complete for g in groups)


def test_discover_handles_missing_channels(tmp_path):
    # Frame 3 is missing the G channel
    touch(tmp_path / "Roll001_Frame003_R.ARW")
    touch(tmp_path / "Roll001_Frame003_B.ARW")
    groups = discover_frames(tmp_path)
    assert len(groups) == 1
    g = groups[0]
    assert not g.complete
    assert g.missing_channels == ["G"]


def test_discover_sorts_numerically_not_lexically(tmp_path):
    # If we sorted lexically, "Frame010" would come before "Frame002"
    for n in (10, 2, 1):
        make_triplet(tmp_path, "Roll001", n)
    groups = discover_frames(tmp_path)
    assert [g.frame_number for g in groups] == [1, 2, 10]


def test_discover_ignores_non_matching_files(tmp_path):
    make_triplet(tmp_path, "Roll001", 1)
    touch(tmp_path / "stray.txt")
    touch(tmp_path / "Roll001_Frame001.jpg")  # wrong extension
    touch(tmp_path / "Roll001_Frame001_X.ARW")  # bad channel
    groups = discover_frames(tmp_path)
    assert len(groups) == 1


def test_discover_does_not_merge_across_roll_names(tmp_path):
    """Two different roll names with the same frame number must be treated
    as separate (incomplete) frames, never silently merged."""
    touch(tmp_path / "RollA_Frame001_R.ARW")
    touch(tmp_path / "RollA_Frame001_G.ARW")
    touch(tmp_path / "RollB_Frame001_B.ARW")
    groups = discover_frames(tmp_path)
    # Two groups, each incomplete — RollA missing B, RollB missing R+G.
    assert len(groups) == 2
    rolls = {g.roll for g in groups}
    assert rolls == {"RollA", "RollB"}
    for g in groups:
        assert not g.complete


# ---------- composite_roll ----------

def test_composite_roll_runs_each_complete_frame(stub_composite, tmp_path):
    make_triplet(tmp_path, "Roll001", 1)
    make_triplet(tmp_path, "Roll001", 2)
    make_triplet(tmp_path, "Roll001", 3)

    result = composite_roll(tmp_path, workers=1)
    assert len(result.composited) == 3
    assert len(result.skipped) == 0
    assert len(result.failed) == 0

    # Outputs land in the composites/ subdir with the right names
    expected = {
        tmp_path / "composites" / f"Roll001_Frame{n:03d}.tif" for n in (1, 2, 3)
    }
    assert set(result.composited) == expected
    for p in expected:
        assert p.exists()


def test_composite_roll_skips_incomplete_frames(stub_composite, tmp_path):
    # Frame 1: complete. Frame 2: missing B.
    make_triplet(tmp_path, "Roll001", 1)
    touch(tmp_path / "Roll001_Frame002_R.ARW")
    touch(tmp_path / "Roll001_Frame002_G.ARW")

    result = composite_roll(tmp_path, workers=1)
    assert len(result.composited) == 1
    assert len(result.skipped) == 1
    skipped_group, reason = result.skipped[0]
    assert reason is SkipReason.MISSING_CHANNEL
    assert skipped_group.frame_number == 2


def test_composite_roll_skips_existing_outputs_by_default(stub_composite, tmp_path):
    make_triplet(tmp_path, "Roll001", 1)
    composites = tmp_path / "composites"
    composites.mkdir()
    (composites / "Roll001_Frame001.tif").write_bytes(b"existing")

    result = composite_roll(tmp_path, workers=1)
    assert len(result.composited) == 0
    assert len(result.skipped) == 1
    _, reason = result.skipped[0]
    assert reason is SkipReason.OUTPUT_EXISTS
    # The existing file is untouched
    assert (composites / "Roll001_Frame001.tif").read_bytes() == b"existing"


def test_composite_roll_overwrite_re_runs(stub_composite, tmp_path):
    make_triplet(tmp_path, "Roll001", 1)
    composites = tmp_path / "composites"
    composites.mkdir()
    (composites / "Roll001_Frame001.tif").write_bytes(b"old")

    result = composite_roll(tmp_path, workers=1, overwrite=True)
    assert len(result.composited) == 1
    assert (composites / "Roll001_Frame001.tif").read_bytes() == b"<fake tiff>"


def test_composite_roll_reports_failures(monkeypatch, tmp_path):
    make_triplet(tmp_path, "Roll001", 1)
    make_triplet(tmp_path, "Roll001", 2)

    def explodes(r, g, b, out, **_kwargs):
        raise RuntimeError("simulated rawpy failure")

    import sys, types
    fake_mod = types.ModuleType("rgb_composite")
    fake_mod.composite_triplet = explodes
    monkeypatch.setitem(sys.modules, "rgb_composite", fake_mod)

    result = composite_roll(tmp_path, workers=1)
    assert len(result.composited) == 0
    assert len(result.failed) == 2
    for _g, msg in result.failed:
        assert "simulated rawpy failure" in msg


def test_composite_roll_rejects_non_directory(tmp_path):
    p = tmp_path / "not_a_dir.txt"
    p.write_text("nope")
    with pytest.raises(NotADirectoryError):
        composite_roll(p)


# ---------- CLI ----------

def test_cli_summary_output(stub_composite, tmp_path, capsys):
    make_triplet(tmp_path, "Roll001", 1)
    make_triplet(tmp_path, "Roll001", 2)
    # Incomplete frame:
    touch(tmp_path / "Roll001_Frame003_R.ARW")

    rc = bm.main([str(tmp_path), "--workers", "1"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "composited: 2" in out
    assert "skipped: 1" in out
    assert "failed: 0" in out


def test_default_worker_count_is_capped(monkeypatch, stub_composite, tmp_path):
    """Confirm we don't fan out to all cores by default — peak memory bound."""
    make_triplet(tmp_path, "Roll001", 1)

    seen_max_workers = []

    class FakePool:
        def __init__(self, max_workers=None):
            seen_max_workers.append(max_workers)
        def submit(self, fn, *args, **kwargs):
            class F:
                def __init__(self, r): self._r = r
                def result(self): return self._r
            return F(fn(*args, **kwargs))
        def __enter__(self): return self
        def __exit__(self, *a): return None

    monkeypatch.setattr(bm, "ProcessPoolExecutor", FakePool)
    monkeypatch.setattr(bm, "as_completed", lambda futs: list(futs.keys()))
    monkeypatch.setattr(bm.os, "cpu_count", lambda: 64)  # pretend we're on a beefy box

    composite_roll(tmp_path)  # workers=None → should pick the cap, not 64
    assert seen_max_workers == [bm._DEFAULT_WORKER_CAP]


def test_cli_nonzero_on_failure(monkeypatch, tmp_path, capsys):
    make_triplet(tmp_path, "Roll001", 1)

    def explodes(r, g, b, out, **_kwargs):
        raise RuntimeError("boom")

    import sys, types
    fake_mod = types.ModuleType("rgb_composite")
    fake_mod.composite_triplet = explodes
    monkeypatch.setitem(sys.modules, "rgb_composite", fake_mod)

    rc = bm.main([str(tmp_path), "--workers", "1"])
    assert rc == 1
