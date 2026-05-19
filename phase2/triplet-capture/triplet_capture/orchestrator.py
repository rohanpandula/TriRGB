"""Per-frame capture orchestrator.

Two trigger modes, picked via `CaptureSettings.trigger_mode`:

  "sdk" (default):  Mac fires the camera over USB via the Sony SDK.
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
import shutil
import subprocess
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from . import inbox as inbox_mod

logger = logging.getLogger("triplet-capture")


TRIGGER_MODES = ("sdk", "hw")


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
      - "sdk" (default): fire the camera over USB via sony-capture.
      - "hw": pulse the Scanlight's 3.5mm shutter output; pick up the
        resulting file from `ied_inbox` (where Imaging Edge Desktop has
        downloaded it over Wi-Fi).
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
        if self.frame_number < 1:
            raise ValueError(f"frame_number must be >= 1, got {self.frame_number}")
        if not self.roll_name or any(c.isspace() for c in self.roll_name):
            raise ValueError(f"roll_name must be non-empty ASCII without spaces: {self.roll_name!r}")
        if self.trigger_mode not in TRIGGER_MODES:
            raise ValueError(
                f"trigger_mode must be one of {TRIGGER_MODES}, got {self.trigger_mode!r}"
            )
        if self.trigger_mode == "hw":
            if self.ied_inbox is None:
                raise ValueError(
                    "trigger_mode='hw' requires ied_inbox to be set "
                    "(path where Imaging Edge Desktop writes received files)"
                )
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
        self._runner = self._pick_runner()

    def _pick_runner(self) -> Callable[[str, Path, int], int]:
        """Choose the capture runner.

        Precedence:
          1. Caller-provided `sony_capture_runner` from __init__ (tests).
          2. trigger_mode=hw → internal HW runner.
          3. trigger_mode=sdk → internal SDK runner (sony-capture subprocess).
        """
        if self._explicit_runner is not None:
            return self._explicit_runner
        if self._settings.trigger_mode == "hw":
            return self._hw_runner
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
        """
        s = self._settings
        s.output_folder.mkdir(parents=True, exist_ok=True)
        log_path = s.output_folder / "scan_log.jsonl"

        files: dict[str, Path] = {}
        t_start = self._clock()
        levels = {"R": s.level_r, "G": s.level_g, "B": s.level_b}

        # Tag every log line with this single capture's frame info.
        def log(event: str, **kwargs):
            self._append_log(log_path, event, frame=s.frame_number, roll=s.roll_name, **kwargs)

        log("triplet_start", retake=retake)

        # In hardware-trigger mode, sweep the IED inbox so a previous
        # frame's late-arriving ARW can't poison this triplet's baseline
        # snapshots. No-op in sdk mode.
        if s.trigger_mode == "hw" and s.ied_inbox is not None:
            stale = inbox_mod.quarantine_stale_files(s.ied_inbox)
            if stale:
                log("inbox_quarantine", count=len(stale),
                    files=[p.name for p in stale])

        triplet_aborted: Optional[str] = None
        try:
            try:
                for channel in ("R", "G", "B"):
                    out = self._frame_path(channel)
                    self._capture_one(channel, levels[channel], out, log)
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
        out_path: Path,
        log: Callable[..., None],
    ) -> None:
        """Set one Scanlight channel, settle, capture, sanity-check size."""
        s = self._settings

        # 1. Light up the channel.
        kwargs = {"r": 0, "g": 0, "b": 0, "w": 0}
        kwargs[channel.lower()] = level
        self._scanlight.set_color(**kwargs)
        log("scanlight_on", channel=channel, level=level)

        # 2. Let the LED settle.
        if s.settle_ms > 0:
            self._sleep(s.settle_ms / 1000.0)

        # 3. Run sony-capture. exit_code != 0 → abort with a clear error.
        log("sony_capture_start", channel=channel, out=str(out_path))
        exit_code = self._runner(channel, out_path, s.sony_capture_timeout_s)
        if exit_code != 0:
            log("sony_capture_fail", channel=channel, exit_code=exit_code)
            raise TripletAbort(
                f"sony-capture failed for channel {channel} (exit {exit_code})"
            )

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

    def _default_runner(self, channel: str, out_path: Path, timeout_s: int) -> int:
        """Shell out to the `sony-capture` binary."""
        s = self._settings
        # Find the binary on $PATH; fail clearly if missing.
        binary = shutil.which(s.sony_capture_path) or s.sony_capture_path
        try:
            proc = subprocess.run(
                [
                    binary,
                    "--out", str(out_path),
                    "--timeout", str(timeout_s),
                ],
                capture_output=True,
                text=True,
                timeout=timeout_s + 5,  # 5s buffer for SDK teardown
                check=False,
            )
        except FileNotFoundError:
            logger.error("sony-capture binary not found at %s", binary)
            return 127
        except subprocess.TimeoutExpired:
            logger.error("sony-capture timed out after %ds for channel %s", timeout_s, channel)
            return 124

        # Surface stderr to our own log on failure for operator debugging.
        if proc.returncode != 0 and proc.stderr:
            logger.error("sony-capture stderr (channel %s): %s", channel, proc.stderr.strip())
        return proc.returncode

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
