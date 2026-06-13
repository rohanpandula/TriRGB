// CalibrationWizardViewModel — @MainActor ObservableObject driving the 4-step
// guided calibration wizard (Phase 14 R-29).
//
// Four steps:
//   1 — Rig Check:  fetchState() → OrchestratorState (existing route)
//   2 — Exposure:   calibrateExposure() → ExposureCalibrationResult
//   3 — Flat Field: calibrateFFC(exposureResult:) → FlatFieldResponse
//   4 — Results:    calibrateChecks() → [WizardCheckResult]
//
// Design:
//   - Inject OrchestratorClient so tests can stub the HTTP transport.
//   - isRunning guards the nav footer; a per-step error in lastError[step]
//     surfaces via a Banner without blocking forward navigation (CONTEXT decision 3).
//   - FAIL on any check result does NOT prevent navigation to the next step.
//   - rebateRegion is set by the live-view picker in Step 2; nil = not selected.

import Foundation

@MainActor
final class CalibrationWizardViewModel: ObservableObject {

    // MARK: - Injected dependencies

    var orchestratorClient: OrchestratorClient

    // MARK: - Published state

    /// Current active wizard step, 1–4.
    @Published var currentStep: Int = 1

    /// True while any async backend call is in progress.
    @Published var isRunning: Bool = false

    /// Per-step error messages (key = step number 1–4).
    /// Nil entry = no error on that step.
    @Published var lastError: [Int: String] = [:]

    /// Step 1 result — OrchestratorState from GET /api/state.
    @Published var rigCheckResult: OrchestratorState? = nil

    /// Live status text while Step 1 starts the backend and probes state.
    @Published var rigCheckProgressText: String? = nil

    /// Step 2 result — decoded ExposureCalibrationResult.
    @Published var exposureResult: ExposureCalibrationResult? = nil

    /// Live status text while Step 2 waits on camera captures and RAW downloads.
    @Published var exposureProgressText: String? = nil

    /// Recent backend calibration attempts, decoded from scan_log.jsonl via
    /// GET /api/calibrate/progress while exposure calibration is running.
    @Published var exposureLogEntries: [CalibrationProgressLogEntry] = []

    /// Monotonic token used by the Exposure step to auto-refresh the Sony
    /// live-view frame after Start & Check Rig successfully starts the backend.
    @Published var previewRefreshGeneration: Int = 0

    /// Step 3 result — decoded FlatFieldResponse.
    @Published var ffcResult: FlatFieldResponse? = nil

    /// True when the operator intentionally skips flat-field correction for
    /// the current calibration flow.
    @Published var ffcSkipped: Bool = false

    /// Step 4 result — array of decoded WizardCheckResult.
    @Published var checkResults: [WizardCheckResult]? = nil

    /// Required real RAW rebate region from the live-view picker before exposure calibration.
    /// Nil = no selected measurement crop.
    @Published var rebateRegion: RebateRegion? = nil

    /// Backward-compatible point view used by existing tests and labels.
    /// The stored value is always the real RAW top-left of `rebateRegion`.
    var rebateCoord: (col: Int, row: Int)? {
        get {
            guard let rebateRegion else { return nil }
            return (col: rebateRegion.x, row: rebateRegion.y)
        }
        set {
            guard let newValue else {
                rebateRegion = nil
                return
            }
            rebateRegion = RebateRegion.centeredAtRawPoint(
                x: newValue.col,
                y: newValue.row
            )
        }
    }

    /// Film stock name used when saving the current RGB exposure solve.
    @Published var calibrationStockName: String = ""

    /// Selected saved stock profile for loading repeat-stock RGB exposure solves.
    @Published var selectedStockProfileID: UUID? = nil

    /// Non-blocking copy for stock profile save/apply actions.
    @Published var stockProfileMessage: String? = nil

    /// Exposure calibration can take minutes after it starts shooting, but the
    /// backend should write a progress event almost immediately. If it stays
    /// idle, release the UI instead of leaving the wizard locked.
    internal var exposureNoStartTimeoutSeconds: TimeInterval = 30
    internal var exposureProgressPollIntervalNanoseconds: UInt64 = 1_000_000_000

    // MARK: - Init

    init(orchestratorClient: OrchestratorClient) {
        self.orchestratorClient = orchestratorClient
    }

    // MARK: - Step triggers

    /// Ensure the shared `triplet-capture` child process is running for
    /// calibration, then run the Step-1 rig probe. This gives the Calibrate tab
    /// the same managed lifecycle as the Scan tab instead of requiring an
    /// already-running Python server.
    @discardableResult
    func prepareForCalibration(
        store: SettingsStore,
        coordinator: ScanCoordinator,
        cameraConnection: SonyCameraConnection
    ) async -> Bool {
        guard !isRunning else { return false }
        isRunning = true
        rigCheckProgressText = "Validating settings."
        lastError[1] = nil
        defer { isRunning = false }

        let errors = store.validate()
        guard errors.isEmpty else {
            lastError[1] = errors.values.sorted().joined(separator: " ")
            rigCheckProgressText = nil
            return false
        }

        if store.settings.triggerMode == "sdk" {
            rigCheckProgressText = "Checking Sony camera connection."
            guard await cameraConnection.check(store: store, orchestratorClient: orchestratorClient) else {
                lastError[1] = "Sony camera is not reachable. \(cameraConnection.detailText)"
                rigCheckProgressText = nil
                return false
            }
        }

        if coordinator.phase == .idle {
            rigCheckProgressText = "Starting calibration backend."
            await coordinator.startCalibration(settings: store.settings)
        }

        guard coordinator.phase == .calibrating else {
            lastError[1] = coordinator.lastError.isEmpty
                ? "Stop the active scan before calibrating."
                : coordinator.lastError
            rigCheckProgressText = nil
            return false
        }

        rigCheckProgressText = "Checking backend state."
        let ok = await fetchRigCheckState(step: 1)
        if ok {
            previewRefreshGeneration += 1
            rigCheckProgressText = "Rig check complete. Refreshing live frame."
        } else {
            rigCheckProgressText = nil
        }
        return rigCheckResult != nil
    }

    /// Step 1: probe rig state via the existing GET /api/state route.
    func triggerRigCheck() async {
        guard !isRunning else { return }
        isRunning = true
        rigCheckProgressText = "Checking backend state."
        lastError[1] = nil
        defer {
            isRunning = false
            if rigCheckResult == nil {
                rigCheckProgressText = nil
            }
        }
        _ = await fetchRigCheckState(step: 1)
    }

    private func fetchRigCheckState(step: Int) async -> Bool {
        do {
            let state = try await orchestratorClient.fetchState()
            rigCheckResult = state
            rigCheckProgressText = "Rig check complete."
            // Navigation to step 2 is handled by WizardNavFooter.primaryAction().
            // The trigger only sets the result; the operator must press "Next" to advance.
            return true
        } catch {
            lastError[step] = errorCopy(for: error, step: step)
            return false
        }
    }

    /// Step 2: run exposure calibration via POST /api/calibrate/exposure.
    func triggerExposure(seed: ExposureCalibrationResult? = nil, targetFraction: Double? = nil) async {
        guard !isRunning else { return }
        guard let rebateRegion else {
            lastError[2] = "Select a film-base sample in the preview before running exposure. The app measures only the highlighted RAW crop."
            return
        }
        let callID = UUID().uuidString
        isRunning = true
        lastError[2] = nil
        exposureResult = nil
        exposureLogEntries = []
        exposureProgressText = "Starting exposure calibration."
        defer {
            exposureProgressText = nil
            isRunning = false
        }
        do {
            try? await orchestratorClient.setCalibrationPreviewLight(enabled: false)
            let result: ExposureCalibrationResult
            do {
                result = try await runExposureWithNoStartWatchdog(
                    rebateCol: rebateRegion.x,
                    rebateRow: rebateRegion.y,
                    rebateW: rebateRegion.w,
                    rebateH: rebateRegion.h,
                    seed: seed,
                    callID: callID,
                    targetFraction: targetFraction
                )
            } catch {
                guard await shouldRetryIdleBusyConflict(error) else { throw error }
                exposureProgressText = "Backend lock cleared; retrying exposure calibration."
                try? await Task.sleep(nanoseconds: 1_000_000_000)
                result = try await runExposureWithNoStartWatchdog(
                    rebateCol: rebateRegion.x,
                    rebateRow: rebateRegion.y,
                    rebateW: rebateRegion.w,
                    rebateH: rebateRegion.h,
                    seed: seed,
                    callID: callID,
                    targetFraction: targetFraction
                )
            }
            exposureResult = result
            await refreshExposureProgress(callID: callID)
            // Navigation to step 3 is handled by WizardNavFooter.primaryAction().
        } catch {
            await refreshExposureProgress(callID: callID)
            if isBackendBusyConflict(error) {
                exposureProgressText = "Exposure calibration is already running; watching the existing run."
                let existingCallID = conflictCallID(from: error)
                if let existingCallID,
                   let result = await waitForExistingExposureResult(callID: existingCallID) {
                    exposureResult = result
                    lastError[2] = nil
                    await refreshExposureProgress(callID: existingCallID)
                } else {
                    lastError[2] = "The camera/light backend is busy, but no matching exposure result is available. Wait for the current capture or live preview to finish, then try again."
                }
            } else {
                lastError[2] = errorCopy(for: error, step: 2)
            }
        }
    }

    private enum ExposureRaceResult {
        case completed(ExposureCalibrationResult)
    }

    private struct ExposureDidNotStartError: LocalizedError {
        var errorDescription: String? {
            "Exposure calibration did not start. The backend stayed idle, so no camera capture is running. Try Run Exposure again; if it repeats, stop calibration and run the rig check again."
        }
    }

    private func runExposureWithNoStartWatchdog(
        rebateCol: Int,
        rebateRow: Int,
        rebateW: Int,
        rebateH: Int,
        seed: ExposureCalibrationResult?,
        callID: String,
        targetFraction: Double?
    ) async throws -> ExposureCalibrationResult {
        try await withThrowingTaskGroup(of: ExposureRaceResult.self) { group in
            group.addTask { [orchestratorClient] in
                let result = try await orchestratorClient.calibrateExposure(
                    rebateCol: rebateCol,
                    rebateRow: rebateRow,
                    rebateW: rebateW,
                    rebateH: rebateH,
                    seed: seed,
                    callID: callID,
                    targetFraction: targetFraction
                )
                return .completed(result)
            }
            group.addTask { [weak self] in
                try await self?.watchExposureStart(callID: callID)
                throw CancellationError()
            }

            guard let first = try await group.next() else {
                group.cancelAll()
                throw CancellationError()
            }
            group.cancelAll()
            switch first {
            case .completed(let result):
                return result
            }
        }
    }

    private func watchExposureStart(callID: String) async throws {
        let deadline = Date(timeIntervalSinceNow: exposureNoStartTimeoutSeconds)
        var sawBackendActivity = false

        while !Task.isCancelled {
            try? await Task.sleep(nanoseconds: exposureProgressPollIntervalNanoseconds)
            if Task.isCancelled { return }
            do {
                let progress = try await orchestratorClient.calibrationProgress(callID: callID)
                exposureProgressText = progress.message
                exposureLogEntries = progress.recentEvents ?? []
                if !isIdleProgress(progress) {
                    sawBackendActivity = true
                }
                if !sawBackendActivity && Date() >= deadline {
                    throw ExposureDidNotStartError()
                }
            } catch is ExposureDidNotStartError {
                throw ExposureDidNotStartError()
            } catch {
                // The long exposure POST is still authoritative. Progress fetches
                // only decide the no-start case when the backend explicitly says idle.
            }
        }
    }

    private func refreshExposureProgress(callID: String? = nil) async {
        do {
            let progress = try await orchestratorClient.calibrationProgress(callID: callID)
            exposureProgressText = progress.message
            exposureLogEntries = progress.recentEvents ?? []
        } catch {
            // The exposure POST remains the source of truth; progress polling is
            // best-effort UI feedback while the long request is in flight.
        }
    }

    private func shouldRetryIdleBusyConflict(_ error: Error) async -> Bool {
        guard isBackendBusyConflict(error),
              conflictCallID(from: error) == nil else {
            return false
        }
        guard let progress = try? await orchestratorClient.calibrationProgress(callID: nil) else {
            return false
        }
        return isIdleProgress(progress)
    }

    private func isIdleProgress(_ progress: CalibrationProgress) -> Bool {
        progress.event.lowercased() == "idle" &&
            (progress.recentEvents ?? []).isEmpty
    }

    private func waitForExistingExposureResult(callID: String) async -> ExposureCalibrationResult? {
        for _ in 0..<900 {
            if Task.isCancelled { return nil }
            if let result = try? await orchestratorClient.lastExposureResult(callID: callID) {
                return result
            }
            await refreshExposureProgress(callID: callID)
            try? await Task.sleep(nanoseconds: 2_000_000_000)
        }
        return nil
    }

    /// Step 3: capture flat frames via POST /api/calibrate/ffc.
    /// Requires exposureResult to be set (from Step 2).
    func triggerFFC() async {
        guard !isRunning else { return }
        guard let exp = exposureResult else {
            lastError[3] = "Run exposure calibration first (Step 2)."
            return
        }
        isRunning = true
        lastError[3] = nil
        ffcSkipped = false
        defer { isRunning = false }
        do {
            let result = try await orchestratorClient.calibrateFFC(exposureResult: exp)
            ffcResult = result
            // Navigation to step 4 is handled by WizardNavFooter.primaryAction().
        } catch {
            lastError[3] = errorCopy(for: error, step: 3)
        }
    }

    /// Continue without generating a new flat-field correction. Clearing the
    /// active FFC path avoids silently applying a stale correction from an old
    /// roll/session after the operator explicitly skips this step.
    func skipFFC(in store: SettingsStore) {
        ffcSkipped = true
        ffcResult = nil
        lastError[3] = nil
        store.settings.ffcCalibration = nil
        currentStep = 4
    }

    /// Step 4: run calibration checks via POST /api/calibrate/checks.
    func triggerResultsCheck() async {
        guard !isRunning else { return }
        isRunning = true
        lastError[4] = nil
        defer { isRunning = false }
        do {
            let results = try await orchestratorClient.calibrateChecks()
            checkResults = results
        } catch {
            lastError[4] = errorCopy(for: error, step: 4)
        }
    }

    // MARK: - Actions

    /// Write exposure-calibrated RGB levels to the settings store.
    func applyExposure(to store: SettingsStore) {
        guard let exposureResult else { return }
        store.settings.levelR = exposureResult.r.ledLevel
        store.settings.levelG = exposureResult.g.ledLevel
        store.settings.levelB = exposureResult.b.ledLevel
        store.settings.shutterR = exposureResult.r.shutterSpeed
        store.settings.shutterG = exposureResult.g.shutterSpeed
        store.settings.shutterB = exposureResult.b.shutterSpeed
    }

    /// Write the calibrated RGB levels and, when the backend returns a
    /// persisted flat-data path, the FFC calibration reference to Settings.
    func applyCalibration(to store: SettingsStore) {
        applyExposure(to: store)
        if let path = ffcResult?.flatField.flatDataPath, !path.isEmpty {
            store.settings.ffcCalibration = path
            store.lastCalibrationDir = path
        }
    }

    /// Save the current exposure result as a named per-stock RGB profile.
    func saveCurrentStockProfile(to store: SettingsStore) {
        guard let exposureResult else {
            stockProfileMessage = "Run exposure calibration before saving a stock profile."
            return
        }

        guard let profile = store.saveStockCalibrationProfile(
            stockName: calibrationStockName,
            exposureResult: exposureResult
        ) else {
            stockProfileMessage = "Enter a film stock name before saving."
            return
        }

        selectedStockProfileID = profile.id
        calibrationStockName = profile.stockName
        stockProfileMessage = "Saved RGB profile for \(profile.stockName)."
    }

    /// Load a saved per-stock RGB profile into Settings and the wizard result.
    func applySelectedStockProfile(from store: SettingsStore) {
        guard let profile = store.stockCalibrationProfile(id: selectedStockProfileID) else {
            stockProfileMessage = "Choose a saved stock profile first."
            return
        }

        store.applyStockCalibrationProfile(profile)
        exposureResult = profile.exposureResult
        calibrationStockName = profile.stockName
        stockProfileMessage = "Applied RGB profile for \(profile.stockName). Capture a fresh flat field before scanning."
        lastError[2] = nil
    }

    /// Reset all wizard state to the initial Step-1 idle state.
    func reset() {
        currentStep = 1
        isRunning = false
        lastError = [:]
        rigCheckResult = nil
        rigCheckProgressText = nil
        exposureResult = nil
        exposureProgressText = nil
        exposureLogEntries = []
        previewRefreshGeneration = 0
        ffcResult = nil
        ffcSkipped = false
        checkResults = nil
        rebateRegion = nil
        stockProfileMessage = nil
    }

    // MARK: - Error copy

    private func errorCopy(for error: Error, step: Int) -> String {
        if error is ExposureDidNotStartError {
            return ExposureDidNotStartError().localizedDescription
        }
        if let orchErr = error as? OrchestratorError {
            switch orchErr {
            case .httpError(let code, let body):
                if code == 409 {
                    return "The camera/light backend is busy with another capture or live preview. Wait for it to finish and try again."
                }
                return Self.friendlyServerError(statusCode: code, body: body)
            case .invalidResponse:
                return "\(orchErr.localizedDescription) Is the Python server running?"
            case .toolNotFound(let msg):
                return "Tool not found: \(msg)"
            case .startupFailed, .startupTimeout:
                return orchErr.localizedDescription
            }
        }
        return "Step \(step) failed: \(error.localizedDescription)"
    }

    private func isBackendBusyConflict(_ error: Error) -> Bool {
        if case OrchestratorError.httpError(let code, _) = error {
            return code == 409
        }
        return false
    }

    private func conflictCallID(from error: Error) -> String? {
        guard case OrchestratorError.httpError(_, let body) = error,
              let data = body.data(using: .utf8),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return nil
        }
        return (json["call_id"] as? String)
            ?? (json["current_call_id"] as? String)
    }

    internal static func friendlyServerError(statusCode: Int, body: String) -> String {
        let serverMessage = serverErrorMessage(from: body)
        let lower = serverMessage.lowercased()

        if lower.contains("sony-capture") && lower.contains("exit 127") {
            return (
                "Sony SDK capture tool was not found. Build phase1/sony-capture, " +
                "then use Set Up > Check Camera before running calibration."
            )
        }

        if lower.contains("sony-capture") && lower.contains("exit 124") {
            return (
                "Sony SDK capture timed out. Confirm the camera is in the configured SDK remote mode, " +
                "then use Set Up > Check Camera."
            )
        }

        if lower.contains("shutter speed is not writable")
            || lower.contains("camera mode dial to m")
            || lower.contains("manual exposure") {
            return (
                "Camera shutter speed is not writable. Set the camera mode dial to M/manual exposure, " +
                "keep f/8 fixed, and let the SDK set ISO 100 or ISO 125 before running exposure calibration again."
            )
        }

        if lower.contains("dark-frame capture failed") {
            return "Exposure calibration could not capture the dark frame: \(serverMessage)"
        }

        if !serverMessage.isEmpty {
            return "Calibration failed: \(serverMessage)"
        }

        return "Calibration failed with server status \(statusCode)."
    }

    private static func serverErrorMessage(from body: String) -> String {
        guard let data = body.data(using: .utf8) else {
            return body.trimmingCharacters(in: .whitespacesAndNewlines)
        }

        if let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let error = json["error"] as? String {
            return error.trimmingCharacters(in: .whitespacesAndNewlines)
        }

        return body.trimmingCharacters(in: .whitespacesAndNewlines)
    }
}
