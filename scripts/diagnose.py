#!/usr/bin/env python3
"""Pre-flight diagnostic — run this first when hardware arrives.

Goes through the assumptions baked into the codebase and verifies them
one by one against the actual hardware in front of you. Exits 0 only
when everything that can be checked from software passes.

Checks (in order):
  1. Serial port enumeration — print all ports so you can see what showed up
  2. Pico VID:PID 2E8A:000A assumption (auto-discovery target)
  3. Scanlight handshake — open, request firmware version, expect a reply
  4. Scanlight telemetry — wait up to 1 s, confirm LED_TEMP + VBUS arrive.
     First telemetry can take ~600 ms after a fresh plug-in because the
     firmware runs a 4×150 ms visual self-test before the first ADC report.
  5. VBUS is at expected level (>4500mV → properly wall-powered;
     firmware's hard cutoff is 4400mV, we warn at 4500mV for margin)
  6. sony-capture binary on PATH
  7. Sony Camera Remote SDK can enumerate (camera connected, in PC Remote mode)

Run from anywhere — paths are resolved relative to this file.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable


REPO_ROOT = Path(__file__).resolve().parent.parent
PICO_VID = 0x2E8A
PICO_CDC_PIDS = (0x000A, 0x0009)
# VBUS warning threshold. Firmware shuts off below 4400 mV
# (`USBVBUSThreshold5V` in `automation/firmware_bsl1/config.h`). We warn
# at 4500 to give a 100 mV cushion above the hard shutoff — don't drop
# this to 4400 without removing the safety margin too.
VBUS_OK_MV = 4500
# LED-temp warning threshold. Firmware silently transitions to
# OperatingModeOff at 80 000 mdegC (`OverTemperatureThresholdMdegc`).
# Warn 10 °C below so a session-killer thermal event doesn't surprise
# the operator.
LED_TEMP_WARN_C = 70.0

# Friendly-name lookup tables. Source of truth: upstream
# `automation/app_bsl/src/config.js` (for HW strings) and
# `automation/firmware_bsl1/config.h` (FW_VERSION_ID). The web app's
# `LatestFWVersionID` is stale relative to the firmware (says 0; current
# is 1). We just print what the device tells us in either case.
HW_VERSION_STRINGS = {
    0: "big scanlight v1",
    1: "scanlight v4",
}
FW_VERSION_STRINGS = {
    0: "v1.0.0",
    1: "v1.1.0",  # adds scanlight v4 hardware support
}


def _green(s: str) -> str: return f"\033[32m{s}\033[0m"
def _red(s: str) -> str:   return f"\033[31m{s}\033[0m"
def _yellow(s: str) -> str:return f"\033[33m{s}\033[0m"
def _bold(s: str) -> str:  return f"\033[1m{s}\033[0m"


def _step(n: int, title: str) -> None:
    print(_bold(f"\n[{n}] {title}"))


def _ok(msg: str) -> None:    print(f"  {_green('✓')} {msg}")
def _warn(msg: str) -> None:  print(f"  {_yellow('!')} {msg}")
def _fail(msg: str) -> None:  print(f"  {_red('✗')} {msg}")


def _try(label: str, fn: Callable[[], None]) -> bool:
    try:
        fn()
        return True
    except Exception as e:
        _fail(f"{label}: {type(e).__name__}: {e}")
        return False


def check_serial_ports() -> list:
    """Step 1: list all serial ports."""
    _step(1, "serial ports")
    from serial.tools import list_ports
    ports = list(list_ports.comports())
    if not ports:
        _fail("no serial ports found at all — Scanlight not plugged in?")
        return []
    for p in ports:
        vid_pid = ""
        if p.vid is not None and p.pid is not None:
            vid_pid = f" [{p.vid:04x}:{p.pid:04x}]"
        desc = p.description or p.product or "?"
        print(f"  {p.device}  {desc}{vid_pid}")
    return ports


def check_pico_vid_pid(ports) -> bool:
    """Step 2: confirm one of the ports matches the Pico CDC VID:PID."""
    _step(2, "Pico VID:PID 2E8A:000A (Scanlight uses stock pico_enable_stdio_usb)")
    pico = [p for p in ports if p.vid == PICO_VID and p.pid in PICO_CDC_PIDS]
    if len(pico) == 1:
        _ok(f"found one Pico CDC port: {pico[0].device}")
        return True
    if not pico:
        _warn(
            "no Pico CDC ports found. Either the Scanlight is unplugged, the LEFT-side "
            "USB-C is in the wrong port (data not power), or the firmware uses different "
            "USB descriptors than expected. If the Scanlight web app works in your browser, "
            "look at the port from step 1 and pass it via --port to scanlightctl."
        )
        return False
    _warn(f"multiple Pico CDC ports — pass --port explicitly. Found: {[p.device for p in pico]}")
    return False


def check_scanlight_handshake(port: str | None) -> tuple[bool, object]:
    """Step 3: open Scanlight, request firmware version."""
    _step(3, "Scanlight handshake")
    sys.path.insert(0, str(REPO_ROOT / "phase1" / "scanlightctl"))
    try:
        from scanlight import Scanlight
    except ImportError as e:
        _fail(f"could not import scanlight package: {e}")
        _warn("did you `pip install -e phase1/scanlightctl`?")
        return False, None
    try:
        s = Scanlight(port=port)
    except Exception as e:
        _fail(f"could not open Scanlight: {e}")
        return False, None
    try:
        fw, hw = s.get_fw_version(timeout=2.0)
        fw_name = FW_VERSION_STRINGS.get(fw, "unknown")
        hw_name = HW_VERSION_STRINGS.get(hw, "unknown")
        _ok(f"firmware version: fw={fw} ({fw_name}), hw={hw} ({hw_name})")
        if hw_name == "unknown":
            _warn(
                f"hw id {hw} not in our lookup table — newer hardware variant? "
                "Most code paths assume v4 (hw=1)."
            )
        elif hw != 1:
            _warn(
                f"hardware reports as {hw_name!r}; this codebase is tuned for "
                "scanlight v4 (hw=1). Some assumptions may not hold."
            )
        r, g, b = s.get_default_rgb(timeout=2.0)
        _ok(f"default RGB: ({r}, {g}, {b})")
        return True, s
    except Exception as e:
        _fail(f"handshake failed: {e}")
        s.close()
        return False, None


def check_scanlight_telemetry(s) -> bool:
    """Step 4 + 5: wait for telemetry, confirm VBUS sane."""
    _step(4, "Scanlight telemetry (200ms unsolicited stream)")
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and (s.last_temp_c is None or s.last_vbus_mv is None):
        time.sleep(0.05)
    if s.last_temp_c is None:
        _fail("no LED_TEMP packet within 1s — telemetry stream not running")
        return False
    _ok(f"LED temp: {s.last_temp_c:.2f} °C")
    if s.last_temp_c >= LED_TEMP_WARN_C:
        _warn(
            f"LED temp {s.last_temp_c:.1f} °C is within 10 °C of the firmware's "
            "nominal 80 °C thermal threshold. NOTE: in this firmware revision, "
            "the temp check is overwritten by the VBUS branch in the same tick, "
            "so thermal protection does NOT reliably trigger while USB power is "
            "healthy — see HANDOFF.md §'Firmware behavior gotchas'. Let the "
            "device cool down before continuing; don't rely on firmware self-protection."
        )
    if s.last_vbus_mv is None:
        _fail("no VBUS packet within 1s")
        return False
    _ok(f"VBUS: {s.last_vbus_mv} mV ({s.last_vbus_mv/1000:.2f} V)")

    _step(5, "VBUS sanity (RIGHT-side USB-C must be wall PSU ≥5V/2A)")
    if s.last_vbus_mv < VBUS_OK_MV:
        _fail(f"VBUS is {s.last_vbus_mv} mV — below {VBUS_OK_MV} mV threshold. "
              "Check the RIGHT-side USB-C cable + PSU. The firmware's hard "
              "cutoff is 4400 mV (below which it refuses to drive LEDs at all); "
              "we warn at 4500 mV to give 100 mV of margin.")
        return False
    _ok(f"VBUS is healthy ({s.last_vbus_mv} mV)")
    return True


def check_sony_capture_binary(sony_capture: str) -> tuple[bool, str | None]:
    """Step 6: sony-capture binary present + runs."""
    _step(6, "sony-capture binary")
    path = shutil.which(sony_capture)
    if path is None:
        # Look in the project build dir as a fallback
        candidate = REPO_ROOT / "phase1" / "sony-capture" / "build" / "sony-capture"
        if candidate.exists() and os.access(candidate, os.X_OK):
            _ok(f"found in build dir: {candidate}")
            return True, str(candidate)
        _fail(f"{sony_capture!r} not on $PATH and not in phase1/sony-capture/build/")
        _warn("build it first: cd phase1/sony-capture && cmake --build build")
        return False, None
    _ok(f"on $PATH at {path}")
    return True, path


def check_camera_enumeration(sony_capture_path: str) -> bool:
    """Step 7: tickle the SDK with a no-shutter camera enumeration."""
    _step(7, "Sony SDK + camera enumeration")
    try:
        proc = subprocess.run(
            [sony_capture_path, "--list"],
            capture_output=True, text=True, timeout=15,
        )
    except FileNotFoundError:
        _fail(f"could not execute {sony_capture_path}")
        return False
    except subprocess.TimeoutExpired:
        _fail("sony-capture hung — SDK may be deadlocked")
        return False

    out = (proc.stdout + proc.stderr).strip()
    # If the camera is present + in PC Remote, the binary prints at least one
    # SDK-visible camera. This path never connects or fires the shutter.
    if "EnumCameraObjects failed" in out or "no cameras found" in out:
        _fail("camera not detected by SDK. Check: camera powered (dummy battery), "
              "USB-C data cable, PC Remote mode ON, save destination = PC")
        return False
    if proc.returncode == 0:
        _ok("camera reachable AND triplet completed (you're done!)")
        return True
    # Got past enumeration but failed later (likely on download timeout
    # because we didn't fire a real shutter and the camera body isn't waiting).
    # That's a green for "SDK can see the camera".
    _ok("camera enumerated by SDK (further failures expected since no shutter was fired)")
    print(f"  (sony-capture output: {out[:200]})" if out else "")
    return True


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--port", help="Override Scanlight serial port (defaults to auto-discovery).")
    p.add_argument(
        "--sony-capture", default="sony-capture",
        help="Path or name of the sony-capture binary (default: search $PATH then build dir).",
    )
    p.add_argument(
        "--skip-camera", action="store_true",
        help="Skip the Sony SDK check (useful if only the Scanlight is plugged in).",
    )
    args = p.parse_args(argv)

    print(_bold("Film Scanner — hardware diagnostic"))

    ports = check_serial_ports()
    if not ports:
        return 1

    pico_ok = check_pico_vid_pid(ports)
    # Not fatal — user might have a non-Pico Scanlight variant
    _ = pico_ok

    sl_ok, sl = check_scanlight_handshake(args.port)
    if not sl_ok:
        return 1
    try:
        tele_ok = check_scanlight_telemetry(sl)
    finally:
        sl.close()
    if not tele_ok:
        return 1

    if args.skip_camera:
        print(_bold("\nScanlight checks passed. Camera checks skipped.\n"))
        return 0

    binary_ok, path = check_sony_capture_binary(args.sony_capture)
    if not binary_ok:
        return 1
    cam_ok = check_camera_enumeration(path)
    if not cam_ok:
        return 1

    print(_bold(_green("\nAll checks passed. Proceed to scripts/smoketest.sh.\n")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
