// SettingsCalibrationUITests — Phase 06 acceptance test suite.
//
// Covers:
//   R-20: Settings form validates input; Save Settings pushes applyRuntimeSettings
//         when the orchestrator is running (testSaveSettingsPushesRuntimeSettings).
//   NFR-09: Every new Phase 06 AX-ID has >= 1 rendered element (testNewAccessibilityIDsRendered).
//
// Note: R-21 (CalibrationViewModel stub-runner test) was removed in Phase 14 when
// CalibrationView/CalibrationViewModel were replaced by the guided CalibrationWizardView.
// The wizard's view-model is tested in CalibrationWizardViewModelTests.swift.
//
// Test strategy: unit tests (no app launch) for validation logic and the
// applyRuntimeSettings runtime push. A single XCUIApplication test
// (testNewAccessibilityIDsRendered) covers the Settings AX-ID coverage gate; it
// skips gracefully in headless CI.
//
// URLProtocol injection for testSaveSettingsPushesRuntimeSettings mirrors the exact
// pattern from OrchestratorClientTests (StubURLProtocol + makeStubSession).

import XCTest
@testable import ScanlightApp

// MARK: - Test Suite

@MainActor
final class SettingsCalibrationUITests: XCTestCase {

    override func setUpWithError() throws {
        continueAfterFailure = false
        // Reset StubURLProtocol state before each test so routes from one test
        // cannot leak into another.
        StubURLProtocol.routes = [:]
        StubURLProtocol.lastRequest = nil
        StubURLProtocol.lastBody = nil
    }

    // MARK: - AX-ID array (16 Settings-view cases)

    /// The 16 Settings-view AccessibilityID string values introduced in Phase 06.
    /// Using the type constants (not string literals) so a rename fails at compile time.
    ///
    /// Note: the 6 old Calibration-view IDs (calCaptureBtn, calVerdictR/G/B,
    /// calOverallLabel, calUseBtn) were removed in Phase 14 along with
    /// CalibrationView.swift/CalibrationViewModel.swift.
    private let newAccessibilityIDs: [String] = [
        // Settings view (16)
        AccessibilityID.settingsRollNameField,
        AccessibilityID.settingsPickOutputBtn,
        AccessibilityID.settingsOutputPathLabel,
        AccessibilityID.settingsTriggerModePicker,
        AccessibilityID.settingsPickInboxBtn,
        AccessibilityID.settingsInboxPathLabel,
        AccessibilityID.settingsLevelRSlider,
        AccessibilityID.settingsLevelGSlider,
        AccessibilityID.settingsLevelBSlider,
        AccessibilityID.settingsSettleStepper,
        AccessibilityID.settingsPickFfcBtn,
        AccessibilityID.settingsFfcPathLabel,
        AccessibilityID.settingsCameraModelPicker,
        AccessibilityID.settingsStreamToggle,
        AccessibilityID.settingsCompositeFormat,
        AccessibilityID.settingsSaveBtn,
    ]

    // MARK: - Test 1: AX-ID coverage gate (requires window server)

    /// Verify that every Phase 06 Settings AX-ID has at least one rendered SwiftUI element.
    ///
    /// Requires accessibility permissions and a window server. Skips gracefully
    /// when neither is available (headless CI, SSH). Navigates to the Settings tab
    /// to cover the 16 Settings IDs.
    ///
    /// Note: the 6 old Calibration-view IDs were removed in Phase 14.
    /// The new wizard IDs are covered by CalibrationWizardUITests.
    func testNewAccessibilityIDsRendered() throws {
        let app = try makeFakeApp()
        app.launch()
        defer { app.terminate() }

        guard app.windows.firstMatch.waitForExistence(timeout: 5.0) else {
            throw XCTSkip("App window did not appear — Accessibility permission may not be granted, or running headless.")
        }

        // Navigate to the Settings tab and check the 16 Settings AX-IDs.
        let settingsTab = app.tabs["Settings"]
        if settingsTab.exists { settingsTab.click() }

        for id in newAccessibilityIDs {
            let matches = app.descendants(matching: .any).matching(identifier: id)
            XCTAssertGreaterThanOrEqual(
                matches.count, 1,
                "New Settings AccessibilityID '\(id)' has zero rendered descendants"
            )
        }
    }

    // MARK: - Test 2: Roll name validation rejects spaces

    /// Unit test — no app launch.
    func testRollNameValidationRejectsSpaces() {
        let store = SettingsStore()
        store.settings.rollName = "Roll With Spaces"
        let errors = store.validate()
        XCTAssertNotNil(errors["rollName"],
                        "Expected rollName error for name with spaces")
        XCTAssertTrue(errors["rollName"]?.contains("spaces") ?? false,
                      "Error should mention 'spaces', got: \(errors["rollName"] ?? "nil")")
    }

    // MARK: - Test 3: Roll name validation rejects empty

    /// Unit test — no app launch.
    func testRollNameValidationRejectsEmpty() {
        let store = SettingsStore()
        store.settings.rollName = ""
        let errors = store.validate()
        XCTAssertNotNil(errors["rollName"],
                        "Expected rollName error for empty name")
        XCTAssertTrue(errors["rollName"]?.contains("required") ?? false,
                      "Error should mention 'required', got: \(errors["rollName"] ?? "nil")")
    }

    // MARK: - Test 4: HW trigger requires IED inbox

    /// Unit test — no app launch.
    func testHWTriggerRequiresIedInbox() {
        let store = SettingsStore()
        store.settings.triggerMode = "hw"
        store.settings.iedInbox = nil
        let errors = store.validate()
        XCTAssertNotNil(errors["iedInbox"],
                        "Expected iedInbox error when triggerMode == hw and iedInbox is nil")
    }

    // MARK: - Test 5: SDK trigger does not require IED inbox

    /// Unit test — no app launch.
    func testSDKTriggerDoesNotRequireIedInbox() {
        let store = SettingsStore()
        store.settings.triggerMode = "sdk"
        store.settings.iedInbox = nil
        store.settings.rollName = "Roll001"
        store.settings.outputFolder = "/tmp/out"
        let errors = store.validate()
        XCTAssertNil(errors["iedInbox"],
                     "SDK trigger should not require iedInbox, got: \(errors["iedInbox"] ?? "nil")")
    }

    // MARK: - Test 6: Save Settings pushes applyRuntimeSettings when orchestrator is running

    /// Unit test — no app launch.
    ///
    /// R-20: Save Settings calls applyRuntimeSettings when orchestrator is running
    /// → POST /api/settings with levelR/G/B. When not running → no POST.
    ///
    /// Exercises the ACTUAL wiring: creates a ScanSettingsView with the
    /// orchestratorClient injected and calls saveSettings() — the same method
    /// that the Save button action now invokes — to verify the wiring is correct,
    /// not just the capability of applyRuntimeSettings in isolation.
    ///
    /// Uses the StubURLProtocol injection pattern from OrchestratorClientTests.
    func testSaveSettingsPushesRuntimeSettings() async throws {
        // Pre-program StubURLProtocol to return HTTP 200 for POST /api/settings
        let stateResponseData = """
        {
            "roll_name": "TestRoll",
            "frame_number": 1,
            "output_folder": "/tmp/out/TestRoll",
            "level_r": 210,
            "level_g": 195,
            "level_b": 220,
            "settle_ms": 50
        }
        """.data(using: .utf8)!
        StubURLProtocol.routes["/api/settings"] = (stateResponseData, 200)

        // Build OrchestratorClient with stub session (same pattern as OrchestratorClientTests)
        let stubSession = makeStubSession()
        let orchestratorClient = OrchestratorClient(session: stubSession)
        orchestratorClient.webPort = 9999
        // Mark the orchestrator as running (matches pattern from testTerminationHandlerClearsIsRunningOnChildExit)
        orchestratorClient.isRunning = true

        // Build SettingsStore with known level values.
        // Use triggerMode "sdk" so iedInbox is not required — the test focuses on
        // the applyRuntimeSettings POST path, not trigger-mode validation.
        let store = SettingsStore()
        store.settings.levelR = 210
        store.settings.levelG = 195
        store.settings.levelB = 220
        store.settings.rollName = "TestRoll"
        store.settings.outputFolder = "/tmp/out"
        store.settings.triggerMode = "sdk"

        // Create ScanSettingsView with the orchestratorClient injected — this verifies
        // the wiring (not just calling applyRuntimeSettings directly). Call saveSettings()
        // which is the exact method the Save button action delegates to.
        let view = ScanSettingsView(store: store, orchestratorClient: orchestratorClient)
        await view.saveSettings()

        // Assert: validation passed (no errors after saveSettings())
        XCTAssertTrue(store.validationErrors.isEmpty,
                      "Validation should pass for valid settings, errors: \(store.validationErrors)")

        // Assert: POST /api/settings was sent with level fields
        XCTAssertNotNil(StubURLProtocol.lastRequest,
                        "StubURLProtocol should have intercepted a request")
        XCTAssertEqual(StubURLProtocol.lastRequest?.httpMethod, "POST",
                       "Save Settings must POST to /api/settings")
        XCTAssertEqual(StubURLProtocol.lastRequest?.url?.path, "/api/settings",
                       "Request path must be /api/settings")

        // Decode the request body and verify level fields
        guard let bodyData = StubURLProtocol.lastBody else {
            XCTFail("No request body was captured by StubURLProtocol")
            return
        }

        let decoded = try JSONSerialization.jsonObject(with: bodyData) as? [String: Any]
        guard let decoded = decoded else {
            XCTFail("Could not decode request body as [String: Any]")
            return
        }

        XCTAssertEqual(decoded["level_r"] as? Int, 210,
                       "Expected level_r = 210, got: \(decoded["level_r"] ?? "nil")")
        XCTAssertEqual(decoded["level_g"] as? Int, 195,
                       "Expected level_g = 195, got: \(decoded["level_g"] ?? "nil")")
        XCTAssertEqual(decoded["level_b"] as? Int, 220,
                       "Expected level_b = 220, got: \(decoded["level_b"] ?? "nil")")

        // applyRuntimeSettings sends ONLY levels + settle_ms (not output_folder or roll_name)
        // to avoid the output_folder asymmetry (RESEARCH.md Finding 9).
        XCTAssertNil(decoded["output_folder"],
                     "applyRuntimeSettings must NOT send output_folder")
        XCTAssertNil(decoded["roll_name"],
                     "applyRuntimeSettings must NOT send roll_name")

        // Negative case: when orchestrator is NOT running, Save must not POST.
        StubURLProtocol.lastRequest = nil
        StubURLProtocol.lastBody = nil

        orchestratorClient.isRunning = false
        await view.saveSettings()

        XCTAssertNil(StubURLProtocol.lastRequest,
                     "When orchestrator is not running, Save Settings must not POST to /api/settings")
    }

    // MARK: - Private helpers

    /// Build an XCUIApplication pointed at the sibling binary (copied verbatim
    /// from AccessibilityIDCoverageTests for parity — see that file for rationale).
    private func makeFakeApp() throws -> XCUIApplication {
        let buildDir = binaryDirectory()
        let binaryURL = buildDir.appendingPathComponent("scanlight-app")

        let fm = FileManager.default
        guard fm.isExecutableFile(atPath: binaryURL.path) else {
            throw XCTSkip("scanlight-app binary not found at \(binaryURL.path) — run `swift build --product scanlight-app` first")
        }

        let app = XCUIApplication(url: binaryURL)
        app.launchArguments = ["-FakeTransport", "YES"]
        return app
    }

    /// Returns the directory containing the test bundle.
    /// (Copied verbatim from AccessibilityIDCoverageTests.)
    private func binaryDirectory() -> URL {
        let testBundleURL = URL(fileURLWithPath: Bundle(for: Self.self).bundlePath)
        if testBundleURL.pathExtension == "xctest" {
            return testBundleURL.deletingLastPathComponent()
        }
        return testBundleURL.deletingLastPathComponent()
    }
}

// MARK: - StubURLProtocol helpers (re-declared locally to avoid cross-target import)
//
// Note: StubURLProtocol is defined in OrchestratorClientTests.swift in the same
// ScanlightAppUITests target. It is visible here via the shared target without
// re-declaration. makeStubSession() is a file-private helper in that file, so
// we replicate the minimal construction here.

private func makeStubSession() -> URLSession {
    let config = URLSessionConfiguration.ephemeral
    config.protocolClasses = [StubURLProtocol.self]
    return URLSession(configuration: config)
}
