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
    static var lastRequest: URLRequest? = nil
    static var lastBody: Data? = nil

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

private func makeExposureJSON(ledR: Int = 180, ledG: Int = 160, ledB: Int = 230) -> Data {
    let json = """
    {
        "r": {
            "channel": "R",
            "led_level": \(ledR),
            "black_level": 256.0,
            "gain": 1.234,
            "clip_fraction": 0.02,
            "schema_version": 1
        },
        "g": {
            "channel": "G",
            "led_level": \(ledG),
            "black_level": 260.0,
            "gain": 1.100,
            "clip_fraction": 0.01,
            "schema_version": 1
        },
        "b": {
            "channel": "B",
            "led_level": \(ledB),
            "black_level": 258.0,
            "gain": 1.500,
            "clip_fraction": 0.03,
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
    let json = """
    [
        {"name": "registration", "passed": true, "deltas": {"rg_shift": 0.12, "gb_shift": 0.08}, "schema_version": 1},
        {"name": "base_neutrality", "passed": true, "deltas": {"base_r": 8930.0, "base_g": 12097.0, "base_b": 2952.0}, "schema_version": 1},
        {"name": "frame_anomaly", "passed": true, "deltas": {}, "schema_version": 1}
    ]
    """
    return json.data(using: .utf8)!
}

private func makeChecksFailJSON() -> Data {
    let json = """
    [
        {"name": "registration", "passed": false, "deltas": {"rg_shift": 2.5, "gb_shift": 1.8}, "schema_version": 1},
        {"name": "base_neutrality", "passed": false, "deltas": {"deviation": 800.0}, "schema_version": 1},
        {"name": "frame_anomaly", "passed": true, "deltas": {}, "schema_version": 1}
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
        WizardStubURLProtocol.lastRequest = nil
        WizardStubURLProtocol.lastBody = nil
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

        // Step 1 — rig check
        await vm.triggerRigCheck()
        XCTAssertNotNil(vm.rigCheckResult, "rigCheckResult should be set after Step 1")
        XCTAssertEqual(vm.currentStep, 2, "Step should advance to 2")
        XCTAssertNil(vm.lastError[1], "No error on Step 1")

        // Step 2 — exposure
        await vm.triggerExposure()
        XCTAssertNotNil(vm.exposureResult, "exposureResult should be set after Step 2")
        XCTAssertEqual(vm.exposureResult?.r.ledLevel, 180, "R LED level should decode")
        XCTAssertEqual(vm.currentStep, 3, "Step should advance to 3")
        XCTAssertNil(vm.lastError[2], "No error on Step 2")

        // Step 3 — FFC
        await vm.triggerFFC()
        XCTAssertNotNil(vm.ffcResult, "ffcResult should be set after Step 3")
        XCTAssertEqual(vm.ffcResult?.flatField.nFramesAveraged, 8)
        XCTAssertEqual(vm.currentStep, 4, "Step should advance to 4")
        XCTAssertNil(vm.lastError[3], "No error on Step 3")

        // Step 4 — results checks
        await vm.triggerResultsCheck()
        XCTAssertEqual(vm.checkResults?.count, 3, "Should have 3 check results")
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
        XCTAssertGreaterThan(result.r.clipFraction, 0)
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
        await vm.triggerExposure()
        await vm.triggerFFC()
        await vm.triggerResultsCheck()

        // All checks failed but the view model should still hold the results (not crash)
        XCTAssertEqual(vm.checkResults?.count, 3)
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
        await vm.triggerExposure()
        XCTAssertNotNil(vm.lastError[2], "lastError[2] set on 500 response")
        XCTAssertNil(vm.exposureResult, "exposureResult stays nil on error")
        XCTAssertFalse(vm.isRunning, "isRunning false after error")
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
        await vm.triggerExposure()

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
    }

    // MARK: - AX-ID count is exactly 44 (compile-time rename guard)

    func testAXIDWizardCountIsExactly44() {
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

        XCTAssertEqual(wizardIDs.count, 44, "Expected exactly 44 wizard AX-IDs")

        // Spot-check prefixes per naming convention
        let indicators = wizardIDs.filter { $0.hasPrefix("indicator-") }
        XCTAssertEqual(indicators.count, 4, "4 indicator-* IDs")

        let btns = wizardIDs.filter { $0.hasPrefix("btn-") }
        XCTAssertGreaterThanOrEqual(btns.count, 3, "At least 3 btn-* IDs")

        let lbls = wizardIDs.filter { $0.hasPrefix("lbl-") }
        XCTAssertGreaterThan(lbls.count, 30, "Majority are lbl-* IDs")

        let pickers = wizardIDs.filter { $0.hasPrefix("picker-") }
        XCTAssertEqual(pickers.count, 1, "1 picker-* ID")
    }
}
