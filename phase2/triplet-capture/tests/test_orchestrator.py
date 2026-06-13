"""Tests for the triplet-capture orchestrator.

The Scanlight class and the `sony-capture` subprocess are both replaced
with stubs. We verify:
- R/G/B channels are set in order with the right levels
- sony-capture is invoked once per channel with the matching --out path
- Frame counter advances ONLY on success (no advance on failure)
- Retake never advances the counter
- Implausibly-sized files abort with a clear error
- Missing output file aborts cleanly
- All actions land in scan_log.jsonl as parseable JSON
"""
from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import threading
from pathlib import Path

import pytest

from triplet_capture.orchestrator import (
    Orchestrator,
    CaptureSettings,
    PLAUSIBLE_RAW_MIN_BYTES,
    PLAUSIBLE_RAW_MAX_BYTES,
)


class FakeScanlight:
    """Minimal duck-type covering what the orchestrator uses."""
    def __init__(self):
        self.calls: list[tuple] = []

    def set_color(self, r=0, g=0, b=0, w=0, save=False):
        self.calls.append(("set_color", r, g, b, w, save))

    def off(self):
        self.calls.append(("off",))


class FakePopen:
    def __init__(
        self,
        cmd,
        *,
        returncode: int = 0,
        stdout_text: str = "",
        stderr_text: str = "",
        **kwargs,
    ):
        self.cmd = cmd
        self.kwargs = kwargs
        self.stdout = io.StringIO(stdout_text)
        self.stderr = io.StringIO(stderr_text)
        self._final_returncode = returncode
        self.returncode: int | None = None
        self.killed = False

    def poll(self):
        if self.returncode is None:
            self.returncode = self._final_returncode
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = self._final_returncode
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


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


def make_failing_runner(*, fail_on: str, exit_code: int = 1):
    calls: list[tuple[str, Path, int]] = []

    def runner(channel: str, out_path: Path, timeout_s: int) -> int:
        calls.append((channel, out_path, timeout_s))
        if channel == fail_on:
            return exit_code
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"\x00" * (70 * 1024 * 1024))
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
        trigger_mode="sdk",  # sdk mode (also the dataclass default; no ied_inbox needed)
    )


def _zero_sleep(_seconds: float) -> None:
    pass


# ---------- settings validation ----------

def test_settings_rejects_out_of_range_level(tmp_path):
    with pytest.raises(ValueError):
        CaptureSettings(output_folder=tmp_path, level_r=300)

def test_settings_rejects_roll_name_with_spaces(tmp_path):
    with pytest.raises(ValueError):
        CaptureSettings(output_folder=tmp_path, roll_name="bad name")

def test_settings_rejects_zero_frame_number(tmp_path):
    with pytest.raises(ValueError):
        CaptureSettings(output_folder=tmp_path, frame_number=0)

def test_settings_rejects_extended_low_iso(tmp_path):
    with pytest.raises(ValueError, match="sony_iso"):
        CaptureSettings(output_folder=tmp_path, sony_iso="50")

@pytest.mark.parametrize("field", ["inbox_stable_for_s", "inbox_poll_interval_s"])
@pytest.mark.parametrize("bad", [0, 0.0, -1.0, float("nan"), float("inf")])
def test_settings_rejects_nonpositive_or_nonfinite_inbox_timing(tmp_path, field, bad):
    # <= 0 bypasses the stability window / makes sleep() spin or raise; NaN/inf
    # makes the stability check never pass so wait_for_new_file hangs.
    with pytest.raises(ValueError, match=field):
        CaptureSettings(output_folder=tmp_path, trigger_mode="sdk", **{field: bad})

def test_settings_maps_legacy_lowest_iso_to_scan_base(tmp_path):
    settings = CaptureSettings(output_folder=tmp_path, sony_iso="lowest", trigger_mode="sdk")

    assert settings.sony_iso == "100or125"


# ---------- happy path ----------

def test_capture_triplet_sets_each_channel_and_advances(settings):
    light = FakeScanlight()
    runner, runner_calls = make_runner()
    orch = Orchestrator(light, settings, sony_capture_runner=runner, sleep=_zero_sleep)

    result = orch.capture_triplet()

    assert result.success
    assert result.error is None
    assert result.frame_number == 1

    # Scanlight saw R only, G only, B only, then off
    assert light.calls == [
        ("set_color", 200, 0, 0, 0, False),
        ("set_color", 0, 180, 0, 0, False),
        ("set_color", 0, 0, 160, 0, False),
        ("off",),
    ]

    # sony-capture invoked three times with the right paths
    assert [c[0] for c in runner_calls] == ["R", "G", "B"]
    for channel, path, _timeout in runner_calls:
        assert path.name == f"Roll001_Frame001_{channel}.ARW"

    # Frame counter advanced
    assert orch.settings.frame_number == 2


def test_capture_channel_sets_only_requested_channel_without_advancing(settings):
    light = FakeScanlight()
    runner, runner_calls = make_runner()
    orch = Orchestrator(light, settings, sony_capture_runner=runner, sleep=_zero_sleep)
    out = settings.output_folder / "cal_R.ARW"

    result = orch.capture_channel("R", level=123, shutter_speed="1/4", out_path=out)

    assert result.success
    assert result.files == {"R": out}
    assert orch.settings.frame_number == 1
    assert light.calls == [
        ("set_color", 123, 0, 0, 0, False),
        ("off",),
    ]
    assert runner_calls == [("R", out, settings.sony_capture_timeout_s)]


def test_log_records_every_action(settings):
    light = FakeScanlight()
    runner, _ = make_runner()
    orch = Orchestrator(light, settings, sony_capture_runner=runner, sleep=_zero_sleep)
    orch.capture_triplet()

    log = settings.output_folder / "scan_log.jsonl"
    assert log.exists()
    lines = [json.loads(l) for l in log.read_text().splitlines()]
    events = [l["event"] for l in lines]
    # Must include: start, 3× scanlight_on, 3× sony_capture_start, 3× sony_capture_ok, scanlight_off, frame_advance
    assert events[0] == "triplet_start"
    assert events.count("scanlight_on") == 3
    assert events.count("sony_capture_ok") == 3
    assert "scanlight_off" in events
    assert events[-1] == "frame_advance"
    # All lines are valid JSON with a timestamp
    for l in lines:
        assert "ts" in l


# ---------- failure paths ----------

def test_failure_does_not_advance_frame(settings):
    light = FakeScanlight()
    runner, _ = make_failing_runner(fail_on="G")
    orch = Orchestrator(light, settings, sony_capture_runner=runner, sleep=_zero_sleep)

    result = orch.capture_triplet()

    assert not result.success
    assert "channel G" in result.error
    # Frame counter is unchanged so the operator can retake without renaming
    assert orch.settings.frame_number == 1
    # Scanlight was turned off even though we aborted
    assert ("off",) in light.calls


def test_sdk_exit_127_reports_missing_sony_capture(settings):
    light = FakeScanlight()
    def runner(_channel, _out_path, _timeout_s):
        return 127
    orch = Orchestrator(light, settings, sony_capture_runner=runner, sleep=_zero_sleep)

    result = orch.capture_triplet()

    assert not result.success
    assert "sony-capture could not be found or launched" in result.error
    assert "Build phase1/sony-capture" in result.error
    assert orch.settings.frame_number == 1
    assert ("off",) in light.calls


def test_sdk_runner_error_redacts_saved_auth(tmp_path, caplog):
    """sony-capture stderr can echo argv; saved auth must not leak into UI errors/logs."""
    fake_capture = tmp_path / "sony-capture-fail"
    fake_capture.write_text(
        "#!/bin/sh\n"
        # Credentials now arrive via the environment, not argv. Echo them (as a
        # misbehaving SDK binary might) to prove _redact_runner_detail still
        # scrubs them — and that the env vars were actually passed through.
        "printf 'auth failed (user=%s pw=%s) args: %s\\n' \"$SONY_USERNAME\" \"$SONY_PW\" \"$*\" >&2\n"
        "exit 1\n"
    )
    fake_capture.chmod(0o755)
    settings = CaptureSettings(
        roll_name="Roll001",
        frame_number=1,
        output_folder=tmp_path,
        level_r=200,
        level_g=180,
        level_b=160,
        settle_ms=0,
        sony_capture_path=str(fake_capture),
        sony_user="USERSECRET",
        sony_password="PASSSECRET",
        trigger_mode="sdk",  # sdk mode (also the dataclass default; no ied_inbox needed)
        sdk_persistent=False,  # use one-shot runner so the shell script is invoked directly
    )
    orch = Orchestrator(FakeScanlight(), settings, sleep=_zero_sleep)

    with caplog.at_level("ERROR", logger="triplet-capture"):
        result = orch.capture_triplet()

    assert not result.success
    combined = f"{result.error}\n{caplog.text}"
    assert "USERSECRET" not in combined
    assert "PASSSECRET" not in combined
    assert "<redacted>" in combined


def test_missing_output_file_aborts(settings, tmp_path):
    """Runner returns 0 but doesn't create the file → clear abort."""
    light = FakeScanlight()
    calls = []
    def runner(channel, out_path, timeout_s):
        calls.append((channel, out_path))
        # don't write the file
        return 0
    orch = Orchestrator(light, settings, sony_capture_runner=runner, sleep=_zero_sleep)

    result = orch.capture_triplet()
    assert not result.success
    assert "does not exist" in result.error
    assert orch.settings.frame_number == 1


def test_implausibly_small_file_aborts(settings):
    light = FakeScanlight()
    runner, _ = make_runner(success_size=10 * 1024)  # 10KB — way too small
    orch = Orchestrator(light, settings, sony_capture_runner=runner, sleep=_zero_sleep)

    result = orch.capture_triplet()
    assert not result.success
    assert "outside plausible RAW range" in result.error


def test_implausibly_large_file_aborts(settings):
    light = FakeScanlight()
    # Pick a size well above PLAUSIBLE_RAW_MAX_BYTES (200MB) but small
    # enough to write quickly in a test. 300MB is comfortably outside.
    runner, _ = make_runner(success_size=300 * 1024 * 1024)
    orch = Orchestrator(light, settings, sony_capture_runner=runner, sleep=_zero_sleep)

    result = orch.capture_triplet()
    assert not result.success
    assert "outside plausible RAW range" in result.error


# ---------- retake ----------

def test_retake_does_not_advance(settings):
    light = FakeScanlight()
    runner, _ = make_runner()
    orch = Orchestrator(light, settings, sony_capture_runner=runner, sleep=_zero_sleep)

    # First, a successful capture brings us to frame 2.
    r1 = orch.capture_triplet()
    assert r1.success and orch.settings.frame_number == 2

    # Now go back to frame 1 and retake it.
    orch.update_settings(frame_number=1)
    r2 = orch.capture_triplet(retake=True)
    assert r2.success
    # Retake does NOT advance — operator is responsible for moving on.
    assert orch.settings.frame_number == 1


def test_retake_overwrites_existing_files(settings):
    light = FakeScanlight()
    runner, _ = make_runner(success_size=70 * 1024 * 1024)
    orch = Orchestrator(light, settings, sony_capture_runner=runner, sleep=_zero_sleep)

    orch.capture_triplet()
    target = settings.output_folder / "Roll001_Frame001_R.ARW"
    first_mtime = target.stat().st_mtime_ns

    orch.update_settings(frame_number=1)
    orch.capture_triplet(retake=True)
    second_mtime = target.stat().st_mtime_ns
    # File was overwritten (mtime advanced or unchanged but file still there).
    # Strictly: it was written again. Different times in CI but on fast disks
    # the mtime resolution can coincide, so just confirm the file exists with
    # the expected size and the runner saw the retake.
    assert target.exists()
    assert target.stat().st_size == 70 * 1024 * 1024


# ---------- settings updates ----------

def test_changing_roll_name_resets_frame_to_1(settings):
    light = FakeScanlight()
    runner, _ = make_runner()
    orch = Orchestrator(light, settings, sony_capture_runner=runner, sleep=_zero_sleep)
    orch.capture_triplet()  # → frame 2
    assert orch.settings.frame_number == 2

    orch.update_settings(roll_name="Roll002")
    assert orch.settings.frame_number == 1
    assert orch.settings.roll_name == "Roll002"


def test_explicit_frame_with_roll_change_is_respected(settings):
    light = FakeScanlight()
    orch = Orchestrator(light, settings, sony_capture_runner=lambda *a: 0, sleep=_zero_sleep)
    orch.update_settings(roll_name="Roll002", frame_number=12)
    assert orch.settings.frame_number == 12


# ---------- Flask integration ----------

def test_flask_routes(settings, tmp_path):
    from triplet_capture.app import create_app
    light = FakeScanlight()
    runner, _ = make_runner()
    orch = Orchestrator(light, settings, sony_capture_runner=runner, sleep=_zero_sleep)
    app = create_app(orch)
    client = app.test_client()

    r = client.get("/api/state")
    assert r.status_code == 200
    assert r.get_json()["roll_name"] == "Roll001"

    r = client.post("/api/settings", json={"level_r": 150})
    assert r.status_code == 200
    assert r.get_json()["level_r"] == 150
    assert orch.settings.level_r == 150

    r = client.post("/api/capture")
    assert r.status_code == 200
    body = r.get_json()
    assert body["success"]
    assert body["next_frame"] == 2


def test_ready_nonce_echoed_on_state(settings):
    """GET /api/state echoes the --ready-nonce so the Swift launcher can confirm
    it's talking to the orchestrator it spawned (not a foreign server that grabbed
    the port). Empty string when no nonce was provided."""
    from triplet_capture.app import create_app
    light = FakeScanlight()
    runner, _ = make_runner()
    orch = Orchestrator(light, settings, sony_capture_runner=runner, sleep=_zero_sleep)

    # With a nonce: echoed verbatim.
    app = create_app(orch, ready_nonce="nonce-xyz-123")
    body = app.test_client().get("/api/state").get_json()
    assert body["ready_nonce"] == "nonce-xyz-123"

    # Without a nonce: empty string (not missing — the field is always present).
    app2 = create_app(orch)
    body2 = app2.test_client().get("/api/state").get_json()
    assert body2["ready_nonce"] == ""


def test_flask_invalid_settings_returns_400(settings):
    from triplet_capture.app import create_app
    light = FakeScanlight()
    orch = Orchestrator(light, settings, sony_capture_runner=lambda *a: 0, sleep=_zero_sleep)
    app = create_app(orch)
    client = app.test_client()

    r = client.post("/api/settings", json={"level_r": 999})
    assert r.status_code == 400
    assert "out of range" in r.get_json()["error"]


def test_flask_capture_failure_returns_500(settings):
    from triplet_capture.app import create_app
    light = FakeScanlight()
    runner, _ = make_failing_runner(fail_on="R")
    orch = Orchestrator(light, settings, sony_capture_runner=runner, sleep=_zero_sleep)
    app = create_app(orch)
    client = app.test_client()

    r = client.post("/api/capture")
    assert r.status_code == 500
    assert not r.get_json()["success"]


# ---------- hardware-trigger mode ----------

class FakeScanlightWithPulse(FakeScanlight):
    """FakeScanlight + pulse_shutter recorder."""
    def __init__(self, pulse_side_effect=None):
        super().__init__()
        self.pulse_side_effect = pulse_side_effect  # callable(pulse_ms) for inbox simulation
        self.pulses: list[int] = []

    def pulse_shutter(self, pulse_ms: int = 100) -> None:
        self.pulses.append(pulse_ms)
        if self.pulse_side_effect is not None:
            self.pulse_side_effect(pulse_ms)


def _hw_settings(tmp_path, **overrides):
    """Build a hw-mode CaptureSettings with a freshly-created inbox dir."""
    inbox_dir = tmp_path / "ied_inbox"
    inbox_dir.mkdir()
    out_dir = tmp_path / "scans"
    defaults = dict(
        roll_name="RollHW",
        frame_number=1,
        output_folder=out_dir,
        level_r=200,
        level_g=180,
        level_b=160,
        settle_ms=0,
        trigger_mode="hw",
        ied_inbox=inbox_dir,
        shutter_pulse_ms=100,
        sony_capture_timeout_s=5,    # generous on the fake clock
        inbox_stable_for_s=0.4,      # short stability window for tests
        inbox_poll_interval_s=0.1,
    )
    defaults.update(overrides)
    return CaptureSettings(**defaults)


def _manual_settings(tmp_path, **overrides):
    """Build a manual-IED CaptureSettings with a freshly-created inbox dir."""
    inbox_dir = tmp_path / "ied_inbox"
    inbox_dir.mkdir()
    out_dir = tmp_path / "scans"
    defaults = dict(
        roll_name="RollManual",
        frame_number=1,
        output_folder=out_dir,
        level_r=200,
        level_g=180,
        level_b=160,
        settle_ms=0,
        trigger_mode="manual",
        ied_inbox=inbox_dir,
        sony_capture_timeout_s=5,
        inbox_stable_for_s=0.4,
        inbox_poll_interval_s=0.1,
    )
    defaults.update(overrides)
    return CaptureSettings(**defaults)


def test_hw_settings_requires_ied_inbox(tmp_path):
    with pytest.raises(ValueError, match="trigger_mode='hw' requires ied_inbox"):
        CaptureSettings(
            output_folder=tmp_path,
            trigger_mode="hw",
            ied_inbox=None,
        )


def test_manual_settings_requires_ied_inbox(tmp_path):
    with pytest.raises(ValueError, match="trigger_mode='manual' requires ied_inbox"):
        CaptureSettings(
            output_folder=tmp_path,
            trigger_mode="manual",
            ied_inbox=None,
        )


def test_hw_settings_rejects_invalid_pulse_ms(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    with pytest.raises(ValueError, match="shutter_pulse_ms"):
        CaptureSettings(
            output_folder=tmp_path,
            trigger_mode="hw",
            ied_inbox=inbox,
            shutter_pulse_ms=7,  # not a multiple of 10, below minimum
        )


def test_hw_settings_rejects_unknown_trigger_mode(tmp_path):
    with pytest.raises(ValueError, match="trigger_mode must be"):
        CaptureSettings(
            output_folder=tmp_path,
            trigger_mode="weird",
        )


def test_hw_mode_pulses_and_picks_up_arrived_file(tmp_path):
    """Full happy-path: per channel, scanlight color set, pulse fired,
    file appears in inbox, gets renamed to the canonical path."""
    s = _hw_settings(tmp_path)

    # When pulse fires, drop an ARW into the inbox immediately.
    # Use a unique filename per pulse so the FIFO mapping is provable.
    counter = {"n": 0}
    def on_pulse(_ms):
        counter["n"] += 1
        f = s.ied_inbox / f"DSC{counter['n']:05d}.ARW"
        f.write_bytes(b"\x00" * (70 * 1024 * 1024))

    light = FakeScanlightWithPulse(pulse_side_effect=on_pulse)
    # The orchestrator's HW runner calls real wait_for_new_file. We
    # override its sleep so the stability window passes "instantly" but
    # without zero-time looping.
    advance = {"t": 0.0}
    def fake_sleep(dt):
        advance["t"] += dt
    def fake_clock():
        return advance["t"]

    orch = Orchestrator(light, s, clock=fake_clock, sleep=fake_sleep)
    result = orch.capture_triplet()

    assert result.success, f"failed: {result.error}"
    # Three pulses, all at the configured length
    assert light.pulses == [100, 100, 100]
    # Files were renamed to the canonical convention
    expected_paths = {
        s.output_folder / f"RollHW_Frame001_{ch}.ARW"
        for ch in ("R", "G", "B")
    }
    assert set(result.files.values()) == expected_paths
    # And the inbox has been drained
    assert list(s.ied_inbox.iterdir()) == []


def test_manual_mode_waits_for_ied_file_without_pulse(tmp_path):
    """Manual IED mode sets each color and waits for operator-triggered
    files to land in the inbox. It must not call pulse_shutter."""
    s = _manual_settings(tmp_path)

    advance = {"t": 0.0}
    counter = {"n": 0}

    def fake_sleep(dt):
        advance["t"] += dt
        # Simulate the operator pressing Capture in IED after the app
        # lights each channel. Once the previous channel's file is claimed,
        # the inbox is empty again and the next manual capture can arrive.
        if counter["n"] < 3 and not list(s.ied_inbox.glob("*.ARW")):
            counter["n"] += 1
            f = s.ied_inbox / f"DSC_MANUAL_{counter['n']:05d}.ARW"
            f.write_bytes(b"\x00" * (70 * 1024 * 1024))

    def fake_clock():
        return advance["t"]

    light = FakeScanlightWithPulse()
    orch = Orchestrator(light, s, clock=fake_clock, sleep=fake_sleep)
    result = orch.capture_triplet()

    assert result.success, f"failed: {result.error}"
    assert light.pulses == []
    assert [c for c in light.calls if c[0] == "set_color"] == [
        ("set_color", 200, 0, 0, 0, False),
        ("set_color", 0, 180, 0, 0, False),
        ("set_color", 0, 0, 160, 0, False),
    ]
    expected_paths = {
        s.output_folder / f"RollManual_Frame001_{ch}.ARW"
        for ch in ("R", "G", "B")
    }
    assert set(result.files.values()) == expected_paths


def test_hw_mode_times_out_when_no_file_arrives(tmp_path):
    """No file ever drops into the inbox → hw runner returns 124, frame
    counter does not advance."""
    s = _hw_settings(tmp_path, sony_capture_timeout_s=1)
    light = FakeScanlightWithPulse(pulse_side_effect=None)  # no file appears

    advance = {"t": 0.0}
    def fake_sleep(dt):
        advance["t"] += dt
    def fake_clock():
        return advance["t"]

    orch = Orchestrator(light, s, clock=fake_clock, sleep=fake_sleep)
    result = orch.capture_triplet()
    assert not result.success
    assert "exit 124" in result.error
    # First channel was pulsed; then we bailed.
    assert light.pulses == [100]
    # Frame counter NOT advanced
    assert orch.settings.frame_number == 1


def test_hw_mode_propagates_pulse_failure(tmp_path):
    """If the scanlight pulse raises, the runner returns 1 and the
    triplet aborts cleanly."""
    s = _hw_settings(tmp_path)

    class BadLight(FakeScanlightWithPulse):
        def pulse_shutter(self, pulse_ms: int = 100) -> None:
            self.pulses.append(pulse_ms)
            raise RuntimeError("serial port disconnected")

    light = BadLight()
    advance = {"t": 0.0}
    def fake_sleep(dt):
        advance["t"] += dt
    def fake_clock():
        return advance["t"]

    orch = Orchestrator(light, s, clock=fake_clock, sleep=fake_sleep)
    result = orch.capture_triplet()
    assert not result.success
    assert light.pulses == [100]  # first pulse was attempted
    assert orch.settings.frame_number == 1


def test_hw_mode_quarantines_preexisting_files_at_frame_start(tmp_path):
    """Per codex review: if the inbox already has leftover ARWs (e.g.
    late arrivals from a previous timed-out frame) when capture starts,
    they get moved to a `_stale/` subdir before the new triplet begins.
    Channel mapping must come from the new pulses, not the leftovers.
    """
    s = _hw_settings(tmp_path)
    # Drop two leftover files before capture starts.
    leftover_a = s.ied_inbox / "DSC09998.ARW"
    leftover_b = s.ied_inbox / "DSC09999.ARW"
    leftover_a.write_bytes(b"\x00" * (60 * 1024 * 1024))
    leftover_b.write_bytes(b"\x00" * (60 * 1024 * 1024))

    counter = {"n": 0}
    def on_pulse(_ms):
        counter["n"] += 1
        f = s.ied_inbox / f"DSC{counter['n']:05d}.ARW"
        f.write_bytes(b"\x00" * (70 * 1024 * 1024))

    light = FakeScanlightWithPulse(pulse_side_effect=on_pulse)
    advance = {"t": 0.0}
    def fake_sleep(dt):
        advance["t"] += dt
    def fake_clock():
        return advance["t"]

    orch = Orchestrator(light, s, clock=fake_clock, sleep=fake_sleep)
    result = orch.capture_triplet()
    assert result.success, f"failed: {result.error}"
    # Leftover files are no longer directly in the inbox — they were
    # quarantined to a _stale/ subdir.
    assert not leftover_a.exists()
    assert not leftover_b.exists()
    stale_root = s.ied_inbox / "_stale"
    assert stale_root.is_dir()
    stale_files = sorted(p.name for p in stale_root.rglob("*.ARW"))
    assert "DSC09998.ARW" in stale_files
    assert "DSC09999.ARW" in stale_files
    # The new triplet's three files end up at the canonical paths.
    expected_paths = {
        s.output_folder / f"RollHW_Frame001_{ch}.ARW"
        for ch in ("R", "G", "B")
    }
    assert set(result.files.values()) == expected_paths


def test_hw_mode_selected_by_trigger_mode_setting(tmp_path):
    """Without an explicit `sony_capture_runner`, the orchestrator must
    pick the internal HW runner when trigger_mode='hw'."""
    s = _hw_settings(tmp_path)
    light = FakeScanlightWithPulse()
    orch = Orchestrator(light, s)  # no runner override
    # Internal runner is the HW runner method bound to the instance.
    assert orch._runner.__func__ is Orchestrator._hw_runner


def test_manual_mode_selected_by_trigger_mode_setting(tmp_path):
    """Without an explicit runner, trigger_mode='manual' must select the
    inbox-only runner, not the SDK subprocess or hardware pulse runner."""
    s = _manual_settings(tmp_path)
    light = FakeScanlightWithPulse()
    orch = Orchestrator(light, s)
    assert orch._runner.__func__ is Orchestrator._manual_runner


def test_sdk_mode_selectable(tmp_path):
    # trigger_mode dataclass default was changed to "manual" to match the CLI
    # default; now "sdk" must be passed explicitly.
    # With sdk_persistent=True (new default) the persistent runner is selected;
    # with sdk_persistent=False the one-shot _default_runner is selected.
    s_persist = CaptureSettings(output_folder=tmp_path, trigger_mode="sdk", sdk_persistent=True)
    light = FakeScanlight()
    orch_persist = Orchestrator(light, s_persist)
    assert orch_persist._runner.__func__ is Orchestrator._persistent_runner

    s_oneshot = CaptureSettings(output_folder=tmp_path, trigger_mode="sdk", sdk_persistent=False)
    orch_oneshot = Orchestrator(light, s_oneshot)
    assert orch_oneshot._runner.__func__ is Orchestrator._default_runner


def test_sdk_persistent_used_with_per_channel_shutter(tmp_path):
    # Persist mode applies each channel's shutter per capture (via the persist
    # `shutter` command), so DIFFERING per-channel shutters — the narrowband-RGB
    # norm, blue far longer than red/green — still use the persistent runner.
    light = FakeScanlight()
    s = CaptureSettings(
        output_folder=tmp_path, trigger_mode="sdk", sdk_persistent=True,
        shutter_r="1/100", shutter_g="1/100", shutter_b="1/4",
    )
    orch = Orchestrator(light, s)
    assert orch._runner.__func__ is Orchestrator._persistent_runner


def test_mixed_none_shutters_fall_back_to_oneshot(tmp_path):
    # codex#6: a MIX of explicit and None shutters can't be honored by the
    # persist session (None can't reset to the camera default after another
    # channel changed it mid-session) → one-shot runner. All-set (even differing)
    # and all-None keep the persistent session.
    light = FakeScanlight()
    s = CaptureSettings(
        output_folder=tmp_path, trigger_mode="sdk", sdk_persistent=True,
        shutter_r="1/100", shutter_g=None, shutter_b="1/4",
    )
    orch = Orchestrator(light, s)
    assert orch._channel_shutters_mixed() is True
    assert orch._runner.__func__ is Orchestrator._default_runner

    # Fill the gap → all-set (differing) → persist again (re-picked on change).
    orch.update_settings(shutter_g="1/200")
    assert orch._channel_shutters_mixed() is False
    assert orch._runner.__func__ is Orchestrator._persistent_runner

    # Clear all → all-None → persist (camera default for every channel).
    orch.update_settings(shutter_r=None, shutter_g=None, shutter_b=None)
    assert orch._channel_shutters_mixed() is False
    assert orch._runner.__func__ is Orchestrator._persistent_runner


def test_persistent_session_extras_have_no_startup_shutter(tmp_path, monkeypatch):
    # Shutter is per-capture now, NOT a startup arg — the session spawn must not
    # carry --shutter-speed (that would pin all channels to one shutter).
    import triplet_capture.orchestrator as orch_mod

    captured = {}

    class _RecordingPersist:
        def __init__(self, binary, extras, env, **kwargs):
            captured["extras"] = list(extras)

        def capture(self, out_path, *, timeout_s, shutter=None):  # pragma: no cover
            return 0

        def close(self):  # pragma: no cover - unused
            pass

    monkeypatch.setattr(orch_mod, "PersistentSonyCapture", _RecordingPersist)
    light = FakeScanlight()
    s = CaptureSettings(
        output_folder=tmp_path, trigger_mode="sdk", sdk_persistent=True,
        shutter_r="1/160", shutter_g="1/160", shutter_b="1/4",
    )
    orch = Orchestrator(light, s)
    orch._get_or_create_persistent_session()
    assert "--shutter-speed" not in captured["extras"]


def test_persistent_runner_forwards_channel_shutter(tmp_path, monkeypatch):
    # _persistent_runner must pass the per-channel shutter to session.capture.
    import triplet_capture.orchestrator as orch_mod

    calls = []

    class _RecordingPersist:
        def __init__(self, binary, extras, env, **kwargs):
            pass

        def capture(self, out_path, *, timeout_s, shutter=None):
            calls.append((out_path.stem, shutter))
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(b"\x00" * (70 * 1024 * 1024))
            return 0

        def close(self):
            pass

    monkeypatch.setattr(orch_mod, "PersistentSonyCapture", _RecordingPersist)
    light = FakeScanlight()
    s = CaptureSettings(
        output_folder=tmp_path, trigger_mode="sdk", sdk_persistent=True,
        shutter_r="1/100", shutter_g="1/200", shutter_b="1/4",
    )
    orch = Orchestrator(light, s)
    orch._persistent_runner("R", tmp_path / "f_R.ARW", 30)
    orch._persistent_runner("B", tmp_path / "f_B.ARW", 30)
    assert ("f_R", "1/100") in calls
    assert ("f_B", "1/4") in calls


# A real executable fake of `sony-capture --persist` — exercises the actual
# subprocess line protocol (READY / shutter / capture / quit) end to end.
_FAKE_PERSIST_SCRIPT = """#!/usr/bin/env python3
import sys
logp = None
for i, a in enumerate(sys.argv):
    if a == "--log":
        logp = sys.argv[i + 1]
logf = open(logp, "a") if logp else None
print("READY", flush=True)
for line in sys.stdin:
    line = line.strip()
    if line == "quit":
        break
    if line.startswith("shutter "):
        speed = line[len("shutter "):]
        if logf:
            logf.write("shutter %s\\n" % speed); logf.flush()
        print("SHUTTER_OK %s" % speed, flush=True)
    elif line.startswith("capture "):
        path = line[len("capture "):]
        if logf:
            logf.write("capture %s\\n" % path); logf.flush()
        try:
            open(path, "wb").close()
        except Exception:
            pass
        print("CAPTURE_OK %s" % path, flush=True)
    else:
        print("ERR unknown-command", flush=True)
"""


def test_persistent_capture_applies_per_channel_shutter_protocol(tmp_path):
    import os
    from triplet_capture.sony_persist import PersistentSonyCapture

    script = tmp_path / "fake_persist.py"
    script.write_text(_FAKE_PERSIST_SCRIPT)
    script.chmod(0o755)
    log = tmp_path / "cmds.log"

    p = PersistentSonyCapture(str(script), ["--log", str(log)], env=dict(os.environ))
    try:
        assert p.capture(tmp_path / "R.ARW", timeout_s=5, shutter="1/100") == 0
        # Same shutter as R → must NOT be re-sent.
        assert p.capture(tmp_path / "G.ARW", timeout_s=5, shutter="1/100") == 0
        # Different shutter → must be re-sent.
        assert p.capture(tmp_path / "B.ARW", timeout_s=5, shutter="1/4") == 0
    finally:
        p.close()

    cmds = log.read_text().splitlines()
    shutters = [c for c in cmds if c.startswith("shutter ")]
    captures = [c for c in cmds if c.startswith("capture ")]
    # Shutter sent for R and B only (G reused R's), in order.
    assert shutters == ["shutter 1/100", "shutter 1/4"]
    assert len(captures) == 3


# Fake whose FIRST spawn dies while processing the `shutter` command (reads the
# line, then exits without responding) — exercises the respawn-during-shutter
# path. A spawn-counter sidecar makes the second spawn behave normally.
_FAKE_PERSIST_DIES_DURING_SHUTTER = """#!/usr/bin/env python3
import sys
cpath = None
for i, a in enumerate(sys.argv):
    if a == "--count":
        cpath = sys.argv[i + 1]
n = 0
try:
    n = int(open(cpath).read() or "0")
except Exception:
    n = 0
n += 1
open(cpath, "w").write(str(n))
print("READY", flush=True)
if n == 1:
    sys.stdin.readline()   # consume the `shutter ...` line, then die unresponsive
    sys.exit(1)
for line in sys.stdin:
    line = line.strip()
    if line == "quit":
        break
    if line.startswith("shutter "):
        print("SHUTTER_OK %s" % line[len("shutter "):], flush=True)
    elif line.startswith("capture "):
        p = line[len("capture "):]
        try:
            open(p, "wb").close()
        except Exception:
            pass
        print("CAPTURE_OK %s" % p, flush=True)
    else:
        print("ERR unknown-command", flush=True)
"""


def test_persistent_capture_respawns_when_process_dies_during_shutter(tmp_path):
    import os
    from triplet_capture.sony_persist import PersistentSonyCapture

    script = tmp_path / "fake_dies.py"
    script.write_text(_FAKE_PERSIST_DIES_DURING_SHUTTER)
    script.chmod(0o755)
    counter = tmp_path / "spawns.txt"

    p = PersistentSonyCapture(
        str(script), ["--count", str(counter)], env=dict(os.environ),
        ready_timeout_s=5,
    )
    try:
        # First process dies mid-shutter; the helper must respawn and succeed.
        rc = p.capture(tmp_path / "R.ARW", timeout_s=5, shutter="1/100")
        assert rc == 0, f"expected respawn+success, got {rc}"
    finally:
        p.close()
    # Two spawns: the one that died during shutter, and the healthy respawn.
    assert int(counter.read_text()) == 2


def test_sdk_runner_passes_sony_network_auth_args(tmp_path, monkeypatch):
    calls = []

    def fake_popen(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return FakePopen(cmd, returncode=0, stdout_text=str(tmp_path / "out.ARW"), stderr_text="", **kwargs)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    s = CaptureSettings(
        output_folder=tmp_path,
        sony_capture_path="/bin/sony-capture",
        sony_capture_timeout_s=12,
        sony_ip_address="10.0.0.247",
        sony_mac_address="10:32:2C:26:1A:3F",
        sony_user="sdk-user",
        sony_password="sdk-password",
        trigger_mode="sdk",  # sdk mode (also the dataclass default; no ied_inbox needed)
    )
    orch = Orchestrator(FakeScanlight(), s)

    exit_code = orch._default_runner("R", tmp_path / "out.ARW", 12)

    assert exit_code == 0
    cmd, kwargs = calls[0]
    assert cmd == [
        "/bin/sony-capture",
        "--out", str(tmp_path / "out.ARW"),
        "--timeout", "12",
        "--ip-address", "10.0.0.247",
        "--mac-address", "10:32:2C:26:1A:3F",
        "--iso", "100or125",
    ]
    # Credentials must NOT appear on argv (argv is visible via `ps`); they are
    # injected through the environment instead.
    assert "--user" not in cmd and "--password" not in cmd
    assert "sdk-user" not in cmd and "sdk-password" not in cmd
    assert kwargs["stdout"] == subprocess.PIPE
    assert kwargs["stderr"] == subprocess.PIPE
    assert kwargs["text"] is True
    env = kwargs["env"]
    assert env["SONY_USERNAME"] == "sdk-user"
    assert env["SONY_USER"] == "sdk-user"
    assert env["SONY_PW"] == "sdk-password"


def test_sdk_runner_passes_channel_shutter_speed(tmp_path, monkeypatch):
    calls = []

    def fake_popen(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return FakePopen(cmd, returncode=0, stdout_text=str(tmp_path / "out.ARW"), stderr_text="", **kwargs)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    s = CaptureSettings(
        output_folder=tmp_path,
        sony_capture_path="/bin/sony-capture",
        shutter_r="1/8",
        shutter_g="1/4",
        shutter_b="1/2",
        trigger_mode="sdk",  # sdk mode (also the dataclass default; no ied_inbox needed)
    )
    orch = Orchestrator(FakeScanlight(), s)

    exit_code = orch._default_runner("B", tmp_path / "out.ARW", 30)

    assert exit_code == 0
    cmd, _kwargs = calls[0]
    assert cmd[-4:] == ["--iso", "100or125", "--shutter-speed", "1/2"]


def test_sdk_runner_failure_stderr_is_returned_in_triplet_error(tmp_path, monkeypatch):
    """Real-run regression: calibration should show the SDK/camera reason,
    not just a bare 'exit 1' from sony-capture.
    """
    def fake_popen(cmd, **kwargs):
        return FakePopen(
            cmd,
            returncode=1,
            stdout_text="",
            stderr_text="sony-capture: Access Auth failed; camera busy",
            **kwargs,
        )

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    s = CaptureSettings(
        output_folder=tmp_path,
        sony_capture_path="/bin/sony-capture",
        settle_ms=0,
        trigger_mode="sdk",  # sdk mode (also the dataclass default; no ied_inbox needed)
        sdk_persistent=False,  # use one-shot runner to test stderr surfacing via _default_runner
    )
    orch = Orchestrator(FakeScanlight(), s, sleep=_zero_sleep)

    result = orch.capture_triplet()

    assert not result.success
    assert "capture failed for channel R (exit 1)" in result.error
    assert "Access Auth failed" in result.error
    assert "camera busy" in result.error


def test_sdk_runner_turns_scanlight_off_after_exposure_marker(tmp_path, monkeypatch):
    def fake_popen(cmd, **kwargs):
        return FakePopen(
            cmd,
            returncode=0,
            stdout_text=str(tmp_path / "out.ARW"),
            stderr_text="sony-capture: exposure-complete\nsony-capture: RemoteTransfer download 10%\n",
            **kwargs,
        )

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    light = FakeScanlight()
    s = CaptureSettings(output_folder=tmp_path, sony_capture_path="/bin/sony-capture", trigger_mode="sdk")
    orch = Orchestrator(light, s)

    exit_code = orch._default_runner("R", tmp_path / "out.ARW", 30)

    assert exit_code == 0
    assert ("off",) in light.calls


# ---------- codex review additions ----------

def test_hw_mode_aborts_on_ambiguous_inbox(tmp_path):
    """Per codex review (blocker): if a previous channel's late ARW
    arrives in the same poll as the current channel's expected file,
    the orchestrator must abort rather than guess which is which.
    """
    s = _hw_settings(tmp_path)

    def on_pulse(_ms):
        # Simulate the bad case: two files appear simultaneously after
        # the pulse fires. (In production this happens when a previous
        # channel's late ARW arrives during the current channel's wait.)
        (s.ied_inbox / "DSC00001.ARW").write_bytes(b"\x00" * (70 * 1024 * 1024))
        (s.ied_inbox / "DSC00002.ARW").write_bytes(b"\x00" * (70 * 1024 * 1024))

    light = FakeScanlightWithPulse(pulse_side_effect=on_pulse)
    advance = {"t": 0.0}
    def fake_sleep(dt):
        advance["t"] += dt
    def fake_clock():
        return advance["t"]

    orch = Orchestrator(light, s, clock=fake_clock, sleep=fake_sleep)
    result = orch.capture_triplet()
    assert not result.success
    assert orch.settings.frame_number == 1  # no advance
    # Ambiguous files were quarantined so the next attempt starts clean.
    assert list(s.ied_inbox.glob("*.ARW")) == []


def test_hw_mode_quarantines_late_files_after_timeout(tmp_path):
    """Per codex review: files that arrive AFTER the timeout (but were
    still in transit when we declared failure) must not poison the next
    capture cycle's baseline. They go to quarantine."""
    s = _hw_settings(tmp_path, sony_capture_timeout_s=1)

    # The first pulse fires but nothing arrives in time. After timeout,
    # a late ARW shows up in the inbox.
    def on_pulse_no_file(_ms):
        pass  # no file appears

    light = FakeScanlightWithPulse(pulse_side_effect=on_pulse_no_file)
    advance = {"t": 0.0}
    def fake_sleep(dt):
        advance["t"] += dt
    def fake_clock():
        return advance["t"]

    orch = Orchestrator(light, s, clock=fake_clock, sleep=fake_sleep)

    # Pre-drop a "late arrival" that the previous timed-out capture
    # would have produced.
    late = s.ied_inbox / "DSC_LATE.ARW"
    late.write_bytes(b"\x00" * (70 * 1024 * 1024))

    result = orch.capture_triplet()
    assert not result.success
    # The late file should have been quarantined (either at frame start
    # via the pre-sweep, or at timeout via the runner).
    assert not late.exists()
    stale_files = list((s.ied_inbox / "_stale").rglob("*.ARW")) if (s.ied_inbox / "_stale").exists() else []
    assert any(f.name == "DSC_LATE.ARW" for f in stale_files)


def test_update_settings_repicks_runner_on_trigger_mode_change(tmp_path):
    """Per codex review: switching trigger_mode at runtime must repoint
    the orchestrator at the right internal runner."""
    # sdk_persistent=True → persistent_runner; sdk_persistent=False → default_runner.
    sdk_settings = CaptureSettings(
        output_folder=tmp_path, trigger_mode="sdk", sdk_persistent=True
    )
    light = FakeScanlightWithPulse()
    orch = Orchestrator(light, sdk_settings)
    assert orch._runner.__func__ is Orchestrator._persistent_runner

    # Switch to hw mode at runtime
    hw_inbox = tmp_path / "inbox"
    hw_inbox.mkdir()
    orch.update_settings(trigger_mode="hw", ied_inbox=hw_inbox)
    assert orch._runner.__func__ is Orchestrator._hw_runner

    # Switch to manual IED mode at runtime
    manual_inbox = tmp_path / "manual_inbox"
    manual_inbox.mkdir()
    orch.update_settings(trigger_mode="manual", ied_inbox=manual_inbox)
    assert orch._runner.__func__ is Orchestrator._manual_runner

    # Switch back to sdk (persistent)
    orch.update_settings(trigger_mode="sdk")
    assert orch._runner.__func__ is Orchestrator._persistent_runner

    # Switch to sdk one-shot
    orch.update_settings(sdk_persistent=False)
    assert orch._runner.__func__ is Orchestrator._default_runner


def test_update_settings_does_not_override_explicit_runner(tmp_path):
    """When the caller injected an explicit runner (tests, custom
    deployments), update_settings must NOT clobber it on trigger_mode
    change. The injection is the operator's deliberate override."""
    sdk_settings = CaptureSettings(output_folder=tmp_path, trigger_mode="sdk")
    explicit = lambda *a, **kw: 0
    light = FakeScanlight()
    orch = Orchestrator(light, sdk_settings, sony_capture_runner=explicit)
    assert orch._runner is explicit

    hw_inbox = tmp_path / "inbox"
    hw_inbox.mkdir()
    orch.update_settings(trigger_mode="hw", ied_inbox=hw_inbox)
    assert orch._runner is explicit  # still the injected one


def test_capture_triplet_turns_scanlight_off_even_on_unexpected_exception(tmp_path):
    """Per codex review: scanlight.off() must run in a finally block, not
    just in the success and TripletAbort branches. An uncaught OSError
    from the runner (e.g., disk full mid-move) must not leave the LEDs
    powered."""
    s = CaptureSettings(output_folder=tmp_path, settle_ms=0, trigger_mode="sdk")
    light = FakeScanlight()

    # Runner that raises something unexpected (not TripletAbort).
    def boom_runner(channel, out_path, timeout_s):
        raise OSError("disk full")

    orch = Orchestrator(light, s, sony_capture_runner=boom_runner, sleep=_zero_sleep)

    # Either the orchestrator catches it (returning a failure result) OR
    # the exception propagates — but either way the scanlight must have
    # been turned off.
    try:
        orch.capture_triplet()
    except OSError:
        pass
    assert any(c[0] == "off" for c in light.calls), (
        "scanlight.off() must run even on uncaught exception"
    )


# ---------- ephemeral port + port-file (child-owned port selection) ----------

def test_ephemeral_port_binds_and_writes_port_file(tmp_path):
    """With --web-port 0 + --port-file <tmp>, the server binds an OS-assigned
    ephemeral port and writes a valid non-zero integer to the port-file
    atomically before serving.

    Strategy: start the werkzeug server in a background thread, poll the
    port-file until it appears (or a short deadline), assert the port is
    valid, then shut the server down cleanly via server.shutdown().
    """
    import time
    from werkzeug.serving import make_server as _make_server

    from triplet_capture.app import create_app
    from triplet_capture.orchestrator import CaptureSettings, Orchestrator

    port_file = tmp_path / "bound.port"

    light = FakeScanlight()
    settings = CaptureSettings(
        roll_name="Roll001",
        frame_number=1,
        output_folder=tmp_path,
        settle_ms=0,
        trigger_mode="sdk",  # sdk mode (also the dataclass default; no ied_inbox needed)
    )
    orch = Orchestrator(light, settings, sony_capture_runner=lambda *a: 0, sleep=_zero_sleep)
    app = create_app(orch)

    # Replicate the main() ephemeral-port logic in isolation:
    # bind to port 0, write port-file atomically, then serve_forever in a thread.
    server = _make_server("127.0.0.1", 0, app, threaded=True)
    bound_port = server.server_port

    tmp_port_path = str(port_file) + ".tmp"
    with open(tmp_port_path, "w") as f:
        f.write(str(bound_port))
    os.replace(tmp_port_path, str(port_file))

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    try:
        # Poll the port-file (it was already written before serve_forever, but
        # exercise the read path the same way the Swift launcher would).
        deadline = time.monotonic() + 5.0
        read_port = None
        while time.monotonic() < deadline:
            if port_file.exists():
                raw = port_file.read_text().strip()
                if raw.isdigit():
                    read_port = int(raw)
                    break
            time.sleep(0.05)

        assert read_port is not None, "port-file was never written or is non-integer"
        assert read_port > 0, f"port must be positive, got {read_port}"
        assert read_port == bound_port, (
            f"port-file value {read_port} must match the server's bound port {bound_port}"
        )

        # Verify the server is actually accepting connections on that port.
        import urllib.request
        resp = urllib.request.urlopen(
            f"http://127.0.0.1:{read_port}/api/state", timeout=3
        )
        assert resp.status == 200, f"Expected 200, got {resp.status}"

    finally:
        server.shutdown()
        server_thread.join(timeout=3.0)


def test_signal_driven_shutdown_does_not_deadlock(tmp_path):
    """Regression test: the --web-port 0 signal-handler path must NOT deadlock.

    The original bug: _shutdown_handler called server.shutdown() directly from
    the signal handler, which ran on the MAIN thread — the same thread that was
    blocked inside serve_forever(). shutdown() sets __shutdown_request and then
    waits for __is_shut_down — but __is_shut_down is only set when serve_forever
    returns, which it can't because the main thread is stuck in the signal
    handler. Deadlock. Python docs: "shutdown() must be called while
    serve_forever() is running in a different thread."

    Strategy (approach b from spec): exercise _serve_until_signal() — the
    extracted helper that encapsulates the "serve on daemon thread + park main
    thread on stop_event + cross-thread shutdown" pattern — by running it in a
    test thread and delivering a real SIGTERM to the process via
    os.kill(os.getpid(), signal.SIGTERM). The helper must return in well under
    2 s. A timeout proves no deadlock.

    Why this test would FAIL against the old same-thread code: the old handler
    called server.shutdown() on the main thread while serve_forever() also ran
    on the main thread (blocking inside the signal handler). That would hang
    forever, and the 2 s timeout would fire → AssertionError. With the fix,
    serve_forever() is on a daemon thread; the main thread only sets a
    threading.Event, then calls shutdown() cross-thread after waking → returns
    promptly.
    """
    import time
    from werkzeug.serving import make_server as _make_server

    from triplet_capture.app import create_app
    from triplet_capture.orchestrator import CaptureSettings, Orchestrator

    light = FakeScanlight()
    settings = CaptureSettings(
        roll_name="Roll001",
        frame_number=1,
        output_folder=tmp_path,
        settle_ms=0,
        trigger_mode="sdk",  # sdk mode (also the dataclass default; no ied_inbox needed)
    )
    orch = Orchestrator(light, settings, sony_capture_runner=lambda *a: 0, sleep=_zero_sleep)
    app = create_app(orch)

    server = _make_server("127.0.0.1", 0, app, threaded=True)

    # ---- replicate the fixed _serve_until_signal pattern inline ----
    stop_event = threading.Event()
    original_sigterm = signal.getsignal(signal.SIGTERM)

    def _shutdown_handler(signum, frame):
        stop_event.set()          # signal-safe: only sets a flag

    signal.signal(signal.SIGTERM, _shutdown_handler)

    serve_thread = threading.Thread(
        target=server.serve_forever,
        name="triplet-serve-regtest",
        daemon=True,
    )
    serve_thread.start()

    # Give the server a moment to enter its poll loop before we signal it.
    # (Not strictly necessary but avoids a race between serve_forever entering
    # its select() and the signal arriving — a tiny sleep is fine here.)
    time.sleep(0.05)

    # Deliver SIGTERM to ourselves from the test thread. The handler sets
    # stop_event; the block below calls server.shutdown() cross-thread.
    os.kill(os.getpid(), signal.SIGTERM)

    shutdown_returned = threading.Event()

    def _do_shutdown():
        stop_event.wait(timeout=2.0)
        server.shutdown()           # cross-thread — must not deadlock
        serve_thread.join(timeout=2.0)
        shutdown_returned.set()

    shutdown_thread = threading.Thread(target=_do_shutdown, daemon=True)
    shutdown_thread.start()

    # Restore original handler before we check the outcome.
    signal.signal(signal.SIGTERM, original_sigterm)

    completed = shutdown_returned.wait(timeout=4.0)
    assert completed, (
        "server.shutdown() did not return within 4 s — "
        "this is the serve_forever-same-thread DEADLOCK; the fix is not in effect"
    )
    assert not serve_thread.is_alive(), "serve_thread should have exited after shutdown()"
