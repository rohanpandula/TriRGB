"""Tests for GET /api/composite-status endpoint.

Covers:
- disabled path (no composite_worker): returns {enabled: false}
- enabled+empty path (worker present, no jobs submitted): returns {enabled: true, pending: 0, results: []}
- enabled+results path (worker present, job completed): returns history with entries;
  second GET on same app instance returns the same accumulated history (not cleared).
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from triplet_capture.app import create_app
from triplet_capture.composite_worker import CompositeWorker
from triplet_capture.orchestrator import CaptureSettings, Orchestrator


# ---------- helpers copied verbatim from test_orchestrator.py ----------


class FakeScanlight:
    """Minimal duck-type covering what the orchestrator uses."""
    def __init__(self):
        self.calls: list[tuple] = []

    def set_color(self, r=0, g=0, b=0, w=0, save=False):
        self.calls.append(("set_color", r, g, b, w, save))

    def off(self):
        self.calls.append(("off",))


def make_runner(success_size: int = 70 * 1024 * 1024):
    """Returns a (runner, calls) pair. The runner writes a fake RAW of
    the given size and returns 0."""
    calls: list[tuple[str, Path, int]] = []

    def runner(channel: str, out_path: Path, timeout_s: int) -> int:
        calls.append((channel, out_path, timeout_s))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"\x00" * success_size)
        return 0

    return runner, calls


@pytest.fixture
def settings(tmp_path):
    return CaptureSettings(
        roll_name="Roll001",
        frame_number=1,
        output_folder=tmp_path,
        level_r=200,
        level_g=180,
        level_b=160,
        settle_ms=0,  # no sleep in tests
    )


def _zero_sleep(_seconds: float) -> None:
    pass


# ---------- fake job function for CompositeWorker ----------


def _fake_job_fn(r, g, b, out, fmt, ffc, model):
    """Synchronous fake job that writes a placeholder output file.

    Signature matches _composite_job: (r_path, g_path, b_path, out_path,
    output_format, ffc_calibration_dir, dng_camera_model) ->
    tuple[str|None, str|None].
    Returns (output_path_str, None) on success.
    """
    p = Path(out)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"FAKE")
    return (str(p), None)


# ---------- tests ----------


def test_composite_status_disabled(settings):
    """When create_app() is called without composite_worker, the endpoint
    returns {enabled: false} regardless of any query."""
    light = FakeScanlight()
    runner, _ = make_runner()
    orch = Orchestrator(light, settings, sony_capture_runner=runner, sleep=_zero_sleep)

    # No composite_worker — uses the default=None path
    app = create_app(orch)
    client = app.test_client()

    r = client.get("/api/composite-status")
    assert r.status_code == 200
    assert r.get_json() == {"enabled": False}


def test_composite_status_enabled_empty(settings, tmp_path):
    """Worker present but no jobs submitted: endpoint returns enabled=True,
    pending=0, results=[]."""
    light = FakeScanlight()
    runner, _ = make_runner()
    orch = Orchestrator(light, settings, sony_capture_runner=runner, sleep=_zero_sleep)

    worker = CompositeWorker(
        tmp_path / "composites",
        "Roll001",
        executor=ThreadPoolExecutor(max_workers=1),
        job_fn=_fake_job_fn,
    )

    app = create_app(orch, composite_worker=worker)
    client = app.test_client()

    r = client.get("/api/composite-status")
    assert r.status_code == 200
    body = r.get_json()
    assert body["enabled"] is True
    assert body["pending"] == 0
    assert body["results"] == []


def test_composite_status_enabled_with_results(settings, tmp_path):
    """Worker present, one job submitted and completed: endpoint returns
    the result entry. A second GET on the same app instance returns the
    same accumulated history (not cleared between calls — the accumulator
    is the Nyquist check for CompositeWorker.poll() drain-and-forget)."""
    light = FakeScanlight()
    runner, _ = make_runner()
    orch = Orchestrator(light, settings, sony_capture_runner=runner, sleep=_zero_sleep)

    worker = CompositeWorker(
        tmp_path / "composites",
        "Roll001",
        executor=ThreadPoolExecutor(max_workers=1),
        job_fn=_fake_job_fn,
    )

    # Create fake source files so submit() finds them (job_fn doesn't actually
    # read them, but submit() validates that R/G/B keys are present).
    fake_r = tmp_path / "R.ARW"
    fake_g = tmp_path / "G.ARW"
    fake_b = tmp_path / "B.ARW"
    fake_r.write_bytes(b"R")
    fake_g.write_bytes(b"G")
    fake_b.write_bytes(b"B")

    worker.submit(1, {"R": fake_r, "G": fake_g, "B": fake_b})

    # Poll until the background ThreadPoolExecutor job completes (up to 5s).
    # A fixed sleep(0.2) is a flake risk under heavy CI load; a deterministic
    # poll on worker.pending avoids the race.
    deadline = time.monotonic() + 5.0
    while worker.pending > 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert worker.pending == 0, "composite job did not complete within 5 seconds"

    app = create_app(orch, composite_worker=worker)
    client = app.test_client()

    # First GET — should drain the completed job into history
    r1 = client.get("/api/composite-status")
    assert r1.status_code == 200
    first_body = r1.get_json()
    assert first_body["enabled"] is True
    assert isinstance(first_body["pending"], int)
    assert len(first_body["results"]) == 1
    assert first_body["results"][0]["status"] in ("done", "failed")
    assert "frame_number" in first_body["results"][0]

    # Second GET on the SAME client/app instance — history must be accumulated,
    # not cleared. poll() returns [] the second time (drain-and-forget), but the
    # accumulator must preserve the prior results.
    r2 = client.get("/api/composite-status")
    assert r2.status_code == 200
    second_body = r2.get_json()
    assert len(second_body["results"]) == len(first_body["results"])
