// FakeTransport — public in-process transport stub shared by the CLI
// (--fake mode) and the upcoming ScanlightApp (-FakeTransport YES launch
// argument). Having a single canonical fake guarantees GUI and CLI behavior
// stay byte-identical, satisfying the R-11 "GUI ↔ CLI parity" requirement.
//
// IMPORTANT: Tests/ScanlightSwiftTests/ScanlightDriverTests.swift defines
// its OWN simpler internal FakeTransport with a feed() helper and no
// synthesized responses. That is a different, @testable-private fixture and
// must NOT be renamed or removed — it is NOT this class.

import Foundation

/// In-process fake transport that synthesizes realistic Scanlight firmware
/// responses. Used by `scanlight-swift-cli --fake` and `scanlight-app
/// -FakeTransport YES` so end-to-end driver behavior can be validated
/// without a real USB-CDC device.
///
/// Response behaviour:
/// - `getFWVersion` request → `(fw=1, hw=1)` reply.
/// - `getDefaultRGB` request → `(r=255, g=200, b=180)` reply.
/// - Every `write(_:)` also synthesizes a `d2hLEDTemp` (32.5 °C) and a
///   `d2hVBUS` (5050 mV) telemetry packet so the telemetry fields always
///   populate.
public final class FakeTransport: ScanlightTransport {
    private let lock = NSLock()
    private let cv = NSCondition()
    private var tx = Data()
    private var rx = Data()
    private var closed = false

    public init() {}

    /// All bytes written to this transport (accumulated across all calls).
    public var transmitted: Data {
        lock.lock(); defer { lock.unlock() }; return tx
    }

    public func write(_ data: Data) throws {
        if closed { throw ScanlightError.transportClosed }
        lock.lock(); tx.append(data); lock.unlock()
        // Synthesize a response for known H2D requests so the full
        // request/response round-trip works without hardware.
        synthesizeResponses(for: data)
    }

    public func readAvailable() throws -> Data {
        cv.lock()
        if rx.isEmpty {
            cv.wait(until: Date(timeIntervalSinceNow: 0.05))
        }
        let out = rx
        rx.removeAll(keepingCapacity: true)
        cv.unlock()
        return out
    }

    public func close() {
        cv.lock(); closed = true; cv.broadcast(); cv.unlock()
    }

    // MARK: - private helpers

    private func synthesizeResponses(for data: Data) {
        guard data.count >= 3, data[data.startIndex] == ScanlightProtocol.startByte else { return }
        let header = data[data.startIndex + 1]
        if header == ScanlightProtocol.h2dGetFWVersion {
            // FW=1, HW=1 → wire word 0x00010001 BE
            let resp = Data([
                0xFE, ScanlightProtocol.d2hFWVersion, 4,
                0x00, 0x01, 0x00, 0x01,
            ])
            feed(resp)
        } else if header == ScanlightProtocol.h2dGetDefaultRGB {
            feed(Data([0xFE, ScanlightProtocol.d2hDefaultRGB, 3, 255, 200, 180]))
        }
        // Synthesize telemetry on every interaction so status fields have
        // something to show immediately.
        let temp: Int32 = 32500   // 32.5 °C in millidegrees
        let tempPayload = bigEndianInt32(temp)
        feed(Data([0xFE, ScanlightProtocol.d2hLEDTemp, 4]) + tempPayload)
        let vbus: Int32 = 5050    // 5.05 V in millivolts
        let vbusPayload = bigEndianInt32(vbus)
        feed(Data([0xFE, ScanlightProtocol.d2hVBUS, 4]) + vbusPayload)
    }

    private func bigEndianInt32(_ v: Int32) -> Data {
        let raw = UInt32(bitPattern: v)
        return Data([
            UInt8((raw >> 24) & 0xFF),
            UInt8((raw >> 16) & 0xFF),
            UInt8((raw >> 8)  & 0xFF),
            UInt8(raw & 0xFF),
        ])
    }

    private func feed(_ data: Data) {
        cv.lock(); rx.append(data); cv.broadcast(); cv.unlock()
    }
}
