// SettingsStore — @MainActor ObservableObject that wraps ScanSettings (the
// Codable struct from OrchestratorClient) and exposes validation and an
// injectable folder-picker closure.
//
// Pattern mirrors ScanlightViewModel exactly: @MainActor final class with
// @Published properties. Does NOT use the Observable macro.
//
// Validation mirrors CaptureSettings.__post_init__ from
// phase2/triplet-capture/triplet_capture/orchestrator.py exactly:
//   - roll_name: non-empty, no whitespace
//   - output_folder: non-empty (implicitly required, not in __post_init__ but
//     required for the CLI spawn to make sense)
//   - ied_inbox: required when trigger_mode == "hw"
//
// Serial-port ownership is the Phase 07 invariant. These views do not open
// the serial port.

import AppKit
import Foundation

@MainActor
final class SettingsStore: ObservableObject {

    // MARK: - Published state

    /// The full ScanSettings value; views bind directly via $store.settings.field.
    @Published var settings: ScanSettings = ScanSettings(
        rollName: "Roll001",
        outputFolder: "",
        triggerMode: "hw",
        iedInbox: nil,
        streamComposite: true,
        ffcCalibration: nil,
        cameraModel: "Sony ILCE-7CR",
        compositeFormat: "dng",
        levelR: 200,
        levelG: 200,
        levelB: 200,
        settleMs: 50
    )

    /// Field-keyed validation errors, populated by validate(). Empty when valid.
    @Published var validationErrors: [String: String] = [:]

    /// The directory written by the last successful calibration capture.
    /// CalibrationView sets this on success; the "Use last calibration" button
    /// in ScanSettingsView reads it to populate ffcCalibration.
    @Published var lastCalibrationDir: String? = nil

    // MARK: - Injectable folder picker

    /// Presents a folder-picker dialog and returns the selected URL (or nil if
    /// the user cancelled). Default implementation uses NSOpenPanel. Override
    /// in tests with a stub to avoid presenting a real panel in headless runs.
    ///
    /// Usage:
    ///   folderPicker("Select output folder") { url in
    ///       if let url { store.settings.outputFolder = url.path }
    ///   }
    var folderPicker: (String, @escaping (URL?) -> Void) -> Void = { prompt, completion in
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        panel.prompt = prompt
        panel.begin { response in
            // Use DispatchQueue.main.async rather than @MainActor annotation on
            // the closure — avoids Swift 6 Sendable warnings (Pitfall 3 in
            // 06-RESEARCH.md). NSOpenPanel.begin calls back on the main thread
            // already; wrapping in async is safe and warning-free.
            DispatchQueue.main.async { completion(response == .OK ? panel.url : nil) }
        }
    }

    // MARK: - Validation

    /// Validates settings against the CaptureSettings.__post_init__ rules and
    /// updates validationErrors. Returns the error dict (same value stored on
    /// self.validationErrors).
    ///
    /// Rules mirrored from Python (phase2/triplet-capture/triplet_capture/orchestrator.py):
    ///   - roll_name non-empty and no whitespace (any(c.isspace() for c in roll_name))
    ///   - output_folder required (enforced here, coerced to Path in Python)
    ///   - ied_inbox required when trigger_mode == "hw"
    ///
    /// // shutter_pulse_ms not in ScanSettings — Phase 05 decision; validated
    /// // server-side. settle_ms has no __post_init__ rule; step:10 on the
    /// // Stepper is the only UI constraint.
    @discardableResult
    func validate() -> [String: String] {
        var errors: [String: String] = [:]

        // Roll name: non-empty, no whitespace
        // Python: any(c.isspace() for c in roll_name) — matches \n \r \v \f in addition
        // to space and tab. Swift mirror must use .whitespacesAndNewlines (not .whitespaces)
        // so a roll name with a trailing newline (e.g. pasted from clipboard) is rejected
        // here before it reaches the Python orchestrator. (WR-03)
        if settings.rollName.isEmpty {
            errors["rollName"] = "Roll name is required."
        } else if settings.rollName.rangeOfCharacter(from: .whitespacesAndNewlines) != nil {
            errors["rollName"] = "Roll name must not contain spaces."
        }

        // Output folder required
        if settings.outputFolder.isEmpty {
            errors["outputFolder"] = "Output folder is required."
        }

        // IED inbox required for HW trigger
        if settings.triggerMode == "hw" && (settings.iedInbox == nil || settings.iedInbox!.isEmpty) {
            errors["iedInbox"] = "IED inbox folder is required for HW trigger mode."
        }

        // levelR/G/B are clamped 0–255 by the Slider — no range check needed for UI.
        // No settleMs % 10 rule — settle_ms has no __post_init__ validation rule.

        validationErrors = errors
        return errors
    }
}
