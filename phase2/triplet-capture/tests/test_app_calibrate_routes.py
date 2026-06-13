"""Flask test-client tests for the three calibration routes.

POST /api/calibrate/exposure
POST /api/calibrate/ffc
POST /api/calibrate/checks

All tests are hardware-free: a FakeScanlight + stub runner + injected
demosaic (synthetic HxWx3 uint16) so rawpy is never imported.  The
SCALE-stub demosaic_factory mirrors test_calibrate_exposure.py so the
bisection converges deterministically.

These tests import the three routes from triplet_capture.app.create_app —
they are RED until Task 2 registers the routes.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import numpy as np
import pytest

from triplet_capture.app import create_app
from triplet_capture.orchestrator import CaptureSettings, Orchestrator


# ---------------------------------------------------------------------------
# Stubs (verbatim from test_composite_status.py + test_calibrate_exposure.py)
# ---------------------------------------------------------------------------

class FakeScanlight:
    """Minimal duck-type covering what the orchestrator uses."""
    def __init__(self):
        self.calls: list[tuple] = []

    def set_color(self, r=0, g=0, b=0, w=0, save=False):
        self.calls.append(("set_color", r, g, b, w, save))

    def off(self):
        self.calls.append(("off",))


def make_runner(success_size: int = 70 * 1024 * 1024):
    """Returns a (runner, calls) pair. Runner writes a fake RAW and returns 0."""
    calls: list[tuple[str, Path, int]] = []

    def runner(channel: str, out_path: Path, timeout_s: int) -> int:
        calls.append((channel, out_path, timeout_s))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"\x00" * success_size)
        return 0

    return runner, calls


def _zero_sleep(_seconds: float) -> None:
    pass


# ---------------------------------------------------------------------------
# SCALE-stub demosaic (mirrors test_calibrate_exposure.py for convergence)
# ---------------------------------------------------------------------------

TARGET = int(0.95 * 65535)   # 62258
BLACK_OFFSET: float = 256.0
_IMG_H = 64
_IMG_W = 96
_CH_IDX = {"R": 0, "G": 1, "B": 2}

SCALE: dict[str, float] = {
    "R": (TARGET - BLACK_OFFSET) / 180.0,
    "G": (TARGET - BLACK_OFFSET) / 160.0,
    "B": (TARGET - BLACK_OFFSET) / 230.0,
}


def make_calibration_demosaic(orch: Orchestrator) -> Callable[[Path], np.ndarray]:
    """Factory: returns a closure over orch.settings so brightness scales with LED level."""
    def demosaic_fn(path: Path) -> np.ndarray:
        level_r = orch.settings.level_r
        level_g = orch.settings.level_g
        level_b = orch.settings.level_b
        levels = {"R": level_r, "G": level_g, "B": level_b}
        img = np.zeros((_IMG_H, _IMG_W, 3), dtype=np.float32)
        for ch, ch_idx in _CH_IDX.items():
            brightness = levels[ch] * SCALE[ch] + BLACK_OFFSET
            img[:, :, ch_idx] = brightness
        rng = np.random.default_rng(level_r * 1000 + level_g * 100 + level_b)
        noise = rng.normal(0.0, 20.0, size=(_IMG_H, _IMG_W, 3)).astype(np.float32)
        return np.clip(img + noise, 0, 65535).astype(np.uint16)
    return demosaic_fn


def make_flat_demosaic(flat_value: int = 40000) -> Callable[[Path], np.ndarray]:
    """Flat, uniform synthetic flat-frame — no rawpy."""
    def demosaic_fn(path: Path) -> np.ndarray:
        arr = np.full((_IMG_H, _IMG_W, 3), flat_value, dtype=np.uint16)
        return arr
    return demosaic_fn


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def settings(tmp_path):
    return CaptureSettings(
        roll_name="CalibTest",
        frame_number=1,
        output_folder=tmp_path,
        level_r=128,
        level_g=128,
        level_b=128,
        settle_ms=0,
        trigger_mode="sdk",  # sdk mode (also the dataclass default; no ied_inbox needed)
    )


@pytest.fixture
def app_and_orch(settings):
    """Create an app with a fake orchestrator and injected demosaic factory."""
    light = FakeScanlight()
    runner, _ = make_runner()
    orch = Orchestrator(light, settings, sony_capture_runner=runner, sleep=_zero_sleep)
    # Inject the demosaic factory via app.config so routes pick it up.
    application = create_app(orch)
    application.config["DEMOSAIC_FACTORY"] = make_calibration_demosaic
    application.config["FLAT_DEMOSAIC_FN"] = make_flat_demosaic()
    return application, orch


# ---------------------------------------------------------------------------
# Tests — these are RED until Task 2 registers the routes
# ---------------------------------------------------------------------------

def test_calibrate_exposure_route(app_and_orch):
    """POST /api/calibrate/exposure returns 200 with snake_case CalibrationResult shape."""
    app, _ = app_and_orch
    client = app.test_client()

    r = client.post("/api/calibrate/exposure", json={"call_id": "run-001"})
    assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.data}"
    body = r.get_json()
    # Top-level keys: r, g, b, base_region, ffc_cal_dir, schema_version
    for ch in ("r", "g", "b"):
        assert ch in body, f"missing top-level key {ch!r}"
        ch_data = body[ch]
        assert "led_level" in ch_data, f"missing led_level in {ch}"
        assert "black_level" in ch_data, f"missing black_level in {ch}"
        assert "gain" in ch_data, f"missing gain in {ch}"
        assert "clip_fraction" in ch_data, f"missing clip_fraction in {ch}"
        assert "shutter_speed" in ch_data, f"missing shutter_speed in {ch}"
    assert "base_region" in body
    assert body["base_region"]["base_rgb"] != [8930.0, 12097.0, 2952.0]
    assert body["base_region"]["base_rgb"] == pytest.approx([
        body["r"]["p99"],
        body["g"]["p99"],
        body["b"]["p99"],
    ])
    assert "ffc_cal_dir" in body
    assert body["call_id"] == "run-001"

    result_r = client.get("/api/calibrate/exposure-result")
    assert result_r.status_code == 200
    assert result_r.get_json()["r"]["led_level"] == body["r"]["led_level"]

    result_for_call = client.get("/api/calibrate/exposure-result?call_id=run-001")
    assert result_for_call.status_code == 200
    assert result_for_call.get_json()["call_id"] == "run-001"

    stale_result = client.get("/api/calibrate/exposure-result?call_id=old-run")
    assert stale_result.status_code == 404

    log_path = app_and_orch[1].settings.output_folder / "scan_log.jsonl"
    events = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert any(
        event["event"] == "calibration_started" and event["call_id"] == "run-001"
        for event in events
    )
    assert any(
        event["event"] == "calibration_complete" and "base_rgb" in event
        for event in events
    )


def test_calibrate_exposure_route_accepts_target_fraction(app_and_orch):
    """The app can request a lower exposure target for extra RAW headroom."""
    app, _ = app_and_orch
    client = app.test_client()

    r = client.post(
        "/api/calibrate/exposure",
        json={"call_id": "run-080", "target_fraction": 0.80},
    )

    assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.data}"
    body = r.get_json()
    assert 52000 <= body["r"]["target"] <= 52500
    assert body["call_id"] == "run-080"


def test_calibrate_exposure_route_rejects_bad_target_fraction(app_and_orch):
    app, _ = app_and_orch
    client = app.test_client()

    r = client.post("/api/calibrate/exposure", json={"target_fraction": 0.95})

    assert r.status_code == 400
    assert "target_fraction" in r.get_json()["error"]


def test_calibrate_exposure_result_route_404_before_completion(app_and_orch):
    """GET /api/calibrate/exposure-result lets Swift reattach after completion."""
    app, _ = app_and_orch
    client = app.test_client()

    r = client.get("/api/calibrate/exposure-result")

    assert r.status_code == 404
    assert "error" in r.get_json()


def test_calibrate_progress_route_reports_latest_scan_log_event(app_and_orch):
    """GET /api/calibrate/progress turns the JSONL tail into operator status text."""
    app, orch = app_and_orch
    log_path = orch.settings.output_folder / "scan_log.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        json.dumps({
            "ts": "2026-05-23T05:47:43.983356+00:00",
            "event": "sony_capture_start",
            "frame": 1,
            "roll": "CalibTest",
            "channel": "R",
            "out": "/tmp/CalibTest_Frame001_Cal_R.ARW",
            "shutter_speed": "1/2",
        }) + "\n"
    )

    client = app.test_client()
    r = client.get("/api/calibrate/progress")

    assert r.status_code == 200
    body = r.get_json()
    assert body["event"] == "sony_capture_start"
    assert body["channel"] == "R"
    assert body["shutter_speed"] == "1/2"
    assert "capturing/downloading R RAW" in body["message"]
    assert body["recent_events"][0]["event"] == "sony_capture_start"
    assert "capturing/downloading R RAW" in body["recent_events"][0]["message"]

    log_path.write_text(
        json.dumps({
            "ts": "2026-05-23T06:02:04.312322+00:00",
            "event": "calibration_probe",
            "frame": 1,
            "roll": "CalibTest",
            "channel": "R",
            "level": 128,
            "shutter_speed": "1/2",
            "p99": 51234.0,
            "target": 62258,
            "next_level": 156,
            "converged": False,
        }) + "\n"
    )

    r = client.get("/api/calibrate/progress")
    body = r.get_json()
    assert body["event"] == "calibration_probe"
    assert "trying LED 156 next" in body["message"]
    assert body["recent_events"][0]["event"] == "calibration_probe"
    assert "trying LED 156 next" in body["recent_events"][0]["message"]

    log_path.write_text(
        json.dumps({
            "ts": "2026-05-23T06:05:04.312322+00:00",
            "event": "sony_capture_start",
            "frame": 1,
            "roll": "CalibTest",
            "channel": "R",
            "label": "dark-frame",
            "out": "/tmp/CalibTest_Frame001_Cal_Dark.ARW",
            "shutter_speed": "1/40",
        }) + "\n"
    )

    r = client.get("/api/calibrate/progress")
    body = r.get_json()
    assert "dark frame" in body["message"]
    assert "R RAW" not in body["message"]


def test_calibrate_progress_route_ignores_events_before_backend_start(app_and_orch):
    """Current-run progress should not replay stale scan_log rows from a prior backend."""
    app, orch = app_and_orch
    app.config["PROGRESS_STARTED_AT"] = datetime(2026, 5, 23, 6, 0, tzinfo=timezone.utc)
    log_path = orch.settings.output_folder / "scan_log.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    old_event = {
        "ts": "2026-05-23T05:47:43.983356+00:00",
        "event": "sony_capture_start",
        "channel": "R",
        "shutter_speed": "1/2",
    }
    new_event = {
        "ts": "2026-05-23T06:02:04.312322+00:00",
        "event": "calibration_probe",
        "channel": "G",
        "level": 192,
        "shutter_speed": "1/40",
        "p99": 59000.0,
        "target": 62258,
        "next_level": 208,
        "converged": False,
    }
    log_path.write_text(json.dumps(old_event) + "\n" + json.dumps(new_event) + "\n")

    body = app.test_client().get("/api/calibrate/progress").get_json()

    assert body["event"] == "calibration_probe"
    assert [event["event"] for event in body["recent_events"]] == ["calibration_probe"]

    log_path.write_text(json.dumps(old_event) + "\n")
    body = app.test_client().get("/api/calibrate/progress").get_json()
    assert body["event"] == "idle"
    assert body["recent_events"] == []


def test_calibrate_progress_route_filters_by_call_id(app_and_orch):
    """Current-run progress should not replay events from a different calibration run."""
    app, orch = app_and_orch
    log_path = orch.settings.output_folder / "scan_log.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    old_event = {
        "ts": "2026-05-23T06:01:00+00:00",
        "event": "calibration_started",
        "call_id": "old-run",
    }
    current_event = {
        "ts": "2026-05-23T06:02:00+00:00",
        "event": "calibration_started",
        "call_id": "current-run",
    }
    log_path.write_text(json.dumps(old_event) + "\n" + json.dumps(current_event) + "\n")

    body = app.test_client().get(
        "/api/calibrate/progress?call_id=current-run"
    ).get_json()

    assert body["event"] == "calibration_started"
    assert body["call_id"] == "current-run"
    assert [event["call_id"] for event in body["recent_events"]] == ["current-run"]

    missing = app.test_client().get(
        "/api/calibrate/progress?call_id=missing-run"
    ).get_json()
    assert missing["event"] == "idle"
    assert missing["recent_events"] == []


def test_calibrate_preview_light_route_controls_white_light(app_and_orch):
    """POST /api/calibrate/preview-light drives backend-owned white preview light."""
    app, orch = app_and_orch
    client = app.test_client()

    r = client.post("/api/calibrate/preview-light", json={"enabled": True, "level": 177})
    assert r.status_code == 200
    assert r.get_json() == {"enabled": True, "level": 177}
    assert orch._scanlight.calls[-1] == ("set_color", 0, 0, 0, 177, False)

    r = client.post("/api/calibrate/preview-light", json={"enabled": False})
    assert r.status_code == 200
    assert r.get_json() == {"enabled": False, "level": 0}
    assert orch._scanlight.calls[-1] == ("off",)


def test_calibrate_exposure_route_rebate_bounds(app_and_orch):
    """Out-of-range rebate params are rejected/clamped; non-int body returns 400 or is coerced (no 500 crash)."""
    app, _ = app_and_orch
    client = app.test_client()

    # Non-int value: must not crash with 500
    r = client.post("/api/calibrate/exposure", json={"rebate_col": "not_an_int"})
    assert r.status_code in (400, 200), (
        f"expected 400 or 200 for non-int rebate_col, got {r.status_code}"
    )

    # Negative col/row: must be rejected or clamped, not 500
    r2 = client.post("/api/calibrate/exposure", json={"rebate_col": -5, "rebate_row": -1})
    assert r2.status_code in (400, 200), (
        f"expected 400 or 200 for negative rebate, got {r2.status_code}"
    )


def test_calibrate_ffc_route(app_and_orch):
    """POST /api/calibrate/ffc returns 200 with the combined {flat_field, inspection} shape."""
    app, _ = app_and_orch
    client = app.test_client()

    # First run exposure to populate LAST_CAL_RESULT
    client.post("/api/calibrate/exposure", json={})

    r = client.post("/api/calibrate/ffc", json={})
    assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.data}"
    body = r.get_json()

    # Top-level shape: flat_field + inspection
    assert "flat_field" in body, f"missing flat_field: {body}"
    assert "inspection" in body, f"missing inspection: {body}"

    # flat_field keys from FlatFieldResult
    ff = body["flat_field"]
    assert "n_frames_averaged" in ff or "n_frames" in ff or "uniformity_improvement" in ff, (
        f"flat_field missing expected keys: {ff}"
    )

    # inspection shape: {"channels": {"R": {falloff_pct, uniformity_pct, verdict}, ...}, "overall": ...}
    insp = body["inspection"]
    assert "channels" in insp, f"missing channels in inspection: {insp}"
    assert "overall" in insp, f"missing overall in inspection: {insp}"
    for ch in ("R", "G", "B"):
        assert ch in insp["channels"], f"missing channel {ch} in inspection.channels"
        ch_data = insp["channels"][ch]
        assert "falloff_pct" in ch_data, f"missing falloff_pct in inspection.channels.{ch}"
        assert "uniformity_pct" in ch_data, f"missing uniformity_pct in inspection.channels.{ch}"
        assert "verdict" in ch_data, f"missing verdict in inspection.channels.{ch}"


def test_calibrate_ffc_route_uses_posted_exposure_without_last_cal(app_and_orch):
    """The Swift app sends exposureResult into the FFC route; honor it even if
    LAST_CAL_RESULT is not populated in this process."""
    app, orch = app_and_orch
    client = app.test_client()

    r = client.post("/api/calibrate/ffc", json={
        "led_level_r": 120,
        "led_level_g": 130,
        "led_level_b": 140,
        "black_level_r": 256.0,
        "black_level_g": 256.0,
        "black_level_b": 256.0,
        "shutter_r": "1/8",
        "shutter_g": "1/4",
        "shutter_b": "1/2",
    })

    assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.data}"
    assert orch.settings.level_r == 120
    assert orch.settings.level_g == 130
    assert orch.settings.level_b == 140
    assert orch.settings.shutter_r == "1/8"
    assert orch.settings.shutter_g == "1/4"
    assert orch.settings.shutter_b == "1/2"


def test_calibrate_ffc_out_of_range_level_returns_400(app_and_orch):
    """codex#4: a numeric-but-out-of-range led_level passes the numeric parse but
    raises ValueError from update_settings/__post_init__; the route must return a
    controlled 400, not surface an unhandled 500."""
    app, _ = app_and_orch
    client = app.test_client()
    r = client.post("/api/calibrate/ffc", json={
        "led_level_r": 300,   # numeric but out of the 0-255 range
        "led_level_g": 130,
        "led_level_b": 140,
        "black_level_r": 256.0,
        "black_level_g": 256.0,
        "black_level_b": 256.0,
    })
    assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.data}"
    assert "error" in r.get_json()


def test_calibrate_ffc_n_frames_below_one_returns_400(app_and_orch):
    """codex#6: n_frames < 1 (valid int, but capture_flats raises ValueError)
    must be a 400 client error, not an unhandled 500."""
    app, _ = app_and_orch
    client = app.test_client()
    r = client.post("/api/calibrate/ffc", json={"n_frames": 0})
    assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.data}"
    assert "error" in r.get_json()


def test_calibrate_ffc_n_frames_bad_input_returns_400(app_and_orch):
    """FIX-D: non-integer n_frames must return 400, not 500."""
    app, _ = app_and_orch
    client = app.test_client()

    r = client.post("/api/calibrate/ffc", json={"n_frames": "not_an_int"})
    assert r.status_code == 400, (
        f"expected 400 for non-int n_frames, got {r.status_code}: {r.data}"
    )
    body = r.get_json()
    assert "error" in body, f"expected {{error}} key in 400 response, got: {body}"


def test_calibrate_checks_route(app_and_orch):
    """POST /api/calibrate/checks returns 200 with a CheckResult array after prior exposure."""
    app, _ = app_and_orch
    client = app.test_client()

    # Populate LAST_CAL_RESULT via the exposure route
    exp_r = client.post("/api/calibrate/exposure", json={})
    assert exp_r.status_code == 200, f"exposure failed: {exp_r.data}"

    r = client.post("/api/calibrate/checks", json={})
    assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.data}"
    body = r.get_json()

    assert isinstance(body, list), f"expected list, got {type(body)}: {body}"
    assert len(body) >= 1, "expected at least one CheckResult"

    # Check that base_neutrality is present (registration may be absent without LAST_CAL_FRAME)
    names = [item.get("name", "") for item in body]
    assert any("base_neutrality" in n for n in names), (
        f"expected base_neutrality check, got names: {names}"
    )
    for item in body:
        assert "name" in item
        assert "passed" in item
        assert "deltas" in item


def test_calibrate_route_locked_returns_409(app_and_orch):
    """When the activity guard is held (capture or calibration in progress),
    the exposure route returns 409. Bug-2: the guard must cover the whole
    calibration duration, not just a TOCTOU check-then-unlock window."""
    app, orch = app_and_orch
    client = app.test_client()

    # Simulate a capture in progress by claiming the activity slot.
    app.config["CURRENT_CAL_CALL_ID"] = "active-run"
    claimed = orch.try_begin_activity("capture")
    assert claimed, "could not claim activity slot for test setup"
    try:
        r = client.post("/api/calibrate/exposure", json={})
        assert r.status_code == 409, (
            f"expected 409 when activity guard is held, got {r.status_code}: {r.data}"
        )
        assert r.get_json()["call_id"] == "active-run"
    finally:
        orch.end_activity()


def test_settings_post_null_value_returns_400_not_500(app_and_orch):
    """codex#2: JSON null reaching int()/float() coercion is a client error (400),
    not an unhandled TypeError surfacing as a 500."""
    app, _ = app_and_orch
    client = app.test_client()
    for body in ({"inbox_stable_for_s": None}, {"level_r": None}):
        r = client.post("/api/settings", json=body)
        assert r.status_code == 400, f"expected 400 for {body}, got {r.status_code}: {r.data}"
        assert "error" in r.get_json()


def test_settings_post_rejected_while_activity_held(app_and_orch):
    """F1: /api/settings must not mutate levels/output/shutters while a capture
    or calibration owns the rig (they release _lock between channel captures, so
    a mid-run mutation could corrupt an exposure bisection). It claims the same
    activity slot and 409s while one is held; works once released."""
    app, orch = app_and_orch
    client = app.test_client()

    assert orch.try_begin_activity("calibrate_exposure"), "could not claim slot for setup"
    try:
        r = client.post("/api/settings", json={"level_r": 111})
        assert r.status_code == 409, f"expected 409 while activity held, got {r.status_code}: {r.data}"
        assert orch.settings.level_r != 111, "settings must not change while rejected"
    finally:
        orch.end_activity()

    r2 = client.post("/api/settings", json={"level_r": 111})
    assert r2.status_code == 200, f"expected 200 after release, got {r2.status_code}: {r2.data}"
    assert orch.settings.level_r == 111


def test_preview_light_rejected_while_activity_held(app_and_orch):
    """HIGH#2 (Codex): preview-light must not re-colour the Scanlight while a
    capture or calibration owns the rig — those activities release _lock
    between internal captures, so the activity guard is what protects the gap."""
    app, orch = app_and_orch
    client = app.test_client()

    claimed = orch.try_begin_activity("calibrate_exposure")
    assert claimed, "could not claim activity slot for test setup"
    calls_before = len(orch._scanlight.calls)
    try:
        r = client.post("/api/calibrate/preview-light", json={"enabled": True, "level": 200})
        assert r.status_code == 409, (
            f"expected 409 while activity held, got {r.status_code}: {r.data}"
        )
        # The rejected request must not have touched the Scanlight at all.
        assert len(orch._scanlight.calls) == calls_before
    finally:
        orch.end_activity()

    # Once the activity is released, preview-light works again.
    r2 = client.post("/api/calibrate/preview-light", json={"enabled": True, "level": 200})
    assert r2.status_code == 200, f"expected 200 after release, got {r2.status_code}: {r2.data}"


def test_calibrate_route_failclosed_500(settings, tmp_path):
    """A runner that always returns non-zero causes the route to return 500 with {error}."""
    def failing_runner(channel: str, out_path: Path, timeout_s: int) -> int:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"\x00" * (70 * 1024 * 1024))
        return 1  # non-zero → capture failure

    light = FakeScanlight()
    orch = Orchestrator(light, settings, sony_capture_runner=failing_runner, sleep=_zero_sleep)
    app = create_app(orch)
    app.config["DEMOSAIC_FACTORY"] = make_calibration_demosaic
    client = app.test_client()

    r = client.post("/api/calibrate/exposure", json={})
    assert r.status_code == 500, f"expected 500 for failing runner, got {r.status_code}: {r.data}"
    body = r.get_json()
    assert "error" in body, f"expected {{error}} key, got: {body}"
