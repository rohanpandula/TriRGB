import Foundation

enum StockProfileEditError: LocalizedError, Equatable {
    case notFound
    case emptyName
    case duplicateName(String)

    var errorDescription: String? {
        switch self {
        case .notFound:
            return "That film stock profile no longer exists."
        case .emptyName:
            return "Enter a film stock name before saving."
        case .duplicateName(let name):
            return "A film stock named \(name) already exists. Pick a unique name or delete the old profile first."
        }
    }
}

/// Saved per-stock RGB exposure recipe.
///
/// This stores the exposure calibration result, not flat-field data. A stock
/// profile answers "what R/G/B LED levels and shutter speeds put this film base
/// at the target exposure?" FFC still belongs to the current physical rig setup
/// and should be captured after applying or solving a stock profile.
struct StockCalibrationProfile: Codable, Identifiable {
    var id: UUID
    var stockName: String
    var createdAt: Date
    var updatedAt: Date
    var triggerMode: String
    var cameraModel: String?
    var exposureResult: ExposureCalibrationResult
    var schemaVersion: Int

    init(
        id: UUID = UUID(),
        stockName: String,
        createdAt: Date = Date(),
        updatedAt: Date = Date(),
        triggerMode: String,
        cameraModel: String?,
        exposureResult: ExposureCalibrationResult,
        schemaVersion: Int = 1
    ) {
        self.id = id
        self.stockName = stockName
        self.createdAt = createdAt
        self.updatedAt = updatedAt
        self.triggerMode = triggerMode
        self.cameraModel = cameraModel
        self.exposureResult = exposureResult
        self.schemaVersion = schemaVersion
    }

    var displayName: String {
        let shutterSummary = [
            exposureResult.r.shutterSpeed,
            exposureResult.g.shutterSpeed,
            exposureResult.b.shutterSpeed,
        ]
        .map { ($0?.isEmpty == false) ? $0! : "current" }
        .joined(separator: "/")

        return "\(stockName) - RGB \(exposureResult.r.ledLevel)/\(exposureResult.g.ledLevel)/\(exposureResult.b.ledLevel), \(shutterSummary)"
    }

    /// Compact JSON passed to the Python compositor so scans can write a
    /// rendered positive alongside the archival linear negative.
    var positiveProfileJSON: String? {
        let base = exposureResult.baseRegion
        let payload: [String: Any] = [
            "stock_name": stockName,
            "base_region": [
                "x": base.x,
                "y": base.y,
                "w": base.w,
                "h": base.h,
                "base_rgb": base.baseRgb,
                "uniformity_cv": base.uniformityCv,
                "source": base.source,
                "schema_version": base.schemaVersion,
            ],
            "schema_version": 1,
        ]
        guard JSONSerialization.isValidJSONObject(payload),
              let data = try? JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys]) else {
            return nil
        }
        return String(data: data, encoding: .utf8)
    }

    static func normalizedStockName(_ value: String) -> String {
        value
            .split(whereSeparator: { $0.isWhitespace })
            .joined(separator: " ")
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }

    var normalizedKey: String {
        Self.normalizedStockName(stockName).lowercased()
    }
}

extension WizardChannelCalibration {
    func updating(ledLevel: Int, shutterSpeed: String?) -> WizardChannelCalibration {
        WizardChannelCalibration(
            channel: channel,
            ledLevel: ledLevel,
            blackLevel: blackLevel,
            gain: gain,
            clipFraction: clipFraction,
            shutterSpeed: shutterSpeed,
            p99: p99,
            target: target,
            exposureStatus: exposureStatus,
            schemaVersion: schemaVersion
        )
    }
}

extension ExposureCalibrationResult {
    func updatingExposureRecipe(
        ledR: Int,
        ledG: Int,
        ledB: Int,
        shutterR: String?,
        shutterG: String?,
        shutterB: String?
    ) -> ExposureCalibrationResult {
        ExposureCalibrationResult(
            r: r.updating(ledLevel: ledR, shutterSpeed: shutterR),
            g: g.updating(ledLevel: ledG, shutterSpeed: shutterG),
            b: b.updating(ledLevel: ledB, shutterSpeed: shutterB),
            baseRegion: baseRegion,
            ffcCalDir: ffcCalDir,
            schemaVersion: schemaVersion
        )
    }
}
