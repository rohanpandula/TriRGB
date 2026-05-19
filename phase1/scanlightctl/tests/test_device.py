"""Tests for the Scanlight class using an in-memory fake serial port."""
import threading
import time

import pytest

from scanlight import protocol as proto
from scanlight.device import Scanlight


class FakeSerial:
    """Minimal duck-type of pyserial.Serial for tests.

    - `write` records all transmitted bytes.
    - `read` blocks until bytes are available in the rx buffer or until the
      configured timeout elapses (returns whatever is currently available).
    - `feed` is the test-side hook to inject device-to-host bytes.
    """

    def __init__(self):
        self.tx = bytearray()
        self._rx = bytearray()
        self._cv = threading.Condition()
        self.is_open = True
        self.port = "<fake>"

    # --- pyserial-compatible surface ---
    def write(self, data: bytes) -> int:
        self.tx.extend(data)
        return len(data)

    def read(self, size: int = 1) -> bytes:
        with self._cv:
            # Short wait, mimics pyserial timeout. Don't block forever, so
            # the reader thread can poll its stop flag.
            self._cv.wait(timeout=0.05)
            n = min(size, len(self._rx))
            out = bytes(self._rx[:n])
            del self._rx[:n]
            return out

    def close(self):
        self.is_open = False
        with self._cv:
            self._cv.notify_all()

    # --- test helpers ---
    def feed(self, data: bytes) -> None:
        with self._cv:
            self._rx.extend(data)
            self._cv.notify_all()


def _wait_for(predicate, timeout=1.0, step=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return False


def test_set_color_writes_correct_packet():
    fake = FakeSerial()
    with Scanlight(serial_obj=fake) as s:
        s.set_color(r=255, g=0, b=0, w=0)
    assert bytes(fake.tx) == bytes([0xFE, 0, 6, 255, 0, 0, 0, 0, 0])


def test_set_color_with_save_flag():
    fake = FakeSerial()
    with Scanlight(serial_obj=fake) as s:
        s.set_color(r=10, g=20, b=30, save=True)
    # Last byte is save_preset
    assert fake.tx[-1] == 1


def test_set_color_rejects_white_with_rgb():
    fake = FakeSerial()
    with Scanlight(serial_obj=fake) as s:
        with pytest.raises(ValueError):
            s.set_color(r=100, w=100)
    # Nothing should have been sent
    assert len(fake.tx) == 0


def test_off_sends_zeros():
    fake = FakeSerial()
    with Scanlight(serial_obj=fake) as s:
        s.off()
    assert bytes(fake.tx) == bytes([0xFE, 0, 6, 0, 0, 0, 0, 0, 0])


def test_pulse_shutter_default_100ms():
    """100 ms default → byte value 10 in the SHUTTER_PULSE packet."""
    fake = FakeSerial()
    with Scanlight(serial_obj=fake) as s:
        s.pulse_shutter()
    assert bytes(fake.tx) == bytes([0xFE, proto.H2D_SHUTTER_PULSE, 1, 10])


def test_pulse_shutter_explicit_length():
    fake = FakeSerial()
    with Scanlight(serial_obj=fake) as s:
        s.pulse_shutter(300)
    # 300 ms / 10 ms/unit = 30
    assert bytes(fake.tx) == bytes([0xFE, proto.H2D_SHUTTER_PULSE, 1, 30])


def test_pulse_shutter_rejects_out_of_range():
    fake = FakeSerial()
    with Scanlight(serial_obj=fake) as s:
        with pytest.raises(proto.ProtocolError):
            s.pulse_shutter(5)  # below 10 ms minimum
        with pytest.raises(proto.ProtocolError):
            s.pulse_shutter(3000)  # above 2550 ms maximum
    # No bytes should have been sent on the rejected calls.
    assert bytes(fake.tx) == b""


def test_telemetry_updates_properties():
    fake = FakeSerial()
    temp_pkt = bytes([0xFE, proto.D2H_LED_TEMP, 4]) + (32500).to_bytes(4, "big", signed=True)
    vbus_pkt = bytes([0xFE, proto.D2H_VBUS, 4]) + (5050).to_bytes(4, "big", signed=True)
    with Scanlight(serial_obj=fake) as s:
        fake.feed(temp_pkt + vbus_pkt)
        assert _wait_for(lambda: s.last_temp_c is not None and s.last_vbus_mv is not None)
        assert s.last_temp_c == pytest.approx(32.5)
        assert s.last_vbus_mv == 5050


def test_fw_version_request_response():
    fake = FakeSerial()
    # Firmware emits FW + (HW << 16) as big-endian u32.
    fw_id, hw_id = 0x1234, 0x5678
    fw_payload = (fw_id + (hw_id << 16)).to_bytes(4, "big")
    response = bytes([0xFE, proto.D2H_FW_VERSION, 4]) + fw_payload
    with Scanlight(serial_obj=fake) as s:
        # Schedule the response on a side thread to simulate the device.
        def respond():
            time.sleep(0.05)
            fake.feed(response)
        threading.Thread(target=respond, daemon=True).start()
        fw, hw = s.get_fw_version(timeout=1.0)
        assert (fw, hw) == (fw_id, hw_id)
    # And the request packet itself was sent.
    assert bytes([0xFE, proto.H2D_GET_FW_VERSION, 0]) in bytes(fake.tx)


def test_default_rgb_request_response():
    fake = FakeSerial()
    response = bytes([0xFE, proto.D2H_DEFAULT_RGB, 3, 11, 22, 33])
    with Scanlight(serial_obj=fake) as s:
        def respond():
            time.sleep(0.05)
            fake.feed(response)
        threading.Thread(target=respond, daemon=True).start()
        r, g, b = s.get_default_rgb(timeout=1.0)
        assert (r, g, b) == (11, 22, 33)


def test_request_times_out_when_no_response():
    fake = FakeSerial()
    with Scanlight(serial_obj=fake) as s:
        with pytest.raises(TimeoutError):
            s.get_fw_version(timeout=0.1)


def test_telemetry_in_between_does_not_satisfy_request():
    """A burst of unsolicited telemetry must not unblock a pending request."""
    fake = FakeSerial()
    temp_pkt = bytes([0xFE, proto.D2H_LED_TEMP, 4]) + (30000).to_bytes(
        4, "big", signed=True
    )
    vbus_pkt = bytes([0xFE, proto.D2H_VBUS, 4]) + (5000).to_bytes(
        4, "big", signed=True
    )
    with Scanlight(serial_obj=fake) as s:
        # Stream telemetry, then time out.
        def stream():
            for _ in range(5):
                fake.feed(temp_pkt + vbus_pkt)
                time.sleep(0.02)
        threading.Thread(target=stream, daemon=True).start()
        with pytest.raises(TimeoutError):
            s.get_fw_version(timeout=0.2)
        # But telemetry properties should still be populated.
        assert s.last_temp_c is not None


def test_resync_after_junk_bytes():
    """Garbage before a valid start byte must be discarded."""
    fake = FakeSerial()
    junk = b"\x01\x02\x03"
    fw_id, hw_id = 7, 8
    fw_payload = (fw_id + (hw_id << 16)).to_bytes(4, "big")
    response = bytes([0xFE, proto.D2H_FW_VERSION, 4]) + fw_payload
    with Scanlight(serial_obj=fake) as s:
        def respond():
            time.sleep(0.05)
            fake.feed(junk + response)
        threading.Thread(target=respond, daemon=True).start()
        fw, hw = s.get_fw_version(timeout=1.0)
        assert (fw, hw) == (fw_id, hw_id)


def test_split_packet_assembles_correctly():
    """A packet arriving across multiple read chunks must still parse."""
    fake = FakeSerial()
    pkt = bytes([0xFE, proto.D2H_LED_TEMP, 4]) + (20000).to_bytes(4, "big", signed=True)
    with Scanlight(serial_obj=fake) as s:
        # Feed one byte at a time, with delays.
        def feed_drip():
            for byte in pkt:
                fake.feed(bytes([byte]))
                time.sleep(0.005)
        threading.Thread(target=feed_drip, daemon=True).start()
        assert _wait_for(lambda: s.last_temp_c is not None, timeout=2.0)
        assert s.last_temp_c == pytest.approx(20.0)


def test_unknown_header_is_silently_dropped():
    """Forward-compat with newer firmware: unknown headers must not crash."""
    fake = FakeSerial()
    unknown = bytes([0xFE, 99, 2, 0xAA, 0xBB])
    temp_pkt = bytes([0xFE, proto.D2H_LED_TEMP, 4]) + (15000).to_bytes(
        4, "big", signed=True
    )
    with Scanlight(serial_obj=fake) as s:
        fake.feed(unknown + temp_pkt)
        assert _wait_for(lambda: s.last_temp_c is not None)
        assert s.last_temp_c == pytest.approx(15.0)
