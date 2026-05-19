"""Watch a directory for newly-arrived ARW files from Imaging Edge Desktop.

Why this exists
---------------
In hardware-trigger mode the Mac doesn't issue the shutter command — the
Scanlight does, via its 3.5mm jack into the camera's USB-C trigger pins.
Imaging Edge Desktop (or any Sony PC Remote receiver) is the thing that
actually picks the resulting RAW up off the camera and writes it to disk.

The orchestrator's job per channel is then:
  1. Set Scanlight color.
  2. Pulse the Scanlight's shutter output.
  3. Wait for a new `.ARW` to land in IED's save folder.
  4. Move it to our roll's `{roll}_Frame{NNN}_{channel}.ARW` path.

This module owns step 3+4. Tests stub the `wait_for_new_file` function
exactly the way they currently stub `sony-capture` — point a fake
implementation at it, no real disk-watcher needed.

Design choices
--------------
- **Polling, not fsevents/inotify.** Polling at ~5 Hz is plenty for a
  workflow gated by 60+ MB RAFs arriving over Wi-Fi. fsevents would add a
  macOS-only dependency for negligible benefit.
- **Track files as a set diff before and after the pulse.** "Newest mtime"
  alone fails when IED writes a `.ARW.tmp` then renames; "highest
  filename" fails when IED's counter wraps. A diff of full filenames is
  unambiguous and survives both.
- **Wait for file size to stabilize** before declaring the file complete.
  IED downloads can take seconds; if we move the file mid-write we get
  a truncated RAW that breaks demosaic downstream.
"""
from __future__ import annotations

import logging
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional


logger = logging.getLogger("triplet-capture.inbox")


# Sony save extensions IED can emit. Lowercase variants included because
# different IED versions / SD-card pulls have used both.
_ARW_EXTS = (".ARW", ".arw")

# Default polling cadence. 5 Hz is fast enough that the first-file-to-land
# wins by < 200 ms (well below LED settle time), and slow enough that we
# don't hammer the filesystem during a long Wi-Fi transfer.
DEFAULT_POLL_INTERVAL_S = 0.2

# How long the file size must stay constant before we consider it
# "done writing." 3 s is conservative enough that a Wi-Fi stall mid-
# transfer is unlikely to be misread as a completed write (typical
# stalls observed in Sony's PC Remote stack are sub-2 s). Configurable
# at the caller level if your link is unusually flaky.
DEFAULT_STABLE_FOR_S = 3.0


class AmbiguousInboxError(RuntimeError):
    """Raised when more than one new file passes stability in the same
    poll — typically a previous channel's late-arriving RAF showed up
    alongside the current channel's, and assigning either one to the
    current channel would silently corrupt the channel-to-file mapping.
    """


def list_inbox(inbox: Path) -> set[Path]:
    """Return the set of ARW files currently in `inbox`. Non-recursive."""
    if not inbox.is_dir():
        return set()
    out: set[Path] = set()
    for ext in _ARW_EXTS:
        out.update(p for p in inbox.glob(f"*{ext}") if p.is_file())
    return out


def wait_for_new_file(
    inbox: Path,
    *,
    baseline: Iterable[Path],
    timeout_s: float,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    stable_for_s: float = DEFAULT_STABLE_FOR_S,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Optional[Path]:
    """Block until a new `.ARW` appears in `inbox` and finishes writing.

    Args:
        inbox: directory IED writes its captures to.
        baseline: files that were present BEFORE the trigger fired — the
            return value is guaranteed to not be one of these.
        timeout_s: hard upper bound on wait time, in seconds. Returns
            None if no new file appears within this window.
        poll_interval_s: how often to re-scan the directory.
        stable_for_s: file size must hold constant for this long before
            the file is declared finished. Protects against picking up
            a partial write.
        clock / sleep: injectable for tests.

    Returns:
        Path to the newly-arrived file once its size has stabilized,
        or None on timeout.
    """
    baseline_set = set(baseline)
    deadline = clock() + timeout_s
    seen_size: dict[Path, tuple[int, float]] = {}

    while clock() < deadline:
        now = list_inbox(inbox)
        new_files = now - baseline_set
        stable_now: list[Path] = []
        for f in new_files:
            try:
                size = f.stat().st_size
            except FileNotFoundError:
                # File got renamed/removed mid-poll — skip; we'll re-see
                # it on the next iteration if it's real.
                continue

            prev_size, first_seen_at = seen_size.get(f, (-1, clock()))
            if size == prev_size and size > 0:
                # Same size as last poll. Has it been stable long enough?
                if clock() - first_seen_at >= stable_for_s:
                    stable_now.append(f)
                else:
                    seen_size[f] = (size, first_seen_at)
            else:
                # New size or first sighting — restart the stability timer.
                seen_size[f] = (size, clock())

        if len(stable_now) > 1:
            # Two-or-more "ready" files at the same time means we can't
            # tell which one belongs to the channel we just pulsed for.
            # Refuse rather than guess.
            raise AmbiguousInboxError(
                f"{len(stable_now)} new files in {inbox} passed stability "
                f"simultaneously; cannot map to the current channel: "
                f"{sorted(p.name for p in stable_now)}"
            )
        if stable_now:
            return stable_now[0]

        sleep(poll_interval_s)

    return None


def claim_new_file(src: Path, dst: Path) -> Path:
    """Move `src` to `dst`, creating parent dirs. Returns the new path.

    Uses `shutil.move`, which handles the cross-volume case (IED inbox
    on internal SSD, roll output on an external SSD) by falling back to
    a copy-then-delete sequence. `Path.rename()` alone would raise
    `OSError(EXDEV)` in that situation.

    Raises FileNotFoundError if `src` vanished before the move.
    Raises OSError for other I/O failures (out of space, permissions).
    """
    if not src.exists():
        raise FileNotFoundError(f"source no longer exists: {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return dst


def quarantine_stale_files(
    inbox: Path,
    *,
    dest_root: Optional[Path] = None,
    timestamp: Optional[str] = None,
) -> list[Path]:
    """Move any current ARWs in `inbox` aside before a new capture cycle.

    Why: in hardware-trigger mode a previous channel's RAF can still be
    in transit when the next channel's wait window starts. If the late
    file then lands during the new wait it gets misassigned. We pre-
    empt this by sweeping the inbox at the start of every frame, so the
    new triplet's baseline is empty.

    Files are moved to `<dest_root>/<timestamp>/` rather than deleted —
    if the operator wants to investigate later, the data is recoverable.
    Default `dest_root` is `<inbox>/_stale/`; default timestamp is the
    current UTC time in ISO basic format.

    Returns the list of new destination paths. Empty if the inbox was
    already clean.
    """
    if not inbox.is_dir():
        return []
    current = list_inbox(inbox)
    if not current:
        return []
    if dest_root is None:
        dest_root = inbox / "_stale"
    if timestamp is None:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    bucket = dest_root / timestamp
    bucket.mkdir(parents=True, exist_ok=True)
    moved: list[Path] = []
    for f in sorted(current):
        new_path = bucket / f.name
        try:
            shutil.move(str(f), str(new_path))
            moved.append(new_path)
        except OSError as exc:
            logger.warning("could not quarantine %s: %s", f, exc)
    if moved:
        logger.info(
            "quarantined %d stale ARW(s) from %s → %s",
            len(moved), inbox, bucket,
        )
    return moved
