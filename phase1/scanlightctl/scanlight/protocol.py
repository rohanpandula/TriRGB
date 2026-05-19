"""Scanlight v4 USB-CDC serial protocol — packet constants and codec helpers.

Wire format (both directions):

    byte 0: 0xFE       start byte, always
    byte 1: header     packet type
    byte 2: length N   payload length in bytes
    bytes 3..3+N: data payload

Telemetry packets (D2H LED_TEMP, VBUS) arrive every ~200ms unsolicited.
Responses (D2H FW_VERSION, DEFAULT_RGB) arrive only after the matching
host request. Dispatch by header byte; never assume read order.
"""
from __future__ import annotations

START_BYTE = 0xFE

# Host-to-device headers
H2D_SET_COLOR = 0
H2D_GET_DEFAULT_RGB = 1
H2D_GET_FW_VERSION = 2
# H2D_SHUTTER_PULSE was originally marked "do not use" because firing the
# 3.5mm jack while the camera is USB-tethered to the same Mac as the
# Scanlight creates a ground loop. It is **safe to use when the camera
# tether is Wi-Fi** (no closed USB-ground loop). See PROJECT.md and
# `docs/optical_dry_run.md` for the operator-side rules.
H2D_SHUTTER_PULSE = 3

# Device-to-host headers
# D2H_ACK (header 0) is declared by the firmware/web app but never handled
# in the canonical Vue app — we likewise drop ACK frames on the floor.
D2H_LED_TEMP = 1
D2H_VBUS = 2
D2H_FW_VERSION = 3
D2H_DEFAULT_RGB = 4

# Shutter-pulse byte encoding (per canonical `app_bsl/src/components/Main.vue`):
# the firmware takes one byte representing pulse length in units of 10 ms,
# clamped to [1, 255] → 10 ms … 2550 ms. We accept ms at the public API
# layer and convert here.
SHUTTER_PULSE_UNIT_MS = 10
SHUTTER_PULSE_MIN_MS = SHUTTER_PULSE_UNIT_MS              # 10 ms
SHUTTER_PULSE_MAX_MS = SHUTTER_PULSE_UNIT_MS * 255        # 2550 ms


class ProtocolError(Exception):
    """Raised for malformed packets or out-of-range values."""


def encode_packet(header: int, data: bytes = b"") -> bytes:
    """Frame a single packet for transmission."""
    if not 0 <= header <= 255:
        raise ProtocolError(f"header out of range: {header}")
    if len(data) > 255:
        raise ProtocolError(f"data too long: {len(data)} bytes")
    return bytes([START_BYTE, header, len(data)]) + bytes(data)


def encode_set_color(r: int, g: int, b: int, w: int = 0, save: bool = False) -> bytes:
    """Build a PKT_H2D_SET_COLOR packet.

    IR byte is always 0 (ignored by v4 firmware).
    save_preset writes the values to NVM as power-on defaults — finite write
    cycles, so it must be opt-in.
    """
    for name, value in (("r", r), ("g", g), ("b", b), ("w", w)):
        if not 0 <= value <= 255:
            raise ProtocolError(f"{name} channel out of range 0–255: {value}")
    payload = bytes([r, g, b, w, 0, 1 if save else 0])
    return encode_packet(H2D_SET_COLOR, payload)


def encode_shutter_pulse(pulse_ms: int) -> bytes:
    """Build a PKT_H2D_SHUTTER_PULSE packet.

    `pulse_ms` is the requested pulse length in milliseconds. We
    **validate strictly** rather than clamp: out-of-range or sub-10 ms
    values raise `ProtocolError`. Silent rounding would mask the common
    bug of confusing seconds-as-float (0.1) with ms-as-int (100).
    Firmware accepts the range [10, 2550] ms in 10 ms steps.
    """
    if not isinstance(pulse_ms, int):
        raise ProtocolError(
            f"pulse_ms must be int (got {type(pulse_ms).__name__}); "
            "if you have seconds, multiply by 1000 yourself"
        )
    if pulse_ms < SHUTTER_PULSE_MIN_MS or pulse_ms > SHUTTER_PULSE_MAX_MS:
        raise ProtocolError(
            f"pulse_ms {pulse_ms} out of range "
            f"[{SHUTTER_PULSE_MIN_MS}, {SHUTTER_PULSE_MAX_MS}] ms"
        )
    if pulse_ms % SHUTTER_PULSE_UNIT_MS != 0:
        raise ProtocolError(
            f"pulse_ms {pulse_ms} is not a multiple of {SHUTTER_PULSE_UNIT_MS} ms "
            "(firmware resolution)"
        )
    byte_value = pulse_ms // SHUTTER_PULSE_UNIT_MS
    return encode_packet(H2D_SHUTTER_PULSE, bytes([byte_value]))


def decode_led_temp(data: bytes) -> float:
    """LED_TEMP payload → degrees C.

    Wire format is 32-bit signed millidegrees, **big-endian** (firmware emits
    MSB-first via `protocol_send_packet_int32`).
    """
    if len(data) < 4:
        raise ProtocolError(f"LED_TEMP payload too short: {len(data)} bytes")
    millideg = int.from_bytes(data[:4], "big", signed=True)
    return millideg / 1000.0


def decode_vbus(data: bytes) -> int:
    """VBUS payload → millivolts.

    Wire format is 32-bit signed millivolts, **big-endian**.
    """
    if len(data) < 4:
        raise ProtocolError(f"VBUS payload too short: {len(data)} bytes")
    return int.from_bytes(data[:4], "big", signed=True)


def decode_fw_version(data: bytes) -> tuple[int, int]:
    """FW_VERSION payload → (firmware_id, hardware_id).

    Firmware computes `FW + (HW << 16)` and emits as a big-endian u32. So on
    the wire the four bytes are `[HW_hi, HW_lo, FW_hi, FW_lo]` and the
    low 16 bits of the word are FW, the high 16 bits are HW. (The published
    bsl_control_interface.md spec has the byte order reversed; the firmware
    and official web app are ground truth here.)
    """
    if len(data) < 4:
        raise ProtocolError(f"FW_VERSION payload too short: {len(data)} bytes")
    word = int.from_bytes(data[:4], "big")
    fw = word & 0xFFFF
    hw = (word >> 16) & 0xFFFF
    return fw, hw


def decode_default_rgb(data: bytes) -> tuple[int, int, int]:
    """DEFAULT_RGB payload → (r, g, b)."""
    if len(data) < 3:
        raise ProtocolError(f"DEFAULT_RGB payload too short: {len(data)} bytes")
    return data[0], data[1], data[2]
