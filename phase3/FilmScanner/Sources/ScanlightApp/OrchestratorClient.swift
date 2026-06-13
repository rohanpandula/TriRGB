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

enum OrchestratorError: LocalizedError, CustomStringConvertible {
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

    var errorDescription: String? {
        switch self {
        case .startupTimeout(let stderr):
            return Self.withDiagnostic(
                "Python server did not become ready before the startup timeout.",
                stderr: stderr,
                fallback: "No stderr was captured."
            )
        case .startupFailed(let exitCode, let stderr):
            return Self.withDiagnostic(
                "Python server exited before it became ready (exit \(exitCode)).",
                stderr: stderr,
                fallback: "No stderr was captured."
            )
        case .invalidResponse(let statusCode):
            return "Unexpected Python server response (HTTP \(statusCode))."
        case .httpError(let statusCode, let body):
            return Self.withDiagnostic(
                "Python server returned HTTP \(statusCode).",
                stderr: body,
                fallback: "No response body was returned."
            )
        case .toolNotFound(let message):
            return "Required tool was not found: \(message)"
        }
    }

    var description: String {
        errorDescription ?? String(describing: self)
    }

    private static func withDiagnostic(
        _ prefix: String,
        stderr: String,
        fallback: String
    ) -> String {
        let diagnostic = diagnosticSnippet(from: stderr)
        guard !diagnostic.isEmpty else {
            return "\(prefix) \(fallback)"
        }
        return "\(prefix) Details: \(diagnostic)"
    }

    private static func diagnosticSnippet(from rawText: String) -> String {
        let redacted = redactedDiagnostics(rawText)
        let lines = redacted
            .split(whereSeparator: \.isNewline)
            .map { String($0).trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
        let tail = lines.suffix(8).joined(separator: " | ")
        guard tail.count > 1200 else { return tail }
        return String(tail.suffix(1200))
    }

    private static func redactedDiagnostics(_ text: String) -> String {
        var output = text
        let patterns = [
            #"(?i)(--sony-password\s+)\S+"#,
            #"(?i)(--password\s+)\S+"#,
            #"(?i)(--sony-user\s+)\S+"#,
            #"(?i)(--user\s+)\S+"#,
            #"(?i)(sony_password[\"']?\s*[:=]\s*[\"']?)[^\"'\s,}]+"#,
            #"(?i)(sony_user[\"']?\s*[:=]\s*[\"']?)[^\"'\s,}]+"#,
            #"(?i)(password[\"']?\s*[:=]\s*[\"']?)[^\"'\s,}]+"#,
            #"(?i)(user[\"']?\s*[:=]\s*[\"']?)[^\"'\s,}]+"#,
        ]
        for pattern in patterns {
            output = output.replacingOccurrences(
                of: pattern,
                with: "$1<redacted>",
                options: .regularExpression
            )
        }
        return output
    }
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
    var shutterR: String? = nil
    var shutterG: String? = nil
    var shutterB: String? = nil
    /// Set by the Python backend when a manual/hw triplet is in progress and the
    /// operator must fire the IED for the named channel. Null/absent when idle.
    /// Values: "R" | "G" | "B" | nil
    var waitingForChannel: String? = nil
}

/// Sent to start() for CLI args and to updateSettings() as POST /api/settings body.
/// Snake-case encoding via .convertToSnakeCase.
struct ScanSettings: Codable {
    var rollName: String
    var outputFolder: String      // BASE folder for CLI spawn; full path for POST
    var scanlightPort: String? = nil
    var triggerMode: String       // "manual" | "hw" | "sdk"
    var iedInbox: String?
    var sonyTransport: String? = nil // "wifi" | "usb"; nil preserves existing Wi-Fi settings
    var sonyIpAddress: String? = nil
    var sonyMacAddress: String? = nil
    var sonyUser: String? = nil
    var sonyPassword: String? = nil
    var sonyCapturePath: String? = nil
    var streamComposite: Bool
    var ffcCalibration: String?
    var cameraModel: String?
    var compositeFormat: String   // "tiff" | "dng" | "both"
    var positiveProfileJSON: String? = nil
    var calibrationTargetFraction: Double? = nil
    var levelR: Int
    var levelG: Int
    var levelB: Int
    var settleMs: Int
    var shutterR: String? = nil
    var shutterG: String? = nil
    var shutterB: String? = nil
}

extension ScanSettings {
    var sonyTransportMode: String {
        let raw = (sonyTransport ?? "wifi")
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .lowercased()
        return raw == "usb" ? "usb" : "wifi"
    }

    var usesSonyUSB: Bool {
        sonyTransportMode == "usb"
    }

    var sonyTransportDisplayName: String {
        usesSonyUSB ? "USB" : "Wi-Fi"
    }
}

/// Result from a non-shooting Sony SDK connection probe.
struct SonyConnectionProbeResult: Equatable {
    var success: Bool
    var message: String
}

/// Result from a non-shooting Sony SDK live-view frame pull.
struct SonyLiveViewFrameResult: Equatable {
    var success: Bool
    var message: String
    var imageURL: URL?
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

/// Latest calibration progress event from GET /api/calibrate/progress.
struct CalibrationProgress: Codable, Equatable {
    var event: String
    var message: String
    var callId: String?
    var ts: String?
    var channel: String?
    var level: Int?
    var shutterSpeed: String?
    var label: String?
    var error: String?
    var recentEvents: [CalibrationProgressLogEntry]?
}

/// Compact timestamped event shown in the calibration log viewer.
struct CalibrationProgressLogEntry: Codable, Equatable, Identifiable {
    var ts: String?
    var event: String
    var message: String
    var callId: String?
    var channel: String?
    var level: Int?
    var shutterSpeed: String?
    var label: String?
    var p99: Double?
    var p999: Double?
    var target: Double?
    var clipFraction: Double?
    var sensorClipFraction: Double?
    var outputClipFraction: Double?
    var nextLevel: Int?
    var exposureStatus: String?
    var converged: Bool?
    var error: String?

    var id: String {
        [
            ts ?? "",
            event,
            callId ?? "",
            channel ?? "",
            level.map(String.init) ?? "",
            shutterSpeed ?? "",
            p99.map { String(format: "%.2f", $0) } ?? "",
            nextLevel.map(String.init) ?? "",
            error ?? "",
        ].joined(separator: "|")
    }
}

/// A running Python orchestrator process discovered from `ps`.
internal struct RunningTripletProcess: Equatable {
    var pid: Int32
    var parentPID: Int32
    var command: String
}

/// Minimal decode of GET /api/state used only by waitForReady to confirm the
/// responding server is the child we spawned (it echoes our --ready-nonce).
private struct ReadyProbe: Codable {
    var readyNonce: String?
}

/// Thread-safe incremental accumulator for a pipe's stdout/stderr.
///
/// `drain(handle:)` blocks on `availableData` in a loop until the writer
/// closes the pipe (`availableData.isEmpty` signals EOF). The caller polls
/// `isFinished` and only ever reads `collectedString` after deciding to stop
/// — never blocks on the drain itself. Used by `runSonyProbeProcess` to
/// avoid the synchronous-`readDataToEndOfFile` hang that froze the UI when
/// the Sony SDK left a pipe writer dangling.
final class PipeDrainer: @unchecked Sendable {
    private let lock = NSLock()
    private var data = Data()
    private var finished = false

    func drain(handle: FileHandle) {
        while true {
            let chunk = handle.availableData
            if chunk.isEmpty { break }
            lock.lock()
            data.append(chunk)
            // Cap to avoid unbounded growth on a chatty probe. Keep the tail
            // because that's where the operative error/status line lives.
            let cap = 64 * 1024
            if data.count > cap {
                data.removeFirst(data.count - cap)
            }
            lock.unlock()
        }
        lock.lock()
        finished = true
        lock.unlock()
    }

    var isFinished: Bool {
        lock.lock(); defer { lock.unlock() }
        return finished
    }

    var collectedString: String {
        lock.lock(); defer { lock.unlock() }
        return String(data: data, encoding: .utf8) ?? ""
    }
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
    private static let calibrationRequestTimeout: TimeInterval = 30 * 60
    /// Per-channel backend capture-wait caps, in seconds. These MUST match what
    /// the daemon actually uses per `trigger_mode`:
    ///  - sdk: the app launches the backend with `--capture-timeout-s 60`
    ///    (`sdkBackendCaptureTimeoutS` is the single source of truth for that
    ///    arg AND the client timeout — see start()).
    ///  - manual/hw: the app passes no override, so the backend uses its default
    ///    `CaptureSettings.sony_capture_timeout_s` (30s); track it here.
    static let sdkBackendCaptureTimeoutS: TimeInterval = 60
    static let defaultBackendCaptureTimeoutS: TimeInterval = 30
    /// The per-channel backend timeout for the currently-launched mode; set in
    /// start() from the trigger mode. captureFrame derives its HTTP timeout from
    /// this (× 3 channels + margin) so an SDK triplet (3 × 60s) can't be killed
    /// client-side before the backend finishes. Internal for test injection.
    var activePerChannelCaptureTimeoutS: TimeInterval = OrchestratorClient.defaultBackendCaptureTimeoutS
    /// Margin (seconds) added on top of 3 × per-channel before the client gives up.
    private static let captureTimeoutMarginS: TimeInterval = 15
    internal var sonyConnectionProbeOverride: ((ScanSettings) async -> SonyConnectionProbeResult)?
    internal static var sonyARPTableProvider: () -> String = {
        let proc = Process()
        let pipe = Pipe()
        proc.executableURL = URL(fileURLWithPath: "/usr/sbin/arp")
        proc.arguments = ["-an"]
        proc.standardOutput = pipe
        proc.standardError = Pipe()
        do {
            try proc.run()
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            proc.waitUntilExit()
            return String(data: data, encoding: .utf8) ?? ""
        } catch {
            return ""
        }
    }
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

    private static func url(
        path: String,
        webPort: Int,
        queryItems: [URLQueryItem] = []
    ) -> URL {
        var components = URLComponents()
        components.scheme = "http"
        components.host = "127.0.0.1"
        components.port = webPort
        components.path = path
        components.queryItems = queryItems.isEmpty ? nil : queryItems
        return components.url!
    }

    deinit {
        if let process, process.isRunning {
            process.terminate()
        }
    }

    /// Best-effort synchronous teardown for app termination.
    ///
    /// `stop()` is the normal async path while the UI is alive. During
    /// `applicationWillTerminate`, there may be no time left for an async task to
    /// run, so terminate the child immediately and clear local state. This keeps a
    /// crashed or closed UI from leaving a Python backend holding the camera/light
    /// workflow and causing the next launch to report a stale "already running".
    func terminateChildForAppShutdown() {
        stopping = true
        defer {
            isRunning = false
            process = nil
            stopping = false
        }

        guard let proc = process, proc.isRunning else { return }
        proc.interrupt()
        usleep(300_000)
        if proc.isRunning {
            kill(proc.processIdentifier, SIGKILL)
        }
    }

    // MARK: - Public API

    /// Probe the Sony SDK camera path without firing the shutter.
    ///
    /// `sony-capture --connect-only` connects, completes Access Auth/fingerprint
    /// caching, then disconnects. The scan loop still opens a fresh SDK session
    /// for each R/G/B capture, so this is a readiness check rather than a held
    /// connection.
    func checkSonyConnection(settings: ScanSettings) async -> SonyConnectionProbeResult {
        if let sonyConnectionProbeOverride {
            return await sonyConnectionProbeOverride(settingsWithResolvedSonyIP(settings))
        }

        do {
            let command = try buildSonyConnectionProbeCommand(
                settings: settingsWithResolvedSonyIP(settings),
                timeoutSeconds: 10
            )
            let maxAttempts = 3
            var lastFailure = SonyConnectionProbeResult(
                success: false,
                message: "Sony connection check did not run."
            )

            for attempt in 1...maxAttempts {
                let result = try await Self.runSonyProbeProcess(
                    executableURL: command.executableURL,
                    arguments: command.arguments,
                    environment: command.environment,
                    timeout: 15
                )

                if result.timedOut {
                    lastFailure = SonyConnectionProbeResult(
                        success: false,
                        message: "Timed out connecting to the Sony camera."
                    )
                } else {
                    let output = Self.conciseProcessOutput(stdout: result.stdout, stderr: result.stderr)
                    if result.exitCode == 0 {
                        let message = output.lowercased() == "connected"
                            ? "Connected to Sony camera."
                            : output
                        return SonyConnectionProbeResult(
                            success: true,
                            message: message.isEmpty ? "Connected to Sony camera." : message
                        )
                    }

                    lastFailure = SonyConnectionProbeResult(
                        success: false,
                        message: output.isEmpty
                            ? "Sony connection failed (exit \(result.exitCode))."
                            : "Sony connection failed (exit \(result.exitCode)): \(output)"
                    )
                }

                if attempt < maxAttempts, Self.isTransientSonyConnectionFailure(lastFailure.message) {
                    // Bug 8: Use a cancellation-propagating sleep so stop() returns
                    // promptly instead of being delayed by the full retry interval.
                    // If cancelled, exit the loop and return the last failure.
                    do {
                        try await Task.sleep(nanoseconds: UInt64(attempt) * 750_000_000)
                    } catch {
                        break  // Task cancelled — surface last failure immediately
                    }
                    continue
                }

                if attempt > 1, Self.isTransientSonyConnectionFailure(lastFailure.message) {
                    return SonyConnectionProbeResult(
                        success: false,
                        message: "\(lastFailure.message) Retried \(attempt) times. If this keeps happening, leave only one active 10.0.0.x network interface enabled while checking the camera."
                    )
                }
                return lastFailure
            }

            return SonyConnectionProbeResult(
                success: false,
                message: "\(lastFailure.message) Retried \(maxAttempts) times. If this keeps happening, leave only one active 10.0.0.x network interface enabled while checking the camera."
            )
        } catch PythonToolLocatorError.toolNotFound(let message) {
            return SonyConnectionProbeResult(success: false, message: message)
        } catch {
            return SonyConnectionProbeResult(
                success: false,
                message: "Could not run sony-capture: \(error.localizedDescription)"
            )
        }
    }

    private static func isTransientSonyConnectionFailure(_ message: String) -> Bool {
        let normalized = message.lowercased()
        return normalized.contains("no route to host")
            || normalized.contains("network is unreachable")
            || normalized.contains("host is down")
            || normalized.contains("operation timed out")
            || normalized.contains("timed out connecting")
            || normalized.contains("connection timed out")
    }

    private static func sonyCaptureEnvironment(for settings: ScanSettings) -> [String: String] {
        var environment = ProcessInfo.processInfo.environment
        if let user = settings.sonyUser, !user.isEmpty {
            environment["SONY_USERNAME"] = user
            environment["SONY_USER"] = user
        }
        if let password = settings.sonyPassword, !password.isEmpty {
            environment["SONY_PW"] = password
        }
        return environment
    }

    /// Pull one Sony SDK live-view JPEG frame without firing the shutter.
    ///
    /// This is intentionally process-scoped just like the SDK connection probe:
    /// `sony-capture --live-view-out` opens a short SDK session, writes one JPEG
    /// frame atomically, then disconnects. The scan loop remains responsible for
    /// actual RAW captures.
    func captureSonyLiveViewFrame(
        settings: ScanSettings,
        outputURL: URL
    ) async -> SonyLiveViewFrameResult {
        do {
            let sdkTimeoutSeconds = 20
            let processTimeoutSeconds: TimeInterval = 30
            let command = try buildSonyLiveViewFrameCommand(
                settings: settingsWithResolvedSonyIP(settings),
                outputURL: outputURL,
                timeoutSeconds: sdkTimeoutSeconds
            )
            let result = try await Self.runSonyProbeProcess(
                executableURL: command.executableURL,
                arguments: command.arguments,
                environment: command.environment,
                timeout: processTimeoutSeconds
            )

            let output = Self.conciseProcessOutput(stdout: result.stdout, stderr: result.stderr)

            if result.timedOut {
                let detail = output.isEmpty ? "" : " Details: \(output)"
                return SonyLiveViewFrameResult(
                    success: false,
                    message: "Timed out after \(Int(processTimeoutSeconds))s waiting for a Sony live-view frame.\(detail)",
                    imageURL: nil
                )
            }

            if result.exitCode == 0 {
                guard FileManager.default.fileExists(atPath: outputURL.path) else {
                    return SonyLiveViewFrameResult(
                        success: false,
                        message: "Sony live view finished but did not write a JPEG.",
                        imageURL: nil
                    )
                }
                return SonyLiveViewFrameResult(
                    success: true,
                    message: output.isEmpty ? "Live view updated." : output,
                    imageURL: outputURL
                )
            }

            return SonyLiveViewFrameResult(
                success: false,
                message: output.isEmpty
                    ? "Sony live view failed (exit \(result.exitCode))."
                    : "Sony live view failed (exit \(result.exitCode)): \(output)",
                imageURL: nil
            )
        } catch PythonToolLocatorError.toolNotFound(let message) {
            return SonyLiveViewFrameResult(success: false, message: message, imageURL: nil)
        } catch {
            return SonyLiveViewFrameResult(
                success: false,
                message: "Could not run sony-capture: \(error.localizedDescription)",
                imageURL: nil
            )
        }
    }

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

        // Record the per-channel backend capture timeout for this launch so
        // captureFrame's HTTP timeout covers the real worst case for this mode
        // (SDK uses 60s/channel; manual/hw use the backend default 30s).
        activePerChannelCaptureTimeoutS = settings.triggerMode == "sdk"
            ? Self.sdkBackendCaptureTimeoutS
            : Self.defaultBackendCaptureTimeoutS

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

        // A killed app can leave its Python child reparented to launchd while
        // still holding the Scanlight serial port. Remove same-checkout orphans
        // before starting another backend.
        let launchSettings = settingsWithResolvedSonyIP(settings)
        cleanupStaleTripletCaptureOrphans(settings: launchSettings)

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
        proc.arguments = buildArgs(settings: launchSettings, portFile: portFileURL.path, readyNonce: readyNonce)
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
            // Read incrementally (availableData returns as soon as a chunk is
            // ready) rather than readDataToEndOfFile (which only returns at EOF /
            // child exit). This still drains the pipe to avoid a full-buffer
            // stall on a chatty child, but ALSO makes a startup traceback visible
            // to the fast-fail paths (which read stderrData the moment
            // childExitStatus trips) instead of racing the EOF read. (Audit Low)
            while true {
                let chunk = fileHandle.availableData
                if chunk.isEmpty { break }  // EOF — write end closed (child exited)
                await MainActor.run { [weak self] in
                    // Ignore stderr belonging to a launch that has been superseded.
                    guard let self, self.launchGeneration == generation else { return }
                    self.stderrData.append(chunk)
                    // Keep only the tail so a long, chatty session can't grow this
                    // unbounded — a startup/crash traceback lives at the end anyway.
                    let cap = 64 * 1024
                    if self.stderrData.count > cap {
                        self.stderrData.removeFirst(self.stderrData.count - cap)
                    }
                }
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
            // Bug 3: The Python finally-block (scanlight.off()) never runs when the
            // child is SIGKILLed — the firmware thermal protection is known-broken,
            // so the LED would stay lit indefinitely. Fire a best-effort, non-blocking
            // `scanlightctl off` to turn the light off from the Swift side.
            // Swallow all errors: this is fire-and-forget insurance, not a hard requirement.
            Task.detached(priority: .background) {
                await Self.bestEffortScanlightOff()
            }
        }
    }

    /// Fire `scanlightctl off` in a best-effort, non-blocking way after a SIGKILL
    /// escalation. Tries the project-locator path first, falls back to
    /// `python3 -m scanlight.cli off`. Swallows all errors and enforces a short
    /// timeout so a missing binary can't stall the shutdown path.
    private nonisolated static func bestEffortScanlightOff() async {
        // Resolve `scanlightctl` via PythonToolLocator (mirrors how other CLI
        // tools are resolved in this file). Fall back to the Python module form.
        let candidates: [(executable: String, arguments: [String])] = {
            var list: [(String, [String])] = []
            if let url = try? PythonToolLocator.resolve("scanlightctl") {
                list.append((url.path, ["off"]))
            }
            // Fallback: Python module form — works when scanlightctl is installed
            // as a Python entry-point but the wrapper script isn't on PATH.
            list.append(("/usr/bin/env", ["python3", "-m", "scanlight.cli", "off"]))
            return list
        }()

        for (executable, arguments) in candidates {
            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: executable)
            proc.arguments = arguments
            proc.standardOutput = Pipe()
            proc.standardError = Pipe()
            do {
                try proc.run()
                // Wait up to 3 seconds; if it takes longer something is wrong
                // and we should not block the caller further.
                let deadline = Date(timeIntervalSinceNow: 3.0)
                while proc.isRunning && Date() < deadline {
                    try? await Task.sleep(nanoseconds: 100_000_000)  // 100ms
                }
                if proc.isRunning { proc.terminate() }
                if proc.terminationStatus == 0 { return }  // success — done
            } catch {
                // Process didn't launch (binary not found etc.) — try next candidate.
            }
        }
        // All candidates failed or errored — nothing more we can do.
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
        var body: [String: Any] = [
            "level_r": settings.levelR,
            "level_g": settings.levelG,
            "level_b": settings.levelB,
            "settle_ms": settings.settleMs,
        ]
        if let shutterR = settings.shutterR, !shutterR.isEmpty {
            body["shutter_r"] = shutterR
        }
        if let shutterG = settings.shutterG, !shutterG.isEmpty {
            body["shutter_g"] = shutterG
        }
        if let shutterB = settings.shutterB, !shutterB.isEmpty {
            body["shutter_b"] = shutterB
        }
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
        // Explicit timeout covering 3 channels × the launched mode's per-channel
        // backend timeout + margin. The URLSession default (60s) is far less than
        // a real triplet — SDK is 3 × 60s, manual is 3 IED fires at 30s each — so
        // without this the client would abort while the backend is still capturing.
        request.timeoutInterval = activePerChannelCaptureTimeoutS * 3 + Self.captureTimeoutMarginS
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

    // MARK: - Calibration wizard methods (Phase 14)

    /// Run exposure calibration via POST /api/calibrate/exposure.
    ///
    /// - Parameters:
    ///   - rebateCol: Optional column index of the rebate region (auto-detect when nil).
    ///   - rebateRow: Optional row index of the rebate region (auto-detect when nil).
    ///   - rebateW: Rebate region width in RAW pixels.
    ///   - rebateH: Rebate region height in RAW pixels.
    /// - Returns: `ExposureCalibrationResult` decoded from the snake_case JSON.
    /// - Throws: `OrchestratorError.httpError` for non-200 responses (includes 409 conflict).
    func calibrateExposure(
        rebateCol: Int? = nil,
        rebateRow: Int? = nil,
        rebateW: Int = RebateRegion.defaultWidth,
        rebateH: Int = RebateRegion.defaultHeight,
        seed: ExposureCalibrationResult? = nil,
        callID: String? = nil,
        targetFraction: Double? = nil
    ) async throws -> ExposureCalibrationResult {
        let url = URL(string: "http://127.0.0.1:\(webPort)/api/calibrate/exposure")!
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.timeoutInterval = Self.calibrationRequestTimeout
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")

        var body: [String: Any] = ["rebate_w": rebateW, "rebate_h": rebateH]
        if let col = rebateCol, let row = rebateRow {
            body["rebate_col"] = col
            body["rebate_row"] = row
        }
        if let callID, !callID.isEmpty {
            body["call_id"] = callID
        }
        if let targetFraction {
            body["target_fraction"] = targetFraction
        }
        if let seed {
            body["seed"] = [
                "R": [
                    "led_level": seed.r.ledLevel,
                    "shutter_speed": seed.r.shutterSpeed ?? "",
                ],
                "G": [
                    "led_level": seed.g.ledLevel,
                    "shutter_speed": seed.g.shutterSpeed ?? "",
                ],
                "B": [
                    "led_level": seed.b.ledLevel,
                    "shutter_speed": seed.b.shutterSpeed ?? "",
                ],
            ]
        }
        request.httpBody = try JSONSerialization.data(withJSONObject: body)

        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw OrchestratorError.invalidResponse(statusCode: 0)
        }
        guard http.statusCode == 200 else {
            let body = String(data: data, encoding: .utf8) ?? ""
            throw OrchestratorError.httpError(statusCode: http.statusCode, body: body)
        }
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(ExposureCalibrationResult.self, from: data)
    }

    /// Fetch the latest backend calibration progress event.
    func calibrationProgress(callID: String? = nil) async throws -> CalibrationProgress {
        let url = Self.url(
            path: "/api/calibrate/progress",
            webPort: webPort,
            queryItems: callID.map { [URLQueryItem(name: "call_id", value: $0)] } ?? []
        )
        let (data, response) = try await session.data(from: url)
        guard let http = response as? HTTPURLResponse else {
            throw OrchestratorError.invalidResponse(statusCode: 0)
        }
        guard http.statusCode == 200 else {
            throw OrchestratorError.invalidResponse(statusCode: http.statusCode)
        }
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(CalibrationProgress.self, from: data)
    }

    /// Fetch the last completed exposure calibration result, if the backend has one.
    func lastExposureResult(callID: String? = nil) async throws -> ExposureCalibrationResult? {
        let url = Self.url(
            path: "/api/calibrate/exposure-result",
            webPort: webPort,
            queryItems: callID.map { [URLQueryItem(name: "call_id", value: $0)] } ?? []
        )
        let (data, response) = try await session.data(from: url)
        guard let http = response as? HTTPURLResponse else {
            throw OrchestratorError.invalidResponse(statusCode: 0)
        }
        if http.statusCode == 404 {
            return nil
        }
        guard http.statusCode == 200 else {
            let body = String(data: data, encoding: .utf8) ?? ""
            throw OrchestratorError.httpError(statusCode: http.statusCode, body: body)
        }
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(ExposureCalibrationResult.self, from: data)
    }

    /// Turn the backend-owned Scanlight preview white light on/off for live-view framing.
    func setCalibrationPreviewLight(enabled: Bool, level: Int = 200) async throws {
        let url = URL(string: "http://127.0.0.1:\(webPort)/api/calibrate/preview-light")!
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.timeoutInterval = 5
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: [
            "enabled": enabled,
            "level": level,
        ])

        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw OrchestratorError.invalidResponse(statusCode: 0)
        }
        guard http.statusCode == 200 else {
            let body = String(data: data, encoding: .utf8) ?? ""
            throw OrchestratorError.httpError(statusCode: http.statusCode, body: body)
        }
    }

    /// Capture flat frames via POST /api/calibrate/ffc.
    ///
    /// - Parameter exposureResult: The result of the prior exposure calibration,
    ///   used to send led_level_{r,g,b} + black_level_{r,g,b} in the request body.
    /// - Returns: `FlatFieldResponse` with flat_field metadata and the inspection dict.
    /// - Throws: `OrchestratorError.httpError` for non-200 responses.
    func calibrateFFC(exposureResult: ExposureCalibrationResult) async throws -> FlatFieldResponse {
        let url = URL(string: "http://127.0.0.1:\(webPort)/api/calibrate/ffc")!
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.timeoutInterval = Self.calibrationRequestTimeout
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")

        var body: [String: Any] = [
            "led_level_r":   exposureResult.r.ledLevel,
            "led_level_g":   exposureResult.g.ledLevel,
            "led_level_b":   exposureResult.b.ledLevel,
            "black_level_r": exposureResult.r.blackLevel,
            "black_level_g": exposureResult.g.blackLevel,
            "black_level_b": exposureResult.b.blackLevel,
        ]
        if let shutter = exposureResult.r.shutterSpeed, !shutter.isEmpty {
            body["shutter_r"] = shutter
        }
        if let shutter = exposureResult.g.shutterSpeed, !shutter.isEmpty {
            body["shutter_g"] = shutter
        }
        if let shutter = exposureResult.b.shutterSpeed, !shutter.isEmpty {
            body["shutter_b"] = shutter
        }
        request.httpBody = try JSONSerialization.data(withJSONObject: body)

        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw OrchestratorError.invalidResponse(statusCode: 0)
        }
        guard http.statusCode == 200 else {
            let body = String(data: data, encoding: .utf8) ?? ""
            throw OrchestratorError.httpError(statusCode: http.statusCode, body: body)
        }
        // Use a plain JSONDecoder (no .convertFromSnakeCase) for FlatFieldResponse.
        // BOTH nested types use explicit CodingKeys with snake_case raw values:
        //   - WizardFlatFieldResult: explicit CodingKeys ("flat_data_path", etc.)
        //   - CalibrationResult: explicit CodingKeys ("falloff_pct", "uniformity_pct", etc.)
        // Adding .convertFromSnakeCase would transform JSON keys BEFORE matching explicit
        // CodingKeys, breaking both types (the decoder would look for already-camelCased
        // keys but the raw values in CodingKeys still reference the original snake_case).
        let decoder = JSONDecoder()
        return try decoder.decode(FlatFieldResponse.self, from: data)
    }

    /// Run calibration checks via POST /api/calibrate/checks.
    ///
    /// Swift sends an empty body; the route reads app.config["LAST_CAL_RESULT"]
    /// set by the prior exposure route. Must be called after calibrateExposure.
    ///
    /// - Returns: Array of `WizardCheckResult` (registration, base_neutrality).
    ///   frame_anomaly (per-frame vs roll baseline) is deferred to Phase 15 — no roll baseline
    ///   exists during a single calibration.
    /// - Throws: `OrchestratorError.httpError` for non-200 responses (includes 409 if
    ///   no prior exposure result is stored).
    func calibrateChecks() async throws -> [WizardCheckResult] {
        let url = URL(string: "http://127.0.0.1:\(webPort)/api/calibrate/checks")!
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: [:])

        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw OrchestratorError.invalidResponse(statusCode: 0)
        }
        guard http.statusCode == 200 else {
            let body = String(data: data, encoding: .utf8) ?? ""
            throw OrchestratorError.httpError(statusCode: http.statusCode, body: body)
        }
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode([WizardCheckResult].self, from: data)
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

    private func cleanupStaleTripletCaptureOrphans(settings: ScanSettings) {
        let psOutput = Self.processOutput(
            executable: "/bin/ps",
            arguments: ["-axo", "pid=,ppid=,command="]
        )
        let cwd = FileManager.default.currentDirectoryPath
        let repoHints = [
            cwd,
            settings.sonyCapturePath ?? "",
        ].filter { !$0.isEmpty }

        for candidate in Self.parseTripletProcesses(from: psOutput) {
            guard Self.shouldCleanupStaleTripletProcess(
                candidate,
                repoHints: repoHints,
                currentWorkingDirectory: cwd,
                cwdLookup: { Self.cwdForProcess(pid: $0) }
            ) else { continue }

            kill(candidate.pid, SIGTERM)
            usleep(400_000)
            if kill(candidate.pid, 0) == 0 {
                kill(candidate.pid, SIGKILL)
            }
        }
    }

    internal static func parseTripletProcesses(from psOutput: String) -> [RunningTripletProcess] {
        psOutput
            .split(separator: "\n")
            .compactMap { rawLine -> RunningTripletProcess? in
                let fields = rawLine
                    .split(maxSplits: 2, omittingEmptySubsequences: true) { $0.isWhitespace }
                guard fields.count == 3,
                      let pid = Int32(fields[0]),
                      let parentPID = Int32(fields[1]) else {
                    return nil
                }
                let command = String(fields[2])
                guard command.contains("-m triplet_capture.app") else { return nil }
                return RunningTripletProcess(pid: pid, parentPID: parentPID, command: command)
            }
    }

    internal static func shouldCleanupStaleTripletProcess(
        _ process: RunningTripletProcess,
        repoHints: [String],
        currentWorkingDirectory: String,
        cwdLookup: (Int32) -> String?
    ) -> Bool {
        guard process.parentPID == 1,
              process.pid != Int32(ProcessInfo.processInfo.processIdentifier),
              process.command.contains("-m triplet_capture.app") else {
            return false
        }

        if repoHints.contains(where: { !$0.isEmpty && process.command.contains($0) }) {
            return true
        }

        guard let processCwd = cwdLookup(process.pid) else { return false }
        let current = URL(fileURLWithPath: currentWorkingDirectory).standardizedFileURL.path
        let discovered = URL(fileURLWithPath: processCwd).standardizedFileURL.path
        return discovered == current
    }

    private static func cwdForProcess(pid: Int32) -> String? {
        let output = processOutput(
            executable: "/usr/sbin/lsof",
            arguments: ["-a", "-p", "\(pid)", "-d", "cwd", "-Fn"]
        )
        return output
            .split(separator: "\n")
            .first { $0.hasPrefix("n") }
            .map { String($0.dropFirst()) }
    }

    internal static func processOutput(executable: String, arguments: [String]) -> String {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: executable)
        proc.arguments = arguments
        let pipe = Pipe()
        proc.standardOutput = pipe
        proc.standardError = Pipe()
        do {
            try proc.run()
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            proc.waitUntilExit()
            return String(data: data, encoding: .utf8) ?? ""
        } catch {
            return ""
        }
    }

    // MARK: - Private helpers

    internal func settingsWithResolvedSonyIP(_ settings: ScanSettings) -> ScanSettings {
        guard !settings.usesSonyUSB else { return settings }
        guard let mac = settings.sonyMacAddress?.trimmingCharacters(in: .whitespacesAndNewlines),
              !mac.isEmpty,
              let resolvedIP = Self.resolveSonyIPFromARP(macAddress: mac),
              !resolvedIP.isEmpty else {
            return settings
        }

        var copy = settings
        copy.sonyIpAddress = resolvedIP
        return copy
    }

    internal static func resolveSonyIPFromARP(macAddress: String) -> String? {
        let normalizedMAC = normalizeMAC(macAddress)
        guard !normalizedMAC.isEmpty else { return nil }

        let table = sonyARPTableProvider()
        for rawLine in table.split(whereSeparator: \.isNewline) {
            let line = String(rawLine)
            guard normalizeMAC(line).contains(normalizedMAC),
                  let open = line.firstIndex(of: "("),
                  let close = line[open...].firstIndex(of: ")") else {
                continue
            }
            let start = line.index(after: open)
            let ip = String(line[start..<close]).trimmingCharacters(in: .whitespacesAndNewlines)
            if !ip.isEmpty {
                return ip
            }
        }
        return nil
    }

    private static func normalizeMAC(_ text: String) -> String {
        text
            .lowercased()
            .replacingOccurrences(of: "-", with: ":")
            .filter { $0.isHexDigit || $0 == ":" }
    }

    /// Build the argument array for the child process.
    ///
    /// IMPORTANT: This always returns a `[String]` array — NEVER a shell string.
    /// `Process.arguments` does not use a shell, so there is no injection surface (T-05-04).
    ///
    /// `--web-port 0` tells the child to bind an ephemeral OS-assigned port.
    /// `--port-file <path>` tells the child where to write the actual bound port
    /// once listening. Swift reads that file via `waitForPort(file:timeout:)`.
    internal func buildArgs(settings: ScanSettings, portFile: String, readyNonce: String) -> [String] {
        var args: [String] = [
            "--roll-name",      settings.rollName,
            "--output-folder",  settings.outputFolder,  // BASE folder; Python appends /rollName
            "--trigger-mode",   settings.triggerMode,
            "--web-port",       "0",
            "--port-file",      portFile,
            "--ready-nonce",    readyNonce,
            "--no-browser",
        ]
        if let scanlightPort = settings.scanlightPort, !scanlightPort.isEmpty {
            args += ["--port", scanlightPort]
        }
        if let inbox = settings.iedInbox {
            args += ["--ied-inbox", inbox]
        }
        if settings.triggerMode == "sdk" {
            args += ["--capture-timeout-s", String(Int(Self.sdkBackendCaptureTimeoutS))]
            if let sonyCapture = settings.sonyCapturePath, !sonyCapture.isEmpty {
                args += ["--sony-capture", sonyCapture]
            } else if let resolved = try? PythonToolLocator.resolve("sony-capture") {
                args += ["--sony-capture", resolved.path]
            }
            if !settings.usesSonyUSB, let ip = settings.sonyIpAddress, !ip.isEmpty {
                args += ["--sony-ip-address", ip]
            }
            if !settings.usesSonyUSB, let mac = settings.sonyMacAddress, !mac.isEmpty {
                args += ["--sony-mac-address", mac]
            }
            if let user = settings.sonyUser, !user.isEmpty {
                args += ["--sony-user", user]
            }
            if let password = settings.sonyPassword, !password.isEmpty {
                args += ["--sony-password", password]
            }
            args += ["--sony-iso", "100or125"]
            if let shutterR = settings.shutterR, !shutterR.isEmpty {
                args += ["--shutter-r", shutterR]
            }
            if let shutterG = settings.shutterG, !shutterG.isEmpty {
                args += ["--shutter-g", shutterG]
            }
            if let shutterB = settings.shutterB, !shutterB.isEmpty {
                args += ["--shutter-b", shutterB]
            }
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
        if let positiveProfileJSON = settings.positiveProfileJSON,
           !positiveProfileJSON.isEmpty {
            args += ["--positive-profile-json", positiveProfileJSON]
        }
        args += ["--composite-format", settings.compositeFormat]
        return args
    }

    internal func buildSonyConnectionProbeCommand(
        settings: ScanSettings,
        timeoutSeconds: Int
    ) throws -> (executableURL: URL, arguments: [String], environment: [String: String]) {
        let executableURL: URL
        if let sonyCapturePath = settings.sonyCapturePath, !sonyCapturePath.isEmpty {
            executableURL = URL(fileURLWithPath: sonyCapturePath)
        } else {
            executableURL = try PythonToolLocator.resolve("sony-capture")
        }

        var args = [
            "--connect-only",
            "--timeout", "\(timeoutSeconds)",
        ]
        if !settings.usesSonyUSB, let ip = settings.sonyIpAddress, !ip.isEmpty {
            args += ["--ip-address", ip]
        }
        if !settings.usesSonyUSB, let mac = settings.sonyMacAddress, !mac.isEmpty {
            args += ["--mac-address", mac]
        }
        return (executableURL, args, Self.sonyCaptureEnvironment(for: settings))
    }

    internal func buildSonyLiveViewFrameCommand(
        settings: ScanSettings,
        outputURL: URL,
        timeoutSeconds: Int
    ) throws -> (executableURL: URL, arguments: [String], environment: [String: String]) {
        let executableURL: URL
        if let sonyCapturePath = settings.sonyCapturePath, !sonyCapturePath.isEmpty {
            executableURL = URL(fileURLWithPath: sonyCapturePath)
        } else {
            executableURL = try PythonToolLocator.resolve("sony-capture")
        }

        var args = [
            "--live-view-out", outputURL.path,
            "--timeout", "\(timeoutSeconds)",
        ]
        if !settings.usesSonyUSB, let ip = settings.sonyIpAddress, !ip.isEmpty {
            args += ["--ip-address", ip]
        }
        if !settings.usesSonyUSB, let mac = settings.sonyMacAddress, !mac.isEmpty {
            args += ["--mac-address", mac]
        }
        return (executableURL, args, Self.sonyCaptureEnvironment(for: settings))
    }

    internal func buildSonyLiveViewStreamCommand(
        settings: ScanSettings,
        outputURL: URL,
        intervalMs: Int,
        timeoutSeconds: Int
    ) throws -> (executableURL: URL, arguments: [String], environment: [String: String]) {
        let executableURL: URL
        if let sonyCapturePath = settings.sonyCapturePath, !sonyCapturePath.isEmpty {
            executableURL = URL(fileURLWithPath: sonyCapturePath)
        } else {
            executableURL = try PythonToolLocator.resolve("sony-capture")
        }

        var args = [
            "--live-view-stream-out", outputURL.path,
            "--live-view-interval-ms", "\(intervalMs)",
            "--timeout", "\(timeoutSeconds)",
        ]
        if !settings.usesSonyUSB, let ip = settings.sonyIpAddress, !ip.isEmpty {
            args += ["--ip-address", ip]
        }
        if !settings.usesSonyUSB, let mac = settings.sonyMacAddress, !mac.isEmpty {
            args += ["--mac-address", mac]
        }
        return (executableURL, args, Self.sonyCaptureEnvironment(for: settings))
    }

    internal struct SonyProbeProcessResult {
        var exitCode: Int32
        var stdout: String
        var stderr: String
        var timedOut: Bool
    }

    private enum SonyProbeWaitResult {
        case exited(Int32)
        case timedOut
        case cancelled
    }

    /// Spawn `sony-capture` and capture its stdout/stderr without blocking
    /// MainActor or the calling task.
    ///
    /// `nonisolated` so the synchronous `Process` and `Pipe` machinery runs on
    /// the cooperative pool, not on MainActor — a blocking pipe read here used
    /// to freeze the UI ("Checking…" stuck forever).
    ///
    /// Pipes are drained INCREMENTALLY via `availableData` polling on detached
    /// tasks. We never call `readDataToEndOfFile()`: on Darwin the parent's
    /// write end of a `Pipe` may stay open after the child exits (or a Sony
    /// SDK background thread may keep it alive), which makes that call block
    /// forever even though `ps` shows no child process. Closing
    /// `fileHandleForWriting` after spawn is a belt-and-braces step.
    internal nonisolated static func runSonyProbeProcess(
        executableURL: URL,
        arguments: [String],
        environment: [String: String]? = nil,
        timeout: TimeInterval
    ) async throws -> SonyProbeProcessResult {
        let proc = Process()
        proc.executableURL = executableURL
        proc.arguments = arguments
        proc.environment = environment ?? ProcessInfo.processInfo.environment

        let stdoutPipe = Pipe()
        let stderrPipe = Pipe()
        proc.standardOutput = stdoutPipe
        proc.standardError = stderrPipe

        try proc.run()

        // After fork+exec the child has its own dup'd write FDs. Close our
        // parent-side copies so the readers can see EOF as soon as the child
        // (and only the child) closes its end.
        try? stdoutPipe.fileHandleForWriting.close()
        try? stderrPipe.fileHandleForWriting.close()

        let stdoutCollector = PipeDrainer()
        let stderrCollector = PipeDrainer()
        let stdoutTask = Task.detached(priority: .userInitiated) {
            stdoutCollector.drain(handle: stdoutPipe.fileHandleForReading)
        }
        let stderrTask = Task.detached(priority: .userInitiated) {
            stderrCollector.drain(handle: stderrPipe.fileHandleForReading)
        }

        let waitResult = await withTaskGroup(of: SonyProbeWaitResult.self) { group in
            group.addTask {
                while proc.isRunning {
                    if Task.isCancelled {
                        return .cancelled
                    }
                    try? await Task.sleep(nanoseconds: 50_000_000)
                }
                return .exited(proc.terminationStatus)
            }
            group.addTask {
                let ns = UInt64(max(timeout, 0.1) * 1_000_000_000)
                try? await Task.sleep(nanoseconds: ns)
                if Task.isCancelled {
                    return .cancelled
                }
                if proc.isRunning {
                    proc.terminate()
                    return .timedOut
                }
                return .exited(proc.terminationStatus)
            }

            let first = await group.next() ?? .timedOut
            group.cancelAll()
            return first
        }

        if case .timedOut = waitResult, proc.isRunning {
            await terminateSonyProbeProcess(proc)
        } else if case .cancelled = waitResult, proc.isRunning {
            await terminateSonyProbeProcess(proc)
        }

        // Give the drainers a short grace window after the child exits to
        // collect anything still in flight, then stop them. We never await
        // them unbounded: a stuck pipe (e.g. a Sony SDK background thread
        // that inherited the FD) must not pin this task.
        let collectionDeadline = Date(timeIntervalSinceNow: 1.0)
        while !(stdoutCollector.isFinished && stderrCollector.isFinished),
              Date() < collectionDeadline,
              !Task.isCancelled {
            try? await Task.sleep(nanoseconds: 50_000_000)
        }
        stdoutTask.cancel()
        stderrTask.cancel()
        // Closing the read end interrupts any pending availableData read in
        // the drainer tasks (they exit their while-loop on the next iter).
        try? stdoutPipe.fileHandleForReading.close()
        try? stderrPipe.fileHandleForReading.close()

        let stdout = stdoutCollector.collectedString
        let stderr = stderrCollector.collectedString

        switch waitResult {
        case .exited(let code):
            return SonyProbeProcessResult(exitCode: code, stdout: stdout, stderr: stderr, timedOut: false)
        case .timedOut:
            let code: Int32 = proc.isRunning ? -1 : proc.terminationStatus
            return SonyProbeProcessResult(exitCode: code, stdout: stdout, stderr: stderr, timedOut: true)
        case .cancelled:
            let code: Int32 = proc.isRunning ? -1 : proc.terminationStatus
            return SonyProbeProcessResult(exitCode: code, stdout: stdout, stderr: stderr, timedOut: false)
        }
    }

    private nonisolated static func terminateSonyProbeProcess(_ proc: Process) async {
        if proc.isRunning {
            proc.terminate()
        }

        let terminateDeadline = Date(timeIntervalSinceNow: 1.0)
        while proc.isRunning && Date() < terminateDeadline {
            try? await Task.sleep(nanoseconds: 50_000_000)
        }

        if proc.isRunning {
            kill(proc.processIdentifier, SIGKILL)
        }

        let killDeadline = Date(timeIntervalSinceNow: 1.0)
        while proc.isRunning && Date() < killDeadline {
            try? await Task.sleep(nanoseconds: 50_000_000)
        }
    }

    private static func conciseProcessOutput(stdout: String, stderr: String) -> String {
        let combined = [stdout, stderr]
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
            .joined(separator: " ")
        guard combined.count > 240 else { return combined }
        let suffix = combined.suffix(240)
        return "...\(suffix)"
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
