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
//   start(settings:) → PythonToolLocator.resolve → findFreePort → Process.run
//                    → waitForReady (polls /api/state every 200ms, 10s timeout)
//   stop()           → process.interrupt (SIGTERM) → 3s grace → SIGKILL

import Combine
import Darwin
import Foundation

// MARK: - Errors

enum OrchestratorError: Error {
    /// Process started but never responded to /api/state within the timeout.
    case startupTimeout(stderr: String)
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

    // MARK: Init

    init(session: URLSession = .shared) {
        self.session = session
    }

    // MARK: - Public API

    /// Spawn `triplet-capture` with the given settings and wait for it to be ready.
    ///
    /// - Resolves the tool URL via `PythonToolLocator`.
    /// - Picks a free localhost port via POSIX `bind(port:0)`.
    /// - Builds a `[String]` argument array (never a shell string — T-05-04).
    /// - Starts the process and polls `GET /api/state` until HTTP 200 (or timeout).
    func start(settings: ScanSettings) async throws {
        // 1. Locate the tool
        let toolURL: URL
        do {
            toolURL = try PythonToolLocator.resolve("triplet-capture")
        } catch PythonToolLocatorError.toolNotFound(let msg) {
            throw OrchestratorError.toolNotFound(msg)
        }

        // 2. Free port
        let port = findFreePort(fallback: 8765)

        // 3. Build process
        let proc = Process()
        proc.executableURL = toolURL
        proc.arguments = buildArgs(settings: settings, port: port)
        proc.environment = ProcessInfo.processInfo.environment

        let stderrPipe = Pipe()
        proc.standardError = stderrPipe

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
                self?.stderrData = data
            }
        }

        // 6. Wait for readiness (will throw OrchestratorError.startupTimeout on failure)
        try await waitForReady(port: port, timeout: 10.0)

        // 7. Mark as running
        isRunning = true
        webPort = port
        lastError = ""
    }

    /// Send SIGTERM to the child process and wait up to 3 seconds for graceful
    /// shutdown. Falls back to SIGKILL if still running.
    func stop() async {
        guard let proc = process, proc.isRunning else { return }
        proc.interrupt()   // sends SIGTERM on Darwin
        let deadline = Date(timeIntervalSinceNow: 3.0)
        while proc.isRunning && Date() < deadline {
            try? await Task.sleep(nanoseconds: 100_000_000)  // 100ms
        }
        if proc.isRunning {
            kill(proc.processIdentifier, SIGKILL)
            proc.waitUntilExit()
        }
        isRunning = false
        process = nil
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
    func updateSettings(_ settings: ScanSettings) async throws {
        let url = URL(string: "http://127.0.0.1:\(webPort)/api/settings")!
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        request.httpBody = try encoder.encode(settings)
        let (_, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw OrchestratorError.invalidResponse(statusCode: 0)
        }
        guard http.statusCode == 200 else {
            throw OrchestratorError.invalidResponse(statusCode: http.statusCode)
        }
    }

    /// Trigger a triplet capture.
    ///
    /// `retake` is appended as a URL query parameter (`?retake=true`/`?retake=false`),
    /// NOT as a JSON body field. The Python route reads `request.args.get("retake")`.
    func captureFrame(retake: Bool) async throws -> TripletOutcome {
        let url = URL(string: "http://127.0.0.1:\(webPort)/api/capture?retake=\(retake)")!
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        // Empty body — retake is a query param only
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw OrchestratorError.invalidResponse(statusCode: 0)
        }
        // Accept 200 (success) and 500 (failure — TripletOutcome.success == false)
        guard http.statusCode == 200 || http.statusCode == 500 else {
            throw OrchestratorError.invalidResponse(statusCode: http.statusCode)
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
    internal func waitForReady(port: Int, timeout: TimeInterval) async throws {
        let url = URL(string: "http://127.0.0.1:\(port)/api/state")!
        let deadline = Date(timeIntervalSinceNow: timeout)
        while Date() < deadline {
            do {
                let (_, response) = try await session.data(from: url)
                if (response as? HTTPURLResponse)?.statusCode == 200 { return }
            } catch let err as URLError where err.code == .cannotConnectToHost {
                // Normal during startup — retry silently
            }
            try? await Task.sleep(nanoseconds: 200_000_000)  // 200ms
        }
        // Timeout: capture any accumulated stderr
        let stderrText = String(data: stderrData, encoding: .utf8) ?? ""
        throw OrchestratorError.startupTimeout(stderr: stderrText)
    }

    // MARK: - Private helpers

    /// Build the argument array for the child process.
    ///
    /// IMPORTANT: This always returns a `[String]` array — NEVER a shell string.
    /// `Process.arguments` does not use a shell, so there is no injection surface (T-05-04).
    private func buildArgs(settings: ScanSettings, port: Int) -> [String] {
        var args: [String] = [
            "--roll-name",      settings.rollName,
            "--output-folder",  settings.outputFolder,  // BASE folder; Python appends /rollName
            "--trigger-mode",   settings.triggerMode,
            "--web-port",       "\(port)",
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

    /// Select a free localhost port using POSIX `bind(port: 0)` + `getsockname`.
    ///
    /// Falls back to `fallback` on any socket error.
    private func findFreePort(fallback: Int) -> Int {
        let s = socket(AF_INET, SOCK_STREAM, 0)
        guard s >= 0 else { return fallback }
        defer { close(s) }
        var addr = sockaddr_in()
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = 0
        addr.sin_addr.s_addr = INADDR_LOOPBACK.bigEndian
        let bindResult = withUnsafeMutablePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                bind(s, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        guard bindResult == 0 else { return fallback }
        var len = socklen_t(MemoryLayout<sockaddr_in>.size)
        _ = withUnsafeMutablePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                getsockname(s, $0, &len)
            }
        }
        let assignedPort = Int(UInt16(bigEndian: addr.sin_port))
        return assignedPort > 0 ? assignedPort : fallback
    }
}
