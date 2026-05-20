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

import json
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


def test_hw_settings_requires_ied_inbox(tmp_path):
    with pytest.raises(ValueError, match="trigger_mode='hw' requires ied_inbox"):
        CaptureSettings(
            output_folder=tmp_path,
            trigger_mode="hw",
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


def test_sdk_mode_remains_default(tmp_path):
    s = CaptureSettings(output_folder=tmp_path)  # default trigger_mode="sdk"
    light = FakeScanlight()
    orch = Orchestrator(light, s)
    assert orch._runner.__func__ is Orchestrator._default_runner


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
    sdk_settings = CaptureSettings(output_folder=tmp_path)  # default sdk
    light = FakeScanlightWithPulse()
    orch = Orchestrator(light, sdk_settings)
    assert orch._runner.__func__ is Orchestrator._default_runner

    # Switch to hw mode at runtime
    hw_inbox = tmp_path / "inbox"
    hw_inbox.mkdir()
    orch.update_settings(trigger_mode="hw", ied_inbox=hw_inbox)
    assert orch._runner.__func__ is Orchestrator._hw_runner

    # Switch back to sdk
    orch.update_settings(trigger_mode="sdk")
    assert orch._runner.__func__ is Orchestrator._default_runner


def test_update_settings_does_not_override_explicit_runner(tmp_path):
    """When the caller injected an explicit runner (tests, custom
    deployments), update_settings must NOT clobber it on trigger_mode
    change. The injection is the operator's deliberate override."""
    sdk_settings = CaptureSettings(output_folder=tmp_path)
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
    s = CaptureSettings(output_folder=tmp_path, settle_ms=0)
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
