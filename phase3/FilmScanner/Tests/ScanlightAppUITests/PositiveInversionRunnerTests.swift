import AppKit
import XCTest
@testable import ScanlightApp

final class PositiveInversionRunnerTests: XCTestCase {
    func testTripletRunnerUsesPythonWithTiffDependencies() async throws {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("scanlight-positive-runner-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }

        let red = root.appendingPathComponent("red.tif")
        let green = root.appendingPathComponent("green.tif")
        let blue = root.appendingPathComponent("blue.tif")
        try writeRGBTIFF(red, rgb: (220, 28, 34))
        try writeRGBTIFF(green, rgb: (31, 218, 42))
        try writeRGBTIFF(blue, rgb: (35, 44, 221))

        let out = root.appendingPathComponent("out")
        let preview = try await TripletPositiveRunner.preview(
            inputFiles: [green, blue, red],
            outputFolder: out
        )
        XCTAssertTrue(FileManager.default.fileExists(atPath: preview.previewPath))
        XCTAssertEqual(URL(fileURLWithPath: preview.previewPath).pathExtension, "png")
        XCTAssertEqual(preview.fullWidth, 96)
        XCTAssertEqual(preview.fullHeight, 80)

        let result = try await TripletPositiveRunner.run(
            inputFiles: [green, blue, red],
            outputFolder: out,
            baseRegion: TripletBaseRegionInput(x: 0, y: 0, w: 32, h: 32)
        )

        XCTAssertTrue(FileManager.default.fileExists(atPath: result.positivePath))
        XCTAssertTrue(FileManager.default.fileExists(atPath: result.compositePath))
        XCTAssertTrue(FileManager.default.fileExists(atPath: result.reportPath))
        XCTAssertEqual(
            result.mapping.sorted { $0.channelIndex < $1.channelIndex }.map(\.channel),
            ["R", "G", "B"]
        )
    }

    private func writeRGBTIFF(
        _ url: URL,
        rgb: (UInt8, UInt8, UInt8),
        width: Int = 96,
        height: Int = 80
    ) throws {
        guard let rep = NSBitmapImageRep(
            bitmapDataPlanes: nil,
            pixelsWide: width,
            pixelsHigh: height,
            bitsPerSample: 8,
            samplesPerPixel: 3,
            hasAlpha: false,
            isPlanar: false,
            colorSpaceName: .deviceRGB,
            bytesPerRow: width * 3,
            bitsPerPixel: 24
        ), let data = rep.bitmapData else {
            XCTFail("could not allocate TIFF test image")
            return
        }

        for y in 0..<height {
            for x in 0..<width {
                let idx = y * rep.bytesPerRow + x * 3
                data[idx] = rgb.0
                data[idx + 1] = rgb.1
                data[idx + 2] = rgb.2
            }
        }

        guard let tiff = rep.representation(using: .tiff, properties: [:]) else {
            XCTFail("could not encode TIFF test image")
            return
        }
        try tiff.write(to: url)
    }
}
