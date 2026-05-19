import XCTest
@testable import ScanlightSwift

final class ProtocolTests: XCTestCase {

    func testEncodePacketZeroData() throws {
        let pkt = try ScanlightProtocol.encodePacket(header: ScanlightProtocol.h2dGetFWVersion)
        XCTAssertEqual(pkt, Data([0xFE, 2, 0]))
    }

    func testEncodePacketWithData() throws {
        let pkt = try ScanlightProtocol.encodePacket(
            header: ScanlightProtocol.h2dSetColor,
            data: Data([10, 20, 30, 0, 0, 0])
        )
        XCTAssertEqual(pkt, Data([0xFE, 0, 6, 10, 20, 30, 0, 0, 0]))
    }

    func testEncodeSetColorRedFull() throws {
        let pkt = try ScanlightProtocol.encodeSetColor(r: 255, g: 0, b: 0)
        XCTAssertEqual(pkt, Data([0xFE, 0, 6, 255, 0, 0, 0, 0, 0]))
    }

    func testEncodeSetColorSaveFlag() throws {
        let pkt = try ScanlightProtocol.encodeSetColor(r: 10, g: 20, b: 30, save: true)
        XCTAssertEqual(pkt.last, 1)
        // IR byte is always 0 (index 7 in 9-byte packet)
        XCTAssertEqual(pkt[7], 0)
    }

    func testEncodeSetColorRejectsOutOfRange() {
        XCTAssertThrowsError(try ScanlightProtocol.encodeSetColor(r: 256, g: 0, b: 0))
        XCTAssertThrowsError(try ScanlightProtocol.encodeSetColor(r: -1, g: 0, b: 0))
    }

    // --- decoder tests: big-endian on the wire ---

    func testDecodeLEDTempPositive() throws {
        // 35.500 °C = 35500 mdeg, big-endian
        let mdeg: Int32 = 35500
        let data = Data([
            UInt8((UInt32(bitPattern: mdeg) >> 24) & 0xFF),
            UInt8((UInt32(bitPattern: mdeg) >> 16) & 0xFF),
            UInt8((UInt32(bitPattern: mdeg) >> 8) & 0xFF),
            UInt8(UInt32(bitPattern: mdeg) & 0xFF),
        ])
        XCTAssertEqual(try ScanlightProtocol.decodeLEDTemp(data), 35.5, accuracy: 1e-9)
    }

    func testDecodeLEDTempNegative() throws {
        let mdeg: Int32 = -12345
        let raw = UInt32(bitPattern: mdeg)
        let data = Data([
            UInt8((raw >> 24) & 0xFF),
            UInt8((raw >> 16) & 0xFF),
            UInt8((raw >> 8) & 0xFF),
            UInt8(raw & 0xFF),
        ])
        XCTAssertEqual(try ScanlightProtocol.decodeLEDTemp(data), -12.345, accuracy: 1e-9)
    }

    func testDecodeVBUS() throws {
        let mv: Int32 = 5040
        let raw = UInt32(bitPattern: mv)
        let data = Data([
            UInt8((raw >> 24) & 0xFF),
            UInt8((raw >> 16) & 0xFF),
            UInt8((raw >> 8) & 0xFF),
            UInt8(raw & 0xFF),
        ])
        XCTAssertEqual(try ScanlightProtocol.decodeVBUS(data), 5040)
    }

    func testDecodeFWVersionWireFormat() throws {
        // Firmware sends FW + (HW << 16) as big-endian u32.
        // For scanlight v4 firmware 1.1.0: FW=1, HW=1 → bytes 00 01 00 01
        let data = Data([0x00, 0x01, 0x00, 0x01])
        let (fw, hw) = try ScanlightProtocol.decodeFWVersion(data)
        XCTAssertEqual(fw, 1)
        XCTAssertEqual(hw, 1)
    }

    func testDecodeFWVersionDistinguishesFields() throws {
        // fw=0x0042, hw=0x00AB → word = 0x00AB0042 → bytes 00 AB 00 42
        let data = Data([0x00, 0xAB, 0x00, 0x42])
        let (fw, hw) = try ScanlightProtocol.decodeFWVersion(data)
        XCTAssertEqual(fw, 0x0042)
        XCTAssertEqual(hw, 0x00AB)
    }

    func testDecodeDefaultRGB() throws {
        let data = Data([100, 150, 200])
        let (r, g, b) = try ScanlightProtocol.decodeDefaultRGB(data)
        XCTAssertEqual(r, 100); XCTAssertEqual(g, 150); XCTAssertEqual(b, 200)
    }

    func testDecodeRejectsShortPayload() {
        XCTAssertThrowsError(try ScanlightProtocol.decodeLEDTemp(Data([0, 0])))
        XCTAssertThrowsError(try ScanlightProtocol.decodeFWVersion(Data([0, 0])))
        XCTAssertThrowsError(try ScanlightProtocol.decodeDefaultRGB(Data([0, 0])))
    }
}
