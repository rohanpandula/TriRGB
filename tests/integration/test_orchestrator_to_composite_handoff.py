"""Integration tests: triplet-capture orchestrator → batch-composite filename contract.

Requirement: R-15 — end-to-end synthetic-ARW integration test suite.

Two risk patterns that per-package unit tests cannot catch:

  1. Filename drift: the orchestrator's HW-mode path could change the naming
     convention (e.g. suffix from `_R.ARW` to `-R.arw`) without batch-composite's
     tests noticing, because batch-composite stubs the orchestrator.

  2. Underscore-bearing roll names: `FRAME_PATTERN` uses `(?P<roll>.+?)` with a
     lazy quantifier.  A naive reading suggests `.+?` might stop at the first `_`
     and produce roll='My', frame_group='Roll_001_Frame001', breaking discovery.
     In practice the `_Frame(NNN)_[RGB].ARW$` anchor forces `.+?` to consume
     up through the *last* `_Frame` literal, so `My_Roll_001` survives intact.
     This test is the proof.

These tests do NOT exercise rgb-composite or batch-composite compositing — they
verify only the filename contract between the orchestrator and `discover_frames`.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from triplet_capture import Orchestrator, CaptureSettings
from batch_composite import discover_frames
from batch_composite.batch import FRAME_PATTERN


# ---------------------------------------------------------------------------
# Module-level test helpers
# ---------------------------------------------------------------------------

class FakeScanlightWithPulse:
    """Minimal duck-type of scanlight.Scanlight for HW-trigger tests.

    Mirrors phase2/triplet-capture/tests/test_orchestrator.py lines 319-329.
    """

    def __init__(self, pulse_side_effect=None):
        self.pulse_side_effect = pulse_side_effect
        self.pulses: list[int] = []

    def set_color(self, r=0, g=0, b=0, w=0, save=False) -> None:
        pass

    def off(self) -> None:
        pass

    def pulse_shutter(self, pulse_ms: int = 100) -> None:
        self.pulses.append(pulse_ms)
        if self.pulse_side_effect is not None:
            self.pulse_side_effect(pulse_ms)


def _make_hw_settings(tmp_path: Path, roll_name: str, **overrides) -> CaptureSettings:
    """Build CaptureSettings for HW-trigger mode tests.

    Mirrors the `_hw_settings` helper in test_orchestrator.py lines 332-353,
    parameterized on `roll_name` so both tests can use it.
    """
    inbox_dir = tmp_path / "ied_inbox"
    inbox_dir.mkdir(exist_ok=True)
    out_dir = tmp_path / "scans"
    defaults = dict(
        roll_name=roll_name,
        frame_number=1,
        output_folder=out_dir,
        level_r=200,
        level_g=180,
        level_b=160,
        settle_ms=0,
        trigger_mode="hw",
        ied_inbox=inbox_dir,
        shutter_pulse_ms=100,
        sony_capture_timeout_s=5,
        inbox_stable_for_s=0.4,
        inbox_poll_interval_s=0.1,
    )
    defaults.update(overrides)
    return CaptureSettings(**defaults)


# ---------------------------------------------------------------------------
# Test 1: orchestrator HW-mode filenames match FRAME_PATTERN + discover_frames
# ---------------------------------------------------------------------------

def test_orchestrator_writes_files_in_batch_compatible_pattern(tmp_path):
    """Three-frame HW-mode run: every produced file matches FRAME_PATTERN;
    discover_frames picks up all three complete FrameGroups."""
    s = _make_hw_settings(tmp_path, roll_name="RollIntTest")

    # Fake clock + sleep so stability windows pass without real wall-clock time.
    advance = {"t": 0.0}

    def fake_sleep(dt):
        advance["t"] += dt

    def fake_clock():
        return advance["t"]

    # On each pulse, drop a 70 MiB fake ARW into the inbox.
    # The orchestrator sanity-checks file size against PLAUSIBLE_RAW_MIN_BYTES
    # (40 MiB); 70 MiB comfortably passes.
    counter = {"n": 0}

    def on_pulse(_ms):
        counter["n"] += 1
        f = s.ied_inbox / f"DSC{counter['n']:05d}.ARW"
        f.write_bytes(b"\x00" * (70 * 1024 * 1024))

    light = FakeScanlightWithPulse(pulse_side_effect=on_pulse)
    orch = Orchestrator(light, s, clock=fake_clock, sleep=fake_sleep)

    # Capture three frames back-to-back (frame_number advances automatically
    # on success: 1 → 2 → 3).
    results = [orch.capture_triplet() for _ in range(3)]

    # All three captures must succeed.
    for i, r in enumerate(results, start=1):
        assert r.success, f"Frame {i} failed: {r.error}"

    # Nine total pulses: 3 channels × 3 frames.
    assert len(light.pulses) == 9, (
        f"expected 9 pulses (3 channels × 3 frames), got {len(light.pulses)}"
    )

    # Critical handoff assertion: every produced file matches FRAME_PATTERN.
    all_paths = [path for r in results for path in r.files.values()]
    assert len(all_paths) == 9, f"expected 9 output files, got {len(all_paths)}"

    for p in all_paths:
        m = FRAME_PATTERN.match(p.name)
        assert m is not None, (
            f"orchestrator file {p.name!r} does not match FRAME_PATTERN "
            f"({FRAME_PATTERN.pattern!r}); filename format drifted?"
        )
        assert m.group("roll") == "RollIntTest", (
            f"expected roll='RollIntTest', got {m.group('roll')!r} from {p.name!r}"
        )
        assert m.group("channel").upper() in ("R", "G", "B"), (
            f"unexpected channel {m.group('channel')!r} in {p.name!r}"
        )
        frame_num = int(m.group("frame"))
        assert 1 <= frame_num <= 3, (
            f"frame number {frame_num} out of expected range [1,3] in {p.name!r}"
        )

    # End-to-end handoff: discover_frames picks up all three frames, all complete.
    groups = discover_frames(s.output_folder)
    assert len(groups) == 3, (
        f"discover_frames returned {len(groups)} groups, expected 3"
    )
    assert all(g.complete for g in groups), (
        f"some FrameGroups are incomplete: {[g for g in groups if not g.complete]}"
    )
    frame_numbers = {g.frame_number for g in groups}
    assert frame_numbers == {1, 2, 3}, (
        f"expected frame numbers {{1,2,3}}, got {frame_numbers}"
    )
    roll_names = {g.roll for g in groups}
    assert roll_names == {"RollIntTest"}, (
        f"expected roll name 'RollIntTest', got {roll_names}"
    )


# ---------------------------------------------------------------------------
# Test 2: roll name with underscores round-trips through FRAME_PATTERN
# ---------------------------------------------------------------------------

def test_roll_name_with_underscores(tmp_path):
    """Roll name 'My_Roll_001' survives the FRAME_PATTERN lazy quantifier intact.

    FRAME_PATTERN uses `(?P<roll>.+?)` (lazy).  The anchor
    `_Frame(NNN)_[RGB].ARW$` forces `.+?` to consume up to the last
    occurrence of `_Frame` in the filename, so `My_Roll_001_Frame001_R.ARW`
    correctly yields roll='My_Roll_001', not roll='My'.
    """
    s = _make_hw_settings(tmp_path, roll_name="My_Roll_001")

    advance = {"t": 0.0}

    def fake_sleep(dt):
        advance["t"] += dt

    def fake_clock():
        return advance["t"]

    counter = {"n": 0}

    def on_pulse(_ms):
        counter["n"] += 1
        f = s.ied_inbox / f"DSC{counter['n']:05d}.ARW"
        f.write_bytes(b"\x00" * (70 * 1024 * 1024))

    light = FakeScanlightWithPulse(pulse_side_effect=on_pulse)
    orch = Orchestrator(light, s, clock=fake_clock, sleep=fake_sleep)

    result = orch.capture_triplet()
    assert result.success, f"capture failed: {result.error}"

    # Verify the orchestrator wrote underscore-bearing filenames.
    for channel, path in result.files.items():
        expected_name = f"My_Roll_001_Frame001_{channel}.ARW"
        assert path.name == expected_name, (
            f"expected filename {expected_name!r}, got {path.name!r}"
        )

        # FRAME_PATTERN must match, and the roll group must be the full name.
        m = FRAME_PATTERN.match(path.name)
        assert m is not None, (
            f"FRAME_PATTERN did not match {path.name!r}; "
            "orchestrator filename format drifted?"
        )
        assert m.group("roll") == "My_Roll_001", (
            f"expected roll='My_Roll_001', got {m.group('roll')!r}; "
            "FRAME_PATTERN lazy quantifier mis-parsed the underscored roll name"
        )
        assert m.group("frame") == "001", (
            f"expected frame='001', got {m.group('frame')!r}"
        )
        assert m.group("channel").upper() == channel, (
            f"expected channel={channel!r}, got {m.group('channel')!r}"
        )

    # discover_frames must group the three files as a single complete frame
    # with the full roll name preserved.
    groups = discover_frames(s.output_folder)
    assert len(groups) == 1, (
        f"expected 1 FrameGroup, got {len(groups)}; "
        f"groups: {[(g.roll, g.frame_number) for g in groups]}"
    )
    assert groups[0].roll == "My_Roll_001", (
        f"expected roll='My_Roll_001', got {groups[0].roll!r}"
    )
    assert groups[0].frame_number == 1, (
        f"expected frame_number=1, got {groups[0].frame_number}"
    )
    assert groups[0].complete is True, (
        f"FrameGroup is not complete: missing channels {groups[0].missing_channels}"
    )
