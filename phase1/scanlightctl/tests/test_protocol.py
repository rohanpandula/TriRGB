"""Unit tests for the Scanlight v4 wire protocol codec."""
import pytest

from scanlight import protocol as proto


class TestEncodePacket:
    def test_zero_data(self):
        assert proto.encode_packet(proto.H2D_GET_FW_VERSION) == bytes(
            [0xFE, 2, 0]
        )

    def test_with_data(self):
        pkt = proto.encode_packet(proto.H2D_SET_COLOR, bytes([10, 20, 30, 0, 0, 0]))
        assert pkt == bytes([0xFE, 0, 6, 10, 20, 30, 0, 0, 0])

    def test_header_out_of_range(self):
        with pytest.raises(proto.ProtocolError):
            proto.encode_packet(256)

    def test_data_too_long(self):
        with pytest.raises(proto.ProtocolError):
            proto.encode_packet(0, bytes(256))


class TestEncodeSetColor:
    def test_red_full(self):
        pkt = proto.encode_set_color(r=255, g=0, b=0, w=0)
        # 0xFE | 0 | 6 | R G B W IR save
        assert pkt == bytes([0xFE, 0, 6, 255, 0, 0, 0, 0, 0])

    def test_save_flag(self):
        pkt = proto.encode_set_color(r=10, g=20, b=30, w=0, save=True)
        assert pkt[-1] == 1
        # IR byte (index -2 in payload, byte 7 overall) always 0
        assert pkt[7] == 0

    def test_save_flag_default_zero(self):
        pkt = proto.encode_set_color(r=10, g=20, b=30, w=0)
        assert pkt[-1] == 0

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"r": -1, "g": 0, "b": 0, "w": 0},
            {"r": 256, "g": 0, "b": 0, "w": 0},
            {"r": 0, "g": -1, "b": 0, "w": 0},
            {"r": 0, "g": 0, "b": 999, "w": 0},
            {"r": 0, "g": 0, "b": 0, "w": 1000},
        ],
    )
    def test_range_validation(self, kwargs):
        with pytest.raises(proto.ProtocolError):
            proto.encode_set_color(**kwargs)


class TestEncodeShutterPulse:
    def test_default_100ms(self):
        """100 ms matches the canonical app_bsl default → byte value 10."""
        pkt = proto.encode_shutter_pulse(100)
        # 0xFE | header=3 | length=1 | byte=10
        assert pkt == bytes([0xFE, proto.H2D_SHUTTER_PULSE, 1, 10])

    def test_minimum(self):
        pkt = proto.encode_shutter_pulse(10)
        assert pkt[-1] == 1

    def test_maximum(self):
        pkt = proto.encode_shutter_pulse(2550)
        assert pkt[-1] == 255

    @pytest.mark.parametrize("ms", [0, 5, 9, 2551, 5000])
    def test_out_of_range(self, ms):
        with pytest.raises(proto.ProtocolError, match="out of range"):
            proto.encode_shutter_pulse(ms)

    @pytest.mark.parametrize("ms", [15, 25, 101, 999])
    def test_non_multiple_of_10_rejected(self, ms):
        with pytest.raises(proto.ProtocolError, match="multiple of 10"):
            proto.encode_shutter_pulse(ms)

    def test_float_rejected(self):
        """Common bug: someone passes seconds-as-float (0.1 → meant 100 ms)."""
        with pytest.raises(proto.ProtocolError, match="must be int"):
            proto.encode_shutter_pulse(0.1)  # type: ignore[arg-type]


class TestDecoders:
    def test_led_temp_positive(self):
        # 35.500 °C → 35500 millideg, big-endian on the wire
        data = (35500).to_bytes(4, "big", signed=True)
        assert proto.decode_led_temp(data) == pytest.approx(35.5)

    def test_led_temp_negative(self):
        data = (-12345).to_bytes(4, "big", signed=True)
        assert proto.decode_led_temp(data) == pytest.approx(-12.345)

    def test_led_temp_short(self):
        with pytest.raises(proto.ProtocolError):
            proto.decode_led_temp(b"\x00\x00")

    def test_vbus(self):
        data = (5040).to_bytes(4, "big", signed=True)
        assert proto.decode_vbus(data) == 5040

    def test_fw_version_wire_format(self):
        # Firmware sends FW_VERSION_ID + (HW_VERSION_ID << 16) as big-endian u32.
        # For scanlight v4 firmware 1.1.0: FW_VERSION_ID=1, HW_VERSION_ID=1.
        # So the four wire bytes are 0x00, 0x01, 0x00, 0x01.
        wire = bytes([0x00, 0x01, 0x00, 0x01])
        fw, hw = proto.decode_fw_version(wire)
        assert fw == 1
        assert hw == 1

    def test_fw_version_distinguishes_fw_and_hw(self):
        # Construct distinct values so we can tell which slot is which.
        # fw=0x0042, hw=0x00AB → word = 0x00AB0042 → bytes 00 AB 00 42
        wire = bytes([0x00, 0xAB, 0x00, 0x42])
        fw, hw = proto.decode_fw_version(wire)
        assert fw == 0x0042
        assert hw == 0x00AB

    def test_default_rgb(self):
        data = bytes([100, 150, 200])
        assert proto.decode_default_rgb(data) == (100, 150, 200)

    def test_default_rgb_short(self):
        with pytest.raises(proto.ProtocolError):
            proto.decode_default_rgb(b"\x01\x02")
