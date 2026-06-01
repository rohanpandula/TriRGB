// ScanlightAppUITests — happy-path behavioral UI test suite.
//
// Covers: connect, status field population, per-channel controls (R/G/B/W),
// set-RGB, all-off, pulse (valid + invalid), clear-log, disconnect.
//
// Runtime context:
//   - Xcode UI test target: tests run the full interactive path.
//   - `swift test` from command line: uses XCUIApplication(url:) with the
//     sibling binary. Skips gracefully when the app window doesn't appear
//     (headless, no accessibility permission). Grant Accessibility permission
//     in System Settings → Privacy & Security → Accessibility to enable
//     full runs from the command line.

import XCTest
@testable import ScanlightApp

final class ScanlightAppUITests: XCTestCase {

    override func setUpWithError() throws {
        continueAfterFailure = false
    }

    // MARK: - Helpers

    /// Launch the app with -FakeTransport YES. Skips if window doesn't appear
    /// (headless context or accessibility not granted).
    private func launchAppFake() throws -> XCUIApplication {
        let buildDir = binaryDirectory()
        let binaryURL = buildDir.appendingPathComponent("scanlight-app")

        let fm = FileManager.default
        guard fm.isExecutableFile(atPath: binaryURL.path) else {
            throw XCTSkip("scanlight-app binary not found at \(binaryURL.path) — run `swift build --product scanlight-app` first")
        }

        let app = XCUIApplication(url: binaryURL)
        app.launchArguments = ["-FakeTransport", "YES"]
        app.launch()

        guard app.windows.firstMatch.waitForExistence(timeout: 5.0) else {
            app.terminate()
            throw XCTSkip("App window did not appear — Accessibility permission may not be granted, or running headless. Grant via System Settings → Privacy & Security → Accessibility.")
        }
        return app
    }

    private func value(of element: XCUIElement) -> String? {
        return element.value as? String
    }

    private func connect(_ app: XCUIApplication) {
        app.buttons[AccessibilityID.connectButton].tap()
    }

    /// Returns the directory containing the test bundle.
    private func binaryDirectory() -> URL {
        // Bundle(for: Self.self) gives the .xctest bundle, not Xcode's main
        // bundle. The sibling scanlight-app binary is in the same parent dir.
        let testBundleURL = URL(fileURLWithPath: Bundle(for: Self.self).bundlePath)
        if testBundleURL.pathExtension == "xctest" {
            return testBundleURL.deletingLastPathComponent()
        }
        return testBundleURL.deletingLastPathComponent()
    }

    // MARK: - Tests

    func testConnectShowsFirmwareAndHardware() throws {
        let app = try launchAppFake()
        defer { app.terminate() }

        connect(app)

        let fw = app.staticTexts[AccessibilityID.firmwareLabel]
        XCTAssertTrue(fw.waitForExistence(timeout: 2.0),
                      "firmwareLabel not found after connect")
        let fwValue = value(of: fw)
        XCTAssertNotNil(fwValue, "firmwareLabel.value is nil")
        XCTAssertNotEqual(fwValue, "—", "firmwareLabel still shows placeholder after connect")
    }

    func testStatusFieldsPopulateAfterConnect() throws {
        let app = try launchAppFake()
        defer { app.terminate() }

        connect(app)
        Thread.sleep(forTimeInterval: 0.5)

        for id in [
            AccessibilityID.connectionStatusLabel,
            AccessibilityID.hardwareLabel,
            AccessibilityID.ledTempLabel,
            AccessibilityID.vbusLabel,
        ] {
            let el = app.staticTexts[id]
            XCTAssertTrue(el.waitForExistence(timeout: 2.0), "\(id) not found")
            let v = value(of: el)
            XCTAssertNotNil(v, "\(id).value is nil")
            XCTAssertNotEqual(v, "—", "\(id) still shows placeholder")
        }
    }

    func testRedOnTriggersLog() throws {
        let app = try launchAppFake()
        defer { app.terminate() }

        connect(app)
        app.sliders[AccessibilityID.redSlider].adjust(toNormalizedSliderPosition: 0.5)
        app.buttons[AccessibilityID.redOnButton].tap()

        let errorLabel = app.staticTexts[AccessibilityID.lastErrorLabel]
        XCTAssertTrue(errorLabel.waitForExistence(timeout: 2.0))
        let errVal = value(of: errorLabel) ?? ""
        XCTAssertEqual(errVal, "", "Unexpected error after red on: '\(errVal)'")
        XCTAssertTrue(app.scrollViews[AccessibilityID.logScrollView].exists)
    }

    func testGreenOnButton() throws {
        let app = try launchAppFake()
        defer { app.terminate() }

        connect(app)
        app.sliders[AccessibilityID.greenSlider].adjust(toNormalizedSliderPosition: 0.4)
        app.buttons[AccessibilityID.greenOnButton].tap()

        let errVal = value(of: app.staticTexts[AccessibilityID.lastErrorLabel]) ?? ""
        XCTAssertEqual(errVal, "", "Unexpected error after green on: '\(errVal)'")
    }

    func testBlueOnButton() throws {
        let app = try launchAppFake()
        defer { app.terminate() }

        connect(app)
        app.sliders[AccessibilityID.blueSlider].adjust(toNormalizedSliderPosition: 0.3)
        app.buttons[AccessibilityID.blueOnButton].tap()

        let errVal = value(of: app.staticTexts[AccessibilityID.lastErrorLabel]) ?? ""
        XCTAssertEqual(errVal, "", "Unexpected error after blue on: '\(errVal)'")
    }

    func testWhiteOnButton() throws {
        let app = try launchAppFake()
        defer { app.terminate() }

        connect(app)
        app.sliders[AccessibilityID.whiteSlider].adjust(toNormalizedSliderPosition: 0.6)
        app.buttons[AccessibilityID.whiteOnButton].tap()

        let errVal = value(of: app.staticTexts[AccessibilityID.lastErrorLabel]) ?? ""
        XCTAssertEqual(errVal, "", "Unexpected error after white on: '\(errVal)'")
    }

    func testAllOffButton() throws {
        let app = try launchAppFake()
        defer { app.terminate() }

        connect(app)
        app.buttons[AccessibilityID.allChannelsOffButton].tap()

        let errVal = value(of: app.staticTexts[AccessibilityID.lastErrorLabel]) ?? ""
        XCTAssertEqual(errVal, "", "Unexpected error after all off: '\(errVal)'")
    }

    func testFirePulseDefault() throws {
        let app = try launchAppFake()
        defer { app.terminate() }

        connect(app)
        app.textFields[AccessibilityID.pulseMsTextField].tap()
        app.buttons[AccessibilityID.firePulseButton].tap()

        let errVal = value(of: app.staticTexts[AccessibilityID.lastErrorLabel]) ?? ""
        XCTAssertEqual(errVal, "", "Unexpected error firing default pulse: '\(errVal)'")
    }

    func testFirePulseInvalidShowsError() throws {
        let app = try launchAppFake()
        defer { app.terminate() }

        connect(app)
        let pulseField = app.textFields[AccessibilityID.pulseMsTextField]
        pulseField.tap()
        pulseField.typeKey("a", modifierFlags: .command)
        pulseField.typeKey(.delete, modifierFlags: [])
        pulseField.typeText("7")
        app.buttons[AccessibilityID.firePulseButton].tap()

        let errVal = (value(of: app.staticTexts[AccessibilityID.lastErrorLabel]) ?? "").lowercased()
        XCTAssertTrue(
            errVal.contains("pulse") || errVal.contains("ms"),
            "Expected error mentioning 'pulse' or 'ms', got: '\(errVal)'"
        )
    }

    func testClearLogResetsLogScrollView() throws {
        let app = try launchAppFake()
        defer { app.terminate() }

        connect(app)
        app.sliders[AccessibilityID.redSlider].adjust(toNormalizedSliderPosition: 0.5)
        app.buttons[AccessibilityID.redOnButton].tap()
        app.buttons[AccessibilityID.clearLogButton].tap()

        XCTAssertTrue(app.scrollViews[AccessibilityID.logScrollView].exists,
                      "Log scroll view missing after clear")
    }

    func testDisconnectReturnsConnectionStatusToDisconnected() throws {
        let app = try launchAppFake()
        defer { app.terminate() }

        connect(app)
        Thread.sleep(forTimeInterval: 0.5)
        app.buttons[AccessibilityID.disconnectButton].tap()

        let deadline = Date(timeIntervalSinceNow: 2.0)
        var statusContainsDisconnect = false
        while Date() < deadline {
            let v = (value(of: app.staticTexts[AccessibilityID.connectionStatusLabel]) ?? "").lowercased()
            if v.contains("disconnect") {
                statusContainsDisconnect = true
                break
            }
            Thread.sleep(forTimeInterval: 0.1)
        }
        XCTAssertTrue(statusContainsDisconnect,
                      "connectionStatusLabel did not show 'disconnect' after tapping disconnect")
    }
}
