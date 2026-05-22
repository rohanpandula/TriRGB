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

    r = client.post("/api/calibrate/exposure", json={})
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
    assert "base_region" in body
    assert "ffc_cal_dir" in body


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
    """When orch._lock is already held, the exposure route returns 409."""
    app, orch = app_and_orch
    client = app.test_client()

    # Acquire the lock to simulate a capture in progress
    acquired = orch._lock.acquire(blocking=False)
    assert acquired, "could not acquire lock for test setup"
    try:
        r = client.post("/api/calibrate/exposure", json={})
        assert r.status_code == 409, (
            f"expected 409 when lock is held, got {r.status_code}: {r.data}"
        )
    finally:
        orch._lock.release()


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
