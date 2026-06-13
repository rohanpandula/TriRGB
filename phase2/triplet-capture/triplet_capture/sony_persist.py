"""PersistentSonyCapture — reusable per-session sony-capture subprocess.

Implements the persistent-mode contract (added to sony-capture in parallel):

  Spawn:   <binary> <regular-flags> --persist
           On connect → "READY\n" on stdout.
           On connect failure → "FAIL <reason>\n" + nonzero exit.

  Capture: write  "capture <abs-out-path>\n" on stdin.
           Success → "CAPTURE_OK <path>\n" on stdout.
           Failure → "CAPTURE_FAIL <reason>\n" (session stays alive).

  Marker:  SONY_CAPTURE_EXPOSURE_COMPLETE on stderr per capture
           (triggers the callback hook so the LED can be turned off early).

  Quit:    write "quit\n" on stdin or close stdin → exit 0.

The one-shot `_default_runner` in orchestrator.py stays as the fallback.
"""
from __future__ import annotations

import io
import logging
import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("triplet-capture.persist")

# Sentinel written by the binary on stderr when the shutter has returned.
SONY_EXPOSURE_COMPLETE_MARKER = "sony-capture: exposure-complete"

_DEFAULT_READY_TIMEOUT_S = 30
_DEFAULT_CAPTURE_TIMEOUT_S = 35  # single capture; 5 s slack over typical 30 s
_DEFAULT_KILL_TIMEOUT_S = 3


class PersistentSonyCapture:
    """Long-lived wrapper around a `sony-capture --persist` subprocess.

    Usage
    -----
    persist = PersistentSonyCapture(binary, cmd_extras, env)
    try:
        exit_code = persist.capture("/tmp/frame_R.ARW", timeout_s=30)
    finally:
        persist.close()

    Thread safety
    -------------
    `capture()` is NOT thread-safe; the orchestrator serialises calls through
    its own `_lock` so only one capture runs at a time per session.  `close()`
    is safe to call from any thread.

    Respawn policy
    --------------
    If the subprocess dies between two captures (e.g. intermittent Wi-Fi drop),
    `capture()` respawns it once and retries the capture.  If the respawned
    process also fails, it returns the failure exit code (no further retries).

    Seam for testing
    ----------------
    Pass a `popen_factory` callable that takes `(cmd, **kwargs)` and returns a
    Popen-compatible mock.  The default is `subprocess.Popen`.
    """

    def __init__(
        self,
        binary: str,
        cmd_extras: list[str],
        env: dict,
        *,
        ready_timeout_s: float = _DEFAULT_READY_TIMEOUT_S,
        kill_timeout_s: float = _DEFAULT_KILL_TIMEOUT_S,
        on_exposure_complete: Optional[Callable[[], None]] = None,
        popen_factory: Optional[Callable[..., "subprocess.Popen[str]"]] = None,
    ) -> None:
        self._binary = binary
        self._cmd_extras = list(cmd_extras)
        self._env = env
        self._ready_timeout_s = ready_timeout_s
        self._kill_timeout_s = kill_timeout_s
        # Called once per capture when the SONY_EXPOSURE_COMPLETE_MARKER appears
        # on stderr (to turn the LED off early).  Must be non-blocking; called
        # from the stderr drain thread.
        self._on_exposure_complete = on_exposure_complete
        self._popen_factory = popen_factory or subprocess.Popen

        self._proc: Optional["subprocess.Popen[str]"] = None
        self._closed = False
        # Last shutter applied to the live session via the `shutter` command.
        # Reset to None on (re)spawn — a fresh process is at the camera's own
        # shutter, so the next non-None request must re-apply.
        self._last_shutter: Optional[str] = None

        # Per-capture channel for routing stdout lines.
        self._stdout_q: "queue.Queue[str]" = queue.Queue()
        # Stderr drain thread (runs for the lifetime of the process).
        self._stderr_thread: Optional[threading.Thread] = None
        self._stdout_thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def capture(
        self, out_path: Path, *, timeout_s: int, shutter: Optional[str] = None
    ) -> int:
        """Trigger one capture via the persistent session.

        `shutter` (e.g. "1/100") is applied to the live session before the
        capture when it differs from the last-applied value — narrowband RGB
        uses a different shutter per channel. `None` leaves the current shutter
        untouched (matching the one-shot runner skipping --shutter-speed).

        Returns 0 on success, 1 on CAPTURE_FAIL / shutter-set failure, 124 on
        timeout, 127 if the binary is not found, or other non-zero on
        unexpected error.

        Automatically spawns the process on first call (lazy init).
        Respawns once if the process died between captures.
        """
        if self._closed:
            return 1

        # Initial lazy spawn only. If the process died between captures,
        # _do_capture owns the single respawn-and-retry (is_retry guard), so we
        # must NOT also respawn here — doing both could respawn twice in one
        # capture() and double-fire the shutter. (Codex review fix.)
        if self._proc is None:
            rc = self._spawn()
            if rc != 0:
                return rc

        return self._do_capture(
            out_path, timeout_s=timeout_s, is_retry=False, shutter=shutter
        )

    def close(self) -> None:
        """Gracefully shut down the persistent subprocess.

        Sends "quit\\n" then closes stdin.  If the process doesn't exit within
        `kill_timeout_s`, escalates to SIGKILL.  Safe to call multiple times.
        """
        self._closed = True
        self._cleanup_proc()

    def is_alive(self) -> bool:
        """True if the subprocess is running and connected."""
        return self._proc is not None and self._proc.poll() is None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _spawn(self) -> int:
        """Start the subprocess and wait for READY or FAIL.

        Returns 0 on success, 127 if not found, 1 on FAIL / timeout.
        """
        cmd = [self._binary, "--persist"] + self._cmd_extras
        logger.info("spawning persistent sony-capture: %s", " ".join(cmd))

        try:
            proc = self._popen_factory(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=self._env,
            )
        except FileNotFoundError:
            logger.error("sony-capture binary not found: %s", self._binary)
            return 127

        self._proc = proc
        # Fresh process → camera is at its own shutter; force re-apply.
        self._last_shutter = None

        # Start stderr drain (fires on_exposure_complete for every marker line).
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr,
            args=(proc,),
            daemon=True,
            name="sony-persist-stderr",
        )
        self._stderr_thread.start()

        # Start stdout drain (routes lines to _stdout_q).
        self._stdout_thread = threading.Thread(
            target=self._drain_stdout,
            args=(proc,),
            daemon=True,
            name="sony-persist-stdout",
        )
        self._stdout_thread.start()

        # Wait for READY or FAIL within the timeout.
        deadline = time.monotonic() + self._ready_timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.error(
                    "sony-capture persistent process did not send READY within %gs",
                    self._ready_timeout_s,
                )
                self._cleanup_proc()
                return 1
            try:
                line = self._stdout_q.get(timeout=min(remaining, 0.1))
            except queue.Empty:
                if proc.poll() is not None:
                    logger.error(
                        "sony-capture persistent process exited %d before READY",
                        proc.returncode,
                    )
                    self._cleanup_proc()
                    return 1
                continue

            line = line.strip()
            if line == "READY":
                logger.info("sony-capture persistent session ready")
                return 0
            if line.startswith("FAIL"):
                reason = line[4:].strip()
                logger.error(
                    "sony-capture persistent process failed to connect: %s", reason
                )
                self._cleanup_proc()
                return 1
            # Other lines before READY (e.g. log output): ignore.
            logger.debug("sony-capture pre-READY stdout: %s", line)

    def _do_capture(
        self,
        out_path: Path,
        *,
        timeout_s: int,
        is_retry: bool,
        shutter: Optional[str] = None,
    ) -> int:
        """Send the capture command and wait for CAPTURE_OK or CAPTURE_FAIL."""
        proc = self._proc
        if proc is None or proc.poll() is not None:
            if not is_retry:
                logger.warning("process died before capture; respawning")
                self._cleanup_proc()
                rc = self._spawn()
                if rc != 0:
                    return rc
                return self._do_capture(
                    out_path, timeout_s=timeout_s, is_retry=True, shutter=shutter
                )
            return 1

        # Apply the per-channel shutter before firing, if it changed. Firing at
        # the wrong exposure would silently corrupt per-channel calibration.
        if shutter is not None and shutter != self._last_shutter:
            if not self._apply_shutter(shutter):
                # Distinguish the two failure modes (Codex review fix):
                #  - process DIED during shutter apply → respawn-and-retry once,
                #    same policy as the capture-write path below (the new process
                #    reset _last_shutter to None, so the retry re-applies it).
                #  - camera REJECTED the speed (process still alive) → abort;
                #    retrying the same invalid speed won't help.
                proc = self._proc
                if (proc is None or proc.poll() is not None) and not is_retry:
                    logger.warning("process died during shutter apply; respawning")
                    self._cleanup_proc()
                    rc = self._spawn()
                    if rc != 0:
                        return rc
                    return self._do_capture(
                        out_path, timeout_s=timeout_s, is_retry=True, shutter=shutter
                    )
                logger.error("could not apply shutter %s; aborting capture", shutter)
                return 1

        # Clear any leftover lines from a previous capture.
        while not self._stdout_q.empty():
            try:
                self._stdout_q.get_nowait()
            except queue.Empty:
                break

        cmd_line = f"capture {out_path}\n"
        try:
            proc.stdin.write(cmd_line)
            proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            logger.warning("stdin write failed (process died?): %s", exc)
            if not is_retry:
                self._cleanup_proc()
                rc = self._spawn()
                if rc != 0:
                    return rc
                return self._do_capture(
                    out_path, timeout_s=timeout_s, is_retry=True, shutter=shutter
                )
            return 1

        deadline = time.monotonic() + timeout_s + 5  # 5 s slack for SDK teardown
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.error(
                    "sony-capture timed out waiting for CAPTURE_OK/FAIL (channel %s)",
                    out_path.stem,
                )
                # Kill the now-desynced child: a late CAPTURE_OK/FAIL arriving
                # after this timeout must not be misread by the next capture's
                # queue. The orchestrator also closes the session on any nonzero
                # result, but make the helper correct in isolation. (Codex fix.)
                self._cleanup_proc()
                return 124

            try:
                line = self._stdout_q.get(timeout=min(remaining, 0.1))
            except queue.Empty:
                if proc.poll() is not None:
                    logger.error(
                        "sony-capture persistent process exited %d mid-capture",
                        proc.returncode,
                    )
                    if not is_retry:
                        self._cleanup_proc()
                        rc = self._spawn()
                        if rc != 0:
                            return rc
                        return self._do_capture(
                            out_path, timeout_s=timeout_s, is_retry=True,
                            shutter=shutter,
                        )
                    return 1
                continue

            line = line.strip()
            if line.startswith("CAPTURE_OK"):
                logger.debug("CAPTURE_OK: %s", line)
                return 0
            if line.startswith("CAPTURE_FAIL"):
                reason = line[len("CAPTURE_FAIL"):].strip()
                logger.error("CAPTURE_FAIL: %s", reason)
                return 1
            logger.debug("sony-capture mid-capture stdout: %s", line)

    def _apply_shutter(self, speed: str, *, timeout_s: float = 10.0) -> bool:
        """Apply `speed` to the live session via the `shutter` command.

        Returns True on SHUTTER_OK (and records `_last_shutter`); False on
        SHUTTER_FAIL, timeout, or a dead process — the caller must then abort
        rather than capture at the wrong exposure. Does not respawn (a dead
        process here is handled by _do_capture's own retry on the next call).
        """
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return False

        # Drain stale lines so we read this command's response, not a prior one.
        while not self._stdout_q.empty():
            try:
                self._stdout_q.get_nowait()
            except queue.Empty:
                break

        try:
            proc.stdin.write(f"shutter {speed}\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            logger.warning("shutter stdin write failed (process died?): %s", exc)
            return False

        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.error("sony-capture timed out applying shutter %s", speed)
                return False
            try:
                line = self._stdout_q.get(timeout=min(remaining, 0.1))
            except queue.Empty:
                if proc.poll() is not None:
                    return False
                continue
            line = line.strip()
            if line.startswith("SHUTTER_OK"):
                self._last_shutter = speed
                return True
            if line.startswith("SHUTTER_FAIL"):
                logger.error("sony-capture rejected shutter %s: %s", speed, line)
                return False
            # Other lines (logs): ignore and keep waiting.
            logger.debug("sony-capture pre-SHUTTER_OK stdout: %s", line)

    def _drain_stderr(self, proc: "subprocess.Popen[str]") -> None:
        """Read stderr in a dedicated thread; fire on_exposure_complete on marker."""
        try:
            for line in proc.stderr:
                logger.debug("sony-capture stderr: %s", line.rstrip())
                if SONY_EXPOSURE_COMPLETE_MARKER in line and self._on_exposure_complete:
                    try:
                        self._on_exposure_complete()
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("on_exposure_complete hook raised: %s", exc)
        finally:
            try:
                proc.stderr.close()
            except Exception:
                pass

    def _drain_stdout(self, proc: "subprocess.Popen[str]") -> None:
        """Read stdout in a dedicated thread; route lines to _stdout_q."""
        try:
            for line in proc.stdout:
                self._stdout_q.put(line)
        finally:
            try:
                proc.stdout.close()
            except Exception:
                pass

    def _cleanup_proc(self) -> None:
        """Gracefully stop the current process (quit → SIGTERM → SIGKILL)."""
        proc = self._proc
        if proc is None:
            return
        self._proc = None

        # Try graceful quit first.
        if proc.poll() is None:
            try:
                if proc.stdin and not proc.stdin.closed:
                    proc.stdin.write("quit\n")
                    proc.stdin.flush()
                    proc.stdin.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                proc.wait(timeout=self._kill_timeout_s)
            except subprocess.TimeoutExpired:
                logger.warning("sony-capture did not quit in time; killing")
                proc.kill()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass

        # Let drain threads finish naturally.
        for t in (self._stderr_thread, self._stdout_thread):
            if t is not None and t.is_alive():
                t.join(timeout=1.0)
        self._stderr_thread = None
        self._stdout_thread = None
