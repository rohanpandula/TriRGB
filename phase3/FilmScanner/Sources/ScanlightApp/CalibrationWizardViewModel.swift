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
//   - rebateCoord is set by the spatial picker in Step 2; nil = auto-detect.

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

    /// Step 2 result — decoded ExposureCalibrationResult.
    @Published var exposureResult: ExposureCalibrationResult? = nil

    /// Step 3 result — decoded FlatFieldResponse.
    @Published var ffcResult: FlatFieldResponse? = nil

    /// Step 4 result — array of decoded WizardCheckResult.
    @Published var checkResults: [WizardCheckResult]? = nil

    /// Optional rebate coordinate from the spatial picker (col, row).
    /// Nil = auto-detect (default).
    var rebateCoord: (col: Int, row: Int)? = nil

    // MARK: - Init

    init(orchestratorClient: OrchestratorClient) {
        self.orchestratorClient = orchestratorClient
    }

    // MARK: - Step triggers

    /// Step 1: probe rig state via the existing GET /api/state route.
    func triggerRigCheck() async {
        guard !isRunning else { return }
        isRunning = true
        lastError[1] = nil
        defer { isRunning = false }
        do {
            let state = try await orchestratorClient.fetchState()
            rigCheckResult = state
            // Navigation to step 2 is handled by WizardNavFooter.primaryAction().
            // The trigger only sets the result; the operator must press "Next" to advance.
        } catch {
            lastError[1] = errorCopy(for: error, step: 1)
        }
    }

    /// Step 2: run exposure calibration via POST /api/calibrate/exposure.
    func triggerExposure() async {
        guard !isRunning else { return }
        isRunning = true
        lastError[2] = nil
        defer { isRunning = false }
        do {
            let result = try await orchestratorClient.calibrateExposure(
                rebateCol: rebateCoord?.col,
                rebateRow: rebateCoord?.row
            )
            exposureResult = result
            // Navigation to step 3 is handled by WizardNavFooter.primaryAction().
        } catch {
            lastError[2] = errorCopy(for: error, step: 2)
        }
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
        defer { isRunning = false }
        do {
            let result = try await orchestratorClient.calibrateFFC(exposureResult: exp)
            ffcResult = result
            // Navigation to step 4 is handled by WizardNavFooter.primaryAction().
        } catch {
            lastError[3] = errorCopy(for: error, step: 3)
        }
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

    /// Write the FFC calibration path to the settings store.
    /// Called by "Use this calibration" on Step 3.
    func applyCalibration(to store: SettingsStore) {
        if let path = ffcResult?.flatField.flatDataPath, !path.isEmpty {
            store.settings.ffcCalibration = path
        }
    }

    /// Reset all wizard state to the initial Step-1 idle state.
    func reset() {
        currentStep = 1
        isRunning = false
        lastError = [:]
        rigCheckResult = nil
        exposureResult = nil
        ffcResult = nil
        checkResults = nil
        rebateCoord = nil
    }

    // MARK: - Error copy

    private func errorCopy(for error: Error, step: Int) -> String {
        if let orchErr = error as? OrchestratorError {
            switch orchErr {
            case .httpError(let code, let body):
                if code == 409 {
                    return "A scan or calibration is already running. Wait for it to finish and try again."
                }
                return "Server returned \(code): \(body.prefix(120))"
            case .invalidResponse:
                return "Unexpected server response. Is the Python server running?"
            case .toolNotFound(let msg):
                return "Tool not found: \(msg)"
            case .startupFailed:
                return "Python server failed to start."
            case .startupTimeout:
                return "Python server timed out. Check the console for errors."
            }
        }
        return "Step \(step) failed: \(error.localizedDescription)"
    }
}
