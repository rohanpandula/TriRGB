// CalibrationViewModel — @MainActor ObservableObject that owns calibration state
// and runs the two-step capture + inspect pipeline.
//
// Extraction rationale: keeping state in a separate class (not inside the View)
// lets tests instantiate CalibrationViewModel directly, inject a stub runner,
// call runCalibration(), and assert on @Published state — with no app launch
// needed. This mirrors how OrchestratorClientTests injects a stub URLSession.
//
// Injectable runner: CalibrationRunner typealias is defined HERE (not in
// CalibrationView.swift) so tests can import it without importing the View.
//
// Security notes (T-06-08):
//   - calDir is built from DateFormatter output (no user input).
//   - calDir.path is passed as a positional [String] argument — never sh -c.
//   - Guard: calDir.path.contains("..") == false before use.
//
// Pattern mirrors ScanlightViewModel exactly: @MainActor final class with
// @Published properties. Does NOT use the Observable macro.

import Foundation

// MARK: - CalibrationRunner typealias

/// A closure that runs the full capture+inspect pipeline for a calibration
/// session directory and returns the decoded CalibrationResult.
///
/// The default implementation (CalibrationViewModel.defaultRunner) spawns the
/// real scripts. Tests inject a closure that returns a known CalibrationResult
/// without touching the filesystem or hardware.
typealias CalibrationRunner = (URL) async throws -> CalibrationResult

// MARK: - CalibrationRunnerError

/// Errors that CalibrationRunner (and therefore runCalibration()) may throw.
/// All cases carry a userMessage with actionable copy from the UI spec.
enum CalibrationRunnerError: Error {
    /// The named tool was not found via PythonToolLocator.
    case toolNotFound(String)

    /// capture-calibration.sh exited non-zero.
    case captureFailed(exitCode: Int32, stderr: String)

    /// inspect-calibration.py exited non-zero.
    case inspectFailed(exitCode: Int32, stderr: String)

    /// inspect-calibration.py stdout could not be decoded as CalibrationResult JSON.
    case jsonDecodeFailed(String)

    /// Human-readable message suitable for display in the UI.
    var userMessage: String {
        switch self {
        case .toolNotFound:
            return "Tool not found. Run `pip install -e phase2/triplet-capture`."
        case .captureFailed(let exitCode, _):
            return "Calibration capture failed (exit \(exitCode)). Check that the camera is connected."
        case .inspectFailed, .jsonDecodeFailed:
            return "Could not parse calibration results. Run `inspect-calibration.py --json` manually to debug."
        }
    }
}

// MARK: - CalibrationViewModel

/// Owns calibration state and the injectable runner.
///
/// Views receive this as @StateObject and observe it. Tests instantiate it
/// directly, replace calibrationRunner, and call runCalibration().
@MainActor
final class CalibrationViewModel: ObservableObject {

    // MARK: Published state

    /// True while the capture+inspect pipeline is running.
    @Published var isRunning: Bool = false

    /// The most recent successful CalibrationResult. Nil until a run completes.
    @Published var result: CalibrationResult? = nil

    /// Human-readable error message from the most recent failed run. Nil when
    /// no error has occurred or after a successful run clears it.
    @Published var lastError: String? = nil

    /// The session directory that was passed to the runner on the last
    /// successful run. Used by applyCalibration(to:) to write the FFC path.
    @Published var lastCalDir: URL? = nil

    // MARK: Injectable runner

    /// The calibration pipeline. Default implementation spawns real scripts.
    /// Override in tests to return a stub CalibrationResult without hardware.
    var calibrationRunner: CalibrationRunner = CalibrationViewModel.defaultRunner

    // MARK: Default runner (real implementation)

    /// Real runner: spawns capture-calibration.sh --no-confirm then
    /// inspect-calibration.py --json, and decodes the JSON output.
    ///
    /// Never called in tests — replaced by a stub closure.
    static var defaultRunner: CalibrationRunner = { calDir in
        // 1. Resolve capture-calibration.sh
        let captureURL: URL
        do {
            captureURL = try PythonToolLocator.resolve("capture-calibration.sh")
        } catch PythonToolLocatorError.toolNotFound(let msg) {
            throw CalibrationRunnerError.toolNotFound(msg)
        }

        // 2. Spawn capture-calibration.sh --no-confirm <calDir>
        // Arguments are a [String] array — never sh -c (T-06-08).
        let captureProc = Process()
        captureProc.executableURL = captureURL
        captureProc.arguments = ["--no-confirm", calDir.path]
        captureProc.environment = ProcessInfo.processInfo.environment

        let captureStdout = Pipe()
        let captureStderr = Pipe()
        captureProc.standardOutput = captureStdout
        captureProc.standardError = captureStderr

        try captureProc.run()
        // Await termination off the main actor via a checked continuation
        await withCheckedContinuation { continuation in
            captureProc.terminationHandler = { _ in continuation.resume() }
        }

        guard captureProc.terminationStatus == 0 else {
            let errData = captureStderr.fileHandleForReading.readDataToEndOfFile()
            let stderr = String(data: errData, encoding: .utf8) ?? ""
            throw CalibrationRunnerError.captureFailed(
                exitCode: captureProc.terminationStatus,
                stderr: stderr
            )
        }

        // 3. Resolve inspect-calibration.py
        let inspectURL: URL
        do {
            inspectURL = try PythonToolLocator.resolve("inspect-calibration.py")
        } catch PythonToolLocatorError.toolNotFound(let msg) {
            throw CalibrationRunnerError.toolNotFound(msg)
        }

        // 4. Spawn inspect-calibration.py --json <calDir>
        let inspectProc = Process()
        inspectProc.executableURL = inspectURL
        inspectProc.arguments = ["--json", calDir.path]
        inspectProc.environment = ProcessInfo.processInfo.environment

        let inspectStdout = Pipe()
        let inspectStderr = Pipe()
        inspectProc.standardOutput = inspectStdout
        inspectProc.standardError = inspectStderr

        try inspectProc.run()
        await withCheckedContinuation { continuation in
            inspectProc.terminationHandler = { _ in continuation.resume() }
        }

        let stdoutData = inspectStdout.fileHandleForReading.readDataToEndOfFile()

        // 5. Decode CalibrationResult from stdout FIRST.
        //
        // inspect-calibration.py emits valid JSON on exit code 0 (clean/acceptable)
        // AND exit code 1 (fail verdict). Exit code 2 means file/decode error (no
        // valid JSON). So: attempt decode first; only fall back to inspectFailed
        // for exit code 2 or when the decode genuinely fails.
        let decoded: CalibrationResult
        do {
            let decoder = JSONDecoder()
            decoder.keyDecodingStrategy = .convertFromSnakeCase
            decoded = try decoder.decode(CalibrationResult.self, from: stdoutData)
        } catch let decodingError {
            // Real decode failure — also capture stderr for context.
            let errData = inspectStderr.fileHandleForReading.readDataToEndOfFile()
            let stderrStr = String(data: errData, encoding: .utf8) ?? ""
            if !stderrStr.isEmpty {
                throw CalibrationRunnerError.inspectFailed(
                    exitCode: inspectProc.terminationStatus, stderr: stderrStr)
            }
            throw CalibrationRunnerError.jsonDecodeFailed(decodingError.localizedDescription)
        }

        // Exit code 2 means file-not-found / decode error on the Python side (stderr
        // explains). Exit codes 0 and 1 both produce valid JSON; 1 simply means
        // overall == "fail" — the verdict is inside the decoded result.
        if inspectProc.terminationStatus == 2 {
            let errData = inspectStderr.fileHandleForReading.readDataToEndOfFile()
            let stderr = String(data: errData, encoding: .utf8) ?? ""
            throw CalibrationRunnerError.inspectFailed(
                exitCode: inspectProc.terminationStatus, stderr: stderr)
        }

        return decoded
    }

    // MARK: - Public API

    /// Run the full calibration pipeline: create session directory, invoke
    /// calibrationRunner, publish the result or error.
    ///
    /// Idempotent guard: returns immediately if already running.
    func runCalibration() async {
        guard !isRunning else { return }

        isRunning = true
        lastError = nil
        result = nil

        // Build session cal dir: ~/.scanlight/calibration/YYYY-MM-DD-HHmm/
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy-MM-dd-HHmm"
        let calURL = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".scanlight/calibration")
            .appendingPathComponent(formatter.string(from: Date()))

        // T-06-08: guard path traversal — calDir is built from DateFormatter
        // output only, so ".." cannot appear, but check defensively.
        guard !calURL.path.contains("..") else {
            lastError = "Invalid calibration directory path."
            isRunning = false
            return
        }

        // Create the directory
        do {
            try FileManager.default.createDirectory(
                at: calURL,
                withIntermediateDirectories: true,
                attributes: nil
            )
        } catch {
            lastError = "Failed to create calibration directory: \(error.localizedDescription)"
            isRunning = false
            return
        }

        // Run the pipeline
        do {
            let res = try await calibrationRunner(calURL)
            result = res
            lastCalDir = calURL
        } catch let err as CalibrationRunnerError {
            lastError = err.userMessage
        } catch {
            lastError = "Unexpected error: \(error.localizedDescription)"
        }

        isRunning = false
    }

    /// Write the last calibration directory path to the given SettingsStore.
    ///
    /// Called by "Use this calibration" in CalibrationView. Sets both
    /// store.settings.ffcCalibration (the CLI flag field) and
    /// store.lastCalibrationDir (the "Use last calibration" shortcut in
    /// ScanSettingsView).
    func applyCalibration(to store: SettingsStore) {
        store.settings.ffcCalibration = lastCalDir?.path
        store.lastCalibrationDir = lastCalDir?.path
    }
}
