// CalibrationResult — Codable structs that mirror the JSON output of
// `inspect-calibration.py --json` (added in Phase 06).
//
// JSON shape produced by the Python script:
//   {
//     "channels": {
//       "R": { "falloff_pct": 5.2, "uniformity_pct": 1.0, "verdict": "clean" },
//       "G": { ... },
//       "B": { ... }
//     },
//     "overall": "clean"
//   }
//
// Decoding note: CodingKeys provides explicit snake_case → camelCase mapping so
// both JSONDecoder() (default) and JSONDecoder with
// .keyDecodingStrategy = .convertFromSnakeCase work correctly.
//
// Pattern mirrors OrchestratorClient.swift Codable struct style exactly.

import Foundation

/// Per-channel calibration statistics from inspect-calibration.py --json.
struct ChannelCalResult: Codable {
    /// Falloff from centre to edge as a percentage of peak brightness.
    let falloffPct: Double

    /// Pixel-level non-uniformity expressed as a percentage.
    let uniformityPct: Double

    /// Per-channel verdict: "clean" | "acceptable" | "fail"
    let verdict: String

    enum CodingKeys: String, CodingKey {
        case falloffPct    = "falloff_pct"
        case uniformityPct = "uniformity_pct"
        case verdict
    }
}

/// Top-level calibration result from inspect-calibration.py --json.
struct CalibrationResult: Codable {
    /// Per-channel results keyed by "R", "G", "B".
    let channels: [String: ChannelCalResult]

    /// Aggregate verdict: "clean" | "acceptable" | "fail"
    /// Derived as: fail if any channel is fail; acceptable if any is acceptable; else clean.
    let overall: String

    // MARK: - Convenience accessors

    /// Result for the Red channel. Nil if the JSON did not include "R".
    var channelR: ChannelCalResult? { channels["R"] }

    /// Result for the Green channel. Nil if the JSON did not include "G".
    var channelG: ChannelCalResult? { channels["G"] }

    /// Result for the Blue channel. Nil if the JSON did not include "B".
    var channelB: ChannelCalResult? { channels["B"] }
}
