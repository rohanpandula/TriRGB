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

private func containsAdjacent(_ values: [String], _ pair: [String]) -> Bool {
    guard pair.count == 2, values.count >= 2 else { return false }
    for idx in 0..<(values.count - 1) {
        if values[idx] == pair[0], values[idx + 1] == pair[1] {
            return true
        }
    }
    return false
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

private func makeExposureJSONData(ledR: Int = 180, ledG: Int = 160, ledB: Int = 230) -> Data {
    let json = """
    {
        "r": {"channel":"R","led_level":\(ledR),"black_level":256,"gain":1,"clip_fraction":0,"p99":55000,"target":55700,"exposure_status":"target","schema_version":1},
        "g": {"channel":"G","led_level":\(ledG),"black_level":256,"gain":1,"clip_fraction":0,"p99":55000,"target":55700,"exposure_status":"target","schema_version":1},
        "b": {"channel":"B","led_level":\(ledB),"black_level":256,"gain":1,"clip_fraction":0,"p99":55000,"target":55700,"exposure_status":"target","schema_version":1},
        "base_region": {"x":10,"y":20,"w":128,"h":128,"base_rgb":[1,1,1],"uniformity_cv":1,"source":"manual","schema_version":1},
        "ffc_cal_dir": "",
        "schema_version": 1
    }
    """
    return json.data(using: .utf8)!
}

private func makeExposureSeed() -> ExposureCalibrationResult {
    ExposureCalibrationResult(
        r: WizardChannelCalibration(
            channel: "R",
            ledLevel: 180,
            blackLevel: 256,
            gain: 1,
            clipFraction: 0,
            shutterSpeed: "1/40",
            p99: 55000,
            target: 55700,
            exposureStatus: "target",
            schemaVersion: 1
        ),
        g: WizardChannelCalibration(
            channel: "G",
            ledLevel: 190,
            blackLevel: 256,
            gain: 1,
            clipFraction: 0,
            shutterSpeed: "1/30",
            p99: 55000,
            target: 55700,
            exposureStatus: "target",
            schemaVersion: 1
        ),
        b: WizardChannelCalibration(
            channel: "B",
            ledLevel: 220,
            blackLevel: 256,
            gain: 1,
            clipFraction: 0,
            shutterSpeed: "1/20",
            p99: 55000,
            target: 55700,
            exposureStatus: "target",
            schemaVersion: 1
        ),
        baseRegion: WizardBaseRegion(
            x: 10,
            y: 20,
            w: 128,
            h: 128,
            baseRgb: [1, 1, 1],
            uniformityCv: 1,
            source: "manual",
            schemaVersion: 1
        ),
        ffcCalDir: "",
        schemaVersion: 1
    )
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

    // MARK: - Error copy

    func testOrchestratorErrorLocalizedDescriptionIncludesStartupDiagnosticsAndRedactsSecrets() {
        let stderr = """
        Traceback (most recent call last):
        command: sony-capture --sony-user 6SCzVb --sony-password D8MM1Ktc
        ModuleNotFoundError: No module named 'rawpy'
        """

        let message = OrchestratorError.startupFailed(exitCode: 1, stderr: stderr).localizedDescription

        XCTAssertTrue(message.contains("exit 1"))
        XCTAssertTrue(message.contains("ModuleNotFoundError"))
        XCTAssertTrue(message.contains("<redacted>"))
        XCTAssertFalse(message.contains("6SCzVb"))
        XCTAssertFalse(message.contains("D8MM1Ktc"))
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

    func testLocatorPrefersCheckoutTripletCaptureOverInstalledPath() throws {
        let url = try PythonToolLocator.resolve("triplet-capture")
        XCTAssertTrue(
            url.path.hasSuffix("/scripts/triplet-capture"),
            "Expected checkout wrapper, got: \(url.path)"
        )
        XCTAssertTrue(
            FileManager.default.isExecutableFile(atPath: url.path),
            "Resolved path should be executable: \(url.path)"
        )
    }

    func testParseTripletProcessesFindsPythonModuleBackends() {
        let psOutput = """
          111     1 /opt/homebrew/bin/python3 -m triplet_capture.app --roll-name Roll001
          222   100 /bin/zsh -lc rg triplet_capture.app
          333   300 /opt/homebrew/bin/python3 -m other_module
        """

        let processes = OrchestratorClient.parseTripletProcesses(from: psOutput)

        XCTAssertEqual(processes, [
            RunningTripletProcess(
                pid: 111,
                parentPID: 1,
                command: "/opt/homebrew/bin/python3 -m triplet_capture.app --roll-name Roll001"
            ),
        ])
    }

    func testStaleTripletCleanupOnlyTargetsSameCheckoutOrphans() {
        let orphan = RunningTripletProcess(
            pid: 111,
            parentPID: 1,
            command: "/opt/homebrew/bin/python3 -m triplet_capture.app --sony-capture /repo/phase1/sony-capture/build/sony-capture"
        )
        let managedChild = RunningTripletProcess(
            pid: 222,
            parentPID: 999,
            command: "/opt/homebrew/bin/python3 -m triplet_capture.app --sony-capture /repo/phase1/sony-capture/build/sony-capture"
        )
        let unrelatedOrphan = RunningTripletProcess(
            pid: 333,
            parentPID: 1,
            command: "/opt/homebrew/bin/python3 -m triplet_capture.app --roll-name Other"
        )

        XCTAssertTrue(OrchestratorClient.shouldCleanupStaleTripletProcess(
            orphan,
            repoHints: ["/repo"],
            currentWorkingDirectory: "/repo",
            cwdLookup: { _ in nil }
        ))
        XCTAssertFalse(OrchestratorClient.shouldCleanupStaleTripletProcess(
            managedChild,
            repoHints: ["/repo"],
            currentWorkingDirectory: "/repo",
            cwdLookup: { _ in "/repo" }
        ))
        XCTAssertFalse(OrchestratorClient.shouldCleanupStaleTripletProcess(
            unrelatedOrphan,
            repoHints: ["/repo"],
            currentWorkingDirectory: "/repo",
            cwdLookup: { _ in "/other" }
        ))
    }

    func testProcessOutputDrainsLargeStdoutBeforeWaiting() {
        let output = OrchestratorClient.processOutput(
            executable: "/bin/zsh",
            arguments: ["-c", "for i in {1..20000}; do echo line; done"]
        )

        XCTAssertGreaterThan(output.count, 80_000)
        XCTAssertTrue(output.hasSuffix("line\n"))
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
            try await client.waitForReady(port: 9999, timeout: 0.5, expectedNonce: "test-nonce")
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
        do {
            let state = try await client.fetchState()
            XCTAssertEqual(state.rollName, "LiveTest")
            XCTAssertTrue(client.isRunning)
            await client.stop()
        } catch {
            await client.stop()
            throw error
        }
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
            try await client.waitForReady(port: 9999, timeout: 10.0, expectedNonce: "test-nonce")
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
        client.installTerminationHandler(on: proc, generation: client.launchGeneration)
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

    // MARK: - Test 12: a stale-generation termination callback is ignored

    /// Regression: a late termination callback from a prior (superseded) launch
    /// must NOT clobber the current launch's state. Installs a handler tagged with
    /// an old generation, then bumps the client's current generation; when the
    /// process exits, the stale callback must be a no-op.
    func testStaleTerminationCallbackIsIgnored() async throws {
        let truePath = "/usr/bin/true"
        guard FileManager.default.isExecutableFile(atPath: truePath) else {
            throw XCTSkip("\(truePath) not available")
        }

        let client = OrchestratorClient(session: makeStubSession())
        client.isRunning = true
        client.launchGeneration = 2  // the "current" launch is generation 2

        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: truePath)
        client.installTerminationHandler(on: proc, generation: 1)  // STALE generation
        try proc.run()

        // Give the (stale) handler ample time to fire and hop to MainActor.
        try await Task.sleep(nanoseconds: 400_000_000)  // 400ms

        XCTAssertTrue(client.isRunning,
                      "a stale-generation termination callback must NOT clear isRunning")
        XCTAssertNil(client.childExitStatus,
                     "a stale-generation callback must NOT record an exit status for the current launch")
    }

    // MARK: - Test 13: applyRuntimeSettings sends only levels+settle (not output_folder)

    /// Regression: start()'s post-ready settings push must NOT re-post output_folder
    /// (POST /api/settings treats it as a full path, reverting the spawn's
    /// `<base>/<rollName>`). It must send ONLY the runtime-adjustable fields.
    func testApplyRuntimeSettingsOmitsOutputFolder() async throws {
        StubURLProtocol.routes["/api/settings"] = (makeStateJSON(), 200)

        let client = OrchestratorClient(session: makeStubSession())
        client.webPort = 9999

        let settings = ScanSettings(
            rollName: "Roll003",
            outputFolder: "/Volumes/Scans",
            triggerMode: "sdk",
            iedInbox: nil,
            streamComposite: false,
            ffcCalibration: nil,
            cameraModel: nil,
            compositeFormat: "dng",
            levelR: 211,
            levelG: 199,
            levelB: 188,
            settleMs: 66
        )

        try await client.applyRuntimeSettings(settings)

        guard let bodyData = StubURLProtocol.lastBody else {
            XCTFail("No body was recorded by StubURLProtocol")
            return
        }
        let decoded = try JSONSerialization.jsonObject(with: bodyData) as? [String: Any]
        guard let decoded = decoded else {
            XCTFail("Could not decode body as [String: Any]")
            return
        }

        // The runtime fields are present...
        XCTAssertEqual(decoded["level_r"] as? Int, 211)
        XCTAssertEqual(decoded["level_g"] as? Int, 199)
        XCTAssertEqual(decoded["level_b"] as? Int, 188)
        XCTAssertEqual(decoded["settle_ms"] as? Int, 66)
        // ...and output_folder / roll_name are NOT (so the spawn's roll subfolder survives).
        XCTAssertNil(decoded["output_folder"],
                     "applyRuntimeSettings must NOT post output_folder (would revert the roll subfolder)")
        XCTAssertNil(decoded["roll_name"],
                     "applyRuntimeSettings must NOT post roll_name")
    }

    // MARK: - Test 13b: sdk launch args include Sony Wi-Fi auth

    func testBuildArgsIncludesSonyNetworkAuthForSDKMode() {
        let client = OrchestratorClient()
        let settings = ScanSettings(
            rollName: "RollSDK",
            outputFolder: "/Volumes/Scans",
            scanlightPort: "/dev/cu.usbmodem1234",
            triggerMode: "sdk",
            iedInbox: nil,
            sonyIpAddress: "10.0.0.247",
            sonyMacAddress: "10:32:2C:26:1A:3F",
            sonyUser: "sdk-user",
            sonyPassword: "sdk-password",
            sonyCapturePath: "/tmp/sony-capture",
            streamComposite: false,
            ffcCalibration: nil,
            cameraModel: "Sony ILCE-7CR",
            compositeFormat: "dng",
            levelR: 200,
            levelG: 200,
            levelB: 200,
            settleMs: 50
        )

        let args = client.buildArgs(settings: settings, portFile: "/tmp/port", readyNonce: "nonce")

        XCTAssertTrue(containsAdjacent(args, ["--port", "/dev/cu.usbmodem1234"]))
        XCTAssertTrue(containsAdjacent(args, ["--sony-capture", "/tmp/sony-capture"]))
        XCTAssertTrue(containsAdjacent(args, ["--sony-ip-address", "10.0.0.247"]))
        XCTAssertTrue(containsAdjacent(args, ["--sony-mac-address", "10:32:2C:26:1A:3F"]))
        XCTAssertTrue(containsAdjacent(args, ["--sony-user", "sdk-user"]))
        XCTAssertTrue(containsAdjacent(args, ["--sony-password", "sdk-password"]))
        XCTAssertTrue(containsAdjacent(args, ["--sony-iso", "100or125"]))
        XCTAssertTrue(containsAdjacent(args, ["--capture-timeout-s", "60"]))
    }

    func testBuildArgsOmitsNetworkAddressForSDKUSBMode() {
        let client = OrchestratorClient()
        let settings = ScanSettings(
            rollName: "RollSDK",
            outputFolder: "/Volumes/Scans",
            scanlightPort: "/dev/cu.usbmodem1234",
            triggerMode: "sdk",
            iedInbox: nil,
            sonyTransport: "usb",
            sonyIpAddress: "10.0.0.247",
            sonyMacAddress: "10:32:2C:26:1A:3F",
            sonyUser: "sdk-user",
            sonyPassword: "sdk-password",
            sonyCapturePath: "/tmp/sony-capture",
            streamComposite: false,
            ffcCalibration: nil,
            cameraModel: "Sony ILCE-7CR",
            compositeFormat: "dng",
            levelR: 200,
            levelG: 200,
            levelB: 200,
            settleMs: 50
        )

        let args = client.buildArgs(settings: settings, portFile: "/tmp/port", readyNonce: "nonce")

        XCTAssertTrue(containsAdjacent(args, ["--sony-capture", "/tmp/sony-capture"]))
        XCTAssertFalse(args.contains("--sony-ip-address"))
        XCTAssertFalse(args.contains("--sony-mac-address"))
        XCTAssertTrue(containsAdjacent(args, ["--sony-user", "sdk-user"]))
        XCTAssertTrue(containsAdjacent(args, ["--sony-password", "sdk-password"]))
    }

    func testSettingsWithResolvedSonyIPUsesARPEntryForSavedMAC() {
        let oldProvider = OrchestratorClient.sonyARPTableProvider
        OrchestratorClient.sonyARPTableProvider = {
            """
            ? (10.0.0.244) at 10:32:2c:26:1a:3f on en0 ifscope [ethernet]
            ? (10.0.0.247) at (incomplete) on en0 ifscope [ethernet]
            """
        }
        defer { OrchestratorClient.sonyARPTableProvider = oldProvider }

        let client = OrchestratorClient()
        let settings = ScanSettings(
            rollName: "RollSDK",
            outputFolder: "/Volumes/Scans",
            triggerMode: "sdk",
            iedInbox: nil,
            sonyIpAddress: "10.0.0.247",
            sonyMacAddress: "10:32:2C:26:1A:3F",
            sonyUser: "sdk-user",
            sonyPassword: "sdk-password",
            sonyCapturePath: "/tmp/sony-capture",
            streamComposite: false,
            ffcCalibration: nil,
            cameraModel: "Sony ILCE-7CR",
            compositeFormat: "dng",
            levelR: 200,
            levelG: 200,
            levelB: 200,
            settleMs: 50
        )

        let resolved = client.settingsWithResolvedSonyIP(settings)

        XCTAssertEqual(resolved.sonyIpAddress, "10.0.0.244")
        XCTAssertEqual(resolved.sonyMacAddress, "10:32:2C:26:1A:3F")
    }

    func testSettingsWithResolvedSonyIPDoesNotResolveUSBTransport() {
        let oldProvider = OrchestratorClient.sonyARPTableProvider
        OrchestratorClient.sonyARPTableProvider = {
            "? (10.0.0.244) at 10:32:2c:26:1a:3f on en0 ifscope [ethernet]"
        }
        defer { OrchestratorClient.sonyARPTableProvider = oldProvider }

        let client = OrchestratorClient()
        let settings = ScanSettings(
            rollName: "RollSDK",
            outputFolder: "/Volumes/Scans",
            triggerMode: "sdk",
            iedInbox: nil,
            sonyTransport: "usb",
            sonyIpAddress: nil,
            sonyMacAddress: "10:32:2C:26:1A:3F",
            sonyUser: "sdk-user",
            sonyPassword: "sdk-password",
            sonyCapturePath: "/tmp/sony-capture",
            streamComposite: false,
            ffcCalibration: nil,
            cameraModel: "Sony ILCE-7CR",
            compositeFormat: "dng",
            levelR: 200,
            levelG: 200,
            levelB: 200,
            settleMs: 50
        )

        let resolved = client.settingsWithResolvedSonyIP(settings)

        XCTAssertNil(resolved.sonyIpAddress)
        XCTAssertEqual(resolved.sonyMacAddress, "10:32:2C:26:1A:3F")
    }

    func testBuildSonyConnectionProbeUsesConnectOnly() throws {
        let client = OrchestratorClient()
        let settings = ScanSettings(
            rollName: "RollSDK",
            outputFolder: "/Volumes/Scans",
            triggerMode: "sdk",
            iedInbox: nil,
            sonyIpAddress: "10.0.0.247",
            sonyMacAddress: "10:32:2C:26:1A:3F",
            sonyUser: "sdk-user",
            sonyPassword: "sdk-password",
            sonyCapturePath: "/tmp/sony-capture",
            streamComposite: false,
            ffcCalibration: nil,
            cameraModel: "Sony ILCE-7CR",
            compositeFormat: "dng",
            levelR: 200,
            levelG: 200,
            levelB: 200,
            settleMs: 50
        )

        let command = try client.buildSonyConnectionProbeCommand(settings: settings, timeoutSeconds: 10)

        XCTAssertEqual(command.executableURL.path, "/tmp/sony-capture")
        XCTAssertTrue(command.arguments.contains("--connect-only"))
        XCTAssertFalse(command.arguments.contains("--out"))
        XCTAssertTrue(containsAdjacent(command.arguments, ["--timeout", "10"]))
        XCTAssertTrue(containsAdjacent(command.arguments, ["--ip-address", "10.0.0.247"]))
        XCTAssertTrue(containsAdjacent(command.arguments, ["--mac-address", "10:32:2C:26:1A:3F"]))
        XCTAssertFalse(command.arguments.contains("--user"))
        XCTAssertFalse(command.arguments.contains("--password"))
        XCTAssertEqual(command.environment["SONY_USERNAME"], "sdk-user")
        XCTAssertEqual(command.environment["SONY_USER"], "sdk-user")
        XCTAssertEqual(command.environment["SONY_PW"], "sdk-password")
    }

    func testBuildSonyConnectionProbeUsesUSBEnumerationWhenTransportIsUSB() throws {
        let client = OrchestratorClient()
        let settings = ScanSettings(
            rollName: "RollSDK",
            outputFolder: "/Volumes/Scans",
            triggerMode: "sdk",
            iedInbox: nil,
            sonyTransport: "usb",
            sonyIpAddress: "10.0.0.247",
            sonyMacAddress: "10:32:2C:26:1A:3F",
            sonyUser: "sdk-user",
            sonyPassword: "sdk-password",
            sonyCapturePath: "/tmp/sony-capture",
            streamComposite: false,
            ffcCalibration: nil,
            cameraModel: "Sony ILCE-7CR",
            compositeFormat: "dng",
            levelR: 200,
            levelG: 200,
            levelB: 200,
            settleMs: 50
        )

        let command = try client.buildSonyConnectionProbeCommand(settings: settings, timeoutSeconds: 10)

        XCTAssertTrue(command.arguments.contains("--connect-only"))
        XCTAssertFalse(command.arguments.contains("--ip-address"))
        XCTAssertFalse(command.arguments.contains("--mac-address"))
        XCTAssertEqual(command.environment["SONY_USERNAME"], "sdk-user")
        XCTAssertEqual(command.environment["SONY_PW"], "sdk-password")
    }

    func testSonyConnectionProbeRetriesTransientRouteFailure() async throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("sony-retry-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(
            at: directory,
            withIntermediateDirectories: true
        )
        defer { try? FileManager.default.removeItem(at: directory) }

        let counterURL = directory.appendingPathComponent("counter.txt")
        let scriptURL = directory.appendingPathComponent("sony-capture")
        let script = """
        #!/bin/zsh
        counter="\(counterURL.path)"
        count=$(cat "$counter" 2>/dev/null || echo 0)
        count=$((count + 1))
        echo "$count" > "$counter"
        if [[ "$count" -eq 1 ]]; then
          echo "sony-capture: camera is not reachable over SDK SSH at 10.0.0.247:22 (connect(10.0.0.247:22) failed: No route to host)" >&2
          exit 1
        fi
        echo connected
        exit 0
        """
        try script.write(to: scriptURL, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o755],
            ofItemAtPath: scriptURL.path
        )

        let client = OrchestratorClient()
        let settings = ScanSettings(
            rollName: "RollSDK",
            outputFolder: "/Volumes/Scans",
            triggerMode: "sdk",
            iedInbox: nil,
            sonyIpAddress: "10.0.0.247",
            sonyMacAddress: "10:32:2C:26:1A:3F",
            sonyUser: "sdk-user",
            sonyPassword: "sdk-password",
            sonyCapturePath: scriptURL.path,
            streamComposite: false,
            ffcCalibration: nil,
            cameraModel: "Sony ILCE-7CR",
            compositeFormat: "dng",
            levelR: 200,
            levelG: 200,
            levelB: 200,
            settleMs: 50
        )

        let result = await client.checkSonyConnection(settings: settings)

        XCTAssertTrue(result.success)
        XCTAssertEqual(result.message, "Connected to Sony camera.")
        XCTAssertEqual(
            try String(contentsOf: counterURL).trimmingCharacters(in: .whitespacesAndNewlines),
            "2"
        )
    }

    func testBuildSonyLiveViewFrameUsesLiveViewOut() throws {
        let client = OrchestratorClient()
        let settings = ScanSettings(
            rollName: "RollSDK",
            outputFolder: "/Volumes/Scans",
            triggerMode: "sdk",
            iedInbox: nil,
            sonyIpAddress: "10.0.0.247",
            sonyMacAddress: "10:32:2C:26:1A:3F",
            sonyUser: "sdk-user",
            sonyPassword: "sdk-password",
            sonyCapturePath: "/tmp/sony-capture",
            streamComposite: false,
            ffcCalibration: nil,
            cameraModel: "Sony ILCE-7CR",
            compositeFormat: "dng",
            levelR: 200,
            levelG: 200,
            levelB: 200,
            settleMs: 50
        )

        let outputURL = URL(fileURLWithPath: "/tmp/sony-live-view.jpg")
        let command = try client.buildSonyLiveViewFrameCommand(
            settings: settings,
            outputURL: outputURL,
            timeoutSeconds: 8
        )

        XCTAssertEqual(command.executableURL.path, "/tmp/sony-capture")
        XCTAssertTrue(containsAdjacent(command.arguments, ["--live-view-out", "/tmp/sony-live-view.jpg"]))
        XCTAssertFalse(command.arguments.contains("--out"))
        XCTAssertFalse(command.arguments.contains("--connect-only"))
        XCTAssertTrue(containsAdjacent(command.arguments, ["--timeout", "8"]))
        XCTAssertTrue(containsAdjacent(command.arguments, ["--ip-address", "10.0.0.247"]))
        XCTAssertTrue(containsAdjacent(command.arguments, ["--mac-address", "10:32:2C:26:1A:3F"]))
        XCTAssertFalse(command.arguments.contains("--user"))
        XCTAssertFalse(command.arguments.contains("--password"))
        XCTAssertEqual(command.environment["SONY_USERNAME"], "sdk-user")
        XCTAssertEqual(command.environment["SONY_USER"], "sdk-user")
        XCTAssertEqual(command.environment["SONY_PW"], "sdk-password")
    }

    func testBuildSonyLiveViewStreamUsesPersistentLiveViewOut() throws {
        let client = OrchestratorClient()
        let settings = ScanSettings(
            rollName: "RollSDK",
            outputFolder: "/Volumes/Scans",
            triggerMode: "sdk",
            iedInbox: nil,
            sonyIpAddress: "10.0.0.247",
            sonyMacAddress: "10:32:2C:26:1A:3F",
            sonyUser: "sdk-user",
            sonyPassword: "sdk-password",
            sonyCapturePath: "/tmp/sony-capture",
            streamComposite: false,
            ffcCalibration: nil,
            cameraModel: "Sony ILCE-7CR",
            compositeFormat: "dng",
            levelR: 200,
            levelG: 200,
            levelB: 200,
            settleMs: 50
        )

        let outputURL = URL(fileURLWithPath: "/tmp/sony-live-view-stream.jpg")
        let command = try client.buildSonyLiveViewStreamCommand(
            settings: settings,
            outputURL: outputURL,
            intervalMs: 250,
            timeoutSeconds: 8
        )

        XCTAssertEqual(command.executableURL.path, "/tmp/sony-capture")
        XCTAssertTrue(containsAdjacent(command.arguments, ["--live-view-stream-out", "/tmp/sony-live-view-stream.jpg"]))
        XCTAssertTrue(containsAdjacent(command.arguments, ["--live-view-interval-ms", "250"]))
        XCTAssertFalse(command.arguments.contains("--out"))
        XCTAssertFalse(command.arguments.contains("--connect-only"))
        XCTAssertTrue(containsAdjacent(command.arguments, ["--timeout", "8"]))
        XCTAssertTrue(containsAdjacent(command.arguments, ["--ip-address", "10.0.0.247"]))
        XCTAssertTrue(containsAdjacent(command.arguments, ["--mac-address", "10:32:2C:26:1A:3F"]))
        XCTAssertFalse(command.arguments.contains("--user"))
        XCTAssertFalse(command.arguments.contains("--password"))
        XCTAssertEqual(command.environment["SONY_USERNAME"], "sdk-user")
        XCTAssertEqual(command.environment["SONY_USER"], "sdk-user")
        XCTAssertEqual(command.environment["SONY_PW"], "sdk-password")
    }

    func testBuildSonyLiveViewStreamUsesUSBEnumerationWhenTransportIsUSB() throws {
        let client = OrchestratorClient()
        let settings = ScanSettings(
            rollName: "RollSDK",
            outputFolder: "/Volumes/Scans",
            triggerMode: "sdk",
            iedInbox: nil,
            sonyTransport: "usb",
            sonyIpAddress: "10.0.0.247",
            sonyMacAddress: "10:32:2C:26:1A:3F",
            sonyUser: "sdk-user",
            sonyPassword: "sdk-password",
            sonyCapturePath: "/tmp/sony-capture",
            streamComposite: false,
            ffcCalibration: nil,
            cameraModel: "Sony ILCE-7CR",
            compositeFormat: "dng",
            levelR: 200,
            levelG: 200,
            levelB: 200,
            settleMs: 50
        )

        let outputURL = URL(fileURLWithPath: "/tmp/sony-live-view-stream.jpg")
        let command = try client.buildSonyLiveViewStreamCommand(
            settings: settings,
            outputURL: outputURL,
            intervalMs: 250,
            timeoutSeconds: 8
        )

        XCTAssertTrue(containsAdjacent(command.arguments, ["--live-view-stream-out", "/tmp/sony-live-view-stream.jpg"]))
        XCTAssertFalse(command.arguments.contains("--ip-address"))
        XCTAssertFalse(command.arguments.contains("--mac-address"))
        XCTAssertEqual(command.environment["SONY_USERNAME"], "sdk-user")
        XCTAssertEqual(command.environment["SONY_PW"], "sdk-password")
    }

    func testSonyConnectionProbeTimeoutDoesNotReadStatusWhileProcessRuns() async throws {
        let result = try await OrchestratorClient.runSonyProbeProcess(
            executableURL: URL(fileURLWithPath: "/bin/zsh"),
            arguments: ["-c", "trap '' TERM; while true; do :; done"],
            timeout: 0.1
        )

        XCTAssertTrue(result.timedOut)
        XCTAssertNotEqual(result.exitCode, 0)
    }

    func testCalibrationExposureUsesLongRequestTimeout() async throws {
        StubURLProtocol.routes["/api/calibrate/exposure"] = (
            "{\"error\":\"forced failure\"}".data(using: .utf8)!,
            500
        )

        let client = OrchestratorClient(session: makeStubSession())
        client.webPort = 9999

        do {
            _ = try await client.calibrateExposure()
            XCTFail("Expected forced calibration route failure")
        } catch OrchestratorError.httpError {
            // Expected; the request was still captured by StubURLProtocol.
        }

        XCTAssertGreaterThanOrEqual(
            StubURLProtocol.lastRequest?.timeoutInterval ?? 0,
            1800.0
        )
    }

    func testCalibrationExposureSendsRealRebateRegionBody() async throws {
        StubURLProtocol.routes["/api/calibrate/exposure"] = (
            "{\"error\":\"forced failure\"}".data(using: .utf8)!,
            500
        )

        let client = OrchestratorClient(session: makeStubSession())
        client.webPort = 9999

        do {
            _ = try await client.calibrateExposure(
                rebateCol: 4464,
                rebateRow: 3108,
                rebateW: 640,
                rebateH: 160,
                seed: makeExposureSeed(),
                callID: "cal-run-123",
                targetFraction: 0.80
            )
            XCTFail("Expected forced calibration route failure")
        } catch OrchestratorError.httpError {
            // Expected; inspect the captured request body below.
        }

        guard let bodyData = StubURLProtocol.lastBody else {
            XCTFail("No body was recorded by StubURLProtocol")
            return
        }
        let decoded = try JSONSerialization.jsonObject(with: bodyData) as? [String: Any]
        XCTAssertEqual(decoded?["rebate_col"] as? Int, 4464)
        XCTAssertEqual(decoded?["rebate_row"] as? Int, 3108)
        XCTAssertEqual(decoded?["rebate_w"] as? Int, 640)
        XCTAssertEqual(decoded?["rebate_h"] as? Int, 160)
        XCTAssertEqual(decoded?["call_id"] as? String, "cal-run-123")
        XCTAssertEqual(decoded?["target_fraction"] as? Double, 0.80)
        let seed = decoded?["seed"] as? [String: Any]
        let red = seed?["R"] as? [String: Any]
        let green = seed?["G"] as? [String: Any]
        let blue = seed?["B"] as? [String: Any]
        XCTAssertEqual(red?["led_level"] as? Int, 180)
        XCTAssertEqual(red?["shutter_speed"] as? String, "1/40")
        XCTAssertEqual(green?["led_level"] as? Int, 190)
        XCTAssertEqual(blue?["shutter_speed"] as? String, "1/20")
    }

    func testCalibrationPreviewLightPostsEnabledAndLevel() async throws {
        StubURLProtocol.routes["/api/calibrate/preview-light"] = (
            "{}".data(using: .utf8)!,
            200
        )

        let client = OrchestratorClient(session: makeStubSession())
        client.webPort = 9999

        try await client.setCalibrationPreviewLight(enabled: true, level: 177)

        XCTAssertEqual(StubURLProtocol.lastRequest?.httpMethod, "POST")
        guard let bodyData = StubURLProtocol.lastBody else {
            XCTFail("No body was recorded by StubURLProtocol")
            return
        }
        let decoded = try JSONSerialization.jsonObject(with: bodyData) as? [String: Any]
        XCTAssertEqual(decoded?["enabled"] as? Bool, true)
        XCTAssertEqual(decoded?["level"] as? Int, 177)
    }

    func testCalibrationProgressDecodesLatestEvent() async throws {
        StubURLProtocol.routes["/api/calibrate/progress"] = (
            """
            {
                "event": "sony_capture_start",
                "message": "Camera is capturing/downloading R RAW at shutter 1/2.",
                "ts": "2026-05-23T05:47:43.983356+00:00",
                "channel": "R",
                "level": 128,
                "shutter_speed": "1/2",
                "label": "exposure-R"
            }
            """.data(using: .utf8)!,
            200
        )

        let client = OrchestratorClient(session: makeStubSession())
        client.webPort = 9999

        let progress = try await client.calibrationProgress()

        XCTAssertEqual(progress.event, "sony_capture_start")
        XCTAssertEqual(progress.channel, "R")
        XCTAssertEqual(progress.level, 128)
        XCTAssertEqual(progress.shutterSpeed, "1/2")
        XCTAssertTrue(progress.message.contains("capturing/downloading R RAW"))
    }

    func testCalibrationProgressAndResultCanScopeToCallID() async throws {
        StubURLProtocol.routes["/api/calibrate/progress"] = (
            """
            {
                "event": "calibration_started",
                "message": "Exposure calibration started; preparing the camera and Scanlight.",
                "call_id": "call-abc",
                "recent_events": []
            }
            """.data(using: .utf8)!,
            200
        )
        StubURLProtocol.routes["/api/calibrate/exposure-result"] = (
            makeExposureJSONData(ledR: 210),
            200
        )

        let client = OrchestratorClient(session: makeStubSession())
        client.webPort = 9999

        let progress = try await client.calibrationProgress(callID: "call-abc")
        XCTAssertEqual(progress.event, "calibration_started")
        XCTAssertEqual(progress.callId, "call-abc")
        XCTAssertEqual(StubURLProtocol.lastRequest?.url?.query, "call_id=call-abc")

        let result = try await client.lastExposureResult(callID: "call-abc")
        XCTAssertEqual(result?.r.ledLevel, 210)
        XCTAssertEqual(StubURLProtocol.lastRequest?.url?.query, "call_id=call-abc")
    }

    // MARK: - Test 14: waitForReady accepts the matching readiness nonce

    /// The orchestrator we spawned echoes our --ready-nonce on /api/state;
    /// waitForReady treats a 200 + matching nonce as ready.
    func testWaitForReadyAcceptsMatchingNonce() async throws {
        let nonce = "abc-123-ready-nonce"
        let json = """
        {"roll_name":"R","frame_number":1,"output_folder":"/tmp","level_r":200,"level_g":200,"level_b":200,"settle_ms":50,"ready_nonce":"\(nonce)"}
        """.data(using: .utf8)!
        StubURLProtocol.routes["/api/state"] = (json, 200)

        let client = OrchestratorClient(session: makeStubSession())
        client.webPort = 9999

        // Must return (ready) without throwing — the nonce matches.
        try await client.waitForReady(port: 9999, timeout: 2.0, expectedNonce: nonce)
    }

    // MARK: - Test 15: waitForReady rejects a foreign server with a non-matching nonce

    /// Regression for the bind-probe TOCTOU: a DIFFERENT localhost server that
    /// grabbed the port answers 200 on /api/state but does not know our nonce.
    /// waitForReady must NOT treat it as ready — it keeps polling until timeout.
    func testWaitForReadyRejectsForeignServerNonce() async throws {
        let foreign = """
        {"roll_name":"R","frame_number":1,"output_folder":"/tmp","level_r":200,"level_g":200,"level_b":200,"settle_ms":50,"ready_nonce":"someone-elses-nonce"}
        """.data(using: .utf8)!
        StubURLProtocol.routes["/api/state"] = (foreign, 200)

        let client = OrchestratorClient(session: makeStubSession())
        client.webPort = 9999

        do {
            try await client.waitForReady(port: 9999, timeout: 0.5, expectedNonce: "my-real-nonce")
            XCTFail("waitForReady must NOT treat a foreign server (wrong nonce) as ready")
        } catch OrchestratorError.startupTimeout {
            // Expected: the nonce never matched, so it timed out instead of
            // declaring a foreign server ready (no false readiness).
        } catch {
            XCTFail("Wrong error type: \(error)")
        }
    }

    // MARK: - Test 16: waitForPort reads a valid port from an existing file

    /// Happy path: if the port-file already contains a valid port integer,
    /// waitForPort must return that port immediately on the first poll.
    func testWaitForPortReadsFromFile() async throws {
        // Write a known port to a temp file — simulates the child writing after bind.
        let tmpURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("test-port-\(UUID().uuidString)")
        try "54321".write(to: tmpURL, atomically: true, encoding: .utf8)

        let client = OrchestratorClient(session: makeStubSession())
        // childExitStatus must be nil so we don't trip the fast-fail path.
        client.childExitStatus = nil

        let port = try await client.waitForPort(file: tmpURL, timeout: 2.0)

        XCTAssertEqual(port, 54321, "waitForPort must return the integer written to the file")
        // The file should be cleaned up by the defer in waitForPort.
        XCTAssertFalse(
            FileManager.default.fileExists(atPath: tmpURL.path),
            "waitForPort must clean up the port-file after reading"
        )
    }

    // MARK: - Test 17: waitForPort fails fast when the child exits before writing

    /// If childExitStatus is already set (the termination handler fired), waitForPort
    /// must throw startupFailed immediately — not hang the full timeout.
    func testWaitForPortFailsFastOnChildExit() async throws {
        let tmpURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("test-port-\(UUID().uuidString)")
        // Do NOT write the file — the child never got that far.

        let client = OrchestratorClient(session: makeStubSession())
        client.childExitStatus = 1  // child died with exit code 1

        let started = Date()
        do {
            // Large timeout — the fast-fail must NOT wait anywhere near this long.
            _ = try await client.waitForPort(file: tmpURL, timeout: 10.0)
            XCTFail("Expected OrchestratorError.startupFailed to be thrown")
        } catch OrchestratorError.startupFailed(let exitCode, _) {
            XCTAssertEqual(exitCode, 1, "Expected the child's exit code to be surfaced")
        } catch {
            XCTFail("Wrong error type thrown: \(error)")
        }
        let elapsed = Date().timeIntervalSince(started)
        XCTAssertLessThan(elapsed, 1.0,
                          "waitForPort must fail fast on child exit (took \(elapsed)s, expected < 1s)")
    }

    // MARK: - Test 18: waitForPort times out when the file never appears

    /// If the child never writes the port-file (and does not exit), waitForPort
    /// must throw startupTimeout after the deadline.
    func testWaitForPortTimesOut() async throws {
        let tmpURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("test-port-\(UUID().uuidString)")
        // Do NOT write the file; do NOT set childExitStatus — simulates a hung child.

        let client = OrchestratorClient(session: makeStubSession())
        // childExitStatus nil so the fast-fail path is not taken.

        do {
            _ = try await client.waitForPort(file: tmpURL, timeout: 0.5)
            XCTFail("Expected OrchestratorError.startupTimeout to be thrown")
        } catch OrchestratorError.startupTimeout(let stderr) {
            // Pass: timed out correctly.
            XCTAssertNotNil(stderr as String?,
                            "startupTimeout.stderr should be a non-nil String")
        } catch {
            XCTFail("Wrong error type thrown: \(error)")
        }
    }

    // MARK: - F0: captureFrame sets explicit request timeout > 60s

    /// Regression for F0: captureFrame must set an explicit timeoutInterval so a
    /// manual triplet (3 × 30s channels + margin) doesn't hit the URLSession 60s
    /// default and time out before the operator finishes all three IED fires.
    func testCaptureFrameSetsExplicitTimeoutAbove60s() async throws {
        StubURLProtocol.routes["/api/capture"] = (makeTripletJSON(), 200)

        let client = OrchestratorClient(session: makeStubSession())
        client.webPort = 9999

        _ = try await client.captureFrame(retake: false)

        let timeout = StubURLProtocol.lastRequest?.timeoutInterval ?? 0
        XCTAssertGreaterThan(
            timeout, 60.0,
            "captureFrame must set timeoutInterval > 60s (URLSession default) to survive a manual triplet; got \(timeout)s"
        )
        // Sanity check: must be at least 3 × default capture_timeout_s + margin
        // (30 × 3 + 15 = 105s). Use 90 as the lower bound to allow for different
        // per-channel timeout defaults without being brittle.
        XCTAssertGreaterThanOrEqual(
            timeout, 90.0,
            "captureFrame timeout must cover at least 3 channels × 30s; got \(timeout)s"
        )
    }

    // MARK: - F0: captureFrame without retake also sets timeout

    func testCaptureFrameWithRetakeFalsoSetsExplicitTimeout() async throws {
        StubURLProtocol.routes["/api/capture"] = (makeTripletJSON(), 200)

        let client = OrchestratorClient(session: makeStubSession())
        client.webPort = 9999

        _ = try await client.captureFrame(retake: true)

        let timeout = StubURLProtocol.lastRequest?.timeoutInterval ?? 0
        XCTAssertGreaterThan(
            timeout, 60.0,
            "captureFrame (retake=true) must also set timeoutInterval > 60s; got \(timeout)s"
        )
    }

    // MARK: - F1: OrchestratorState decodes waiting_for_channel

    func testFetchStateDecodesWaitingForChannelWhenPresent() async throws {
        let json = """
        {
            "roll_name": "Roll001",
            "frame_number": 1,
            "output_folder": "/tmp/scans/Roll001",
            "level_r": 200,
            "level_g": 200,
            "level_b": 200,
            "settle_ms": 50,
            "waiting_for_channel": "G"
        }
        """.data(using: .utf8)!
        StubURLProtocol.routes["/api/state"] = (json, 200)

        let client = OrchestratorClient(session: makeStubSession())
        client.webPort = 9999

        let state = try await client.fetchState()

        XCTAssertEqual(state.waitingForChannel, "G",
                       "Expected waitingForChannel = 'G', got: \(state.waitingForChannel ?? "nil")")
    }

    func testFetchStateDecodesWaitingForChannelWhenAbsent() async throws {
        StubURLProtocol.routes["/api/state"] = (makeStateJSON(), 200)

        let client = OrchestratorClient(session: makeStubSession())
        client.webPort = 9999

        let state = try await client.fetchState()

        XCTAssertNil(state.waitingForChannel,
                     "waitingForChannel should be nil when key is absent from JSON")
    }

    func testFetchStateDecodesWaitingForChannelNull() async throws {
        let json = """
        {
            "roll_name": "Roll001",
            "frame_number": 1,
            "output_folder": "/tmp/scans/Roll001",
            "level_r": 200,
            "level_g": 200,
            "level_b": 200,
            "settle_ms": 50,
            "waiting_for_channel": null
        }
        """.data(using: .utf8)!
        StubURLProtocol.routes["/api/state"] = (json, 200)

        let client = OrchestratorClient(session: makeStubSession())
        client.webPort = 9999

        let state = try await client.fetchState()

        XCTAssertNil(state.waitingForChannel,
                     "waitingForChannel should be nil when JSON value is null")
    }
}
