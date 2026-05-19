"""Tests for the streaming CompositeWorker.

We don't run real rgb_composite here (no rawpy, no real ARWs). We inject a
ThreadPoolExecutor + a fake job function so the queue/poll/drain/failure-
isolation contract is tested deterministically, without spawning real
worker processes or needing the raw decoder.

(A ThreadPoolExecutor is used instead of the production ProcessPoolExecutor
because thread workers share the test process's memory — so the injected
fake job_fn is visible to them. With ProcessPoolExecutor on macOS spawn,
child processes re-import the original module and a monkeypatch/closure
would be invisible. Process isolation itself is a property of
ProcessPoolExecutor, well-tested by CPython; our job is the orchestration
logic, which behaves identically across executor types.)
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from triplet_capture import composite_worker as cw
from triplet_capture.composite_worker import CompositeWorker, CompositeResult


def _fake_job_success(r, g, b, out, fmt, ffc, model):
    """Simulate a successful composite by touching the output path."""
    p = Path(out)
    if fmt == "dng":
        p = p.with_suffix(".dng")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"FAKE-DNG")
    return (str(p), None)


def _fake_job_failure(r, g, b, out, fmt, ffc, model):
    """Simulate a composite that fails inside the worker."""
    return (None, "RuntimeError: simulated composite failure")


def _worker(out_dir, roll="Roll001", *, job=_fake_job_success, max_workers=2, **kw):
    """Build a CompositeWorker with an injected thread pool + fake job."""
    return CompositeWorker(
        out_dir,
        roll,
        executor=ThreadPoolExecutor(max_workers=max_workers),
        job_fn=job,
        **kw,
    )


def _make_triplet(tmp_path: Path, frame: int) -> dict[str, Path]:
    files = {}
    for ch in ("R", "G", "B"):
        p = tmp_path / f"Roll001_Frame{frame:03d}_{ch}.ARW"
        p.write_bytes(b"FAKE-ARW")
        files[ch] = p
    return files


# ---------- success path ----------

def test_submit_and_drain_produces_outputs(tmp_path):
    out_dir = tmp_path / "composites"
    worker = _worker(out_dir, output_format="dng")

    for frame in (1, 2, 3):
        worker.submit(frame, _make_triplet(tmp_path, frame))

    results = worker.drain(timeout=30)
    worker.shutdown(wait=True)

    assert len(results) == 3
    assert all(r.ok for r in results)
    for frame in (1, 2, 3):
        assert (out_dir / f"Roll001_Frame{frame:03d}.dng").exists()


def test_poll_returns_only_finished(tmp_path):
    worker = _worker(tmp_path / "composites")

    worker.submit(1, _make_triplet(tmp_path, 1))
    worker.submit(2, _make_triplet(tmp_path, 2))

    # Drain to force completion, then confirm poll() afterward is empty
    # (drain already collected everything).
    drained = worker.drain(timeout=30)
    assert len(drained) == 2
    assert worker.poll() == []
    assert worker.pending == 0
    worker.shutdown()


# ---------- failure isolation ----------

def test_failed_composite_does_not_block_others(tmp_path):
    # Frame 2 fails; frames 1 and 3 must still succeed.
    def _selective(r, g, b, out, fmt, ffc, model):
        if "Frame002" in r:
            return _fake_job_failure(r, g, b, out, fmt, ffc, model)
        return _fake_job_success(r, g, b, out, fmt, ffc, model)

    out_dir = tmp_path / "composites"
    worker = _worker(out_dir, job=_selective, output_format="dng")

    for frame in (1, 2, 3):
        worker.submit(frame, _make_triplet(tmp_path, frame))

    results = {r.frame_number: r for r in worker.drain(timeout=30)}
    worker.shutdown()

    assert results[1].ok
    assert results[3].ok
    assert not results[2].ok
    assert "simulated composite failure" in results[2].error
    # The two good frames still landed on disk.
    assert (out_dir / "Roll001_Frame001.dng").exists()
    assert (out_dir / "Roll001_Frame003.dng").exists()
    assert not (out_dir / "Roll001_Frame002.dng").exists()


# ---------- input validation ----------

def test_submit_requires_rgb_keys(tmp_path):
    worker = _worker(tmp_path / "composites", max_workers=1)
    with pytest.raises(ValueError, match="missing 'B'"):
        worker.submit(1, {"R": tmp_path / "r.ARW", "G": tmp_path / "g.ARW"})
    worker.shutdown()


def test_submit_after_close_raises(tmp_path):
    worker = _worker(tmp_path / "composites", max_workers=1)
    worker.shutdown()
    with pytest.raises(RuntimeError, match="closed"):
        worker.submit(1, _make_triplet(tmp_path, 1))


# ---------- output naming ----------

def test_output_path_naming(tmp_path):
    worker = _worker(tmp_path / "composites", roll="MyRoll", max_workers=1)
    p = worker._output_path(7)
    assert p.name == "MyRoll_Frame007.tif"
    worker.shutdown()


# ---------- context manager drains on clean exit ----------

def test_context_manager_drains(tmp_path):
    out_dir = tmp_path / "composites"
    with _worker(out_dir, output_format="dng") as worker:
        worker.submit(1, _make_triplet(tmp_path, 1))
        worker.submit(2, _make_triplet(tmp_path, 2))
    # On clean __exit__, drain() ran — both outputs exist.
    assert (out_dir / "Roll001_Frame001.dng").exists()
    assert (out_dir / "Roll001_Frame002.dng").exists()


# ---------- end-to-end with the real orchestrator hook ----------

def test_orchestrator_hook_fires_on_success(tmp_path):
    """The orchestrator's on_triplet_complete hook fires once per successful
    triplet with the right TripletResult — no real compositing involved."""
    from triplet_capture.orchestrator import Orchestrator, CaptureSettings

    class FakeScanlight:
        def set_color(self, **kw): pass
        def off(self): pass

    captured: list = []

    def runner(channel, out_path, timeout_s):
        # Sparse 70MB file: passes the orchestrator's plausible-RAW-size
        # check without actually writing 70MB to disk.
        with open(out_path, "wb") as f:
            f.truncate(70 * 1024 * 1024)
        return 0

    settings = CaptureSettings(
        roll_name="Roll001",
        frame_number=1,
        output_folder=tmp_path / "scans",
    )
    orch = Orchestrator(
        FakeScanlight(),
        settings,
        sony_capture_runner=runner,
        clock=lambda: 0.0,
        sleep=lambda s: None,
        on_triplet_complete=lambda result: captured.append(result),
    )

    res = orch.capture_triplet()
    assert res.success
    assert len(captured) == 1
    assert captured[0].frame_number == 1
    assert set(captured[0].files.keys()) == {"R", "G", "B"}


def test_orchestrator_hook_failure_does_not_abort_capture(tmp_path):
    """A throwing hook must not break the capture loop — the triplet still
    reports success."""
    from triplet_capture.orchestrator import Orchestrator, CaptureSettings

    class FakeScanlight:
        def set_color(self, **kw): pass
        def off(self): pass

    def runner(channel, out_path, timeout_s):
        with open(out_path, "wb") as f:
            f.truncate(70 * 1024 * 1024)
        return 0

    def boom(result):
        raise RuntimeError("composite worker exploded")

    settings = CaptureSettings(
        roll_name="Roll001", frame_number=1, output_folder=tmp_path / "scans"
    )
    orch = Orchestrator(
        FakeScanlight(),
        settings,
        sony_capture_runner=runner,
        clock=lambda: 0.0,
        sleep=lambda s: None,
        on_triplet_complete=boom,
    )

    res = orch.capture_triplet()
    # Capture still succeeds despite the hook throwing.
    assert res.success
    assert res.frame_number == 1
