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
//   - ied_inbox: required when trigger_mode == "hw" or "manual"
//
// Serial-port ownership is the Phase 07 invariant. These views do not open
// the serial port.

import AppKit
import Foundation

@MainActor
final class SettingsStore: ObservableObject {

    private static let settingsStorageKey = "ScanlightApp.Settings.v1"
    private static let lastCalibrationDirStorageKey = "ScanlightApp.LastCalibrationDir.v1"
    private static let stockCalibrationProfilesStorageKey = "ScanlightApp.StockCalibrationProfiles.v1"

    static let defaultSettings = ScanSettings(
        rollName: "Roll001",
        outputFolder: "",
        triggerMode: "manual",
        iedInbox: nil,
        sonyIpAddress: nil,
        sonyMacAddress: nil,
        sonyUser: nil,
        sonyPassword: nil,
        sonyCapturePath: nil,
        streamComposite: true,
        ffcCalibration: nil,
        cameraModel: "Sony ILCE-7CR",
        compositeFormat: "dng",
        calibrationTargetFraction: 0.80,
        levelR: 200,
        levelG: 200,
        levelB: 200,
        settleMs: 50,
        shutterR: nil,
        shutterG: nil,
        shutterB: nil
    )

    // MARK: - Published state

    /// The full ScanSettings value; views bind directly via $store.settings.field.
    @Published var settings: ScanSettings {
        didSet { persistSettings() }
    }

    /// Field-keyed validation errors, populated by validate(). Empty when valid.
    @Published var validationErrors: [String: String] = [:]

    /// The directory written by the last successful calibration capture.
    /// CalibrationView sets this on success; the "Use last calibration" button
    /// in ScanSettingsView reads it to populate ffcCalibration.
    @Published var lastCalibrationDir: String? {
        didSet { persistLastCalibrationDir() }
    }

    /// Saved per-stock exposure profiles. These store RGB LED levels, optional
    /// Sony shutter speeds, black levels, gains, clip fractions, and the rebate
    /// region from the exposure calibration result.
    @Published var stockCalibrationProfiles: [StockCalibrationProfile] {
        didSet { persistStockCalibrationProfiles() }
    }

    private let userDefaults: UserDefaults
    private let persistenceEnabled: Bool

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

    // MARK: - Init

    init(userDefaults: UserDefaults = .standard, persistenceEnabled: Bool = true) {
        self.userDefaults = userDefaults
        self.persistenceEnabled = persistenceEnabled
        self.settings = persistenceEnabled
            ? Self.loadSettings(from: userDefaults)
            : Self.defaultSettings
        self.lastCalibrationDir = persistenceEnabled
            ? userDefaults.string(forKey: Self.lastCalibrationDirStorageKey)
            : nil
        self.stockCalibrationProfiles = persistenceEnabled
            ? Self.loadStockCalibrationProfiles(from: userDefaults)
            : []
    }

    // MARK: - Validation

    /// Validates settings against the CaptureSettings.__post_init__ rules and
    /// updates validationErrors. Returns the error dict (same value stored on
    /// self.validationErrors).
    ///
    /// Rules mirrored from Python (phase2/triplet-capture/triplet_capture/orchestrator.py):
    ///   - roll_name non-empty and no whitespace (any(c.isspace() for c in roll_name))
    ///   - output_folder required (enforced here, coerced to Path in Python)
    ///   - ied_inbox required when trigger_mode == "hw" or "manual"
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

        // IED inbox required for every IED-backed trigger path.
        if ["hw", "manual"].contains(settings.triggerMode)
            && (settings.iedInbox == nil || settings.iedInbox!.isEmpty) {
            errors["iedInbox"] = "IED inbox folder is required for this trigger mode."
        }

        if settings.triggerMode == "sdk" {
            if settings.sonyIpAddress?.isEmpty ?? true {
                errors["sonyIpAddress"] = "Sony camera IP address is required for SDK mode."
            }
            if settings.sonyUser?.isEmpty ?? true {
                errors["sonyUser"] = "Sony Access Auth user is required for SDK mode."
            }
            if settings.sonyPassword?.isEmpty ?? true {
                errors["sonyPassword"] = "Sony Access Auth password is required for SDK mode."
            }
        }

        // levelR/G/B are written by calibration and the backend validates them.
        // No settleMs % 10 rule — settle_ms has no __post_init__ validation rule.

        validationErrors = errors
        return errors
    }

    // MARK: - Persistence

    private static func loadSettings(from userDefaults: UserDefaults) -> ScanSettings {
        guard let data = userDefaults.data(forKey: settingsStorageKey) else {
            return defaultSettings
        }

        do {
            let decoded = try JSONDecoder().decode(ScanSettings.self, from: data)
            return decoded
        } catch {
            userDefaults.removeObject(forKey: settingsStorageKey)
            return defaultSettings
        }
    }

    private func persistSettings() {
        guard persistenceEnabled else { return }
        do {
            let data = try JSONEncoder().encode(settings)
            userDefaults.set(data, forKey: Self.settingsStorageKey)
        } catch {
            userDefaults.removeObject(forKey: Self.settingsStorageKey)
        }
    }

    private func persistLastCalibrationDir() {
        guard persistenceEnabled else { return }
        if let lastCalibrationDir, !lastCalibrationDir.isEmpty {
            userDefaults.set(lastCalibrationDir, forKey: Self.lastCalibrationDirStorageKey)
        } else {
            userDefaults.removeObject(forKey: Self.lastCalibrationDirStorageKey)
        }
    }

    // MARK: - Stock calibration profiles

    @discardableResult
    func saveStockCalibrationProfile(
        stockName rawStockName: String,
        exposureResult: ExposureCalibrationResult
    ) -> StockCalibrationProfile? {
        let stockName = StockCalibrationProfile.normalizedStockName(rawStockName)
        guard !stockName.isEmpty else { return nil }

        let now = Date()
        let existing = stockCalibrationProfiles.first { $0.normalizedKey == stockName.lowercased() }
        let profile = StockCalibrationProfile(
            id: existing?.id ?? UUID(),
            stockName: stockName,
            createdAt: existing?.createdAt ?? now,
            updatedAt: now,
            triggerMode: settings.triggerMode,
            cameraModel: settings.cameraModel,
            exposureResult: exposureResult
        )

        if let idx = stockCalibrationProfiles.firstIndex(where: { $0.normalizedKey == profile.normalizedKey }) {
            stockCalibrationProfiles[idx] = profile
        } else {
            stockCalibrationProfiles.append(profile)
            stockCalibrationProfiles.sort { $0.stockName.localizedCaseInsensitiveCompare($1.stockName) == .orderedAscending }
        }
        return profile
    }

    func stockCalibrationProfile(id: UUID?) -> StockCalibrationProfile? {
        guard let id else { return nil }
        return stockCalibrationProfiles.first { $0.id == id }
    }

    func applyStockCalibrationProfile(_ profile: StockCalibrationProfile) {
        settings.levelR = profile.exposureResult.r.ledLevel
        settings.levelG = profile.exposureResult.g.ledLevel
        settings.levelB = profile.exposureResult.b.ledLevel
        settings.shutterR = profile.exposureResult.r.shutterSpeed
        settings.shutterG = profile.exposureResult.g.shutterSpeed
        settings.shutterB = profile.exposureResult.b.shutterSpeed
    }

    @discardableResult
    func updateStockCalibrationProfile(
        id: UUID,
        stockName rawStockName: String,
        ledR: Int,
        ledG: Int,
        ledB: Int,
        shutterR: String?,
        shutterG: String?,
        shutterB: String?
    ) throws -> StockCalibrationProfile {
        guard let idx = stockCalibrationProfiles.firstIndex(where: { $0.id == id }) else {
            throw StockProfileEditError.notFound
        }

        let stockName = StockCalibrationProfile.normalizedStockName(rawStockName)
        guard !stockName.isEmpty else {
            throw StockProfileEditError.emptyName
        }

        let normalizedKey = stockName.lowercased()
        if stockCalibrationProfiles.contains(where: { $0.id != id && $0.normalizedKey == normalizedKey }) {
            throw StockProfileEditError.duplicateName(stockName)
        }

        let existing = stockCalibrationProfiles[idx]
        let updated = StockCalibrationProfile(
            id: existing.id,
            stockName: stockName,
            createdAt: existing.createdAt,
            updatedAt: Date(),
            triggerMode: existing.triggerMode,
            cameraModel: existing.cameraModel,
            exposureResult: existing.exposureResult.updatingExposureRecipe(
                ledR: Self.clampLEDLevel(ledR),
                ledG: Self.clampLEDLevel(ledG),
                ledB: Self.clampLEDLevel(ledB),
                shutterR: Self.normalizedOptionalString(shutterR),
                shutterG: Self.normalizedOptionalString(shutterG),
                shutterB: Self.normalizedOptionalString(shutterB)
            ),
            schemaVersion: existing.schemaVersion
        )

        stockCalibrationProfiles[idx] = updated
        stockCalibrationProfiles.sort { $0.stockName.localizedCaseInsensitiveCompare($1.stockName) == .orderedAscending }
        return updated
    }

    @discardableResult
    func deleteStockCalibrationProfile(id: UUID) -> Bool {
        let originalCount = stockCalibrationProfiles.count
        stockCalibrationProfiles.removeAll { $0.id == id }
        return stockCalibrationProfiles.count != originalCount
    }

    private static func loadStockCalibrationProfiles(from userDefaults: UserDefaults) -> [StockCalibrationProfile] {
        guard let data = userDefaults.data(forKey: stockCalibrationProfilesStorageKey) else {
            return []
        }
        do {
            return try JSONDecoder().decode([StockCalibrationProfile].self, from: data)
        } catch {
            userDefaults.removeObject(forKey: stockCalibrationProfilesStorageKey)
            return []
        }
    }

    private func persistStockCalibrationProfiles() {
        guard persistenceEnabled else { return }
        do {
            let data = try JSONEncoder().encode(stockCalibrationProfiles)
            userDefaults.set(data, forKey: Self.stockCalibrationProfilesStorageKey)
        } catch {
            userDefaults.removeObject(forKey: Self.stockCalibrationProfilesStorageKey)
        }
    }

    private static func clampLEDLevel(_ value: Int) -> Int {
        min(255, max(0, value))
    }

    private static func normalizedOptionalString(_ value: String?) -> String? {
        let normalized = value?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return normalized.isEmpty ? nil : normalized
    }
}
