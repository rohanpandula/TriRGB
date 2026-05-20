// OrchestratorClientTests — URLProtocol-mocked unit tests for OrchestratorClient
// and PythonToolLocator.
//
// Test strategy: URLSession injection via URLProtocol stub (StubURLProtocol).
// No real ports are bound. No child processes are spawned.
// All tests that need HTTP responses configure StubURLProtocol.routes before
// calling client methods.
//
// To run the live opt-in test:
//   SCANLIGHT_LIVE_ORCH=1 swift test --package-path phase3/FilmScanner \
//       --filter testLiveIntegrationWithRealOrchestrator

import Foundation
import XCTest
@testable import ScanlightApp

// MARK: - StubURLProtocol

/// URLProtocol subclass that intercepts all requests in a test-scoped URLSession.
///
/// Configure routes before making requests:
///   StubURLProtocol.routes["/api/state"] = (jsonData, 200)
///
/// The intercepted request (including httpBody) is available in:
///   StubURLProtocol.lastRequest   — the URLRequest
///   StubURLProtocol.lastBody      — the body Data (drained from httpBodyStream)
final class StubURLProtocol: URLProtocol {

    /// Map from URL path to (response data, HTTP status code).
    static var routes: [String: (Data, Int)] = [:]

    /// The most recently intercepted URLRequest.
    static var lastRequest: URLRequest? = nil

    /// The body data from the most recently intercepted request.
    static var lastBody: Data? = nil

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        // Record the request
        Self.lastRequest = request

        // Drain body stream into Data for test inspection
        if let bodyStream = request.httpBodyStream {
            var bodyData = Data()
            bodyStream.open()
            let bufferSize = 4096
            var buffer = [UInt8](repeating: 0, count: bufferSize)
            while bodyStream.hasBytesAvailable {
                let read = bodyStream.read(&buffer, maxLength: bufferSize)
                if read > 0 {
                    bodyData.append(contentsOf: buffer.prefix(read))
                }
            }
            bodyStream.close()
            Self.lastBody = bodyData.isEmpty ? nil : bodyData
        } else if let bodyData = request.httpBody {
            Self.lastBody = bodyData
        } else {
            Self.lastBody = nil
        }

        // Look up the stub response
        let path = request.url?.path ?? ""
        let (data, status) = Self.routes[path] ?? (Data(), 404)

        let response = HTTPURLResponse(
            url: request.url!,
            statusCode: status,
            httpVersion: nil,
            headerFields: nil
        )!

        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}

// MARK: - Helpers

/// Returns a URLSession that routes all requests through StubURLProtocol.
private func makeStubSession() -> URLSession {
    let config = URLSessionConfiguration.ephemeral
    config.protocolClasses = [StubURLProtocol.self]
    return URLSession(configuration: config)
}

/// Returns Data encoding a valid OrchestratorState JSON object.
private func makeStateJSON() -> Data {
    let json = """
    {
        "roll_name": "Roll001",
        "frame_number": 1,
        "output_folder": "/tmp/scans/Roll001",
        "level_r": 200,
        "level_g": 200,
        "level_b": 200,
        "settle_ms": 50
    }
    """
    return json.data(using: .utf8)!
}

/// Returns Data encoding a valid TripletOutcome JSON object.
private func makeTripletJSON(success: Bool = true, frameNumber: Int = 1, nextFrame: Int = 2) -> Data {
    let successStatus = success ? 200 : 500
    _ = successStatus  // statusCode is set by the route, not embedded in body
    let json = """
    {
        "success": \(success),
        "frame_number": \(frameNumber),
        "files": {},
        "error": null,
        "duration_s": 0.1,
        "next_frame": \(nextFrame)
    }
    """
    return json.data(using: .utf8)!
}

// MARK: - Test Suite

@MainActor
final class OrchestratorClientTests: XCTestCase {

    override func setUpWithError() throws {
        continueAfterFailure = false
        StubURLProtocol.routes = [:]
        StubURLProtocol.lastRequest = nil
        StubURLProtocol.lastBody = nil
    }

    // MARK: - Test 1: State decoding

    func testFetchStateDecodesOrchestratorState() async throws {
        StubURLProtocol.routes["/api/state"] = (makeStateJSON(), 200)

        let client = OrchestratorClient(session: makeStubSession())
        client.webPort = 9999

        let state = try await client.fetchState()

        XCTAssertEqual(state.rollName, "Roll001")
        XCTAssertEqual(state.frameNumber, 1)
        XCTAssertEqual(state.outputFolder, "/tmp/scans/Roll001")
        XCTAssertEqual(state.levelR, 200)
        XCTAssertEqual(state.settleMs, 50)
    }

    // MARK: - Test 2: captureFrame uses query param

    func testCaptureFrameUsesQueryParam() async throws {
        StubURLProtocol.routes["/api/capture"] = (makeTripletJSON(), 200)

        let client = OrchestratorClient(session: makeStubSession())
        client.webPort = 9999

        let outcome = try await client.captureFrame(retake: true)

        // Verify query param is present in the intercepted URL
        XCTAssertTrue(
            StubURLProtocol.lastRequest?.url?.query?.contains("retake=true") ?? false,
            "Expected retake=true in URL query, got: \(StubURLProtocol.lastRequest?.url?.absoluteString ?? "nil")"
        )
        XCTAssertTrue(outcome.success)
        XCTAssertEqual(outcome.nextFrame, 2)
    }

    // MARK: - Test 3: CompositeStatus enabled=true

    func testCompositeStatusDecodesEnabledTrue() async throws {
        let json = """
        {
            "enabled": true,
            "pending": 2,
            "results": [
                {
                    "frame_number": 1,
                    "status": "done",
                    "output_path": "/tmp/out.dng",
                    "error": null
                }
            ]
        }
        """.data(using: .utf8)!
        StubURLProtocol.routes["/api/composite-status"] = (json, 200)

        let client = OrchestratorClient(session: makeStubSession())
        client.webPort = 9999

        let status = try await client.compositeStatus()

        XCTAssertTrue(status.enabled)
        XCTAssertEqual(status.pending, 2)
        XCTAssertEqual(status.results?.count, 1)
        XCTAssertEqual(status.results?.first?.status, "done")
        XCTAssertEqual(status.results?.first?.outputPath, "/tmp/out.dng")
    }

    // MARK: - Test 4: CompositeStatus enabled=false

    func testCompositeStatusDecodesEnabledFalse() async throws {
        let json = """
        {"enabled": false}
        """.data(using: .utf8)!
        StubURLProtocol.routes["/api/composite-status"] = (json, 200)

        let client = OrchestratorClient(session: makeStubSession())
        client.webPort = 9999

        let status = try await client.compositeStatus()

        XCTAssertFalse(status.enabled)
        XCTAssertNil(status.pending)
        XCTAssertNil(status.results)
    }

    // MARK: - Test 5: Tool not found error

    func testStartThrowsWhenToolMissing() async throws {
        // Directly test PythonToolLocator with a name that cannot possibly exist
        do {
            _ = try PythonToolLocator.resolve("_nonexistent_tool_xyz_abc_123_")
            XCTFail("Expected PythonToolLocatorError.toolNotFound to be thrown")
        } catch PythonToolLocatorError.toolNotFound(let msg) {
            XCTAssertTrue(
                msg.contains("pip install"),
                "Error message should contain 'pip install', got: '\(msg)'"
            )
            XCTAssertTrue(
                msg.contains("_nonexistent_tool_xyz_abc_123_"),
                "Error message should name the tool, got: '\(msg)'"
            )
        }
    }

    // MARK: - Test 6: Locator finds installed tool

    func testLocatorFindsInstalledTool() throws {
        guard ProcessInfo.processInfo.environment["PATH"] != nil else {
            throw XCTSkip("no PATH in environment")
        }
        // `swift` is always on PATH when this test is run via `swift test`
        let url = try PythonToolLocator.resolve("swift")
        XCTAssertTrue(
            url.path.contains("swift"),
            "Expected URL path to contain 'swift', got: \(url.path)"
        )
        XCTAssertTrue(
            FileManager.default.isExecutableFile(atPath: url.path),
            "Resolved path should be executable: \(url.path)"
        )
    }

    // MARK: - Test 7: Startup timeout throws with stderr

    /// Exercises the readiness poll: when /api/state always returns HTTP 503,
    /// waitForReady must throw OrchestratorError.startupTimeout after the timeout.
    ///
    /// Uses waitForReady(port:timeout:) directly (possible because it is `internal`).
    /// timeout: 0.5s so the test completes well within 2s wall-clock.
    func testStartupTimeoutThrowsWithStderr() async throws {
        // Every GET /api/state returns 503 — server never becomes healthy
        StubURLProtocol.routes["/api/state"] = (Data(), 503)

        let client = OrchestratorClient(session: makeStubSession())
        client.webPort = 9999

        do {
            try await client.waitForReady(port: 9999, timeout: 0.5)
            XCTFail("Expected OrchestratorError.startupTimeout to be thrown")
        } catch OrchestratorError.startupTimeout(let stderr) {
            // Pass: timeout fired correctly.
            // stderr may be empty (no real process), but the associated value
            // is always a String (never nil).
            XCTAssertNotNil(stderr as String?,
                            "startupTimeout.stderr should be a non-nil String")
        } catch {
            XCTFail("Wrong error type thrown: \(error)")
        }
    }

    // MARK: - Test 8: updateSettings serializes snake_case body

    /// Verifies that ScanSettings fields are encoded with snake_case keys
    /// (via .convertToSnakeCase) when POST /api/settings is called.
    func testUpdateSettingsSerializesBody() async throws {
        StubURLProtocol.routes["/api/settings"] = (makeStateJSON(), 200)

        let client = OrchestratorClient(session: makeStubSession())
        client.webPort = 9999

        let settings = ScanSettings(
            rollName: "Roll002",
            outputFolder: "/Volumes/Scans",
            triggerMode: "sdk",
            iedInbox: nil,
            streamComposite: false,
            ffcCalibration: nil,
            cameraModel: nil,
            compositeFormat: "dng",
            levelR: 210,
            levelG: 195,
            levelB: 180,
            settleMs: 75
        )

        try await client.updateSettings(settings)

        // Decode the intercepted request body
        guard let bodyData = StubURLProtocol.lastBody else {
            XCTFail("No body was recorded by StubURLProtocol")
            return
        }

        let decoded = try JSONSerialization.jsonObject(with: bodyData) as? [String: Any]
        guard let decoded = decoded else {
            XCTFail("Could not decode body as [String: Any]")
            return
        }

        // Assert snake_case keys and correct values
        XCTAssertEqual(decoded["roll_name"] as? String, "Roll002",
                       "Expected roll_name = Roll002, got: \(decoded["roll_name"] ?? "nil")")
        XCTAssertEqual(decoded["level_r"] as? Int, 210,
                       "Expected level_r = 210, got: \(decoded["level_r"] ?? "nil")")
        XCTAssertEqual(decoded["level_g"] as? Int, 195,
                       "Expected level_g = 195, got: \(decoded["level_g"] ?? "nil")")
        XCTAssertEqual(decoded["level_b"] as? Int, 180,
                       "Expected level_b = 180, got: \(decoded["level_b"] ?? "nil")")
        XCTAssertEqual(decoded["settle_ms"] as? Int, 75,
                       "Expected settle_ms = 75, got: \(decoded["settle_ms"] ?? "nil")")
        XCTAssertEqual(decoded["output_folder"] as? String, "/Volumes/Scans",
                       "Expected output_folder = /Volumes/Scans")
        XCTAssertEqual(decoded["trigger_mode"] as? String, "sdk",
                       "Expected trigger_mode = sdk")
    }

    // MARK: - Test 9: Live integration (opt-in)

    /// Full process lifecycle test. Requires the real orchestrator to be installed.
    ///
    /// Set `SCANLIGHT_LIVE_ORCH=1` in the environment to enable.
    func testLiveIntegrationWithRealOrchestrator() async throws {
        guard ProcessInfo.processInfo.environment["SCANLIGHT_LIVE_ORCH"] == "1" else {
            throw XCTSkip("SCANLIGHT_LIVE_ORCH not set — skipping live integration test")
        }

        // Use the default (non-stub) session for real HTTP
        let client = OrchestratorClient()

        let settings = ScanSettings(
            rollName: "LiveTest",
            outputFolder: NSTemporaryDirectory(),
            triggerMode: "sdk",
            iedInbox: nil,
            streamComposite: false,
            ffcCalibration: nil,
            cameraModel: nil,
            compositeFormat: "dng",
            levelR: 200,
            levelG: 200,
            levelB: 200,
            settleMs: 50
        )

        try await client.start(settings: settings)
        defer { Task { await client.stop() } }

        let state = try await client.fetchState()
        XCTAssertEqual(state.rollName, "LiveTest")
        XCTAssertTrue(client.isRunning)
    }

    // MARK: - Test 10: waitForReady fails fast when the child exits during startup

    /// Regression: a child that exits early (bad args / missing dep / import error)
    /// must make waitForReady throw IMMEDIATELY via startupFailed — not poll the
    /// full timeout. Simulates the child-dead signal the termination handler sets.
    func testWaitForReadyFailsFastWhenChildExits() async throws {
        // No /api/state route → the stub would 404; but the childExitStatus check
        // at the top of the poll loop must short-circuit before any HTTP anyway.
        let client = OrchestratorClient(session: makeStubSession())
        client.webPort = 9999
        client.childExitStatus = 137  // e.g. SIGKILL-style exit

        let started = Date()
        do {
            // 10s timeout — but it must NOT take anywhere near that long.
            try await client.waitForReady(port: 9999, timeout: 10.0)
            XCTFail("Expected OrchestratorError.startupFailed to be thrown")
        } catch OrchestratorError.startupFailed(let exitCode, _) {
            XCTAssertEqual(exitCode, 137, "Expected the child's exit code to surface")
        } catch {
            XCTFail("Wrong error type thrown: \(error)")
        }
        let elapsed = Date().timeIntervalSince(started)
        XCTAssertLessThan(elapsed, 1.0,
                          "waitForReady must fail fast on child exit, not poll the full timeout (took \(elapsed)s)")
    }

    // MARK: - Test 11: termination handler clears isRunning when the child dies

    /// Regression: a mid-session child crash must flip isRunning back to false so
    /// the Phase 7 state machine never reads stale port-ownership state. Uses a
    /// real, instantly-exiting process (`/usr/bin/true`) and the internal
    /// installTerminationHandler seam — no orchestrator required.
    func testTerminationHandlerClearsIsRunningOnChildExit() async throws {
        let truePath = "/usr/bin/true"
        guard FileManager.default.isExecutableFile(atPath: truePath) else {
            throw XCTSkip("\(truePath) not available")
        }

        let client = OrchestratorClient(session: makeStubSession())
        client.isRunning = true  // pretend a successful start()

        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: truePath)
        client.installTerminationHandler(on: proc)
        try proc.run()

        // Wait up to 2s for the process to exit and the handler to hop to MainActor.
        let deadline = Date(timeIntervalSinceNow: 2.0)
        while client.isRunning && Date() < deadline {
            try await Task.sleep(nanoseconds: 20_000_000)  // 20ms
        }

        XCTAssertFalse(client.isRunning,
                       "terminationHandler must clear isRunning when the child exits")
        XCTAssertNotNil(client.childExitStatus,
                        "terminationHandler must record the child's exit status")
    }
}
