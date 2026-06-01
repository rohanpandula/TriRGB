// CalibrationWizardCodable — Codable structs for the Phase-14 calibration wizard HTTP routes.
//
// These mirror the Python c41_core contract JSON shapes:
//   POST /api/calibrate/exposure  → ExposureCalibrationResult
//   POST /api/calibrate/ffc       → FlatFieldResponse
//   POST /api/calibrate/checks    → [WizardCheckResult]
//
// Name collision note: CalibrationResult (in CalibrationResult.swift) mirrors
// inspect-calibration.py --json and is REUSED here as FlatFieldResponse.inspection.
// The Phase-12 exposure result is ExposureCalibrationResult (NOT CalibrationResult).
//
// All decoded with JSONDecoder().keyDecodingStrategy = .convertFromSnakeCase so
// `led_level` → `ledLevel`, `base_region` → `baseRegion`, etc.

import Foundation

// MARK: - Nested types for ExposureCalibrationResult

/// Per-channel LED/gain/black-level calibration record — mirrors ChannelCalibration.to_json().
struct WizardChannelCalibration: Codable {
    let channel: String
    let ledLevel: Int
    let blackLevel: Double
    let gain: Double
    let clipFraction: Double
    let shutterSpeed: String?
    let p99: Double?
    let target: Double?
    let exposureStatus: String?
    let schemaVersion: Int
}

/// Pixel bounding box and per-channel base — mirrors BaseRegionDescriptor.to_json().
struct WizardBaseRegion: Codable {
    let x: Int
    let y: Int
    let w: Int
    let h: Int
    let baseRgb: [Double]
    let uniformityCv: Double
    let source: String
    let schemaVersion: Int
}

// MARK: - ExposureCalibrationResult

/// Result of POST /api/calibrate/exposure — mirrors CalibrationResult.to_json().
///
/// Named ExposureCalibrationResult (NOT CalibrationResult) to avoid collision
/// with CalibrationResult in CalibrationResult.swift which mirrors
/// inspect-calibration.py --json. Both names exist in the Swift target.
struct ExposureCalibrationResult: Codable {
    let r: WizardChannelCalibration
    let g: WizardChannelCalibration
    let b: WizardChannelCalibration
    let baseRegion: WizardBaseRegion
    let ffcCalDir: String
    let schemaVersion: Int
}

// MARK: - FlatFieldResponse

/// Flat-field metadata — mirrors FlatFieldResult.to_json().
///
/// Explicit CodingKeys because this struct is decoded from a snake_case JSON
/// response WITHOUT convertFromSnakeCase (see FlatFieldResponse.inspection note).
struct WizardFlatFieldResult: Codable {
    let flatDataPath: String
    let nFramesAveraged: Int
    let warmupS: Double
    let blackLevelR: Double
    let blackLevelG: Double
    let blackLevelB: Double
    let workingBrightness: Int
    let uniformityImprovement: Double
    let schemaVersion: Int

    enum CodingKeys: String, CodingKey {
        case flatDataPath        = "flat_data_path"
        case nFramesAveraged     = "n_frames_averaged"
        case warmupS             = "warmup_s"
        case blackLevelR         = "black_level_r"
        case blackLevelG         = "black_level_g"
        case blackLevelB         = "black_level_b"
        case workingBrightness   = "working_brightness"
        case uniformityImprovement = "uniformity_improvement"
        case schemaVersion       = "schema_version"
    }
}

/// Combined result of POST /api/calibrate/ffc.
///
/// `inspection` reuses CalibrationResult (from CalibrationResult.swift) because
/// the FFC inspection shape is identical to inspect-calibration.py --json:
///   {"channels": {"R": {falloff_pct, uniformity_pct, verdict}, ...}, "overall": ...}
///
/// Decoded with a plain JSONDecoder (no .convertFromSnakeCase) so that
/// CalibrationResult's explicit CodingKeys (`case falloffPct = "falloff_pct"`) work
/// correctly — .convertFromSnakeCase transforms JSON keys BEFORE matching CodingKeys,
/// which breaks explicit raw-value CodingKeys that reference the original snake_case name.
struct FlatFieldResponse: Codable {
    let flatField: WizardFlatFieldResult
    let inspection: CalibrationResult

    enum CodingKeys: String, CodingKey {
        case flatField  = "flat_field"
        case inspection
    }
}

// MARK: - WizardCheckResult

/// One check result — mirrors CheckResult.to_json().
struct WizardCheckResult: Codable {
    let name: String
    let passed: Bool
    let deltas: [String: Double]
    let schemaVersion: Int
}
