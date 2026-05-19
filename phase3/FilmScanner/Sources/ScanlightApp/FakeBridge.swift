// FakeBridge — transport factory that inspects CommandLine.arguments to
// decide whether to return a FakeTransport or a real SerialPortTransport.
//
// This is the deliberate hook that XCTest UI tests and the cua-driver script
// use to bypass hardware. Both pass `-FakeTransport YES` as launch arguments,
// which `XCUIApplication.launchArguments` threads through to
// `CommandLine.arguments` at runtime — so this single detection point covers
// both the XCTest UI path and any external automation that spawns the binary
// directly.
//
// See SPEC.md § "Open decisions" for the rationale behind launch-argument
// injection versus a separate build target or compile-time flag.

import Foundation
import ScanlightSwift

/// Launch-argument-driven transport selector.
///
/// `makeTransport()` is passed as a factory closure to `ScanlightViewModel`
/// so a fresh transport is created on each `connect()` call.
enum FakeBridge {

    /// Returns `.success(FakeTransport())` when `-FakeTransport YES` is
    /// present in `CommandLine.arguments`; otherwise attempts to discover
    /// and open the real serial port.
    ///
    /// On real-serial failure the error is packaged as `.failure(error)` so
    /// the ViewModel can surface it to `lastErrorLabel` rather than crashing
    /// the app on launch.
    static func makeTransport() -> Result<ScanlightTransport, Error> {
        if shouldUseFake() {
            return .success(FakeTransport())
        }
        do {
            let transport = try SerialPortTransport.discoverAndOpen()
            return .success(transport)
        } catch {
            return .failure(error)
        }
    }

    // MARK: - private

    private static func shouldUseFake() -> Bool {
        let args = CommandLine.arguments
        for i in 0..<(args.count - 1) {
            if args[i] == "-FakeTransport" && args[i + 1] == "YES" {
                return true
            }
        }
        return false
    }
}
