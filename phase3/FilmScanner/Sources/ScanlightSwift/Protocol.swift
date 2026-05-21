// Scanlight v4 USB-CDC wire protocol — Swift port of the Python reference
// in phase1/scanlightctl/scanlight/protocol.py. Same constants, same
// endianness (LED_TEMP, VBUS, FW_VERSION are BIG-endian on the wire per the
// firmware's protocol_send_packet_uint32 — the published bsl_control_interface
// spec contradicts the firmware on this; the firmware is ground truth).

import Foundation

public enum ScanlightProtocol {
    public static let startByte: UInt8 = 0xFE

    // Host-to-device headers
    public static let h2dSetColor: UInt8 = 0
    public static let h2dGetDefaultRGB: UInt8 = 1
    public static let h2dGetFWVersion: UInt8 = 2
    // H2D_SHUTTER_PULSE was originally marked "do not use" because firing
    // the 3.5mm jack while the camera is USB-tethered to the same Mac
    // closes a ground loop. It is **safe to use when the camera tether
    // is Wi-Fi** (no closed USB-ground loop). See PROJECT.md §Hardware
    // architecture, Configuration B.
    public static let h2dShutterPulse: UInt8 = 3

    // The remaining H2D packets exist in the firmware but this client never
    // sends them. DFU_MODE (4) reboots into the RP2040 bootloader for flashing
    // (manual/recovery only). SET_TRIM (5)/GET_TRIM (6) write per-channel NVM
    // trim, compiled ONLY for the BSL1 board (`#ifdef HW_VERSION_BSL1`); on the
    // Scanlight v4 (SL4 build) they are no-ops, and we correct per-channel in
    // software anyway. Defined for a faithful mirror of the firmware protocol.h.
    public static let h2dDFUMode: UInt8 = 4
    public static let h2dSetTrim: UInt8 = 5
    public static let h2dGetTrim: UInt8 = 6

    // Shutter-pulse byte encoding: the firmware accepts one byte
    // representing pulse length in 10 ms units, clamped to [1, 255]
    // → 10 ms … 2550 ms.
    public static let shutterPulseUnitMs: Int = 10
    public static let shutterPulseMinMs: Int = 10
    public static let shutterPulseMaxMs: Int = 2550

    // Device-to-host headers.
    // D2H_ACK (0) is a dead opcode — declared by the firmware but never sent
    // (verified against main.c/protocol.c). D2H_TRIM (5) is the GET_TRIM reply,
    // BSL1-only and never emitted by the v4 SL4 build. Both defined only for a
    // complete mirror of the firmware protocol.h; neither is handled here.
    public static let d2hACK: UInt8 = 0
    public static let d2hLEDTemp: UInt8 = 1
    public static let d2hVBUS: UInt8 = 2
    public static let d2hFWVersion: UInt8 = 3
    public static let d2hDefaultRGB: UInt8 = 4
    public static let d2hTrim: UInt8 = 5

    public enum ProtocolError: Error, Equatable {
        case headerOutOfRange(Int)
        case dataTooLong(Int)
        case channelOutOfRange(String, Int)
        case payloadTooShort(name: String, got: Int, need: Int)
    }

    /// Frame a single packet for transmission.
    /// Layout: `[0xFE][header][length][data...]`.
    public static func encodePacket(header: UInt8, data: Data = Data()) throws -> Data {
        guard data.count <= 255 else {
            throw ProtocolError.dataTooLong(data.count)
        }
        var out = Data()
        out.reserveCapacity(3 + data.count)
        out.append(startByte)
        out.append(header)
        out.append(UInt8(data.count))
        out.append(data)
        return out
    }

    /// Build a PKT_H2D_SET_COLOR packet.
    /// IR byte is always 0 (ignored by v4 firmware). `save` writes the
    /// values to NVM as power-on defaults — finite write cycles, opt-in.
    public static func encodeSetColor(
        r: Int, g: Int, b: Int, w: Int = 0, save: Bool = false
    ) throws -> Data {
        for (name, value) in [("r", r), ("g", g), ("b", b), ("w", w)] {
            guard (0...255).contains(value) else {
                throw ProtocolError.channelOutOfRange(name, value)
            }
        }
        let payload = Data([
            UInt8(r), UInt8(g), UInt8(b), UInt8(w),
            0,                       // IR
            save ? 1 : 0,            // save_preset
        ])
        return try encodePacket(header: h2dSetColor, data: payload)
    }

    /// Build a PKT_H2D_SHUTTER_PULSE packet. Validates strictly rather
    /// than clamping — out-of-range or non-10-ms-multiple values throw,
    /// because silent rounding masks the common bug of confusing seconds
    /// (0.1) with ms (100).
    public static func encodeShutterPulse(pulseMs: Int) throws -> Data {
        guard pulseMs >= shutterPulseMinMs, pulseMs <= shutterPulseMaxMs else {
            throw ProtocolError.channelOutOfRange("pulse_ms", pulseMs)
        }
        guard pulseMs % shutterPulseUnitMs == 0 else {
            throw ProtocolError.channelOutOfRange("pulse_ms (not multiple of 10)", pulseMs)
        }
        let byteValue = UInt8(pulseMs / shutterPulseUnitMs)
        return try encodePacket(header: h2dShutterPulse, data: Data([byteValue]))
    }

    /// LED_TEMP payload (int32 BE millidegrees C) → °C.
    public static func decodeLEDTemp(_ data: Data) throws -> Double {
        guard data.count >= 4 else {
            throw ProtocolError.payloadTooShort(name: "LED_TEMP", got: data.count, need: 4)
        }
        let mdeg = readInt32BE(data, offset: 0)
        return Double(mdeg) / 1000.0
    }

    /// VBUS payload (int32 BE millivolts).
    public static func decodeVBUS(_ data: Data) throws -> Int32 {
        guard data.count >= 4 else {
            throw ProtocolError.payloadTooShort(name: "VBUS", got: data.count, need: 4)
        }
        return readInt32BE(data, offset: 0)
    }

    /// FW_VERSION payload. Firmware computes `fw + (hw << 16)` and emits
    /// as big-endian u32 → wire bytes `[hw_hi, hw_lo, fw_hi, fw_lo]`.
    public static func decodeFWVersion(_ data: Data) throws -> (fw: UInt16, hw: UInt16) {
        guard data.count >= 4 else {
            throw ProtocolError.payloadTooShort(name: "FW_VERSION", got: data.count, need: 4)
        }
        let word = readUInt32BE(data, offset: 0)
        let fw = UInt16(word & 0xFFFF)
        let hw = UInt16((word >> 16) & 0xFFFF)
        return (fw, hw)
    }

    /// DEFAULT_RGB payload → (r, g, b).
    public static func decodeDefaultRGB(_ data: Data) throws -> (r: UInt8, g: UInt8, b: UInt8) {
        guard data.count >= 3 else {
            throw ProtocolError.payloadTooShort(name: "DEFAULT_RGB", got: data.count, need: 3)
        }
        return (data[0], data[1], data[2])
    }

    // MARK: - private helpers

    private static func readUInt32BE(_ d: Data, offset: Int) -> UInt32 {
        let i = d.startIndex + offset
        return (UInt32(d[i])     << 24)
             | (UInt32(d[i + 1]) << 16)
             | (UInt32(d[i + 2]) << 8)
             |  UInt32(d[i + 3])
    }

    private static func readInt32BE(_ d: Data, offset: Int) -> Int32 {
        return Int32(bitPattern: readUInt32BE(d, offset: offset))
    }
}
