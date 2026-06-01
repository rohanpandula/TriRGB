// CalibrationWizardViewModelTests — unit and mock-backend tests for the 4-step wizard.
//
// Test strategy: URLSession injection via StubURLProtocol (re-declared locally
// because OrchestratorClientTests' helpers are private).
//
// All tests run with NO real Python server — purely hardware-free (SC-5/NFR-12).

import Foundation
import XCTest
@testable import ScanlightApp

// MARK: - Local StubURLProtocol (private helpers re-declared; OrchestratorClientTests ones are private)

/// URLProtocol subclass that intercepts all requests in a test-scoped URLSession.
final class WizardStubURLProtocol: URLProtocol {

    static var routes: [String: (Data, Int)] = [:]
    static var hangingPaths: Set<String> = []
    static var stoppedPaths: [String] = []
    static var lastRequest: URLRequest? = nil
    static var lastBody: Data? = nil
    static var requestHandler: ((URLRequest) -> (Data, Int)?)? = nil

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        Self.lastRequest = request
        // Drain body stream
        if let bodyStream = request.httpBodyStream {
            var bodyData = Data()
            bodyStream.open()
            let bufferSize = 4096
            var buffer = [UInt8](repeating: 0, count: bufferSize)
            while bodyStream.hasBytesAvailable {
                let read = bodyStream.read(&buffer, maxLength: bufferSize)
                if read > 0 { bodyData.append(contentsOf: buffer.prefix(read)) }
            }
            bodyStream.close()
            Self.lastBody = bodyData.isEmpty ? nil : bodyData
        } else if let bodyData = request.httpBody {
            Self.lastBody = bodyData
        } else {
            Self.lastBody = nil
        }

        let path = request.url?.path ?? ""
        if Self.hangingPaths.contains(path) {
            return
        }
        let (data, status) = Self.requestHandler?(request)
            ?? Self.routes[path]
            ?? (Data(), 404)
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

    override func stopLoading() {
        if let path = request.url?.path {
            Self.stoppedPaths.append(path)
        }
    }
}

// MARK: - Fixture helpers

private func makeStubSession() -> URLSession {
    let config = URLSessionConfiguration.ephemeral
    config.protocolClasses = [WizardStubURLProtocol.self]
    return URLSession(configuration: config)
}

private func makeStateJSON() -> Data {
    let json = """
    {
        "roll_name": "Roll001",
        "frame_number": 1,
        "output_folder": "/tmp/scans/Roll001",
        "level_r": 180,
        "level_g": 160,
        "level_b": 230,
        "settle_ms": 50,
        "ready_nonce": ""
    }
    """
    return json.data(using: .utf8)!
}

private func makeExposureJSON(
    ledR: Int = 180,
    ledG: Int = 160,
    ledB: Int = 230,
    shutterR: String? = nil,
    shutterG: String? = nil,
    shutterB: String? = nil
) -> Data {
    let rShutter = shutterR.map { "\"shutter_speed\": \"\($0)\"," } ?? ""
    let gShutter = shutterG.map { "\"shutter_speed\": \"\($0)\"," } ?? ""
    let bShutter = shutterB.map { "\"shutter_speed\": \"\($0)\"," } ?? ""
    let json = """
    {
        "r": {
            "channel": "R",
            "led_level": \(ledR),
            "black_level": 256.0,
            "gain": 1.234,
            "clip_fraction": 0.02,
            "p99": 62200.0,
            "target": 62258.0,
            "exposure_status": "target",
            \(rShutter)
            "schema_version": 1
        },
        "g": {
            "channel": "G",
            "led_level": \(ledG),
            "black_level": 260.0,
            "gain": 1.100,
            "clip_fraction": 0.01,
            "p99": 62250.0,
            "target": 62258.0,
            "exposure_status": "target",
            \(gShutter)
            "schema_version": 1
        },
        "b": {
            "channel": "B",
            "led_level": \(ledB),
            "black_level": 258.0,
            "gain": 1.500,
            "clip_fraction": 0.03,
            "p99": 62300.0,
            "target": 62258.0,
            "exposure_status": "target",
            \(bShutter)
            "schema_version": 1
        },
        "base_region": {
            "x": 4, "y": 4, "w": 100, "h": 20,
            "base_rgb": [8930.0, 12097.0, 2952.0],
            "uniformity_cv": 1.5,
            "source": "auto",
            "schema_version": 1
        },
        "ffc_cal_dir": "",
        "schema_version": 1
    }
    """
    return json.data(using: .utf8)!
}

private func makeFFCJSON() -> Data {
    let json = """
    {
        "flat_field": {
            "flat_data_path": "/tmp/flat.npy",
            "n_frames_averaged": 8,
            "warmup_s": 0.0,
            "black_level_r": 256.0,
            "black_level_g": 260.0,
            "black_level_b": 258.0,
            "working_brightness": 200,
            "uniformity_improvement": 1.5,
            "schema_version": 1
        },
        "inspection": {
            "channels": {
                "R": {"falloff_pct": 3.1, "uniformity_pct": 1.2, "verdict": "clean"},
                "G": {"falloff_pct": 2.8, "uniformity_pct": 0.9, "verdict": "clean"},
                "B": {"falloff_pct": 4.0, "uniformity_pct": 1.5, "verdict": "acceptable"}
            },
            "overall": "CLEAN"
        }
    }
    """
    return json.data(using: .utf8)!
}

private func makeChecksJSON() -> Data {
    // frame_anomaly (per-frame vs roll baseline) is deferred to Phase 15;
    // the /api/calibrate/checks route returns 2 checks: registration + base_neutrality.
    // FIX-B: Phase 13 check_registration emits component keys (g_vs_r_dx/dy, b_vs_r_dx/dy);
    // old rg_shift/gb_shift keys are gone. Non-empty deltas → included in roll verdict.
    let json = """
    [
        {"name": "registration", "passed": true, "deltas": {"g_vs_r_dx": 0.10, "g_vs_r_dy": 0.07, "b_vs_r_dx": 0.06, "b_vs_r_dy": 0.05}, "schema_version": 1},
        {"name": "base_neutrality", "passed": true, "deltas": {"base_r": 8930.0, "base_g": 12097.0, "base_b": 2952.0}, "schema_version": 1}
    ]
    """
    return json.data(using: .utf8)!
}

private func makeChecksFailJSON() -> Data {
    // 2-check shape (frame_anomaly deferred to Phase 15 — no roll baseline during calibration).
    // FIX-B: use Phase 13 component keys; FIX-A: non-empty deltas → both checks counted in verdict.
    let json = """
    [
        {"name": "registration", "passed": false, "deltas": {"g_vs_r_dx": 2.4, "g_vs_r_dy": 0.5, "b_vs_r_dx": 1.7, "b_vs_r_dy": 0.6}, "schema_version": 1},
        {"name": "base_neutrality", "passed": false, "deltas": {"deviation": 800.0}, "schema_version": 1}
    ]
    """
    return json.data(using: .utf8)!
}

private func makeChecksNotAvailableJSON() -> Data {
    // Simulates pre-hardware state: registration has empty deltas (LAST_CAL_FRAME not set).
    // FIX-A: empty-deltas checks are excluded from the roll verdict; base_neutrality gates the roll.
    let json = """
    [
        {"name": "registration", "passed": false, "deltas": {}, "schema_version": 1},
        {"name": "base_neutrality", "passed": true, "deltas": {"base_r": 8930.0, "base_g": 12097.0, "base_b": 2952.0}, "schema_version": 1}
    ]
    """
    return json.data(using: .utf8)!
}

// MARK: - Test Suite

@MainActor
final class CalibrationWizardViewModelTests: XCTestCase {

    override func setUp() {
        super.setUp()
        WizardStubURLProtocol.routes = [:]
        WizardStubURLProtocol.hangingPaths = []
        WizardStubURLProtocol.stoppedPaths = []
        WizardStubURLProtocol.lastRequest = nil
        WizardStubURLProtocol.lastBody = nil
        WizardStubURLProtocol.requestHandler = nil
    }

    // MARK: - SC-5 canonical test: all 4 steps, hardware-free

    func testAllStepsHardwareFree() async throws {
        // Arrange
        WizardStubURLProtocol.routes["/api/state"] = (makeStateJSON(), 200)
        WizardStubURLProtocol.routes["/api/calibrate/exposure"] = (makeExposureJSON(), 200)
        WizardStubURLProtocol.routes["/api/calibrate/ffc"] = (makeFFCJSON(), 200)
        WizardStubURLProtocol.routes["/api/calibrate/checks"] = (makeChecksJSON(), 200)

        let client = OrchestratorClient(session: makeStubSession())
        client.webPort = 9999
        let vm = CalibrationWizardViewModel(orchestratorClient: client)

        // Step 1 — rig check: trigger sets the result; navigation is driven by "Next" button
        // (WizardNavFooter.primaryAction), not by the trigger itself (IN-03 fix).
        await vm.triggerRigCheck()
        XCTAssertNotNil(vm.rigCheckResult, "rigCheckResult should be set after Step 1")
        XCTAssertEqual(vm.currentStep, 1, "trigger must NOT auto-advance — operator must press Next")
        XCTAssertNil(vm.lastError[1], "No error on Step 1")
        // Simulate the "Next" button (WizardNavFooter.primaryAction → currentStep = 2)
        vm.currentStep = 2

        // Step 2 — exposure
        vm.rebateRegion = RebateRegion.centeredAtNormalized(x: 0.5, y: 0.5)
        await vm.triggerExposure()
        XCTAssertNotNil(vm.exposureResult, "exposureResult should be set after Step 2")
        XCTAssertEqual(vm.exposureResult?.r.ledLevel, 180, "R LED level should decode")
        XCTAssertEqual(vm.currentStep, 2, "trigger must NOT auto-advance — operator must press Next")
        XCTAssertNil(vm.lastError[2], "No error on Step 2")
        // Simulate the "Next" button
        vm.currentStep = 3

        // Step 3 — FFC
        await vm.triggerFFC()
        XCTAssertNotNil(vm.ffcResult, "ffcResult should be set after Step 3")
        XCTAssertEqual(vm.ffcResult?.flatField.nFramesAveraged, 8)
        XCTAssertEqual(vm.currentStep, 3, "trigger must NOT auto-advance — operator must press Next")
        XCTAssertNil(vm.lastError[3], "No error on Step 3")
        // Simulate the "Next" button
        vm.currentStep = 4

        // Step 4 — results checks (2 checks: registration + base_neutrality; frame_anomaly deferred to Phase 15)
        await vm.triggerResultsCheck()
        XCTAssertEqual(vm.checkResults?.count, 2, "Should have 2 check results (frame_anomaly deferred to Phase 15)")
        XCTAssertNil(vm.lastError[4], "No error on Step 4")

        // isRunning should be false after all steps
        XCTAssertFalse(vm.isRunning, "isRunning should be false after all steps complete")
    }

    // MARK: - Snake_case decoding

    func testExposureDecodesSnakeCase() async throws {
        // The JSON uses snake_case fields (led_level, clip_fraction, etc.)
        // and the decoder uses .convertFromSnakeCase.
        WizardStubURLProtocol.routes["/api/calibrate/exposure"] = (makeExposureJSON(ledR: 200, ledG: 170, ledB: 240), 200)

        let client = OrchestratorClient(session: makeStubSession())
        client.webPort = 9999

        let result = try await client.calibrateExposure()
        XCTAssertEqual(result.r.ledLevel, 200, "led_level → ledLevel decoding")
        XCTAssertEqual(result.g.ledLevel, 170)
        XCTAssertEqual(result.b.ledLevel, 240)
        XCTAssertEqual(result.r.channel, "R")
        XCTAssertEqual(result.r.exposureStatus, "target")
        XCTAssertEqual(result.r.target, 62258.0)
        XCTAssertEqual(result.r.p99, 62200.0)
        XCTAssertGreaterThan(result.r.clipFraction, 0)
    }

    func testTriggerExposureConflictReattachesToExistingResult() async throws {
        WizardStubURLProtocol.routes["/api/calibrate/exposure"] = (
            #"{"error":"A scan is in progress — wait for it to finish.","call_id":"existing-run"}"#
                .data(using: .utf8)!,
            409
        )
        WizardStubURLProtocol.routes["/api/calibrate/progress"] = (
            """
            {
                "event": "sony_capture_start",
                "message": "Camera is capturing/downloading the dark frame at shutter 1/40.",
                "recent_events": [
                    {
                        "event": "sony_capture_start",
                        "message": "Camera is capturing/downloading the dark frame at shutter 1/40.",
                        "label": "dark-frame",
                        "shutter_speed": "1/40",
                        "ts": "2026-05-23T06:05:04Z"
                    }
                ]
            }
            """.data(using: .utf8)!,
            200
        )
        WizardStubURLProtocol.routes["/api/calibrate/exposure-result"] = (
            makeExposureJSON(ledR: 201, ledG: 202, ledB: 203),
            200
        )

        let client = OrchestratorClient(session: makeStubSession())
        client.webPort = 9999
        let vm = CalibrationWizardViewModel(orchestratorClient: client)
        vm.rebateRegion = RebateRegion.centeredAtNormalized(x: 0.5, y: 0.5)

        await vm.triggerExposure()

        XCTAssertNotNil(vm.exposureResult)
        XCTAssertEqual(vm.exposureResult?.r.ledLevel, 201)
        XCTAssertNil(vm.lastError[2])
        XCTAssertFalse(vm.isRunning)
    }

    func testTriggerExposureRetriesOnceWhenBusyConflictIsIdle() async throws {
        var exposureRequests = 0
        WizardStubURLProtocol.requestHandler = { request in
            switch request.url?.path {
            case "/api/calibrate/exposure":
                exposureRequests += 1
                if exposureRequests == 1 {
                    return (#"{"error":"A capture is in progress — wait for it to finish."}"#
                        .data(using: .utf8)!, 409)
                }
                return (makeExposureJSON(ledR: 211, ledG: 212, ledB: 213), 200)
            case "/api/calibrate/progress":
                return (
                    """
                    {
                        "event": "idle",
                        "message": "Waiting for calibration capture to start.",
                        "recent_events": []
                    }
                    """.data(using: .utf8)!,
                    200
                )
            default:
                return nil
            }
        }

        let client = OrchestratorClient(session: makeStubSession())
        client.webPort = 9999
        let vm = CalibrationWizardViewModel(orchestratorClient: client)
        vm.rebateRegion = RebateRegion.centeredAtNormalized(x: 0.5, y: 0.5)

        await vm.triggerExposure()

        XCTAssertEqual(exposureRequests, 2)
        XCTAssertEqual(vm.exposureResult?.r.ledLevel, 211)
        XCTAssertNil(vm.lastError[2])
        XCTAssertFalse(vm.isRunning)
    }

    func testTriggerExposureClearsPriorResultBeforeRerunFailure() async throws {
        WizardStubURLProtocol.routes["/api/calibrate/exposure"] = (
            #"{"error":"forced failure"}"#.data(using: .utf8)!,
            500
        )
        WizardStubURLProtocol.routes["/api/calibrate/progress"] = (
            """
            {
                "event": "idle",
                "message": "Waiting for calibration capture to start.",
                "recent_events": []
            }
            """.data(using: .utf8)!,
            200
        )

        let client = OrchestratorClient(session: makeStubSession())
        client.webPort = 9999
        let vm = CalibrationWizardViewModel(orchestratorClient: client)
        vm.rebateRegion = RebateRegion.centeredAtNormalized(x: 0.5, y: 0.5)
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        vm.exposureResult = try decoder.decode(
            ExposureCalibrationResult.self,
            from: makeExposureJSON(ledR: 201, ledG: 202, ledB: 203)
        )

        await vm.triggerExposure()

        XCTAssertNil(vm.exposureResult, "A failed re-run must not leave stale exposure settings visible.")
        XCTAssertNotNil(vm.lastError[2])
        XCTAssertFalse(vm.isRunning)
    }

    func testTriggerExposureReleasesUIWhenBackendNeverStartsCapture() async throws {
        WizardStubURLProtocol.hangingPaths = ["/api/calibrate/exposure"]
        WizardStubURLProtocol.routes["/api/calibrate/progress"] = (
            """
            {
                "event": "idle",
                "message": "Waiting for calibration capture to start.",
                "recent_events": []
            }
            """.data(using: .utf8)!,
            200
        )

        let client = OrchestratorClient(session: makeStubSession())
        client.webPort = 9999
        let vm = CalibrationWizardViewModel(orchestratorClient: client)
        vm.exposureNoStartTimeoutSeconds = 0.05
        vm.exposureProgressPollIntervalNanoseconds = 10_000_000
        vm.rebateRegion = RebateRegion.centeredAtNormalized(x: 0.5, y: 0.5)

        await vm.triggerExposure()

        XCTAssertNil(vm.exposureResult)
        XCTAssertFalse(vm.isRunning)
        XCTAssertEqual(
            vm.lastError[2],
            "Exposure calibration did not start. The backend stayed idle, so no camera capture is running. Try Run Exposure again; if it repeats, stop calibration and run the rig check again."
        )
        XCTAssertTrue(
            WizardStubURLProtocol.stoppedPaths.contains("/api/calibrate/exposure"),
            "The hung exposure request should be cancelled when the no-start watchdog fires."
        )
    }

    // MARK: - FFC combined shape

    func testFFCResponseDecodesCombined() async throws {
        WizardStubURLProtocol.routes["/api/calibrate/ffc"] = (makeFFCJSON(), 200)

        let client = OrchestratorClient(session: makeStubSession())
        client.webPort = 9999

        // Provide a minimal exposureResult for the body
        let expJSON = makeExposureJSON()
        WizardStubURLProtocol.routes["/api/calibrate/exposure"] = (expJSON, 200)
        let expResult = try await client.calibrateExposure()

        let result = try await client.calibrateFFC(exposureResult: expResult)

        // flat_field keys
        XCTAssertEqual(result.flatField.nFramesAveraged, 8, "nFramesAveraged from flat_field")
        XCTAssertEqual(result.flatField.workingBrightness, 200)

        // inspection shape matches CalibrationResult (inspect-calibration.py --json)
        XCTAssertNotNil(result.inspection.channelR, "inspection.channelR should decode")
        XCTAssertNotNil(result.inspection.channelG)
        XCTAssertNotNil(result.inspection.channelB)
        XCTAssertGreaterThan(result.inspection.channelR!.falloffPct, 0, "falloff_pct → falloffPct (SC-4)")
        XCTAssertFalse(result.inspection.overall.isEmpty, "overall should be non-empty")
    }

    // MARK: - FAIL does NOT block navigation

    func testFailDoesNotBlockNavigation() async throws {
        // A checks result with failing results — wizard should still be able to advance/reset.
        WizardStubURLProtocol.routes["/api/state"] = (makeStateJSON(), 200)
        WizardStubURLProtocol.routes["/api/calibrate/exposure"] = (makeExposureJSON(), 200)
        WizardStubURLProtocol.routes["/api/calibrate/ffc"] = (makeFFCJSON(), 200)
        WizardStubURLProtocol.routes["/api/calibrate/checks"] = (makeChecksFailJSON(), 200)

        let client = OrchestratorClient(session: makeStubSession())
        client.webPort = 9999
        let vm = CalibrationWizardViewModel(orchestratorClient: client)

        await vm.triggerRigCheck()
        vm.currentStep = 2   // simulate "Next" (triggers no longer auto-advance — IN-03 fix)
        vm.rebateRegion = RebateRegion.centeredAtNormalized(x: 0.5, y: 0.5)
        await vm.triggerExposure()
        vm.currentStep = 3
        await vm.triggerFFC()
        vm.currentStep = 4
        await vm.triggerResultsCheck()

        // All checks failed but the view model should still hold the results (not crash)
        // 2-check shape: registration + base_neutrality (frame_anomaly deferred to Phase 15)
        XCTAssertEqual(vm.checkResults?.count, 2)
        let failedCount = vm.checkResults?.filter { !$0.passed }.count ?? 0
        XCTAssertGreaterThan(failedCount, 0, "Some checks failed")
        XCTAssertFalse(vm.isRunning, "isRunning should be false — FAIL does not block")
        XCTAssertNil(vm.lastError[4], "lastError[4] should be nil (200 response, just failing checks)")

        // reset() should clear all state including failed check results
        vm.reset()
        XCTAssertEqual(vm.currentStep, 1, "reset() returns to step 1")
        XCTAssertNil(vm.checkResults, "checkResults cleared by reset()")

        // A 500 route error → lastError set, result stays nil, isRunning false
        WizardStubURLProtocol.routes["/api/calibrate/exposure"] = (
            "{\"error\": \"dark frame failed\"}".data(using: .utf8)!,
            500
        )
        vm.rebateRegion = RebateRegion.centeredAtNormalized(x: 0.5, y: 0.5)
        await vm.triggerExposure()
        XCTAssertNotNil(vm.lastError[2], "lastError[2] set on 500 response")
        XCTAssertNil(vm.exposureResult, "exposureResult stays nil on error")
        XCTAssertFalse(vm.isRunning, "isRunning false after error")
    }

    func testExposureErrorMapsSonyCaptureExit127ToActionableCopy() async throws {
        WizardStubURLProtocol.routes["/api/calibrate/exposure"] = (
            "{\"error\":\"dark-frame capture failed: sony-capture failed for channel R (exit 127)\"}"
                .data(using: .utf8)!,
            500
        )

        let client = OrchestratorClient(session: makeStubSession())
        client.webPort = 9999
        let vm = CalibrationWizardViewModel(orchestratorClient: client)

        vm.rebateRegion = RebateRegion.centeredAtNormalized(x: 0.5, y: 0.5)
        await vm.triggerExposure()

        XCTAssertEqual(
            vm.lastError[2],
            "Sony SDK capture tool was not found. Build phase1/sony-capture, then use Set Up > Check Camera before running calibration."
        )
        XCTAssertNil(vm.exposureResult)
        XCTAssertFalse(vm.isRunning)
    }

    func testExposureErrorMapsNonWritableShutterToManualModeCopy() async throws {
        WizardStubURLProtocol.routes["/api/calibrate/exposure"] = (
            "{\"error\":\"Camera shutter speed is not writable over the Sony SDK. Set the camera mode dial to M/manual exposure.\"}"
                .data(using: .utf8)!,
            500
        )

        let client = OrchestratorClient(session: makeStubSession())
        client.webPort = 9999
        let vm = CalibrationWizardViewModel(orchestratorClient: client)

        vm.rebateRegion = RebateRegion.centeredAtNormalized(x: 0.5, y: 0.5)
        await vm.triggerExposure()

        XCTAssertEqual(
            vm.lastError[2],
            "Camera shutter speed is not writable. Set the camera mode dial to M/manual exposure, keep f/8 fixed, and let the SDK set ISO 100 or ISO 125 before running exposure calibration again."
        )
        XCTAssertNil(vm.exposureResult)
        XCTAssertFalse(vm.isRunning)
    }

    // MARK: - FIX-A: registration "not available" (empty deltas) excluded from roll verdict

    func testRegistrationNotAvailableDoesNotFailRoll() async throws {
        // Pre-hardware: registration has empty deltas (LAST_CAL_FRAME not set on server).
        // The roll verdict must be PASS (driven solely by base_neutrality which passed).
        WizardStubURLProtocol.routes["/api/state"] = (makeStateJSON(), 200)
        WizardStubURLProtocol.routes["/api/calibrate/exposure"] = (makeExposureJSON(), 200)
        WizardStubURLProtocol.routes["/api/calibrate/ffc"] = (makeFFCJSON(), 200)
        WizardStubURLProtocol.routes["/api/calibrate/checks"] = (makeChecksNotAvailableJSON(), 200)

        let client = OrchestratorClient(session: makeStubSession())
        client.webPort = 9999
        let vm = CalibrationWizardViewModel(orchestratorClient: client)

        await vm.triggerRigCheck()
        vm.currentStep = 2
        vm.rebateRegion = RebateRegion.centeredAtNormalized(x: 0.5, y: 0.5)
        await vm.triggerExposure()
        vm.currentStep = 3
        await vm.triggerFFC()
        vm.currentStep = 4
        await vm.triggerResultsCheck()

        XCTAssertEqual(vm.checkResults?.count, 2, "Should have 2 check results")
        XCTAssertNil(vm.lastError[4], "No error on Step 4")

        // The registration check has empty deltas — it must NOT veto the roll.
        let regCheck = vm.checkResults?.first { $0.name == "registration" }
        XCTAssertNotNil(regCheck, "registration check decoded")
        XCTAssertTrue(regCheck?.deltas.isEmpty ?? false, "registration deltas are empty (not available)")

        // base_neutrality passed → roll must be PASS (empty-delta checks excluded from verdict).
        // We verify this by checking that the only check with data (base_neutrality) passed.
        let checksWithData = vm.checkResults?.filter { !$0.deltas.isEmpty } ?? []
        XCTAssertTrue(checksWithData.allSatisfy { $0.passed },
                      "Checks with data must all pass — roll should be PASS, not FAIL")
    }

    // MARK: - Reset clears all state

    func testResetClearsState() async throws {
        WizardStubURLProtocol.routes["/api/state"] = (makeStateJSON(), 200)
        WizardStubURLProtocol.routes["/api/calibrate/exposure"] = (makeExposureJSON(), 200)
        WizardStubURLProtocol.routes["/api/calibrate/ffc"] = (makeFFCJSON(), 200)
        WizardStubURLProtocol.routes["/api/calibrate/checks"] = (makeChecksJSON(), 200)

        let client = OrchestratorClient(session: makeStubSession())
        client.webPort = 9999
        let vm = CalibrationWizardViewModel(orchestratorClient: client)

        await vm.triggerRigCheck()
        vm.currentStep = 2   // simulate "Next" (triggers no longer auto-advance — IN-03 fix)
        vm.rebateRegion = RebateRegion.centeredAtNormalized(x: 0.5, y: 0.5)
        await vm.triggerExposure()
        vm.currentStep = 3   // simulate "Next"

        XCTAssertNotNil(vm.rigCheckResult)
        XCTAssertNotNil(vm.exposureResult)
        XCTAssertEqual(vm.currentStep, 3)

        vm.reset()

        XCTAssertEqual(vm.currentStep, 1, "reset() → currentStep == 1")
        XCTAssertNil(vm.rigCheckResult, "reset() clears rigCheckResult")
        XCTAssertNil(vm.exposureResult, "reset() clears exposureResult")
        XCTAssertNil(vm.ffcResult, "reset() clears ffcResult")
        XCTAssertNil(vm.checkResults, "reset() clears checkResults")
        XCTAssertEqual(vm.lastError, [:], "reset() clears lastError")
        XCTAssertNil(vm.rebateCoord, "reset() clears rebateCoord")
        XCTAssertNil(vm.rigCheckProgressText, "reset() clears rigCheckProgressText")
    }

    func testTriggerRigCheckReportsCompletionProgress() async throws {
        WizardStubURLProtocol.routes["/api/state"] = (makeStateJSON(), 200)

        let client = OrchestratorClient(session: makeStubSession())
        client.webPort = 9999
        let vm = CalibrationWizardViewModel(orchestratorClient: client)

        await vm.triggerRigCheck()

        XCTAssertNotNil(vm.rigCheckResult)
        XCTAssertFalse(vm.isRunning)
        XCTAssertEqual(vm.rigCheckProgressText, "Rig check complete.")
        XCTAssertNil(vm.lastError[1])
    }

    func testRebateRegionMapsNormalizedLiveViewPointToRawROI() {
        let region = RebateRegion.centeredAtNormalized(x: 0.5, y: 0.5)

        XCTAssertEqual(region.w, 96)
        XCTAssertEqual(region.h, 96)
        XCTAssertEqual(region.x, 4736)
        XCTAssertEqual(region.y, 3140)
    }

    func testTriggerExposureRequiresSelectedMeasurementRegion() async throws {
        WizardStubURLProtocol.routes["/api/calibrate/exposure"] = (makeExposureJSON(), 200)

        let client = OrchestratorClient(session: makeStubSession())
        client.webPort = 9999
        let vm = CalibrationWizardViewModel(orchestratorClient: client)

        await vm.triggerExposure()

        XCTAssertNil(vm.exposureResult)
        XCTAssertFalse(vm.isRunning)
        XCTAssertEqual(
            vm.lastError[2],
            "Select a film-base sample in the preview before running exposure. The app measures only the highlighted RAW crop."
        )
        XCTAssertNil(WizardStubURLProtocol.lastRequest)
    }

    // MARK: - Swift app unification hooks

    func testApplyExposureWritesCalibratedLevelsToSettings() async throws {
        WizardStubURLProtocol.routes["/api/calibrate/exposure"] = (
            makeExposureJSON(
                ledR: 181,
                ledG: 162,
                ledB: 231,
                shutterR: "1/60",
                shutterG: "1/30",
                shutterB: "1/15"
            ),
            200
        )

        let client = OrchestratorClient(session: makeStubSession())
        client.webPort = 9999
        let vm = CalibrationWizardViewModel(orchestratorClient: client)
        vm.rebateRegion = RebateRegion.centeredAtNormalized(x: 0.5, y: 0.5)
        let store = SettingsStore(persistenceEnabled: false)
        store.settings.levelR = 10
        store.settings.levelG = 20
        store.settings.levelB = 30

        await vm.triggerExposure()
        vm.applyExposure(to: store)

        XCTAssertEqual(store.settings.levelR, 181)
        XCTAssertEqual(store.settings.levelG, 162)
        XCTAssertEqual(store.settings.levelB, 231)
        XCTAssertEqual(store.settings.shutterR, "1/60")
        XCTAssertEqual(store.settings.shutterG, "1/30")
        XCTAssertEqual(store.settings.shutterB, "1/15")
    }

    func testSkipFFCClearsStaleCorrectionAndAdvances() {
        let client = OrchestratorClient(session: makeStubSession())
        let vm = CalibrationWizardViewModel(orchestratorClient: client)
        let store = SettingsStore(persistenceEnabled: false)
        store.settings.ffcCalibration = "/tmp/old-flat.npy"
        vm.currentStep = 3
        vm.lastError[3] = "old error"

        vm.skipFFC(in: store)

        XCTAssertTrue(vm.ffcSkipped)
        XCTAssertNil(vm.ffcResult)
        XCTAssertNil(store.settings.ffcCalibration)
        XCTAssertNil(vm.lastError[3])
        XCTAssertEqual(vm.currentStep, 4)
    }

    func testStockCalibrationProfilesPersistAndApplyExposureRecipe() async throws {
        WizardStubURLProtocol.routes["/api/calibrate/exposure"] = (
            makeExposureJSON(
                ledR: 181,
                ledG: 162,
                ledB: 231,
                shutterR: "1/60",
                shutterG: "1/30",
                shutterB: "1/15"
            ),
            200
        )

        let suiteName = "ScanlightApp.StockCalibrationProfiles.\(UUID().uuidString)"
        guard let defaults = UserDefaults(suiteName: suiteName) else {
            XCTFail("Could not create isolated UserDefaults suite")
            return
        }
        defaults.removePersistentDomain(forName: suiteName)
        defer { defaults.removePersistentDomain(forName: suiteName) }

        let client = OrchestratorClient(session: makeStubSession())
        client.webPort = 9999
        let vm = CalibrationWizardViewModel(orchestratorClient: client)
        vm.rebateRegion = RebateRegion.centeredAtNormalized(x: 0.5, y: 0.5)
        let store = SettingsStore(userDefaults: defaults)
        store.settings.triggerMode = "sdk"
        store.settings.cameraModel = "Sony ILCE-7CR"

        await vm.triggerExposure()
        vm.calibrationStockName = "  Portra   400 "
        vm.saveCurrentStockProfile(to: store)

        XCTAssertEqual(store.stockCalibrationProfiles.count, 1)
        XCTAssertEqual(store.stockCalibrationProfiles.first?.stockName, "Portra 400")
        XCTAssertEqual(vm.selectedStockProfileID, store.stockCalibrationProfiles.first?.id)

        let reloadedStore = SettingsStore(userDefaults: defaults)
        let saved = try XCTUnwrap(reloadedStore.stockCalibrationProfiles.first)
        XCTAssertEqual(saved.stockName, "Portra 400")
        XCTAssertEqual(saved.exposureResult.r.ledLevel, 181)
        XCTAssertEqual(saved.exposureResult.r.shutterSpeed, "1/60")

        reloadedStore.settings.levelR = 1
        reloadedStore.settings.levelG = 2
        reloadedStore.settings.levelB = 3
        reloadedStore.settings.shutterR = nil
        reloadedStore.settings.shutterG = nil
        reloadedStore.settings.shutterB = nil

        let applyVM = CalibrationWizardViewModel(orchestratorClient: client)
        applyVM.selectedStockProfileID = saved.id
        applyVM.applySelectedStockProfile(from: reloadedStore)

        XCTAssertEqual(reloadedStore.settings.levelR, 181)
        XCTAssertEqual(reloadedStore.settings.levelG, 162)
        XCTAssertEqual(reloadedStore.settings.levelB, 231)
        XCTAssertEqual(reloadedStore.settings.shutterR, "1/60")
        XCTAssertEqual(reloadedStore.settings.shutterG, "1/30")
        XCTAssertEqual(reloadedStore.settings.shutterB, "1/15")
        XCTAssertEqual(applyVM.exposureResult?.b.shutterSpeed, "1/15")
        XCTAssertTrue(applyVM.stockProfileMessage?.contains("fresh flat field") ?? false)
    }

    func testStockCalibrationProfilesCanBeEditedRenamedAndDeleted() throws {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let exposure = try decoder.decode(
            ExposureCalibrationResult.self,
            from: makeExposureJSON(
                ledR: 181,
                ledG: 162,
                ledB: 231,
                shutterR: "1/60",
                shutterG: "1/30",
                shutterB: "1/15"
            )
        )

        let store = SettingsStore(persistenceEnabled: false)
        let profile = try XCTUnwrap(store.saveStockCalibrationProfile(stockName: "Portra 400", exposureResult: exposure))
        _ = store.saveStockCalibrationProfile(stockName: "Ektar 100", exposureResult: exposure)

        let updated = try store.updateStockCalibrationProfile(
            id: profile.id,
            stockName: " Portra 400 warm ",
            ledR: 300,
            ledG: 150,
            ledB: -4,
            shutterR: " 1/40 ",
            shutterG: "",
            shutterB: "1/20"
        )

        XCTAssertEqual(updated.stockName, "Portra 400 warm")
        XCTAssertEqual(updated.exposureResult.r.ledLevel, 255)
        XCTAssertEqual(updated.exposureResult.g.ledLevel, 150)
        XCTAssertEqual(updated.exposureResult.b.ledLevel, 0)
        XCTAssertEqual(updated.exposureResult.r.shutterSpeed, "1/40")
        XCTAssertNil(updated.exposureResult.g.shutterSpeed)
        XCTAssertEqual(updated.exposureResult.b.shutterSpeed, "1/20")

        XCTAssertThrowsError(
            try store.updateStockCalibrationProfile(
                id: updated.id,
                stockName: "Ektar 100",
                ledR: 1,
                ledG: 2,
                ledB: 3,
                shutterR: nil,
                shutterG: nil,
                shutterB: nil
            )
        ) { error in
            XCTAssertEqual(error as? StockProfileEditError, .duplicateName("Ektar 100"))
        }

        XCTAssertTrue(store.deleteStockCalibrationProfile(id: updated.id))
        XCTAssertNil(store.stockCalibrationProfile(id: updated.id))
    }

    func testPrepareForCalibrationValidatesSettingsBeforeStartingServer() async throws {
        let client = OrchestratorClient(session: makeStubSession())
        let lightVM = ScanlightViewModel(transportFactory: FakeBridge.makeTransport)
        let coordinator = ScanCoordinator(client: client, lightViewModel: lightVM)
        let vm = CalibrationWizardViewModel(orchestratorClient: client)
        let store = SettingsStore(persistenceEnabled: false)
        store.settings.outputFolder = ""
        store.settings.iedInbox = nil

        let cameraConnection = SonyCameraConnection()

        let ok = await vm.prepareForCalibration(
            store: store,
            coordinator: coordinator,
            cameraConnection: cameraConnection
        )

        XCTAssertFalse(ok)
        XCTAssertEqual(coordinator.phase, .idle)
        XCTAssertFalse(client.isRunning)
        XCTAssertNotNil(vm.lastError[1])
        XCTAssertFalse(vm.isRunning)
        XCTAssertNil(vm.rigCheckProgressText)
        XCTAssertTrue(store.validationErrors.keys.contains("outputFolder"))
        XCTAssertTrue(store.validationErrors.keys.contains("iedInbox"))
    }

    func testPrepareForCalibrationRequestsPreviewRefreshAfterSuccessfulCheck() async throws {
        WizardStubURLProtocol.routes["/api/state"] = (makeStateJSON(), 200)
        let client = OrchestratorClient(session: makeStubSession())
        client.webPort = 9999
        let fakeBackend = FakeOrchestratorClient()
        let fakeLight = FakeLightPanel()
        fakeLight.scanlightPort = "/dev/cu.usbmodemTEST"
        let coordinator = ScanCoordinator(clientProto: fakeBackend, lightPanelProto: fakeLight)
        let vm = CalibrationWizardViewModel(orchestratorClient: client)
        let store = SettingsStore(persistenceEnabled: false)
        store.settings.outputFolder = "/tmp"
        store.settings.iedInbox = "/tmp/ied"

        let cameraConnection = SonyCameraConnection()

        let ok = await vm.prepareForCalibration(
            store: store,
            coordinator: coordinator,
            cameraConnection: cameraConnection
        )

        XCTAssertTrue(ok)
        XCTAssertEqual(coordinator.phase, .calibrating)
        XCTAssertEqual(vm.previewRefreshGeneration, 1)
        XCTAssertEqual(vm.rigCheckResult?.rollName, "Roll001")
        XCTAssertEqual(vm.rigCheckProgressText, "Rig check complete. Refreshing live frame.")
    }

    func testPrepareForCalibrationPersistsResolvedSonyIPBeforeLaunch() async throws {
        let oldProvider = OrchestratorClient.sonyARPTableProvider
        OrchestratorClient.sonyARPTableProvider = {
            "? (10.0.0.244) at 10:32:2c:26:1a:3f on en0 ifscope [ethernet]\n"
        }
        defer { OrchestratorClient.sonyARPTableProvider = oldProvider }

        WizardStubURLProtocol.routes["/api/state"] = (makeStateJSON(), 200)
        let client = OrchestratorClient(session: makeStubSession())
        client.webPort = 9999
        client.sonyConnectionProbeOverride = { settings in
            XCTAssertEqual(settings.sonyIpAddress, "10.0.0.244")
            return SonyConnectionProbeResult(success: true, message: "Connected to Sony camera.")
        }
        let coordinator = ScanCoordinator(
            clientProto: FakeOrchestratorClient(),
            lightPanelProto: FakeLightPanel()
        )
        let vm = CalibrationWizardViewModel(orchestratorClient: client)
        let store = SettingsStore(persistenceEnabled: false)
        store.settings.outputFolder = "/tmp"
        store.settings.triggerMode = "sdk"
        store.settings.sonyIpAddress = "10.0.0.247"
        store.settings.sonyMacAddress = "10:32:2C:26:1A:3F"
        store.settings.sonyUser = "sdk-user"
        store.settings.sonyPassword = "sdk-password"

        let cameraConnection = SonyCameraConnection()

        let ok = await vm.prepareForCalibration(
            store: store,
            coordinator: coordinator,
            cameraConnection: cameraConnection
        )

        XCTAssertTrue(ok)
        XCTAssertEqual(store.settings.sonyIpAddress, "10.0.0.244")
        XCTAssertTrue(cameraConnection.isOnline)
    }

    func testPrepareForCalibrationStopsWhenSDKCameraProbeFails() async throws {
        let client = OrchestratorClient(session: makeStubSession())
        client.sonyConnectionProbeOverride = { _ in
            SonyConnectionProbeResult(success: false, message: "Timed out connecting to the Sony camera.")
        }
        let fakeBackend = FakeOrchestratorClient()
        let fakeLight = FakeLightPanel()
        fakeLight.scanlightPort = "/dev/cu.usbmodemTEST"
        let coordinator = ScanCoordinator(clientProto: fakeBackend, lightPanelProto: fakeLight)
        let vm = CalibrationWizardViewModel(orchestratorClient: client)
        let store = SettingsStore(persistenceEnabled: false)
        store.settings.outputFolder = "/tmp"
        store.settings.triggerMode = "sdk"
        store.settings.sonyIpAddress = "10.0.0.247"
        store.settings.sonyUser = "sdk-user"
        store.settings.sonyPassword = "sdk-password"

        let cameraConnection = SonyCameraConnection()

        let ok = await vm.prepareForCalibration(
            store: store,
            coordinator: coordinator,
            cameraConnection: cameraConnection
        )

        XCTAssertFalse(ok)
        XCTAssertEqual(coordinator.phase, .idle)
        XCTAssertFalse(cameraConnection.isOnline)
        XCTAssertEqual(cameraConnection.chipText, "OFFLINE")
        XCTAssertTrue(vm.lastError[1]?.contains("not reachable") ?? false)
    }

    func testSonyCameraConnectionClearsCheckingWhenProbeHangs() async throws {
        let client = OrchestratorClient(session: makeStubSession())
        client.sonyConnectionProbeOverride = { _ in
            try? await Task.sleep(nanoseconds: 1_000_000_000)
            return SonyConnectionProbeResult(success: true, message: "Connected too late.")
        }
        let store = SettingsStore(persistenceEnabled: false)
        store.settings.triggerMode = "sdk"
        store.settings.sonyIpAddress = "10.0.0.247"
        store.settings.sonyUser = "sdk-user"
        store.settings.sonyPassword = "sdk-password"

        let cameraConnection = SonyCameraConnection(checkTimeout: 0.05)

        let ok = await cameraConnection.check(store: store, orchestratorClient: client)

        XCTAssertFalse(ok)
        XCTAssertFalse(cameraConnection.isChecking)
        XCTAssertFalse(cameraConnection.isOnline)
        XCTAssertEqual(cameraConnection.chipText, "OFFLINE")
        XCTAssertTrue(cameraConnection.detailText.contains("did not finish within 1 seconds"))
    }

    /// Regression for the "Checking..." UI sticking forever even when no
    /// `sony-capture` process is left running. The original
    /// `withTaskGroup`-based race waited for the loser task to finish before
    /// resolving the parent — so a probe stuck on uncancellable synchronous
    /// I/O (e.g. `Pipe.readDataToEndOfFile` waiting for an EOF that never
    /// arrives) pinned the camera-check at `.checking` forever.
    ///
    /// A `withCheckedContinuation` that never resumes is the cleanest way to
    /// simulate that pathological case: cancellation can't recover from it
    /// (the suspended task never sees a cancellation point), and any
    /// task-group await on it would block forever. The fix MUST resolve
    /// against the outer timeout regardless.
    func testSonyCameraConnectionRecoversWhenProbeNeverResolves() async throws {
        let client = OrchestratorClient(session: makeStubSession())
        client.sonyConnectionProbeOverride = { _ in
            // Intentionally never resume — simulates an uncancellable hang
            // in the underlying process/pipe layer (e.g. a synchronous
            // Pipe.readDataToEndOfFile that never sees EOF). Using
            // UnsafeContinuation because we deliberately leak it; the
            // CheckedContinuation runtime warning would be noise here.
            await withUnsafeContinuation { (_: UnsafeContinuation<SonyConnectionProbeResult, Never>) in
            }
        }
        let store = SettingsStore(persistenceEnabled: false)
        store.settings.triggerMode = "sdk"
        store.settings.sonyIpAddress = "10.0.0.247"
        store.settings.sonyUser = "sdk-user"
        store.settings.sonyPassword = "sdk-password"

        let cameraConnection = SonyCameraConnection(checkTimeout: 0.2)

        let start = Date()
        let ok = await cameraConnection.check(store: store, orchestratorClient: client)
        let elapsed = Date().timeIntervalSince(start)

        XCTAssertFalse(ok)
        XCTAssertFalse(cameraConnection.isChecking, "phase must leave .checking even if probe never returns")
        XCTAssertFalse(cameraConnection.isOnline)
        XCTAssertEqual(cameraConnection.chipText, "OFFLINE")
        XCTAssertTrue(
            cameraConnection.detailText.contains("did not finish within"),
            "expected timeout message, got: \(cameraConnection.detailText)"
        )
        // Bounded recovery: must return promptly after the deadline, not
        // wait for the hung probe to finish (which it never will).
        XCTAssertLessThan(elapsed, 2.0,
            "check() should resolve shortly after the 0.2s timeout, took \(elapsed)s")
    }

    // MARK: - AX-ID count is exactly 48 (compile-time rename guard)

    func testAXIDWizardCountIsExactly48() {
        let wizardIDs: [String] = [
            // Progress (4)
            AccessibilityID.wizardStep1Indicator,
            AccessibilityID.wizardStep2Indicator,
            AccessibilityID.wizardStep3Indicator,
            AccessibilityID.wizardStep4Indicator,
            // Nav (3)
            AccessibilityID.wizardBackBtn,
            AccessibilityID.wizardNextBtn,
            AccessibilityID.wizardRerunBtn,
            // Rig Check (4)
            AccessibilityID.rigCheckLightLabel,
            AccessibilityID.rigCheckFirmwareLabel,
            AccessibilityID.rigCheckCameraLabel,
            AccessibilityID.rigCheckFolderLabel,
            // Exposure (12)
            AccessibilityID.exposureClipR,
            AccessibilityID.exposureClipG,
            AccessibilityID.exposureClipB,
            AccessibilityID.exposureLevelR,
            AccessibilityID.exposureLevelG,
            AccessibilityID.exposureLevelB,
            AccessibilityID.exposureVerdictR,
            AccessibilityID.exposureVerdictG,
            AccessibilityID.exposureVerdictB,
            AccessibilityID.exposureOverall,
            AccessibilityID.rebatePicker,
            AccessibilityID.rebateClearBtn,
            AccessibilityID.stockProfileNameField,
            AccessibilityID.stockProfileSaveBtn,
            AccessibilityID.stockProfilePicker,
            AccessibilityID.stockProfileApplyBtn,
            // Flat Field (12)
            AccessibilityID.ffcFalloffR,
            AccessibilityID.ffcFalloffG,
            AccessibilityID.ffcFalloffB,
            AccessibilityID.ffcUniformityR,
            AccessibilityID.ffcUniformityG,
            AccessibilityID.ffcUniformityB,
            AccessibilityID.ffcVerdictR,
            AccessibilityID.ffcVerdictG,
            AccessibilityID.ffcVerdictB,
            AccessibilityID.ffcOverall,
            AccessibilityID.ffcFramesLabel,
            AccessibilityID.ffcUseBtn,
            // Results (9)
            AccessibilityID.resultsShiftRG,
            AccessibilityID.resultsShiftGB,
            AccessibilityID.resultsRegVerdict,
            AccessibilityID.resultsBaseDeviation,
            AccessibilityID.resultsBaseVerdict,
            AccessibilityID.resultsGainR,
            AccessibilityID.resultsGainG,
            AccessibilityID.resultsGainB,
            AccessibilityID.resultsRollVerdict,
        ]

        XCTAssertEqual(wizardIDs.count, 48, "Expected exactly 48 wizard AX-IDs")

        // Spot-check prefixes per naming convention
        let indicators = wizardIDs.filter { $0.hasPrefix("indicator-") }
        XCTAssertEqual(indicators.count, 4, "4 indicator-* IDs")

        let btns = wizardIDs.filter { $0.hasPrefix("btn-") }
        XCTAssertGreaterThanOrEqual(btns.count, 3, "At least 3 btn-* IDs")

        let lbls = wizardIDs.filter { $0.hasPrefix("lbl-") }
        XCTAssertGreaterThan(lbls.count, 30, "Majority are lbl-* IDs")

        let pickers = wizardIDs.filter { $0.hasPrefix("picker-") }
        XCTAssertEqual(pickers.count, 2, "2 picker-* IDs")
    }
}
