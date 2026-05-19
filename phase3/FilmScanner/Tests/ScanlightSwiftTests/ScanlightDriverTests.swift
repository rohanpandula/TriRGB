import XCTest
@testable import ScanlightSwift

/// In-memory transport for tests. Mirrors the Python `FakeSerial`:
/// `write()` records bytes; `feed(_:)` injects device-to-host bytes;
/// `readAvailable()` returns whatever's been fed.
final class FakeTransport: ScanlightTransport {
    private let lock = NSLock()
    private let cv = NSCondition()
    private var tx = Data()
    private var rx = Data()
    private var closed = false

    var transmitted: Data { lock.lock(); defer { lock.unlock() }; return tx }

    func write(_ data: Data) throws {
        if closed { throw ScanlightError.transportClosed }
        lock.lock(); tx.append(data); lock.unlock()
    }

    func readAvailable() throws -> Data {
        cv.lock()
        if rx.isEmpty {
            // Short wait — mimics pyserial's timeout
            cv.wait(until: Date(timeIntervalSinceNow: 0.05))
        }
        let out = rx
        rx.removeAll(keepingCapacity: true)
        cv.unlock()
        return out
    }

    func close() {
        cv.lock(); closed = true; cv.broadcast(); cv.unlock()
    }

    func feed(_ data: Data) {
        cv.lock(); rx.append(data); cv.broadcast(); cv.unlock()
    }
}

final class ScanlightDriverTests: XCTestCase {

    private func waitFor(_ predicate: () -> Bool, timeout: TimeInterval = 1.0) -> Bool {
        let deadline = Date(timeIntervalSinceNow: timeout)
        while Date() < deadline {
            if predicate() { return true }
            Thread.sleep(forTimeInterval: 0.01)
        }
        return predicate()
    }

    func testSetColorWritesCorrectPacket() throws {
        let fake = FakeTransport()
        let s = Scanlight(transport: fake)
        defer { s.close() }
        try s.setColor(r: 255, g: 0, b: 0)
        XCTAssertEqual(fake.transmitted, Data([0xFE, 0, 6, 255, 0, 0, 0, 0, 0]))
    }

    func testSetColorWithSaveFlag() throws {
        let fake = FakeTransport()
        let s = Scanlight(transport: fake)
        defer { s.close() }
        try s.setColor(r: 10, g: 20, b: 30, save: true)
        XCTAssertEqual(fake.transmitted.last, 1)
    }

    func testSetColorRejectsWhiteWithRGB() throws {
        let fake = FakeTransport()
        let s = Scanlight(transport: fake)
        defer { s.close() }
        XCTAssertThrowsError(try s.setColor(r: 100, w: 100)) { err in
            XCTAssertEqual(err as? ScanlightError, .whiteWithRGB)
        }
        // Nothing should have been written
        XCTAssertEqual(fake.transmitted.count, 0)
    }

    func testOffSendsZeros() throws {
        let fake = FakeTransport()
        let s = Scanlight(transport: fake)
        defer { s.close() }
        try s.off()
        XCTAssertEqual(fake.transmitted, Data([0xFE, 0, 6, 0, 0, 0, 0, 0, 0]))
    }

    func testTelemetryUpdatesProperties() {
        let fake = FakeTransport()
        let s = Scanlight(transport: fake)
        defer { s.close() }

        // 32.500 °C as int32 BE millideg
        let mdeg: Int32 = 32500
        let raw = UInt32(bitPattern: mdeg)
        let tempPayload = Data([
            UInt8((raw >> 24) & 0xFF),
            UInt8((raw >> 16) & 0xFF),
            UInt8((raw >> 8) & 0xFF),
            UInt8(raw & 0xFF),
        ])
        let tempPkt = Data([0xFE, ScanlightProtocol.d2hLEDTemp, 4]) + tempPayload

        let mv: Int32 = 5050
        let mvRaw = UInt32(bitPattern: mv)
        let vbusPayload = Data([
            UInt8((mvRaw >> 24) & 0xFF),
            UInt8((mvRaw >> 16) & 0xFF),
            UInt8((mvRaw >> 8) & 0xFF),
            UInt8(mvRaw & 0xFF),
        ])
        let vbusPkt = Data([0xFE, ScanlightProtocol.d2hVBUS, 4]) + vbusPayload

        fake.feed(tempPkt + vbusPkt)
        XCTAssertTrue(waitFor { s.lastTempC != nil && s.lastVBUSmv != nil })
        XCTAssertEqual(s.lastTempC ?? -1, 32.5, accuracy: 1e-9)
        XCTAssertEqual(s.lastVBUSmv, 5050)
    }

    func testFWVersionRequestResponse() throws {
        let fake = FakeTransport()
        let s = Scanlight(transport: fake)
        defer { s.close() }

        let fwId: UInt16 = 0x1234
        let hwId: UInt16 = 0x5678
        let word = UInt32(fwId) | (UInt32(hwId) << 16)
        let payload = Data([
            UInt8((word >> 24) & 0xFF),
            UInt8((word >> 16) & 0xFF),
            UInt8((word >> 8) & 0xFF),
            UInt8(word & 0xFF),
        ])
        let response = Data([0xFE, ScanlightProtocol.d2hFWVersion, 4]) + payload

        // Schedule the response asynchronously to simulate the device.
        DispatchQueue.global().asyncAfter(deadline: .now() + 0.05) {
            fake.feed(response)
        }

        let (fw, hw) = try s.getFWVersion(timeout: 1.0)
        XCTAssertEqual(fw, fwId)
        XCTAssertEqual(hw, hwId)

        // The request packet should be visible in the transport's TX log
        XCTAssertTrue(fake.transmitted.contains(
            Data([0xFE, ScanlightProtocol.h2dGetFWVersion, 0])
        ))
    }

    func testRequestTimesOut() {
        let fake = FakeTransport()
        let s = Scanlight(transport: fake)
        defer { s.close() }
        XCTAssertThrowsError(try s.getFWVersion(timeout: 0.1)) { err in
            if case ScanlightError.requestTimeout = err { return }
            XCTFail("expected requestTimeout, got \(err)")
        }
    }

    func testResyncAfterJunkBytes() throws {
        let fake = FakeTransport()
        let s = Scanlight(transport: fake)
        defer { s.close() }

        let junk = Data([0x01, 0x02, 0x03])
        let fwId: UInt16 = 7, hwId: UInt16 = 8
        let word = UInt32(fwId) | (UInt32(hwId) << 16)
        let payload = Data([
            UInt8((word >> 24) & 0xFF),
            UInt8((word >> 16) & 0xFF),
            UInt8((word >> 8) & 0xFF),
            UInt8(word & 0xFF),
        ])
        let response = Data([0xFE, ScanlightProtocol.d2hFWVersion, 4]) + payload

        DispatchQueue.global().asyncAfter(deadline: .now() + 0.05) {
            fake.feed(junk + response)
        }

        let (fw, hw) = try s.getFWVersion(timeout: 1.0)
        XCTAssertEqual(fw, fwId)
        XCTAssertEqual(hw, hwId)
    }

    /// Per codex audit: a write that throws while a request is pending
    /// must NOT leave a dangling completion slot. If a stale D2H reply
    /// arrives after a failed write, it would otherwise fire an
    /// orphaned closure capturing dead stack state.
    func testRequestClearsPendingSlotOnWriteFailure() throws {
        // Transport that immediately fails every write — simulates a
        // disconnected serial port mid-request.
        final class WriteFailingTransport: ScanlightTransport {
            func write(_ data: Data) throws {
                throw ScanlightError.transportClosed
            }
            func readAvailable() throws -> Data {
                Thread.sleep(forTimeInterval: 0.05)
                return Data()
            }
            func close() {}
        }

        let failing = WriteFailingTransport()
        let s = Scanlight(transport: failing)
        defer { s.close() }

        // First request — should propagate the write error and clear
        // the pending slot internally.
        XCTAssertThrowsError(try s.getFWVersion(timeout: 0.5))

        // Second request — should ALSO throw the write error (not hang
        // on a stale semaphore from the first request, and not race
        // a leftover closure).
        let start = Date()
        XCTAssertThrowsError(try s.getFWVersion(timeout: 0.5))
        let elapsed = Date().timeIntervalSince(start)
        // Should fail fast on the write, well under the 0.5 s timeout —
        // if a stale slot were blocking us we'd see ~0.5 s.
        XCTAssertLessThan(elapsed, 0.2, "second request hung instead of failing fast")
    }

    func testUnknownHeaderIsDropped() {
        let fake = FakeTransport()
        let s = Scanlight(transport: fake)
        defer { s.close() }

        let unknown = Data([0xFE, 99, 2, 0xAA, 0xBB])
        let mdeg: Int32 = 15000
        let raw = UInt32(bitPattern: mdeg)
        let tempPayload = Data([
            UInt8((raw >> 24) & 0xFF),
            UInt8((raw >> 16) & 0xFF),
            UInt8((raw >> 8) & 0xFF),
            UInt8(raw & 0xFF),
        ])
        let tempPkt = Data([0xFE, ScanlightProtocol.d2hLEDTemp, 4]) + tempPayload

        fake.feed(unknown + tempPkt)
        XCTAssertTrue(waitFor { s.lastTempC != nil })
        XCTAssertEqual(s.lastTempC ?? -1, 15.0, accuracy: 1e-9)
    }
}
