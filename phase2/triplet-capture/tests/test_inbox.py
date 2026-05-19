"""Tests for the IED inbox watcher.

The watcher polls a directory for newly-arrived ARW files. These tests
exercise the polling logic with a fake clock and an inbox we manipulate
directly between polls. No fsevents, no real waiting.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from triplet_capture import inbox


class FakeClock:
    """Monotonic clock + sleep that advances a single internal counter.

    `sleep(dt)` advances the clock by exactly `dt`. Callers don't actually
    wait. This lets a test simulate hours of polling in microseconds.
    """

    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, dt: float) -> None:
        self.t += dt


def _drop_arw(d: Path, name: str, size: int = 1024) -> Path:
    p = d / name
    p.write_bytes(b"\x00" * size)
    return p


def test_list_inbox_empty(tmp_path):
    assert inbox.list_inbox(tmp_path) == set()


def test_list_inbox_picks_up_arw(tmp_path):
    a = _drop_arw(tmp_path, "DSC00001.ARW")
    _drop_arw(tmp_path, "ignore.txt")
    assert inbox.list_inbox(tmp_path) == {a}


def test_list_inbox_case_insensitive_extension(tmp_path):
    a = _drop_arw(tmp_path, "DSC00001.ARW")
    b = _drop_arw(tmp_path, "DSC00002.arw")
    assert inbox.list_inbox(tmp_path) == {a, b}


def test_list_inbox_missing_dir_returns_empty(tmp_path):
    assert inbox.list_inbox(tmp_path / "nope") == set()


def test_wait_for_new_file_returns_on_arrival(tmp_path):
    """A file dropped mid-poll should be picked up, but only after the
    stability window elapses."""
    clock = FakeClock()
    baseline = inbox.list_inbox(tmp_path)

    polls = {"n": 0}

    def fake_sleep(dt):
        polls["n"] += 1
        clock.sleep(dt)
        if polls["n"] == 1:
            # File appears on first poll
            _drop_arw(tmp_path, "DSC00001.ARW", size=1024)

    result = inbox.wait_for_new_file(
        tmp_path,
        baseline=baseline,
        timeout_s=10.0,
        stable_for_s=0.5,
        poll_interval_s=0.2,
        clock=clock.now,
        sleep=fake_sleep,
    )
    assert result is not None
    assert result.name == "DSC00001.ARW"


def test_wait_for_new_file_waits_for_stable_size(tmp_path):
    """File grows over multiple polls — the watcher must wait for size
    to hold steady before returning."""
    clock = FakeClock()
    baseline = inbox.list_inbox(tmp_path)
    target = tmp_path / "DSC00001.ARW"

    growth = iter([10_000, 30_000, 60_000, 80_000, 80_000, 80_000, 80_000, 80_000])

    def fake_sleep(dt):
        clock.sleep(dt)
        try:
            size = next(growth)
            target.write_bytes(b"\x00" * size)
        except StopIteration:
            pass

    result = inbox.wait_for_new_file(
        tmp_path,
        baseline=baseline,
        timeout_s=30.0,
        stable_for_s=0.4,  # ~2 polls at 0.2 interval
        poll_interval_s=0.2,
        clock=clock.now,
        sleep=fake_sleep,
    )
    assert result == target
    # Final size should be the stable value
    assert target.stat().st_size == 80_000


def test_wait_for_new_file_timeout_returns_none(tmp_path):
    """No file ever arrives → returns None at the deadline."""
    clock = FakeClock()
    baseline = inbox.list_inbox(tmp_path)

    def fake_sleep(dt):
        clock.sleep(dt)

    result = inbox.wait_for_new_file(
        tmp_path,
        baseline=baseline,
        timeout_s=2.0,
        poll_interval_s=0.2,
        stable_for_s=0.5,
        clock=clock.now,
        sleep=fake_sleep,
    )
    assert result is None


def test_wait_for_new_file_ignores_baseline_files(tmp_path):
    """Files present in baseline must NOT be returned, even though they
    pass every other check (exist, stable size). Baseline is the snapshot
    BEFORE the trigger fired."""
    pre_existing = _drop_arw(tmp_path, "DSC00001.ARW", size=80_000)
    baseline = inbox.list_inbox(tmp_path)
    assert pre_existing in baseline

    clock = FakeClock()
    polls = {"n": 0}
    new_target = tmp_path / "DSC00002.ARW"

    def fake_sleep(dt):
        polls["n"] += 1
        clock.sleep(dt)
        if polls["n"] == 2:
            new_target.write_bytes(b"\x00" * 60_000)

    result = inbox.wait_for_new_file(
        tmp_path,
        baseline=baseline,
        timeout_s=20.0,
        poll_interval_s=0.2,
        stable_for_s=0.4,
        clock=clock.now,
        sleep=fake_sleep,
    )
    assert result == new_target
    # Original file untouched
    assert pre_existing.exists()


def test_claim_new_file_moves_and_creates_parents(tmp_path):
    src = _drop_arw(tmp_path, "DSC00001.ARW", size=1234)
    dst = tmp_path / "renamed" / "deep" / "Roll001_Frame001_R.ARW"
    result = inbox.claim_new_file(src, dst)
    assert result == dst
    assert dst.exists()
    assert not src.exists()
    assert dst.stat().st_size == 1234


def test_claim_new_file_raises_on_missing_src(tmp_path):
    src = tmp_path / "ghost.ARW"
    dst = tmp_path / "dest.ARW"
    with pytest.raises(FileNotFoundError):
        inbox.claim_new_file(src, dst)


# ---------- codex review additions ----------

def test_ambiguous_inbox_when_two_files_stable_at_once(tmp_path):
    """Per codex review: two files passing stability simultaneously
    means we can't tell which one belongs to the channel we pulsed for.
    The watcher must refuse rather than guess.
    """
    clock = FakeClock()
    baseline = inbox.list_inbox(tmp_path)

    # Drop two files immediately so both pass stability together.
    _drop_arw(tmp_path, "A.ARW", size=70_000)
    _drop_arw(tmp_path, "B.ARW", size=70_000)

    def fake_sleep(dt):
        clock.sleep(dt)
        # don't modify files — they stay stable

    with pytest.raises(inbox.AmbiguousInboxError, match="passed stability"):
        inbox.wait_for_new_file(
            tmp_path,
            baseline=baseline,
            timeout_s=10.0,
            stable_for_s=0.3,
            poll_interval_s=0.2,
            clock=clock.now,
            sleep=fake_sleep,
        )


def test_quarantine_stale_files_moves_arws(tmp_path):
    inbox_dir = tmp_path / "inbox"
    inbox_dir.mkdir()
    a = _drop_arw(inbox_dir, "DSC00001.ARW")
    b = _drop_arw(inbox_dir, "DSC00002.ARW")
    moved = inbox.quarantine_stale_files(inbox_dir, timestamp="20260518T120000Z")
    assert len(moved) == 2
    assert not a.exists()
    assert not b.exists()
    stale_dir = inbox_dir / "_stale" / "20260518T120000Z"
    assert stale_dir.is_dir()
    assert (stale_dir / "DSC00001.ARW").exists()
    assert (stale_dir / "DSC00002.ARW").exists()


def test_quarantine_stale_files_empty_inbox_noop(tmp_path):
    inbox_dir = tmp_path / "inbox"
    inbox_dir.mkdir()
    assert inbox.quarantine_stale_files(inbox_dir) == []
    # No _stale subdir is created when there's nothing to move
    assert not (inbox_dir / "_stale").exists()


def test_claim_new_file_uses_shutil_move(tmp_path, monkeypatch):
    """Per codex review: cross-volume moves must work (shutil.move
    handles EXDEV by falling back to copy+delete). We can't easily test
    a real cross-volume scenario in CI, but we can verify the
    implementation calls shutil.move rather than Path.rename — that's
    what gives us the cross-device safety."""
    import shutil
    calls = []
    real_move = shutil.move

    def spy_move(src, dst):
        calls.append((str(src), str(dst)))
        return real_move(src, dst)

    monkeypatch.setattr(inbox.shutil, "move", spy_move)

    src = _drop_arw(tmp_path, "DSC00001.ARW", size=1024)
    dst = tmp_path / "out" / "Roll001_Frame001_R.ARW"
    result = inbox.claim_new_file(src, dst)
    assert result == dst
    assert dst.exists()
    assert len(calls) == 1
