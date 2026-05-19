"""`scanlightctl` — terminal control for the Scanlight v4."""
from __future__ import annotations

import argparse
import sys
import time
from typing import Optional, Sequence

from .device import Scanlight


def _channel_value(s: str) -> int:
    v = int(s)
    if not 0 <= v <= 255:
        raise argparse.ArgumentTypeError(f"channel value must be 0–255, got {v}")
    return v


def _cmd_on(s: Scanlight, args: argparse.Namespace) -> int:
    kwargs = {"r": 0, "g": 0, "b": 0, "w": 0}
    kwargs[args.channel] = args.level
    s.set_color(**kwargs)
    return 0


def _cmd_off(s: Scanlight, _args: argparse.Namespace) -> int:
    s.off()
    return 0


def _cmd_set(s: Scanlight, args: argparse.Namespace) -> int:
    s.set_color(r=args.r, g=args.g, b=args.b, w=0)
    return 0


def _cmd_set_default(s: Scanlight, args: argparse.Namespace) -> int:
    # save_preset=1 — writes NVM. Explicit user opt-in via this subcommand.
    s.set_color(r=args.r, g=args.g, b=args.b, w=0, save=True)
    print(
        f"persisted defaults R={args.r} G={args.g} B={args.b} to NVM",
        file=sys.stderr,
    )
    return 0


def _pulse_ms_value(s: str) -> int:
    """Validate a millisecond pulse length on the CLI surface.

    Accept integers in [10, 2550] that are multiples of 10. We reject
    fractional values up front rather than rounding them, because
    "1.5 ms" is far more likely to mean "I confused seconds with ms"
    than "I actually wanted a 1.5 ms pulse."
    """
    try:
        v = int(s)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"pulse-ms must be an integer in [10, 2550], got {s!r}"
        )
    if not 10 <= v <= 2550:
        raise argparse.ArgumentTypeError(
            f"pulse-ms must be in [10, 2550] ms, got {v}"
        )
    if v % 10 != 0:
        raise argparse.ArgumentTypeError(
            f"pulse-ms must be a multiple of 10 (firmware resolution), got {v}"
        )
    return v


def _cmd_pulse(s: Scanlight, args: argparse.Namespace) -> int:
    s.pulse_shutter(args.pulse_ms)
    return 0


def _cmd_status(s: Scanlight, args: argparse.Namespace) -> int:
    fw, hw = s.get_fw_version(timeout=args.timeout)
    r, g, b = s.get_default_rgb(timeout=args.timeout)
    # Telemetry arrives every ~200ms unsolicited; wait briefly for first packet.
    deadline = time.monotonic() + 0.6
    while time.monotonic() < deadline and (
        s.last_temp_c is None or s.last_vbus_mv is None
    ):
        time.sleep(0.05)

    temp = s.last_temp_c
    vbus = s.last_vbus_mv
    print(f"port:         {s.port}")
    print(f"firmware:     {fw}")
    print(f"hardware:     {hw}")
    print(f"default RGB:  {r}, {g}, {b}")
    print(f"LED temp:     {temp:.2f} °C" if temp is not None else "LED temp:     (no data yet)")
    if vbus is not None:
        print(f"VBUS:         {vbus} mV ({vbus / 1000.0:.2f} V)")
    else:
        print("VBUS:         (no data yet)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="scanlightctl",
        description="Control the Scanlight v4 narrowband-RGB light source over USB serial.",
    )
    p.add_argument(
        "--port",
        help="Serial port path (e.g. /dev/cu.usbmodem*). Auto-discovered if omitted.",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=2.0,
        help="Response timeout in seconds for requests that expect a reply (default: 2.0).",
    )

    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    p_on = sub.add_parser("on", help="Turn on a single channel; other channels go to 0.")
    p_on.add_argument("channel", choices=["r", "g", "b", "w"])
    p_on.add_argument(
        "--level",
        type=_channel_value,
        default=255,
        help="Channel brightness 0–255 (default: 255).",
    )
    p_on.set_defaults(func=_cmd_on)

    p_off = sub.add_parser("off", help="Set all channels to 0.")
    p_off.set_defaults(func=_cmd_off)

    p_set = sub.add_parser("set", help="Set R, G, B simultaneously; W goes to 0.")
    p_set.add_argument("--r", type=_channel_value, required=True)
    p_set.add_argument("--g", type=_channel_value, required=True)
    p_set.add_argument("--b", type=_channel_value, required=True)
    p_set.set_defaults(func=_cmd_set)

    p_setd = sub.add_parser(
        "set-default",
        help="Set R, G, B and persist as power-on defaults (writes NVM — use sparingly).",
    )
    p_setd.add_argument("--r", type=_channel_value, required=True)
    p_setd.add_argument("--g", type=_channel_value, required=True)
    p_setd.add_argument("--b", type=_channel_value, required=True)
    p_setd.set_defaults(func=_cmd_set_default)

    p_pulse = sub.add_parser(
        "pulse",
        help=(
            "Fire the 3.5mm shutter trigger output. Pulse length in ms, "
            "must be a multiple of 10 in [10, 2550]. Default 100 ms."
        ),
    )
    p_pulse.add_argument(
        "pulse_ms",
        type=_pulse_ms_value,
        nargs="?",
        default=100,
        help="Pulse length in milliseconds (default: 100).",
    )
    p_pulse.set_defaults(func=_cmd_pulse)

    p_status = sub.add_parser(
        "status", help="Print firmware version, NVM defaults, and recent telemetry."
    )
    p_status.set_defaults(func=_cmd_status)

    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        with Scanlight(port=args.port) as s:
            return args.func(s, args)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"scanlightctl: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
