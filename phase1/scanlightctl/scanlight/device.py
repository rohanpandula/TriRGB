"""Scanlight v4 USB-CDC device driver.

A `Scanlight` instance owns one serial port plus a background reader thread.
The reader continuously decodes incoming packets and either updates cached
telemetry (LED_TEMP, VBUS) or hands the payload to a per-header response
queue (FW_VERSION, DEFAULT_RGB). Host requests block on the matching queue.

Designed to be reused in Phase 2's triplet-capture orchestrator — the CLI
in `scanlight.cli` is a thin shell around this class.
"""
from __future__ import annotations

import queue
import threading
from typing import Optional

import serial
from serial.tools import list_ports

from . import protocol as proto


DEFAULT_BAUDRATE = 115200
DEFAULT_READ_TIMEOUT_S = 0.1


# Scanlight v4 firmware builds on the Pico SDK with `pico_enable_stdio_usb`,
# so the device enumerates with the stock Pico CDC descriptors.
PICO_VID = 0x2E8A
PICO_CDC_PIDS = {0x000A, 0x0009}  # 000A = SDK stdio CDC; 0009 = picoboot


def discover_port() -> str:
    """Best-effort auto-discovery of the Scanlight CDC serial port on macOS.

    Order of preference:
      1. A port whose VID:PID matches the Raspberry Pi Pico CDC descriptors.
         The Scanlight v4 firmware uses these stock descriptors (no custom
         vendor strings), so this is the strongest signal.
      2. A port whose descriptor explicitly contains "scanlight" — defensive
         in case future firmware ships custom USB strings.
      3. If exactly one cu.usbmodem* port exists, return it.

    If multiple candidates remain at any level, raise with the list so the
    operator can pick one via `--port`.
    """
    ports = list(list_ports.comports())

    pico = [p for p in ports if p.vid == PICO_VID and p.pid in PICO_CDC_PIDS]
    if len(pico) == 1:
        return pico[0].device
    if len(pico) > 1:
        raise RuntimeError(
            "Multiple Raspberry Pi Pico CDC ports found; pass --port to disambiguate: "
            + ", ".join(f"{p.device} ({p.vid:04x}:{p.pid:04x})" for p in pico)
        )

    def fields(p) -> str:
        return " ".join(
            str(x or "")
            for x in (p.description, p.manufacturer, p.product, p.interface)
        ).lower()

    named = [p for p in ports if "scanlight" in fields(p)]
    if len(named) == 1:
        return named[0].device
    if len(named) > 1:
        raise RuntimeError(
            "Multiple Scanlight-like serial ports found; pass --port to disambiguate: "
            + ", ".join(p.device for p in named)
        )

    usbmodem = [p for p in ports if "usbmodem" in p.device.lower()]
    if len(usbmodem) == 1:
        return usbmodem[0].device

    if not ports:
        raise RuntimeError("No serial ports found. Is the Scanlight plugged in?")
    raise RuntimeError(
        "Could not auto-discover Scanlight serial port. "
        "Pass --port. Available ports: "
        + ", ".join(p.device for p in ports)
    )


class Scanlight:
    """Driver for the Scanlight v4 narrowband-RGB light source.

    Use as a context manager whenever possible — the background reader
    thread must be stopped cleanly for the serial port to release.

    Test seam: pass `serial_obj` to inject a fake serial. When `serial_obj`
    is provided, `port` and `baudrate` are ignored.
    """

    def __init__(
        self,
        port: Optional[str] = None,
        *,
        serial_obj=None,
        baudrate: int = DEFAULT_BAUDRATE,
        read_timeout_s: float = DEFAULT_READ_TIMEOUT_S,
    ):
        if serial_obj is not None:
            self._serial = serial_obj
            self._port = getattr(serial_obj, "port", "<injected>")
        else:
            self._port = port or discover_port()
            self._serial = serial.Serial(
                self._port, baudrate=baudrate, timeout=read_timeout_s
            )

        self._lock = threading.Lock()
        self._last_temp_c: Optional[float] = None
        self._last_vbus_mv: Optional[int] = None
        self._response_queues: dict[int, queue.Queue] = {
            proto.D2H_FW_VERSION: queue.Queue(),
            proto.D2H_DEFAULT_RGB: queue.Queue(),
        }

        self._reader_stop = threading.Event()
        self._reader_error: Optional[BaseException] = None
        self._reader_thread = threading.Thread(
            target=self._reader_loop, name="scanlight-reader", daemon=True
        )
        self._reader_thread.start()

    # ----- public API -----

    @property
    def port(self) -> str:
        return self._port

    @property
    def last_temp_c(self) -> Optional[float]:
        with self._lock:
            return self._last_temp_c

    @property
    def last_vbus_mv(self) -> Optional[int]:
        with self._lock:
            return self._last_vbus_mv

    def set_color(
        self, r: int = 0, g: int = 0, b: int = 0, w: int = 0, save: bool = False
    ) -> None:
        """Set R, G, B, W channels. `save=True` writes to NVM (use sparingly)."""
        if w and (r or g or b):
            # Firmware enforces this too; surfacing it here gives a clearer error.
            raise ValueError(
                "White channel cannot be on simultaneously with any RGB channel"
            )
        self._serial.write(proto.encode_set_color(r, g, b, w, save))

    def off(self) -> None:
        self.set_color(0, 0, 0, 0)

    def pulse_shutter(self, pulse_ms: int = 100) -> None:
        """Fire the 3.5mm shutter trigger output for `pulse_ms` ms.

        Pulse length must be a multiple of 10 ms in [10, 2550] (firmware
        resolution and range). Default 100 ms matches the canonical
        `app_bsl` Vue web app's `shutterPulseLength = 0.1` seconds.

        Notes:
          - Sony bodies typically need at least ~30–100 ms. Fujifilm
            mirrorless needs ~300 ms minimum and 1000 ms between pulses.
          - The host should wait (pulse_ms + ~1 s) between pulses to give
            the camera time to expose and clear its drive cycle.
          - Only safe with the camera tether on Wi-Fi (not USB) — a USB
            tether closes a ground loop through the 3.5mm cable.
        """
        self._serial.write(proto.encode_shutter_pulse(pulse_ms))

    def get_fw_version(self, timeout: float = 2.0) -> tuple[int, int]:
        """Request (firmware_id, hardware_id). Raises TimeoutError on no reply."""
        return self._request(
            proto.H2D_GET_FW_VERSION, proto.D2H_FW_VERSION, proto.decode_fw_version, timeout
        )

    def get_default_rgb(self, timeout: float = 2.0) -> tuple[int, int, int]:
        """Request the NVM-stored power-on RGB defaults."""
        return self._request(
            proto.H2D_GET_DEFAULT_RGB,
            proto.D2H_DEFAULT_RGB,
            proto.decode_default_rgb,
            timeout,
        )

    def close(self) -> None:
        self._reader_stop.set()
        if self._reader_thread.is_alive():
            self._reader_thread.join(timeout=1.0)
        try:
            if getattr(self._serial, "is_open", True):
                self._serial.close()
        except Exception:
            pass

    def __enter__(self) -> "Scanlight":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ----- internals -----

    def _request(self, h2d_header: int, d2h_header: int, decoder, timeout: float):
        q = self._response_queues[d2h_header]
        # Drain any stale response from a prior aborted call.
        while not q.empty():
            try:
                q.get_nowait()
            except queue.Empty:
                break
        self._serial.write(proto.encode_packet(h2d_header))
        try:
            data = q.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError(
                f"No response to header {h2d_header} within {timeout}s"
            )
        if self._reader_error is not None:
            raise self._reader_error
        return decoder(data)

    def _reader_loop(self) -> None:
        buf = bytearray()
        try:
            while not self._reader_stop.is_set():
                chunk = self._serial.read(256)
                if chunk:
                    buf.extend(chunk)
                    self._consume(buf)
        except BaseException as exc:  # noqa: BLE001 — surfaced to main thread
            self._reader_error = exc

    def _consume(self, buf: bytearray) -> None:
        """Parse as many complete packets from `buf` as available, in place."""
        while True:
            if not buf:
                return
            # Resync to start byte.
            if buf[0] != proto.START_BYTE:
                idx = buf.find(bytes([proto.START_BYTE]))
                if idx < 0:
                    buf.clear()
                    return
                del buf[:idx]
                continue
            if len(buf) < 3:
                return  # need header + length
            length = buf[2]
            total = 3 + length
            if len(buf) < total:
                return  # wait for body
            header = buf[1]
            data = bytes(buf[3:total])
            del buf[:total]
            self._dispatch(header, data)

    def _dispatch(self, header: int, data: bytes) -> None:
        if header == proto.D2H_LED_TEMP:
            try:
                temp = proto.decode_led_temp(data)
            except proto.ProtocolError:
                return
            with self._lock:
                self._last_temp_c = temp
        elif header == proto.D2H_VBUS:
            try:
                mv = proto.decode_vbus(data)
            except proto.ProtocolError:
                return
            with self._lock:
                self._last_vbus_mv = mv
        elif header in self._response_queues:
            self._response_queues[header].put(data)
        # Unknown headers are silently dropped — forward-compat with newer firmware.
