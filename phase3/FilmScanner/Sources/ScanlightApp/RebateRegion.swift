import CoreGraphics
import Foundation

struct RebateRegion: Equatable, Codable {
    static let rawWidth = 9568
    static let rawHeight = 6376
    static let defaultSize = 96
    static let defaultWidth = defaultSize
    static let defaultHeight = defaultSize

    let x: Int
    let y: Int
    let w: Int
    let h: Int

    var centerX: Int { x + w / 2 }
    var centerY: Int { y + h / 2 }

    static func centeredAtRawPoint(
        x rawX: Int,
        y rawY: Int,
        width: Int = defaultWidth,
        height: Int = defaultHeight,
        rawWidth: Int = rawWidth,
        rawHeight: Int = rawHeight
    ) -> RebateRegion {
        let clampedWidth = max(1, min(width, rawWidth))
        let clampedHeight = max(1, min(height, rawHeight))
        let left = clamp(rawX - clampedWidth / 2, lower: 0, upper: rawWidth - clampedWidth)
        let top = clamp(rawY - clampedHeight / 2, lower: 0, upper: rawHeight - clampedHeight)
        return RebateRegion(x: left, y: top, w: clampedWidth, h: clampedHeight)
    }

    static func centeredAtNormalized(
        x normalizedX: CGFloat,
        y normalizedY: CGFloat,
        width: Int = defaultWidth,
        height: Int = defaultHeight,
        rawWidth: Int = rawWidth,
        rawHeight: Int = rawHeight
    ) -> RebateRegion {
        let nx = min(1.0, max(0.0, Double(normalizedX)))
        let ny = min(1.0, max(0.0, Double(normalizedY)))
        let rawX = Int((nx * Double(rawWidth)).rounded())
        let rawY = Int((ny * Double(rawHeight)).rounded())
        return centeredAtRawPoint(
            x: rawX,
            y: rawY,
            width: width,
            height: height,
            rawWidth: rawWidth,
            rawHeight: rawHeight
        )
    }

    private static func clamp(_ value: Int, lower: Int, upper: Int) -> Int {
        min(upper, max(lower, value))
    }
}
