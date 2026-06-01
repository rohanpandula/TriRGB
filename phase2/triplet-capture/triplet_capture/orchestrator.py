"""Per-frame capture orchestrator.

Three trigger modes, picked via `CaptureSettings.trigger_mode`:

  "sdk" (dataclass default): Mac fires the camera via sony-capture.
      Scanlight R → settle → sony-capture _R.ARW
      Scanlight G → settle → sony-capture _G.ARW
      Scanlight B → settle → sony-capture _B.ARW
      Scanlight off

  "hw":             Scanlight fires the camera via its 3.5mm shutter jack.
                    Camera saves over Wi-Fi to Imaging Edge Desktop's
                    inbox; we move the freshly-arrived file into our
                    roll's naming convention.
      Scanlight R → settle → scanlight pulse → wait-for-new-file _R.ARW
      Scanlight G → settle → scanlight pulse → wait-for-new-file _G.ARW
      Scanlight B → settle → scanlight pulse → wait-for-new-file _B.ARW
      Scanlight off

  "manual":         Camera is tethered through Imaging Edge Desktop, but the
                    operator manually triggers the shutter in IED for each lit
                    channel. No Sony SDK and no Scanlight shutter pulse.
      Scanlight R → settle → wait-for-new-file _R.ARW
      Scanlight G → settle → wait-for-new-file _G.ARW
      Scanlight B → settle → wait-for-new-file _B.ARW
      Scanlight off

Either way the orchestrator's contract is the same: try R, G, B in order,
verify each output file exists and is plausibly sized, advance the frame
counter only on full success. It writes one JSONL line per action so the
log can be walked frame-by-frame after a roll.

Designed to be:
- Disposable. Phase 3 replaces this with a native Swift app. Don't over-build.
- Testable. The Scanlight, the sony-capture subprocess, and the inbox
  watcher can all be injected; tests in `tests/test_orchestrator.py`
  exercise the state machine and error paths with stubs.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from . import inbox as inbox_mod

logger = logging.getLogger("triplet-capture")


TRIGGER_MODES = ("sdk", "hw", "manual")
IED_TRIGGER_MODES = ("hw", "manual")
SONY_EXPOSURE_COMPLETE_MARKER = "sony-capture: exposure-complete"


# Plausible RAW file size range. PROJECT.md says ~60–80 MB for a7CR
# *lossless-compressed*. Real captures observed in the wild on this drive
# went to 121 MB (uncompressed format), so the upper bound has to cover
# both compressed and uncompressed; the lower bound stays generous to
# catch truncated downloads without false-positiving low-detail frames.
# This guard exists to catch broken captures, not to enforce format.
PLAUSIBLE_RAW_MIN_BYTES = 40 * 1024 * 1024
PLAUSIBLE_RAW_MAX_BYTES = 200 * 1024 * 1024


@dataclass
class CaptureSettings:
    """All the knobs the operator sets before pressing 'Capture Triplet'.

    `levels` is per-channel R/G/B brightness (0–255). The operator
    calibrates these per film stock in the optical dry run.

    `settle_ms` is how long to wait between setting the Scanlight channel
    and triggering the camera. Default 50ms is generous; PROJECT.md
    suggests this as the starting point.

    `trigger_mode` picks the shutter path:
      - "sdk" (dataclass default): fire the camera via sony-capture.
      - "hw": pulse the Scanlight's 3.5mm shutter output; pick up the
        resulting file from `ied_inbox` (where Imaging Edge Desktop has
        downloaded it over Wi-Fi).
      - "manual": do not fire any shutter command. Set each Scanlight
        channel and wait for the operator to manually trigger the camera
        in Imaging Edge Desktop; pick up the resulting file from `ied_inbox`.
    """
    roll_name: str = "Roll001"
    frame_number: int = 1
    output_folder: Path = field(default_factory=lambda: Path("/tmp/scans"))
    level_r: int = 200
    level_g: int = 200
    level_b: int = 200
    settle_ms: int = 50
    sony_capture_path: str = "sony-capture"
    sony_capture_timeout_s: int = 30
    sony_ip_address: Optional[str] = None
    sony_mac_address: Optional[str] = None
    sony_user: Optional[str] = None
    sony_password: Optional[str] = None
    sony_iso: Optional[str] = "100or125"
    shutter_r: Optional[str] = None
    shutter_g: Optional[str] = None
    shutter_b: Optional[str] = None

    # Hardware-trigger mode fields. Default values keep "sdk" mode working
    # unchanged for existing callers.
    trigger_mode: str = "sdk"
    ied_inbox: Optional[Path] = None
    shutter_pulse_ms: int = 100  # matches the canonical app_bsl default
    # How long the inbox file size must hold steady before we call it
    # "done writing." 3 s default; lower it only if Wi-Fi is fast and
    # PC Remote never stalls mid-transfer in your setup.
    inbox_stable_for_s: float = 3.0
    # Inbox polling cadence. 200 ms is plenty for files arriving every
    # several seconds; raise if filesystem ops are hurting you.
    inbox_poll_interval_s: float = 0.2

    def __post_init__(self):
        # Coerce in case caller passes a string from the web UI
        self.output_folder = Path(self.output_folder)
        if self.ied_inbox is not None:
            self.ied_inbox = Path(self.ied_inbox)
        for name in ("level_r", "level_g", "level_b"):
            v = getattr(self, name)
            if not 0 <= v <= 255:
                raise ValueError(f"{name} out of range 0–255: {v}")
        for name in ("shutter_r", "shutter_g", "shutter_b"):
            v = getattr(self, name)
            if v is not None:
                v = str(v).strip()
                object.__setattr__(self, name, v or None)
        if self.sony_iso is not None:
            iso = str(self.sony_iso).strip()
            normalized_iso = (
                iso.lower()
                .replace(" ", "")
                .replace("_", "")
                .replace("-", "")
            )
            if not normalized_iso:
                object.__setattr__(self, "sony_iso", None)
            elif normalized_iso in {"lowest", "low", "min", "base", "fixedlow"}:
                object.__setattr__(self, "sony_iso", "100or125")
            elif normalized_iso in {"100or125", "100/125", "base100", "nativebase", "scanbase"}:
                object.__setattr__(self, "sony_iso", "100or125")
            elif normalized_iso in {"iso100", "100"}:
                object.__setattr__(self, "sony_iso", "100")
            elif normalized_iso in {"iso125", "125"}:
                object.__setattr__(self, "sony_iso", "125")
            else:
                raise ValueError(
                    "sony_iso must be 100, 125, or 100or125. "
                    f"ISO 50/extended-low is not allowed for scan calibration: {self.sony_iso!r}"
                )
        if self.frame_number < 1:
            raise ValueError(f"frame_number must be >= 1, got {self.frame_number}")
        if not self.roll_name or any(c.isspace() for c in self.roll_name):
            raise ValueError(f"roll_name must be non-empty ASCII without spaces: {self.roll_name!r}")
        if self.trigger_mode not in TRIGGER_MODES:
            raise ValueError(
                f"trigger_mode must be one of {TRIGGER_MODES}, got {self.trigger_mode!r}"
            )
        if self.trigger_mode in IED_TRIGGER_MODES:
            if self.ied_inbox is None:
                raise ValueError(
                    f"trigger_mode={self.trigger_mode!r} requires ied_inbox to be set "
                    "(path where Imaging Edge Desktop writes received files)"
                )
        if self.trigger_mode == "hw":
            if not (10 <= self.shutter_pulse_ms <= 2550 and self.shutter_pulse_ms % 10 == 0):
                raise ValueError(
                    f"shutter_pulse_ms must be a multiple of 10 in [10, 2550], "
                    f"got {self.shutter_pulse_ms}"
                )


@dataclass
class TripletResult:
    """Outcome of one Capture Triplet operation."""
    success: bool
    frame_number: int
    files: dict[str, Path]  # 'R', 'G', 'B' → final path
    error: Optional[str] = None
    duration_s: float = 0.0


class TripletAbort(Exception):
    """Raised internally when a channel capture fails — handled inside
    capture_triplet so the frame counter is not advanced."""


class Orchestrator:
    """Stateful capture controller.

    Args:
        scanlight: a `scanlight.Scanlight` instance (already connected).
            Injected so tests can pass a fake.
        settings: CaptureSettings; can be mutated between captures via
            `update_settings()`.
        sony_capture_runner: Optional override of the subprocess invocation.
            Defaults to running `settings.sony_capture_path` via subprocess.
            Signature: `(channel: str, out_path: Path, timeout_s: int) -> int`
            returning the exit code. Tests pass a stub that just `touch`es
            a fake RAW.
        clock: Callable returning current time in seconds; injectable for
            tests so they don't sleep through settle delays.
    """

    def __init__(
        self,
        scanlight,
        settings: CaptureSettings,
        *,
        sony_capture_runner: Optional[Callable[[str, Path, int], int]] = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        on_triplet_complete: Optional[Callable[["TripletResult"], None]] = None,
    ):
        self._scanlight = scanlight
        self._settings = settings
        self._clock = clock
        self._sleep = sleep
        # Serializes capture_triplet + update_settings. Flask serves on a
        # threaded server, so without this two concurrent /api/capture requests
        # (or a capture racing /api/settings) could interleave R/G/B serial
        # writes and race the frame counter. CompositeWorker is already lock-
        # guarded for the same reason; the capture path needs it too.
        self._lock = threading.Lock()
        # Optional hook fired after a successful (non-retake AND retake)
        # triplet, with the TripletResult. The app layer wires a
        # CompositeWorker.submit() here to kick off background compositing
        # while the operator captures the next frame. Kept as an injected
        # callback so the orchestrator stays capture-only and has no
        # dependency on rgb_composite. Exceptions raised by the hook are
        # swallowed and logged — a compositing failure must never abort the
        # capture loop.
        self._on_triplet_complete = on_triplet_complete
        # Track whether the caller passed an explicit runner so that
        # update_settings() can re-pick the right internal runner if the
        # trigger mode changes, without ever overriding a caller-supplied
        # one (tests rely on the override sticking).
        self._explicit_runner = sony_capture_runner
        self._last_runner_detail = ""
        self._runner = self._pick_runner()

    def _pick_runner(self) -> Callable[[str, Path, int], int]:
        """Choose the capture runner.

        Precedence:
          1. Caller-provided `sony_capture_runner` from __init__ (tests).
          2. trigger_mode=hw → internal HW pulse + IED inbox runner.
          3. trigger_mode=manual → internal IED inbox-only runner.
          4. trigger_mode=sdk → internal SDK runner (sony-capture subprocess).
        """
        if self._explicit_runner is not None:
            return self._explicit_runner
        if self._settings.trigger_mode == "hw":
            return self._hw_runner
        if self._settings.trigger_mode == "manual":
            return self._manual_runner
        return self._default_runner

    # ---------------- public API ----------------

    @property
    def settings(self) -> CaptureSettings:
        return self._settings

    def update_settings(self, **kwargs) -> CaptureSettings:
        """Replace zero or more fields. Resets `frame_number` to 1 if
        `roll_name` changed AND `frame_number` wasn't explicitly passed.

        Also re-picks the internal capture runner if `trigger_mode`
        changed and no explicit runner override was provided at init —
        otherwise a runtime switch from sdk→hw (or vice versa) would
        keep firing through the old runner.
        """
        with self._lock:
            if "roll_name" in kwargs and "frame_number" not in kwargs:
                if kwargs["roll_name"] != self._settings.roll_name:
                    kwargs["frame_number"] = 1
            old_trigger_mode = self._settings.trigger_mode
            self._settings = replace(self._settings, **kwargs)
            # Run validators
            self._settings.__post_init__()
            if self._settings.trigger_mode != old_trigger_mode:
                self._runner = self._pick_runner()
            return self._settings

    def capture_triplet(self, *, retake: bool = False) -> TripletResult:
        """Capture R, G, B for the current frame.

        On success the frame counter advances; on any failure it does not.
        `retake=True` overwrites the current frame's files without
        advancing afterwards (the operator presses Retake explicitly).

        Serialized via `self._lock`. Flask serves on a threaded server, so two
        concurrent /api/capture requests could otherwise interleave R/G/B
        serial writes and race the frame counter. A capture that arrives while
        another is in flight is rejected (success=False) rather than queued —
        queuing would silently shoot a second frame with no film advance.
        """
        if not self._lock.acquire(blocking=False):
            return TripletResult(
                success=False,
                frame_number=self._settings.frame_number,
                files={},
                error="a capture is already in progress",
            )
        try:
            return self._capture_triplet_locked(retake=retake)
        finally:
            self._lock.release()

    def capture_channel(
        self,
        channel: str,
        *,
        level: int,
        shutter_speed: Optional[str] = None,
        out_path: Optional[Path] = None,
        label: str = "calibration",
    ) -> TripletResult:
        """Capture one channel without advancing the frame counter.

        Calibration only needs one dark RAW and one active-channel RAW per
        probe. Reusing capture_triplet() for those jobs fired two extra dark
        captures every time, making real SDK calibration slow enough for the
        Swift HTTP request to time out.
        """
        channel = channel.upper()
        if channel not in {"R", "G", "B"}:
            return TripletResult(
                success=False,
                frame_number=self._settings.frame_number,
                files={},
                error=f"invalid channel for single capture: {channel}",
            )

        if not self._lock.acquire(blocking=False):
            return TripletResult(
                success=False,
                frame_number=self._settings.frame_number,
                files={},
                error="a capture is already in progress",
            )

        try:
            return self._capture_channel_locked(
                channel=channel,
                level=level,
                shutter_speed=shutter_speed,
                out_path=out_path,
                label=label,
            )
        finally:
            self._lock.release()

    def _capture_triplet_locked(self, *, retake: bool) -> TripletResult:
        s = self._settings
        s.output_folder.mkdir(parents=True, exist_ok=True)
        log_path = s.output_folder / "scan_log.jsonl"

        files: dict[str, Path] = {}
        t_start = self._clock()
        levels = {"R": s.level_r, "G": s.level_g, "B": s.level_b}
        shutters = {"R": s.shutter_r, "G": s.shutter_g, "B": s.shutter_b}

        # Tag every log line with this single capture's frame info.
        def log(event: str, **kwargs):
            self._append_log(log_path, event, frame=s.frame_number, roll=s.roll_name, **kwargs)

        log("triplet_start", retake=retake)

        # In IED-backed modes, sweep the inbox so a previous
        # frame's late-arriving ARW can't poison this triplet's baseline
        # snapshots. No-op in sdk mode.
        if s.trigger_mode in IED_TRIGGER_MODES and s.ied_inbox is not None:
            stale = inbox_mod.quarantine_stale_files(s.ied_inbox)
            if stale:
                log("inbox_quarantine", count=len(stale),
                    files=[p.name for p in stale])

        triplet_aborted: Optional[str] = None
        try:
            try:
                for channel in ("R", "G", "B"):
                    out = self._frame_path(channel)
                    self._capture_one(channel, levels[channel], shutters[channel], out, log)
                    files[channel] = out
                log("scanlight_off")
            except TripletAbort as exc:
                triplet_aborted = str(exc)
                log("triplet_abort", error=triplet_aborted)
        finally:
            # ALWAYS turn the scanlight off — including on uncaught
            # OSError, cross-device-move failure, KeyboardInterrupt, etc.
            try:
                self._scanlight.off()
            except Exception as off_exc:  # noqa: BLE001
                log("scanlight_off_failed", error=str(off_exc))

        if triplet_aborted is not None:
            return TripletResult(
                success=False,
                frame_number=s.frame_number,
                files=files,
                error=triplet_aborted,
                duration_s=self._clock() - t_start,
            )

        # All three captures succeeded.
        if not retake:
            self._settings = replace(self._settings, frame_number=s.frame_number + 1)
            log("frame_advance", next_frame=self._settings.frame_number)
        else:
            log("retake_complete")
        result = TripletResult(
            success=True,
            frame_number=s.frame_number,
            files=files,
            duration_s=self._clock() - t_start,
        )

        # Kick off background compositing (if wired) while the operator
        # captures the next frame. A hook failure must NOT abort the capture
        # loop — log it and carry on; the operator can always re-run
        # batch-composite on the roll dir afterward to fill any gaps.
        if self._on_triplet_complete is not None:
            try:
                self._on_triplet_complete(result)
            except Exception as hook_exc:  # noqa: BLE001
                log("composite_hook_failed", error=str(hook_exc))

        return result

    def _capture_channel_locked(
        self,
        *,
        channel: str,
        level: int,
        shutter_speed: Optional[str],
        out_path: Optional[Path],
        label: str,
    ) -> TripletResult:
        s = self._settings
        s.output_folder.mkdir(parents=True, exist_ok=True)
        log_path = s.output_folder / "scan_log.jsonl"
        out = out_path or self._frame_path(channel)

        t_start = self._clock()

        def log(event: str, **kwargs):
            self._append_log(
                log_path,
                event,
                frame=s.frame_number,
                roll=s.roll_name,
                **kwargs,
            )

        log("single_capture_start", channel=channel, level=level, label=label)
        aborted: Optional[str] = None
        try:
            try:
                self._capture_one(channel, level, shutter_speed, out, log, label=label)
                log("single_capture_complete", channel=channel, label=label)
            except TripletAbort as exc:
                aborted = str(exc)
                log("single_capture_abort", channel=channel, label=label, error=aborted)
        finally:
            try:
                self._scanlight.off()
            except Exception as off_exc:  # noqa: BLE001
                log("scanlight_off_failed", error=str(off_exc))

        if aborted is not None:
            return TripletResult(
                success=False,
                frame_number=s.frame_number,
                files={},
                error=aborted,
                duration_s=self._clock() - t_start,
            )

        return TripletResult(
            success=True,
            frame_number=s.frame_number,
            files={channel: out},
            duration_s=self._clock() - t_start,
        )

    # ---------------- internals ----------------

    def _frame_path(self, channel: str) -> Path:
        s = self._settings
        return (
            s.output_folder
            / f"{s.roll_name}_Frame{s.frame_number:03d}_{channel}.ARW"
        )

    def _capture_one(
        self,
        channel: str,
        level: int,
        shutter_speed: Optional[str],
        out_path: Path,
        log: Callable[..., None],
        label: str = "",
    ) -> None:
        """Set one Scanlight channel, settle, capture, sanity-check size."""
        s = self._settings

        # 1. Light up the channel.
        kwargs = {"r": 0, "g": 0, "b": 0, "w": 0}
        kwargs[channel.lower()] = level
        self._scanlight.set_color(**kwargs)
        log(
            "scanlight_on",
            channel=channel,
            level=level,
            shutter_speed=shutter_speed,
            **({"label": label} if label else {}),
        )

        # 2. Let the LED settle.
        if s.settle_ms > 0:
            self._sleep(s.settle_ms / 1000.0)

        # 3. Run sony-capture. exit_code != 0 → abort with a clear error.
        log(
            "sony_capture_start",
            channel=channel,
            out=str(out_path),
            shutter_speed=shutter_speed,
            **({"label": label} if label else {}),
        )
        self._last_runner_detail = ""
        exit_code = self._runner(channel, out_path, s.sony_capture_timeout_s)
        if exit_code != 0:
            log("sony_capture_fail", channel=channel, exit_code=exit_code)
            raise TripletAbort(self._capture_failure_message(channel, exit_code))

        # 4. Sanity check the file exists and is plausibly sized.
        if not out_path.exists():
            log("sony_capture_missing_file", channel=channel)
            raise TripletAbort(
                f"sony-capture exited 0 but {out_path} does not exist"
            )
        size = out_path.stat().st_size
        if not PLAUSIBLE_RAW_MIN_BYTES <= size <= PLAUSIBLE_RAW_MAX_BYTES:
            log("sony_capture_implausible_size", channel=channel, size=size)
            raise TripletAbort(
                f"channel {channel} file size {size} bytes is outside "
                f"plausible RAW range ({PLAUSIBLE_RAW_MIN_BYTES}–"
                f"{PLAUSIBLE_RAW_MAX_BYTES})"
            )
        log("sony_capture_ok", channel=channel, size=size)

    def _capture_failure_message(self, channel: str, exit_code: int) -> str:
        s = self._settings
        detail = self._last_runner_detail.strip()
        suffix = f": {detail}" if detail else ""
        if s.trigger_mode == "sdk" and exit_code == 127:
            return (
                "sony-capture could not be found or launched for channel "
                f"{channel} (exit 127). Build phase1/sony-capture or set "
                "--sony-capture to the built binary."
                f"{suffix}"
            )
        if s.trigger_mode == "sdk" and exit_code == 124:
            return (
                f"sony-capture timed out while capturing channel {channel}. "
                "Check that the camera is on Wi-Fi PC Remote and use Settings "
                "to verify the SDK connection."
                f"{suffix}"
            )
        return f"capture failed for channel {channel} (exit {exit_code}){suffix}"

    def _hw_runner(self, channel: str, out_path: Path, timeout_s: int) -> int:
        """Hardware-trigger runner.

        Pulses the Scanlight's 3.5mm shutter output, then waits for a new
        ARW to land in IED's inbox folder and moves it to `out_path`.
        Returns:
          0   on success
          1   on pulse-send failure, ambiguous inbox, or filesystem
              failure during the move (cross-device, permissions, etc.)
          124 on inbox timeout (matches the SDK runner's timeout exit code)

        On any failure path, late ARWs that landed in the inbox during
        this channel's window are swept into a quarantine subdir so
        they don't poison the next channel's baseline.
        """
        s = self._settings
        assert s.ied_inbox is not None  # invariant from CaptureSettings.__post_init__

        # Snapshot the inbox BEFORE pulsing so we can spot the new arrival
        # by set diff. This is robust against IED renaming during write.
        baseline = inbox_mod.list_inbox(s.ied_inbox)

        try:
            self._scanlight.pulse_shutter(s.shutter_pulse_ms)
        except Exception as exc:  # noqa: BLE001 — anything from the serial layer
            logger.error("scanlight pulse failed (channel %s): %s", channel, exc)
            return 1

        try:
            new_file = inbox_mod.wait_for_new_file(
                s.ied_inbox,
                baseline=baseline,
                timeout_s=timeout_s,
                stable_for_s=s.inbox_stable_for_s,
                poll_interval_s=s.inbox_poll_interval_s,
                sleep=self._sleep,
                clock=self._clock,
            )
        except inbox_mod.AmbiguousInboxError as exc:
            logger.error(
                "hw-trigger ambiguous inbox (channel %s): %s — quarantining",
                channel, exc,
            )
            inbox_mod.quarantine_stale_files(s.ied_inbox)
            return 1

        if new_file is None:
            logger.error(
                "hw-trigger timeout: no new ARW in %s within %ds (channel %s)",
                s.ied_inbox, timeout_s, channel,
            )
            # Any files that DID arrive (just not within the window) go
            # to quarantine so the next channel/frame starts clean.
            inbox_mod.quarantine_stale_files(s.ied_inbox)
            return 124

        try:
            inbox_mod.claim_new_file(new_file, out_path)
        except FileNotFoundError as exc:
            logger.error("hw-trigger: source vanished before move: %s", exc)
            return 1
        except OSError as exc:
            # Cross-device EXDEV (handled by shutil.move) shouldn't reach
            # here, but disk-full / permission / read-only-volume can.
            logger.error("hw-trigger: filesystem error moving %s → %s: %s",
                         new_file, out_path, exc)
            return 1
        return 0

    def _manual_runner(self, channel: str, out_path: Path, timeout_s: int) -> int:
        """Manual Imaging Edge Desktop runner.

        The Scanlight channel has already been set by `_capture_one`. This
        runner takes an inbox baseline, then waits for the operator to trigger
        the camera manually in IED. The newly-arrived RAW is moved into the
        roll's canonical path.

        Returns:
          0   on success
          1   on ambiguous inbox or filesystem failure during the move
          124 on inbox timeout
        """
        s = self._settings
        assert s.ied_inbox is not None  # invariant from CaptureSettings.__post_init__

        baseline = inbox_mod.list_inbox(s.ied_inbox)
        try:
            new_file = inbox_mod.wait_for_new_file(
                s.ied_inbox,
                baseline=baseline,
                timeout_s=timeout_s,
                stable_for_s=s.inbox_stable_for_s,
                poll_interval_s=s.inbox_poll_interval_s,
                sleep=self._sleep,
                clock=self._clock,
            )
        except inbox_mod.AmbiguousInboxError as exc:
            logger.error(
                "manual IED ambiguous inbox (channel %s): %s — quarantining",
                channel, exc,
            )
            inbox_mod.quarantine_stale_files(s.ied_inbox)
            return 1

        if new_file is None:
            logger.error(
                "manual IED timeout: no new ARW in %s within %ds (channel %s)",
                s.ied_inbox, timeout_s, channel,
            )
            inbox_mod.quarantine_stale_files(s.ied_inbox)
            return 124

        try:
            inbox_mod.claim_new_file(new_file, out_path)
        except FileNotFoundError as exc:
            logger.error("manual IED: source vanished before move: %s", exc)
            return 1
        except OSError as exc:
            logger.error(
                "manual IED: filesystem error moving %s → %s: %s",
                new_file, out_path, exc,
            )
            return 1
        return 0

    def _default_runner(self, channel: str, out_path: Path, timeout_s: int) -> int:
        """Shell out to the `sony-capture` binary."""
        s = self._settings
        # Find the binary on $PATH; fail clearly if missing.
        binary = shutil.which(s.sony_capture_path) or s.sony_capture_path
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []

        def append_output(kind: str, text: str) -> None:
            target = stderr_parts if kind == "stderr" else stdout_parts
            target.append(text)
            joined = "".join(target)
            if len(joined) > 65536:
                target[:] = [joined[-65536:]]

        def combined_output() -> str:
            stderr_text = "".join(stderr_parts)
            stdout_text = "".join(stdout_parts)
            return stderr_text or stdout_text

        try:
            cmd = [
                binary,
                "--out", str(out_path),
                "--timeout", str(timeout_s),
            ]
            if s.sony_ip_address:
                cmd += ["--ip-address", s.sony_ip_address]
            if s.sony_mac_address:
                cmd += ["--mac-address", s.sony_mac_address]
            if s.sony_iso:
                cmd += ["--iso", s.sony_iso]
            shutter_speed = self._shutter_for_channel(channel)
            if shutter_speed:
                cmd += ["--shutter-speed", shutter_speed]
            # Credentials go through the environment (see _sony_capture_env),
            # never argv — argv is visible to any local process via `ps`.
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=self._sony_capture_env(),
            )
        except FileNotFoundError:
            logger.error("sony-capture binary not found at %s", binary)
            self._last_runner_detail = f"binary not found at {binary}"
            return 127

        line_queue: "queue.Queue[tuple[str, str]]" = queue.Queue()

        def drain_stream(kind: str, stream) -> None:
            if stream is None:
                return
            try:
                while True:
                    line = stream.readline()
                    if not line:
                        break
                    line_queue.put((kind, line))
            finally:
                try:
                    stream.close()
                except Exception:
                    pass

        threads = [
            threading.Thread(target=drain_stream, args=("stdout", proc.stdout), daemon=True),
            threading.Thread(target=drain_stream, args=("stderr", proc.stderr), daemon=True),
        ]
        for thread in threads:
            thread.start()

        early_light_off = False
        deadline = self._clock() + float(timeout_s + 5)  # 5s buffer for SDK teardown

        def consume_available_lines(block_timeout: float) -> None:
            nonlocal early_light_off
            while True:
                try:
                    kind, line = line_queue.get(timeout=block_timeout)
                except queue.Empty:
                    return
                block_timeout = 0.0
                append_output(kind, line)
                if SONY_EXPOSURE_COMPLETE_MARKER in line and not early_light_off:
                    early_light_off = True
                    try:
                        self._scanlight.off()
                    except Exception as exc:  # noqa: BLE001 - hardware cleanup best effort
                        logger.warning(
                            "could not turn Scanlight off after exposure marker: %s",
                            exc,
                        )

        while proc.poll() is None:
            consume_available_lines(0.05)
            if self._clock() >= deadline:
                logger.error("sony-capture timed out after %ds for channel %s", timeout_s, channel)
                proc.kill()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
                for thread in threads:
                    thread.join(timeout=0.2)
                consume_available_lines(0.0)
                self._last_runner_detail = self._redact_runner_detail(combined_output())
                return 124

        for thread in threads:
            thread.join(timeout=0.5)
        consume_available_lines(0.0)

        returncode = proc.returncode if proc.returncode is not None else 1

        # Surface stderr to our own log on failure for operator debugging.
        if returncode != 0:
            detail = combined_output()
            self._last_runner_detail = self._redact_runner_detail(detail)
            stderr_text = "".join(stderr_parts)
            if stderr_text:
                logger.error(
                    "sony-capture stderr (channel %s): %s",
                    channel,
                    self._redact_runner_detail(stderr_text),
                )
        return returncode

    def sdk_shutter_control_preflight(self) -> tuple[bool, str]:
        """Return whether sony-capture can write shutter speed in SDK mode.

        Calibration changes shutter speed as the coarse exposure control. If
        the camera is in A/P/Auto instead of M, Sony exposes the current shutter
        but marks it non-writable. Detect that before the dark frame so the
        operator gets an actionable mode error instead of a failed capture.
        Tests that inject a sony_capture_runner bypass this hardware preflight.
        """
        s = self._settings
        if s.trigger_mode != "sdk" or self._explicit_runner is not None:
            return True, ""

        binary = shutil.which(s.sony_capture_path) or s.sony_capture_path
        cmd = [
            binary,
            "--list-shutter-speeds",
            "--timeout", str(s.sony_capture_timeout_s),
        ]
        if s.sony_ip_address:
            cmd += ["--ip-address", s.sony_ip_address]
        if s.sony_mac_address:
            cmd += ["--mac-address", s.sony_mac_address]

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=s.sony_capture_timeout_s + 5,
                check=False,
                env=self._sony_capture_env(),
            )
        except FileNotFoundError:
            return False, f"sony-capture binary not found at {binary}"
        except subprocess.TimeoutExpired as exc:
            text = exc.stderr or exc.stdout or ""
            if isinstance(text, bytes):
                text = text.decode(errors="replace")
            detail = self._redact_runner_detail(text)
            suffix = f": {detail}" if detail else ""
            return False, f"could not verify Sony shutter control before calibration{suffix}"

        output = "\n".join(part for part in (proc.stdout, proc.stderr) if part)
        detail = self._redact_runner_detail(output, limit=4000)
        if proc.returncode != 0:
            suffix = f": {detail}" if detail else ""
            return False, f"could not verify Sony shutter control before calibration{suffix}"

        shutter_lines = [
            line.strip()
            for line in output.splitlines()
            if "shutter" in line.lower() and "writable=" in line.lower()
        ]
        if any("writable=yes" in line.lower() for line in shutter_lines):
            return True, detail
        if any("writable=no" in line.lower() for line in shutter_lines):
            status = "; ".join(shutter_lines)
            return False, (
                "Camera shutter speed is not writable over the Sony SDK. "
                "Set the camera mode dial to M/manual exposure, keep f/8 fixed, "
                "and let the SDK set ISO 100 or ISO 125 before running exposure calibration again. "
                f"SDK status: {status}"
            )
        return False, (
            "Sony SDK did not report a writable shutter-speed candidate list. "
            "Set the camera to M/manual exposure and retry."
            + (f" SDK output: {detail}" if detail else "")
        )

    @staticmethod
    def _concise_runner_detail(text: str, limit: int = 320) -> str:
        cleaned = " ".join(str(text).split())
        if len(cleaned) <= limit:
            return cleaned
        return "..." + cleaned[-limit:]

    def _sony_capture_env(self) -> dict:
        """Environment for `sony-capture` subprocesses.

        Sony credentials are injected here (``SONY_USERNAME``/``SONY_USER`` for
        the user, ``SONY_PW`` for the password) instead of being passed on
        argv, so they never appear in ``ps`` / Activity Monitor /
        process-accounting logs. ``sony-capture`` reads these env vars as
        fallbacks when ``--username``/``--password`` are absent (see
        ``phase1/sony-capture/src/main.cpp`` getenv handling). Non-secret args
        (ip/mac/iso/shutter) stay on argv.
        """
        env = os.environ.copy()
        s = self._settings
        if s.sony_user:
            env["SONY_USERNAME"] = s.sony_user
            env["SONY_USER"] = s.sony_user
        if s.sony_password:
            env["SONY_PW"] = s.sony_password
        return env

    def _redact_runner_detail(self, text: str, limit: int = 320) -> str:
        redacted = str(text)
        for secret in (self._settings.sony_password, self._settings.sony_user):
            if secret:
                redacted = redacted.replace(str(secret), "<redacted>")
        return self._concise_runner_detail(redacted, limit=limit)

    def _shutter_for_channel(self, channel: str) -> Optional[str]:
        s = self._settings
        return {
            "R": s.shutter_r,
            "G": s.shutter_g,
            "B": s.shutter_b,
        }.get(channel)

    @staticmethod
    def _append_log(path: Path, event: str, **kwargs) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **kwargs,
        }
        # Convert non-JSON values to strings to keep the line stable.
        for k, v in list(record.items()):
            if isinstance(v, Path):
                record[k] = str(v)
        with path.open("a") as f:
            f.write(json.dumps(record) + "\n")
