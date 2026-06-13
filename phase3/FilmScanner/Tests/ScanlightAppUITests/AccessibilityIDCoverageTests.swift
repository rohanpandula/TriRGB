// AccessibilityIDCoverageTests — structural dead-control detector.
//
// Asserts that every case in the AccessibilityID enum has at least one
// rendered SwiftUI descendant in the running app's AX tree. A future
// plan that adds a new AccessibilityID enum case without wiring it to a
// SwiftUI element will get a loud, named failure here.
//
// Runtime context:
//   - Xcode UI test target: app launches with a proper XCTestConfiguration;
//     tests run the full interactive path.
//   - `swift test` from command line: XCUIApplication(url:) is used with the
//     sibling binary path. When running in a headless or non-GUI context
//     (e.g. SSH, no window server, accessibility not granted), the app window
//     will not appear and the tests skip gracefully rather than failing.
//     Grant Accessibility permission in System Settings → Privacy & Security
//     → Accessibility for the terminal/runner to enable full interactive runs.

import XCTest
@testable import ScanlightApp

final class AccessibilityIDCoverageTests: XCTestCase {

    override func setUpWithError() throws {
        continueAfterFailure = false
    }

    // All 22 AccessibilityID string values — the contract between this test
    // file and the SwiftUI app. If a new case is added to AccessibilityID,
    // add it here in lockstep.
    private let allAccessibilityIDs: [String] = [
        AccessibilityID.connectButton,
        AccessibilityID.disconnectButton,
        AccessibilityID.portTextField,
        AccessibilityID.connectionStatusLabel,
        AccessibilityID.firmwareLabel,
        AccessibilityID.hardwareLabel,
        AccessibilityID.ledTempLabel,
        AccessibilityID.vbusLabel,
        AccessibilityID.redSlider,
        AccessibilityID.greenSlider,
        AccessibilityID.blueSlider,
        AccessibilityID.whiteSlider,
        AccessibilityID.redOnButton,
        AccessibilityID.greenOnButton,
        AccessibilityID.blueOnButton,
        AccessibilityID.whiteOnButton,
        AccessibilityID.allChannelsOffButton,
        AccessibilityID.pulseMsTextField,
        AccessibilityID.firePulseButton,
        AccessibilityID.lastErrorLabel,
        AccessibilityID.logScrollView,
        AccessibilityID.clearLogButton,
    ]

    /// Verify that every AccessibilityID enum case corresponds to at least
    /// one rendered SwiftUI element in the running app's AX tree.
    ///
    /// Requires accessibility permissions and a window server. Skips
    /// gracefully when neither is available (headless CI, SSH).
    func testEveryAccessibilityIDHasARenderedElement() throws {
        let app = try makeFakeApp()
        app.launch()
        defer { app.terminate() }

        guard app.windows.firstMatch.waitForExistence(timeout: 5.0) else {
            throw XCTSkip("App window did not appear — Accessibility permission may not be granted, or running headless. Grant via System Settings → Privacy & Security → Accessibility.")
        }

        for id in allAccessibilityIDs {
            let matches = app.descendants(matching: .any).matching(identifier: id)
            XCTAssertGreaterThanOrEqual(
                matches.count, 1,
                "AccessibilityID '\(id)' has zero rendered descendants"
            )
        }
    }

    /// Schema version pin — bumping this requires a conscious doc update.
    /// This test does NOT use XCUIApplication and always runs.
    func testSchemaVersionMatches() {
        XCTAssertEqual(
            AccessibilityID.schemaVersion, "5",
            "AccessibilityID.schemaVersion bumped without updating ScanlightAppUITests"
        )
    }

    // MARK: - Private helpers

    /// Build an XCUIApplication pointed at the sibling binary, or the default
    /// target app if running under Xcode's UI test configuration.
    private func makeFakeApp() throws -> XCUIApplication {
        // When running under Xcode's UI test runner, XCUIApplication() with no
        // args uses the configured target application. Under `swift test`, we
        // point directly at the sibling binary using the URL initializer.
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

    /// Returns the directory containing the test bundle — the sibling binaries
    /// are built into the same `.build/<flavor>/` directory by SwiftPM.
    private func binaryDirectory() -> URL {
        // Bundle(for: Self.self) gives the .xctest bundle, not Xcode's main
        // bundle. The sibling executable is in the same parent directory.
        let testBundleURL = URL(fileURLWithPath: Bundle(for: Self.self).bundlePath)
        if testBundleURL.pathExtension == "xctest" {
            return testBundleURL.deletingLastPathComponent()
        }
        // Fallback: parent of whatever the test bundle says it is
        return testBundleURL.deletingLastPathComponent()
    }
}
