// OrchestratorClient — @MainActor ObservableObject that spawns triplet-capture
// as a managed child process and drives its HTTP API over localhost.
//
// Pattern mirrors ScanlightViewModel exactly: @MainActor final class with
// @Published properties and Combine import. Does NOT use the Observable macro.
//
// HTTP contracts (all routes use /api/ prefix):
//   GET  /api/state            → OrchestratorState
//   POST /api/settings         → OrchestratorState (JSON body, snake_case)
//   POST /api/capture?retake=  → TripletOutcome    (retake is QUERY PARAM)
//   GET  /api/composite-status → CompositeStatus
//
// Process lifecycle:
//   start(settings:) → PythonToolLocator.resolve → Process.run (--web-port 0)
//                    → waitForPort (polls port-file every 200ms, 10s timeout)
//                    → waitForReady (polls /api/state every 200ms, 10s timeout)
//   stop()           → process.interrupt (SIGTERM) → 3s grace → SIGKILL

import Combine
import Darwin
import Foundation

// MARK: - Errors

enum OrchestratorError: Error {
    /// Process started but stayed alive without ever responding to /api/state
    /// within the timeout (hung server).
    case startupTimeout(stderr: String)
    /// Process exited before becoming ready — the common startup failure
    /// (bad args, missing Python dep, import error). Carries the exit code and
    /// whatever the child wrote to stderr (usually the traceback).
    case startupFailed(exitCode: Int32, stderr: String)
    /// HTTP request succeeded but the status code was unexpected.
    case invalidResponse(statusCode: Int)
    /// HTTP request returned an error status with a body.
    case httpError(statusCode: Int, body: String)
    /// The Python tool was not found on PATH.
    case toolNotFound(String)
}

// MARK: - Codable Shapes

/// Response body from GET /api/state and POST /api/settings.
struct OrchestratorState: Codable {
    var rollName: String
    var frameNumber: Int
    var outputFolder: String
    var levelR: Int
    var levelG: Int
    var levelB: Int
    var settleMs: Int
}

/// Sent to start() for CLI args and to updateSettings() as POST /api/settings body.
/// Snake-case encoding via .convertToSnakeCase.
struct ScanSettings: Codable {
    var rollName: String
    var outputFolder: String      // BASE folder for CLI spawn; full path for POST
    var triggerMode: String       // "sdk" | "hw"
    var iedInbox: String?
    var streamComposite: Bool
    var ffcCalibration: String?
    var cameraModel: String?
    var compositeFormat: String   // "tiff" | "dng" | "both"
    var levelR: Int
    var levelG: Int
    var levelB: Int
    var settleMs: Int
}

/// Response body from POST /api/capture (200 success, 500 failure).
struct TripletOutcome: Codable {
    var success: Bool
    var frameNumber: Int
    var files: [String: String]   // "R"/"G"/"B" → file path
    var error: String?
    var durationS: Double
    var nextFrame: Int
}

/// One entry in CompositeStatus.results.
struct CompositeEntry: Codable {
    var frameNumber: Int
    var status: String            // "done" | "failed"
    var outputPath: String?
    var error: String?
}

/// Response body from GET /api/composite-status.
struct CompositeStatus: Codable {
    var enabled: Bool
    var pending: Int?
    var results: [CompositeEntry]?
}

/// Minimal decode of GET /api/state used only by waitForReady to confirm the
/// responding server is the child we spawned (it echoes our --ready-nonce).
private struct ReadyProbe: Codable {
    var readyNonce: String?
}

// MARK: - OrchestratorClient

/// Manages the lifetime of a `triplet-capture` child process and exposes its
/// HTTP API as async Swift methods.
///
/// Mirrors the `ScanlightViewModel` observable pattern exactly:
/// `@MainActor final class … : ObservableObject` with `@Published` properties.
@MainActor
final class OrchestratorClient: ObservableObject {

    // MARK: Published state

    /// Whether the child process is currently running.
    @Published var isRunning: Bool = false

    /// Last error message, or empty string if none.
    @Published var lastError: String = ""

    /// The localhost port the child process is listening on (0 until started).
    @Published var webPort: Int = 0

    // MARK: Privates

    private var process: Process?
    private let session: URLSession
    /// Accumulated stderr output from the child process.
    private var stderrData: Data = Data()
    /// Non-nil once the child process has exited (set by the termination handler).
    /// Reset to nil at the top of each start(). Lets waitForReady fail fast
    /// instead of polling the full timeout when the child dies during startup.
    internal var childExitStatus: Int32?
    /// True only while stop() is intentionally terminating the child, so the
    /// termination handler doesn't surface an "exited unexpectedly" error for a
    /// shutdown we asked for.
    private var stopping = false
    /// Monotonic per-launch token, bumped at the top of each start(). Every async
    /// callback (termination handler, stderr reader) and every deferred cleanup
    /// (start()'s catch, stop()'s defer) captures the generation it belongs to and
    /// no-ops once a newer launch supersedes it — so a slow callback from a
    /// killed/failed child can never clobber a newer launch's state. Internal so
    /// tests can drive the generation guard directly.
    internal var launchGeneration = 0

    // MARK: Init

    init(session: URLSession = .shared) {
        self.session = session
    }

    // MARK: - Public API

    /// Spawn `triplet-capture` with the given settings and wait for it to be ready.
    ///
    /// - Resolves the tool URL via `PythonToolLocator`.
    /// - Passes `--web-port 0` so the child owns port selection (eliminates TOCTOU).
    /// - Passes `--port-file <tmpfile>` so the child reports the actual bound port.
    /// - Builds a `[String]` argument array (never a shell string — T-05-04).
    /// - Polls the port-file until the child writes its bound port, then polls
    ///   `GET /api/state` until HTTP 200 + matching nonce (or timeout).
    ///
    /// Calling `start()` while already running throws immediately (prevents orphans).
    /// On startup timeout, the child is SIGTERMed/SIGKILLed before the error is
    /// re-thrown so a retry never creates an unreachable orphan process.
    func start(settings: ScanSettings) async throws {
        // Guard against double-start: a second call while running would spawn a
        // second child and overwrite self.process, orphaning the first one.
        guard !isRunning, process == nil else {
            throw OrchestratorError.toolNotFound("already running — call stop() first")
        }

        // Reset per-launch state so a prior run's exit status / stderr can't leak
        // into this launch (which would make waitForReady's fast-fail trip spuriously).
        childExitStatus = nil
        stderrData = Data()
        stopping = false

        // Bump the launch token. Every async callback/cleanup below captures this
        // value and no-ops if a newer launch has superseded it — so a slow callback
        // from a prior (killed/failed) child can't corrupt this launch's state.
        launchGeneration &+= 1
        let generation = launchGeneration

        // 1. Locate the tool
        let toolURL: URL
        do {
            toolURL = try PythonToolLocator.resolve("triplet-capture")
        } catch PythonToolLocatorError.toolNotFound(let msg) {
            throw OrchestratorError.toolNotFound(msg)
        }

        // 2. Readiness nonce + a unique port-file path.
        // The nonce lets waitForReady reject a foreign server (the child we spawned
        // echoes it on /api/state). The port-file is where the child writes the
        // actual bound port (--web-port 0 lets the OS pick; the child tells us what
        // it got via the file). Using a UUID-named file per launch avoids any stale
        // data from a prior launch racing with this one.
        let readyNonce = UUID().uuidString
        let portFileURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("triplet-capture-port-\(UUID().uuidString)")

        // 3. Build process
        let proc = Process()
        proc.executableURL = toolURL
        proc.arguments = buildArgs(settings: settings, portFile: portFileURL.path, readyNonce: readyNonce)
        proc.environment = ProcessInfo.processInfo.environment

        let stderrPipe = Pipe()
        proc.standardError = stderrPipe

        // Detect the child dying for ANY reason — startup crash, or a mid-scan
        // crash later. Without this, isRunning goes stale on a crash (the Phase 7
        // state machine reads it for serial-port ownership) and waitForReady polls
        // the full timeout after a fast startup failure. Must be set before run().
        installTerminationHandler(on: proc, generation: generation)

        // 4. Launch
        try proc.run()
        self.process = proc

        // 5. Capture stderr in background (non-blocking)
        // Capture the file handle to avoid capturing self in the detached task.
        // After the data is read, update stderrData via MainActor.run.
        let fileHandle = stderrPipe.fileHandleForReading
        Task.detached { [weak self] in
            let data = fileHandle.readDataToEndOfFile()
            await MainActor.run { [weak self] in
                // Ignore stderr belonging to a launch that has been superseded.
                guard let self, self.launchGeneration == generation else { return }
                self.stderrData = data
            }
        }

        // 6. Wait for the port-file to appear (child writes it once bound), then
        // wait for the server to be ready. Kill orphan before rethrowing so a retry
        // never creates an unreachable orphan process.
        let resolvedPort: Int
        do {
            resolvedPort = try await waitForPort(file: portFileURL, timeout: 10.0)
        } catch {
            await teardownFailedLaunch(proc, generation: generation)
            throw error
        }

        do {
            try await waitForReady(port: resolvedPort, timeout: 10.0, expectedNonce: readyNonce)
        } catch {
            // Tear down THIS launch's child (scoped — never the generic stop(),
            // which acts on the current process and would kill a newer launch's
            // child if a retry superseded us while waitForReady awaited).
            await teardownFailedLaunch(proc, generation: generation)
            throw error
        }

        // 7. Push the runtime-adjustable settings that have NO spawn-time CLI flag
        // (R/G/B levels + settle) so a capture fired right after start() uses the
        // caller's values, not the Python dataclass defaults (200/200/200, 50 ms).
        //
        // Sends ONLY levels+settle — NOT output_folder. The CLI spawn already set
        // output_folder to <base>/<rollName>; re-posting the base folder here would
        // REVERT it, because POST /api/settings treats output_folder as a full path
        // with no rollName append (the documented spawn-vs-POST asymmetry).
        //
        // webPort is set first (the POST needs it); isRunning is published only AFTER
        // the push succeeds, so an observer can't trigger a capture during the window
        // where the orchestrator still has default levels.
        webPort = resolvedPort
        do {
            try await applyRuntimeSettings(settings)
        } catch {
            // Couldn't apply levels — tear down THIS launch's child (scoped, not the
            // generic stop(): a retry may have superseded us while the POST awaited).
            await teardownFailedLaunch(proc, generation: generation)
            throw error
        }

        // 8. The child can die during or just after the settings push. Only publish
        // running state if this launch still owns a LIVE process — otherwise we'd set
        // isRunning=true for a dead/nil process (the exact stale-true hazard we are
        // trying to prevent, since the termination handler ran with wasRunning=false).
        guard launchGeneration == generation, childExitStatus == nil, proc.isRunning else {
            let stderrText = String(data: stderrData, encoding: .utf8) ?? ""
            let code = childExitStatus ?? (proc.isRunning ? -1 : proc.terminationStatus)
            await teardownFailedLaunch(proc, generation: generation)
            throw OrchestratorError.startupFailed(exitCode: code, stderr: stderrText)
        }
        isRunning = true
        lastError = ""
    }

    /// Send SIGTERM to the child process and wait up to 3 seconds for graceful
    /// shutdown. Falls back to SIGKILL if still running.
    ///
    /// isRunning and process are ALWAYS cleared on exit (including the early-return
    /// path when the child has already exited on its own). Without this, a child
    /// that crashes mid-session leaves isRunning=true and a stale process handle,
    /// blocking any subsequent start() call permanently.
    func stop() async {
        stopping = true
        let generation = launchGeneration
        defer {
            // Only clear shared state if a newer start() hasn't superseded us — the
            // child can exit mid-await, letting a concurrent start() launch a
            // replacement whose process handle we must not stomp.
            if launchGeneration == generation {
                isRunning = false
                process = nil
            }
            stopping = false
        }
        guard let proc = process else { return }
        guard proc.isRunning else { return }
        proc.interrupt()   // sends SIGTERM on Darwin
        let deadline = Date(timeIntervalSinceNow: 3.0)
        while proc.isRunning && Date() < deadline {
            try? await Task.sleep(nanoseconds: 100_000_000)  // 100ms
        }
        if proc.isRunning {
            kill(proc.processIdentifier, SIGKILL)
            // Poll asynchronously rather than blocking the main actor with
            // proc.waitUntilExit(). SIGKILL is not deferrable on Darwin so the
            // process dies quickly, but there is no OS timing guarantee.
            for _ in 0..<20 {
                try? await Task.sleep(nanoseconds: 50_000_000)  // 50ms
                if !proc.isRunning { break }
            }
        }
    }

    /// Fetch the current orchestrator state.
    func fetchState() async throws -> OrchestratorState {
        let url = URL(string: "http://127.0.0.1:\(webPort)/api/state")!
        let (data, response) = try await session.data(from: url)
        guard let http = response as? HTTPURLResponse else {
            throw OrchestratorError.invalidResponse(statusCode: 0)
        }
        guard http.statusCode == 200 else {
            throw OrchestratorError.invalidResponse(statusCode: http.statusCode)
        }
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(OrchestratorState.self, from: data)
    }

    /// Update settings on the running orchestrator via POST /api/settings.
    ///
    /// Encodes `settings` as a JSON object with snake_case keys
    /// (`.convertToSnakeCase`), posts to `/api/settings`, and asserts HTTP 200.
    ///
    /// NOTE on `output_folder`: POST /api/settings sets `output_folder` to the
    /// posted value VERBATIM (no `rollName` append, unlike the `--output-folder`
    /// CLI flag which Python appends `/<rollName>` to). `ScanSettings.outputFolder`
    /// holds the BASE folder for spawning, so callers that send the full struct
    /// here will set the orchestrator's output to the bare base. Phase 6's Settings
    /// view must send the intended full path. `start()` deliberately does NOT use
    /// this method for its post-ready push — see `applyRuntimeSettings`.
    func updateSettings(_ settings: ScanSettings) async throws {
        let url = URL(string: "http://127.0.0.1:\(webPort)/api/settings")!
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        request.httpBody = try encoder.encode(settings)
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw OrchestratorError.invalidResponse(statusCode: 0)
        }
        guard http.statusCode == 200 else {
            let body = String(data: data, encoding: .utf8) ?? ""
            throw OrchestratorError.httpError(statusCode: http.statusCode, body: body)
        }
    }

    /// POST only the runtime-adjustable fields that have no spawn-time CLI flag
    /// (R/G/B levels + settle). Deliberately omits `output_folder` and `roll_name`:
    /// those are set correctly at spawn, and POST /api/settings treats
    /// `output_folder` as a full path (no rollName append), so re-posting the base
    /// folder would revert the orchestrator's `<base>/<rollName>` output directory.
    /// `/api/settings` whitelists + applies only the keys present in the body, so
    /// sending a subset leaves every other field untouched.
    func applyRuntimeSettings(_ settings: ScanSettings) async throws {
        let url = URL(string: "http://127.0.0.1:\(webPort)/api/settings")!
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let body: [String: Int] = [
            "level_r": settings.levelR,
            "level_g": settings.levelG,
            "level_b": settings.levelB,
            "settle_ms": settings.settleMs,
        ]
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw OrchestratorError.invalidResponse(statusCode: 0)
        }
        guard http.statusCode == 200 else {
            let respBody = String(data: data, encoding: .utf8) ?? ""
            throw OrchestratorError.httpError(statusCode: http.statusCode, body: respBody)
        }
    }

    /// Trigger a triplet capture.
    ///
    /// `retake` is appended as a URL query parameter (`?retake=true`) only when
    /// `retake == true`. When false, the param is omitted entirely so the Python
    /// route's explicit string check (`in ("1", "true", "yes")`) treats absence
    /// as non-retake — robust against the `bool("false") == True` Python pitfall.
    func captureFrame(retake: Bool) async throws -> TripletOutcome {
        let urlStr = retake
            ? "http://127.0.0.1:\(webPort)/api/capture?retake=true"
            : "http://127.0.0.1:\(webPort)/api/capture"
        let url = URL(string: urlStr)!
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        // Empty body — retake is a query param only
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw OrchestratorError.invalidResponse(statusCode: 0)
        }
        // Accept 200 (success) and 500 (failure — TripletOutcome.success == false)
        guard http.statusCode == 200 || http.statusCode == 500 else {
            let body = String(data: data, encoding: .utf8) ?? ""
            throw OrchestratorError.httpError(statusCode: http.statusCode, body: body)
        }
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(TripletOutcome.self, from: data)
    }

    /// Fetch composite-worker status.
    func compositeStatus() async throws -> CompositeStatus {
        let url = URL(string: "http://127.0.0.1:\(webPort)/api/composite-status")!
        let (data, response) = try await session.data(from: url)
        guard let http = response as? HTTPURLResponse else {
            throw OrchestratorError.invalidResponse(statusCode: 0)
        }
        guard http.statusCode == 200 else {
            throw OrchestratorError.invalidResponse(statusCode: http.statusCode)
        }
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(CompositeStatus.self, from: data)
    }

    // MARK: - Internal (accessible to @testable import for short-timeout tests)

    /// Poll `GET /api/state` every 200 ms until HTTP 200 or `timeout` seconds elapsed.
    ///
    /// - `URLError.cannotConnectToHost` (code -1004) is swallowed silently —
    ///   it is expected while the Flask server is starting up.
    /// - All other errors are propagated immediately.
    /// - Marked `internal` (not `private`) so tests can invoke it directly with a
    ///   short timeout (e.g. 0.5 s) without spawning a real child process.
    internal func waitForReady(port: Int, timeout: TimeInterval, expectedNonce: String) async throws {
        let url = URL(string: "http://127.0.0.1:\(port)/api/state")!
        let deadline = Date(timeIntervalSinceNow: timeout)
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        while Date() < deadline {
            // Honor cancellation promptly (e.g. the scan was aborted mid-startup)
            // instead of spinning to the timeout.
            try Task.checkCancellation()
            // Fast-fail: if the child has already exited (set by the termination
            // handler), stop polling immediately rather than waiting out the full
            // timeout. The common startup failure — bad args, missing Python dep,
            // import error — exits in ~0.2s; without this the caller hangs `timeout`s.
            if let exitCode = childExitStatus {
                let stderrText = String(data: stderrData, encoding: .utf8) ?? ""
                throw OrchestratorError.startupFailed(exitCode: exitCode, stderr: stderrText)
            }
            do {
                let (data, response) = try await session.data(from: url)
                // Ready only when our spawned child answers 200 AND echoes the
                // matching nonce. A foreign server that grabbed the port in the
                // bind-probe TOCTOU gap won't know the nonce, so we keep polling
                // (until the real child binds and answers, or the deadline elapses).
                if (response as? HTTPURLResponse)?.statusCode == 200,
                   let probe = try? decoder.decode(ReadyProbe.self, from: data),
                   probe.readyNonce == expectedNonce {
                    return
                }
            } catch let urlError as URLError {
                // A cancelled request means start() itself was cancelled — propagate.
                if urlError.code == .cancelled { throw CancellationError() }
                // Other URLErrors are normal during startup (cannotConnectToHost,
                // networkConnectionLost, etc.) — retry until the deadline. A genuinely
                // dead child is caught by the childExitStatus check above, not here.
            }
            // No `try?` — let a cancelled sleep propagate so start() unwinds promptly.
            try await Task.sleep(nanoseconds: 200_000_000)  // 200ms
        }
        // Timeout: child stayed alive but never answered. Capture any stderr.
        let stderrText = String(data: stderrData, encoding: .utf8) ?? ""
        throw OrchestratorError.startupTimeout(stderr: stderrText)
    }

    /// Install a termination handler on `proc` that reactively clears running
    /// state the moment the child exits — for ANY reason (startup crash, signal,
    /// or a mid-scan crash long after start() returned).
    ///
    /// Internal (not private) so tests can install it on a trivial short-lived
    /// process and observe the state flip without spawning the real orchestrator.
    internal func installTerminationHandler(on proc: Process, generation: Int) {
        proc.terminationHandler = { [weak self] finished in
            // Read the exit code on the handler's own queue (it owns `finished`),
            // then hop to the main actor with only Sendable values.
            let status = finished.terminationStatus
            Task { @MainActor in
                self?.handleChildTermination(status: status, generation: generation)
            }
        }
    }

    /// Runs on the main actor when the child process exits. Clears running state
    /// so a crashed/exited orchestrator can never leave `isRunning` stale — the
    /// Phase 7 state machine reads `isRunning` to decide serial-port ownership.
    private func handleChildTermination(status: Int32, generation: Int) {
        // Ignore a late callback from a launch that has already been superseded —
        // otherwise a prior child's exit could clobber a newer launch's state.
        guard generation == launchGeneration else { return }
        childExitStatus = status
        process = nil
        let wasRunning = isRunning
        isRunning = false
        // Surface an error only for an UNEXPECTED exit — not a shutdown we asked
        // for via stop() (which sets `stopping`).
        if wasRunning && !stopping && status != 0 {
            let stderrText = String(data: stderrData, encoding: .utf8) ?? ""
            lastError = "orchestrator exited unexpectedly (code \(status))"
                + (stderrText.isEmpty ? "" : ": " + stderrText)
        }
    }

    /// Terminate a SPECIFIC launch's child and clear the shared handle only if this
    /// launch still owns it. Used by start()'s failure paths instead of the public
    /// stop() — stop() acts on the *current* process, which a concurrent retry may
    /// have already replaced with a newer child we must not kill.
    private func teardownFailedLaunch(_ proc: Process, generation: Int) async {
        if proc.isRunning {
            proc.interrupt()   // SIGTERM
            try? await Task.sleep(nanoseconds: 500_000_000)  // 500ms grace
            if proc.isRunning { kill(proc.processIdentifier, SIGKILL) }
        }
        if launchGeneration == generation { process = nil }
    }

    // MARK: - Private helpers

    /// Build the argument array for the child process.
    ///
    /// IMPORTANT: This always returns a `[String]` array — NEVER a shell string.
    /// `Process.arguments` does not use a shell, so there is no injection surface (T-05-04).
    ///
    /// `--web-port 0` tells the child to bind an ephemeral OS-assigned port.
    /// `--port-file <path>` tells the child where to write the actual bound port
    /// once listening. Swift reads that file via `waitForPort(file:timeout:)`.
    private func buildArgs(settings: ScanSettings, portFile: String, readyNonce: String) -> [String] {
        var args: [String] = [
            "--roll-name",      settings.rollName,
            "--output-folder",  settings.outputFolder,  // BASE folder; Python appends /rollName
            "--trigger-mode",   settings.triggerMode,
            "--web-port",       "0",
            "--port-file",      portFile,
            "--ready-nonce",    readyNonce,
            "--no-browser",
        ]
        if let inbox = settings.iedInbox {
            args += ["--ied-inbox", inbox]
        }
        if settings.streamComposite {
            args.append("--stream-composite")
        }
        if let ffc = settings.ffcCalibration {
            args += ["--ffc-calibration", ffc]
        }
        if let model = settings.cameraModel {
            args += ["--camera-model", model]
        }
        args += ["--composite-format", settings.compositeFormat]
        return args
    }

    /// Poll for the port-file written by the child once it is bound and listening.
    ///
    /// The child is spawned with `--web-port 0` (OS-assigned ephemeral port) and
    /// `--port-file <path>`. Once it has bound and is ready to accept connections
    /// it writes the actual port number to that file atomically (via a .tmp rename),
    /// then our `waitForReady` nonce-check provides the second layer of verification.
    ///
    /// - Polls every 200 ms, same cadence as `waitForReady`.
    /// - Fast-fails immediately if `childExitStatus` is set (child died before writing).
    /// - Throws `OrchestratorError.startupTimeout` if the deadline is reached.
    /// - Cleans up the port-file (best-effort) after reading or on failure.
    ///
    /// Marked `internal` so tests can drive it directly without spawning a real child.
    internal func waitForPort(file: URL, timeout: TimeInterval) async throws -> Int {
        let deadline = Date(timeIntervalSinceNow: timeout)
        let filePath = file.path
        defer {
            // Best-effort cleanup — ignore errors (file may not exist if we failed
            // before the child wrote it, or if the child cleaned it up itself).
            try? FileManager.default.removeItem(at: file)
        }
        while Date() < deadline {
            // Honor cancellation promptly.
            try Task.checkCancellation()
            // Fast-fail: if the child already exited without writing the port-file,
            // don't hang the full timeout.
            if let exitCode = childExitStatus {
                let stderrText = String(data: stderrData, encoding: .utf8) ?? ""
                throw OrchestratorError.startupFailed(exitCode: exitCode, stderr: stderrText)
            }
            // Read the file off the main actor (non-blocking string read from a
            // temp-dir file is fast enough that it doesn't need to be dispatched,
            // but we guard against a partial read by requiring a non-empty trimmed
            // string that parses as Int — the child writes atomically via rename
            // so we will either see the complete content or nothing at all).
            if FileManager.default.fileExists(atPath: filePath),
               let raw = try? String(contentsOfFile: filePath, encoding: .utf8),
               let port = Int(raw.trimmingCharacters(in: .whitespacesAndNewlines)),
               port > 0 {
                return port
            }
            // No `try?` — let a cancelled sleep propagate so start() unwinds promptly.
            try await Task.sleep(nanoseconds: 200_000_000)  // 200ms
        }
        let stderrText = String(data: stderrData, encoding: .utf8) ?? ""
        throw OrchestratorError.startupTimeout(stderr: stderrText)
    }
}
