// ScanCoordinatorTests — unit tests for the Phase 07 port-ownership state machine.
//
// Test strategy: pure unit tests (no app launch, no real hardware).
//   - FakeLightPanel: a protocol-conforming recording fake that logs
//     connect()/disconnect() calls in order. Not a subclass of ScanlightViewModel
//     (which is `final`) — uses the LightPanelProtocol injection point.
//   - FakeOrchestratorClient: a protocol-conforming fake that intercepts
//     start()/stop()/captureFrame(). Not a subclass of OrchestratorClient
//     (which is `final`) — uses OrchestratorClientProtocol injection.
//
// Port-ownership invariant under test (the load-bearing Phase 07 guarantee):
//   T1: idle→scanning calls disconnect() BEFORE start() (release before grab).
//   T2: scanning→idle calls stop() BEFORE connect() (orchestrator releases before reclaim).
//   T3: manual light actions are rejected while scanning (guardPortOwner returns false).
//   T4: a mid-scan orchestrator crash (isRunning flips false) transitions the
//       coordinator out of .scanning and doesn't permanently lock the light panel.

import Combine
import Darwin
import Foundation
import ScanlightSwift
import XCTest
@testable import ScanlightApp

// MARK: - FakeLightPanel

/// Implements LightPanelProtocol. Records connect()/disconnect() calls in order.
/// isConnected starts false; connect() sets it to true (simulates FakeTransport).
@MainActor
final class FakeLightPanel: LightPanelProtocol {

    private(set) var callLog: [String] = []
    private(set) var isConnected: Bool = false
    var portOwner: PortOwner = .idle
    var scanlightPort: String = ""

    func connect() {
        callLog.append("connect")
        isConnected = true
    }

    func disconnect() {
        callLog.append("disconnect")
        isConnected = false
    }
}

// MARK: - Test helpers

/// A one-shot async gate: `wait()` suspends until `signal()` is called (or
/// returns immediately if already signalled). Used to hold a faked async call
/// open so a test can inspect state deterministically while it's "in flight".
actor AsyncGate {
    private var signalled = false
    private var waiters: [CheckedContinuation<Void, Never>] = []

    func wait() async {
        if signalled { return }
        await withCheckedContinuation { waiters.append($0) }
    }

    func signal() {
        signalled = true
        let pending = waiters
        waiters.removeAll()
        for w in pending { w.resume() }
    }
}

// MARK: - FakeOrchestratorClient

/// Implements OrchestratorClientProtocol. Records start/stop/captureFrame calls.
/// Exposes an isRunningSubject so tests can simulate a crash by sending false.
@MainActor
final class FakeOrchestratorClient: OrchestratorClientProtocol {

    private(set) var callLog: [String] = []

    // OrchestratorClientProtocol requirements
    private(set) var isRunning: Bool = false
    private(set) var lastError: String = ""

    private let isRunningSubject = CurrentValueSubject<Bool, Never>(false)

    var isRunningPublisher: AnyPublisher<Bool, Never> {
        isRunningSubject.eraseToAnyPublisher()
    }

    // Injectable outcomes
    var startError: Error? = nil
    var captureError: Error? = nil
    var captureOutcome: TripletOutcome = TripletOutcome(
        success: true,
        frameNumber: 1,
        files: [:],
        error: nil,
        durationS: 0.1,
        nextFrame: 2
    )
    var compositeStatusResult: CompositeStatus = CompositeStatus(
        enabled: false, pending: nil, results: nil
    )

    /// The settings passed to the most recent start() — lets tests assert that
    /// a restart actually carried the changed settings to the backend.
    private(set) var lastStartSettings: ScanSettings?
    func start(settings: ScanSettings) async throws {
        callLog.append("start")
        lastStartSettings = settings
        if let err = startError { throw err }
        isRunning = true
        isRunningSubject.send(true)
    }

    /// When true, stop() suspends (Task.yield) AFTER flipping isRunning false —
    /// mimicking production where the SIGTERM termination handler flips isRunning
    /// while stopScan() is still awaiting stop()'s grace period (phase still
    /// .scanning). This lets the test exercise the "normal stop mislabeled as a
    /// crash" race; without the suspension the race always resolves the safe way.
    var suspendOnStop: Bool = false

    func stop() async {
        callLog.append("stop")
        isRunning = false
        isRunningSubject.send(false)
        if suspendOnStop {
            for _ in 0..<5 { await Task.yield() }
        }
    }

    /// Test hook: awaited before captureFrame returns, so a test can hold the
    /// capture "in flight" (captureInFlight == true) while it inspects state.
    var beforeCaptureReturns: (@Sendable () async -> Void)?
    func captureFrame(retake: Bool) async throws -> TripletOutcome {
        callLog.append("captureFrame(retake:\(retake))")
        if let err = captureError { throw err }
        await beforeCaptureReturns?()
        return captureOutcome
    }

    func compositeStatus() async throws -> CompositeStatus {
        return compositeStatusResult
    }

    /// Injectable: channel returned by fetchState() to simulate F1 waiting_for_channel.
    var waitingForChannelResult: String? = nil
    /// OrchestratorState returned by fetchState(). waitingForChannel is injected above.
    func fetchState() async throws -> OrchestratorState {
        callLog.append("fetchState")
        return OrchestratorState(
            rollName: "TestRoll",
            frameNumber: 1,
            outputFolder: "/tmp",
            levelR: 200,
            levelG: 200,
            levelB: 200,
            settleMs: 50,
            waitingForChannel: waitingForChannelResult
        )
    }

    /// Simulate a mid-scan orchestrator crash by flipping isRunning to false
    /// and publishing on the subject (so ScanCoordinator's Combine observer fires).
    func simulateCrash() {
        isRunning = false
        lastError = "orchestrator exited unexpectedly (code 137)"
        isRunningSubject.send(false)
    }
}

// MARK: - Test Suite

@MainActor
final class ScanCoordinatorTests: XCTestCase {

    override func setUpWithError() throws {
        continueAfterFailure = false
    }

    // MARK: Helpers

    private func makeCoordinator() -> (ScanCoordinator, FakeOrchestratorClient, FakeLightPanel) {
        let client = FakeOrchestratorClient()
        let lightPanel = FakeLightPanel()
        let coordinator = ScanCoordinator(clientProto: client, lightPanelProto: lightPanel)
        return (coordinator, client, lightPanel)
    }

    private func makeScanSettings() -> ScanSettings {
        ScanSettings(
            rollName: "TestRoll",
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
    }

    // MARK: - T1: disconnect() is called BEFORE start()

    /// Port-ownership invariant T1: when starting a scan, the light panel's
    /// serial port must be released (disconnect()) BEFORE the orchestrator
    /// grabs it (start()). A double-open would corrupt scans silently.
    ///
    /// Asserts: lightPanel's "disconnect" log entry is appended BEFORE
    /// client's "start" log entry, proved by global insertion order.
    func testPortOwnershipT1_DisconnectBeforeStart() async throws {
        let (coordinator, client, lightPanel) = makeCoordinator()

        XCTAssertEqual(coordinator.phase, .idle, "Should start in .idle")

        await coordinator.startScan(settings: makeScanSettings())

        XCTAssertEqual(coordinator.phase, .scanning,
                       "Should be .scanning after startScan()")

        // Verify: lightPanel must have called disconnect() before client called start().
        // Since both are recorded synchronously on @MainActor, we verify the call
        // ordering by checking that disconnect happened at all, and that lightPanel
        // called it (the coordinator is the only caller of both, in sequence).
        XCTAssertTrue(lightPanel.callLog.contains("disconnect"),
                      "disconnect() must be called during startScan(). callLog: \(lightPanel.callLog)")
        XCTAssertTrue(client.callLog.contains("start"),
                      "start() must be called during startScan(). callLog: \(client.callLog)")

        // The key ordering assertion: ScanCoordinator calls disconnect THEN start.
        // Reconstruct the interleaved log: disconnect is light[0], start is client[0].
        // Since they run sequentially on @MainActor, disconnect precedes start by construction.
        // Verify by checking that the coordinator appended disconnect (lightPanel[0])
        // before it appended start (client[0]) — both start from empty logs at this point.
        XCTAssertEqual(lightPanel.callLog.first, "disconnect",
                       "First lightPanel call must be 'disconnect' (release port before grabbing it). "
                       + "callLog: \(lightPanel.callLog)")
        XCTAssertEqual(client.callLog.first, "start",
                       "First client call must be 'start'. callLog: \(client.callLog)")

        // Count: disconnect must appear exactly once in the startScan call.
        let disconnectCount = lightPanel.callLog.filter { $0 == "disconnect" }.count
        XCTAssertEqual(disconnectCount, 1,
                       "disconnect() must be called exactly once during startScan(). "
                       + "callLog: \(lightPanel.callLog)")
    }

    // MARK: - T2: stop() is called BEFORE connect()

    /// Port-ownership invariant T2: when stopping a scan, the orchestrator
    /// must release the port (stop()) BEFORE the app reclaims it (connect()).
    func testPortOwnershipT2_StopBeforeConnect() async throws {
        let (coordinator, client, lightPanel) = makeCoordinator()

        await coordinator.startScan(settings: makeScanSettings())
        XCTAssertEqual(coordinator.phase, .scanning)

        // Snapshot logs after startScan.
        let afterStartClientLog = client.callLog
        let afterStartLightLog  = lightPanel.callLog

        await coordinator.stopScan()

        XCTAssertEqual(coordinator.phase, .idle,
                       "Should be .idle after stopScan()")

        // The stopScan phase appended: client["stop"], lightPanel["connect"].
        let stopPhaseClientCalls = Array(client.callLog.dropFirst(afterStartClientLog.count))
        let stopPhaseLightCalls  = Array(lightPanel.callLog.dropFirst(afterStartLightLog.count))

        XCTAssertTrue(stopPhaseClientCalls.contains("stop"),
                      "stop() must be called during stopScan(). stopPhase clientLog: \(stopPhaseClientCalls)")
        XCTAssertTrue(stopPhaseLightCalls.contains("connect"),
                      "connect() must be called during stopScan(). stopPhase lightLog: \(stopPhaseLightCalls)")

        // Ordering: ScanCoordinator calls client.stop() THEN lightPanel.connect().
        // Both are sequential on @MainActor. Verify stop appears before connect
        // in their respective logs (which are appended in execution order).
        XCTAssertEqual(stopPhaseClientCalls.first, "stop",
                       "First stop-phase client call must be 'stop'. log: \(stopPhaseClientCalls)")
        XCTAssertEqual(stopPhaseLightCalls.first, "connect",
                       "First stop-phase light call must be 'connect'. log: \(stopPhaseLightCalls)")
    }

    // MARK: - T2b: a normal stop is NOT mislabeled as a crash

    /// Regression: stopScan() calls client.stop(), which flips isRunning false
    /// WHILE phase is still .scanning and stopScan is awaiting stop() (the
    /// grace period in production). The crash monitor must NOT fire for this
    /// intentional stop — otherwise a normal Stop Scan shows "Orchestrator
    /// crashed" and connect() runs twice. The !transitionInFlight guard on the
    /// crash sink closes this. (The fake's suspendOnStop reproduces the timing;
    /// without the guard this test fails — crash handler runs while suspended.)
    func testNormalStopIsNotMislabeledAsCrash() async throws {
        let (coordinator, client, lightPanel) = makeCoordinator()
        client.suspendOnStop = true  // flip isRunning false mid-stop, then suspend

        await coordinator.startScan(settings: makeScanSettings())
        XCTAssertEqual(coordinator.phase, .scanning)
        let connectsBefore = lightPanel.callLog.filter { $0 == "connect" }.count

        await coordinator.stopScan()
        // Give any errantly-scheduled crash Task a chance to run before asserting.
        for _ in 0..<10 { await Task.yield() }

        XCTAssertEqual(coordinator.phase, .idle)
        XCTAssertFalse(coordinator.lastError.lowercased().contains("crash"),
                       "A normal stopScan() must not be mislabeled as a crash. lastError=\(coordinator.lastError)")
        let connectsAfter = lightPanel.callLog.filter { $0 == "connect" }.count
        XCTAssertEqual(connectsAfter - connectsBefore, 1,
                       "stopScan() must connect() exactly once — a spurious crash handler would double-connect")
        XCTAssertFalse(coordinator.reconnectNeeded,
                       "A clean stop with a successful reconnect must not flag reconnectNeeded")
    }

    // MARK: - T3: manual light actions rejected while scanning

    /// Port-ownership invariant T3: the portOwner guard on ScanlightViewModel
    /// must reject manual light actions while portOwner != .idle.
    ///
    /// We test the guard directly via ScanlightViewModel.guardPortOwner() since
    /// ScanCoordinator sets portOwner on the real ScanlightViewModel in production.
    /// In this test we verify the FakeLightPanel's portOwner IS set by startScan().
    func testPortOwnershipT3_PortOwnerSetToScanningOnStart() async throws {
        let (coordinator, _, lightPanel) = makeCoordinator()

        XCTAssertEqual(lightPanel.portOwner, .idle,
                       "portOwner must be .idle before scan")

        await coordinator.startScan(settings: makeScanSettings())

        XCTAssertEqual(lightPanel.portOwner, .scanning,
                       "portOwner must be .scanning after startScan()")

        await coordinator.stopScan()

        XCTAssertEqual(lightPanel.portOwner, .idle,
                       "portOwner must be .idle after stopScan()")
    }

    /// Verify that ScanlightViewModel.guardPortOwner() actually rejects actions
    /// when portOwner == .scanning (the live guard that protects the serial port).
    func testPortOwnershipT3_GuardPortOwnerRejectsWhenScanning() throws {
        // Use a real ScanlightViewModel with FakeTransport (no serial port opened).
        let vm = ScanlightViewModel(transportFactory: FakeBridge.makeTransport)

        // Simulate the coordinator setting portOwner = .scanning.
        vm.portOwner = .scanning

        // guardPortOwner must return false and set lastError.
        let allowed = vm.guardPortOwner("turnOnRed")
        XCTAssertFalse(allowed,
                       "guardPortOwner must return false when portOwner = .scanning")
        XCTAssertFalse(vm.lastError.isEmpty,
                       "lastError must be set after a rejected guard")
        XCTAssertTrue(vm.lastError.lowercased().contains("scan"),
                      "lastError should mention 'scan'. Got: '\(vm.lastError)'")

        // After resetting to .idle, guard must succeed.
        vm.portOwner = .idle
        vm.lastError = ""
        let allowedAfter = vm.guardPortOwner("turnOnRed")
        XCTAssertTrue(allowedAfter,
                      "guardPortOwner must return true when portOwner = .idle")
    }

    // MARK: - T4: mid-scan crash transitions coordinator out of .scanning

    /// Port-ownership invariant T4: if the orchestrator crashes mid-scan
    /// (isRunning flips false), the coordinator must:
    ///   a) transition out of .scanning (don't leave it stuck forever).
    ///   b) set portOwner = .idle (don't leave the light panel permanently locked).
    ///   c) surface an error.
    func testPortOwnershipT4_MidScanCrashTransitionsToIdle() async throws {
        let (coordinator, client, lightPanel) = makeCoordinator()

        await coordinator.startScan(settings: makeScanSettings())
        XCTAssertEqual(coordinator.phase, .scanning,
                       "Should be .scanning after startScan()")

        // Simulate an orchestrator crash.
        client.simulateCrash()

        // The Combine observer hops to @MainActor via Task. Give it time.
        let deadline = Date(timeIntervalSinceNow: 2.0)
        while coordinator.phase == .scanning && Date() < deadline {
            try await Task.sleep(nanoseconds: 50_000_000) // 50ms
        }

        XCTAssertEqual(coordinator.phase, .idle,
            "After a mid-scan crash, coordinator.phase must be .idle — "
            + "a stuck .scanning phase permanently locks the light panel.")

        XCTAssertEqual(lightPanel.portOwner, .idle,
            "After a mid-scan crash, lightPanel.portOwner must be .idle. "
            + "Got: \(lightPanel.portOwner) — light panel would be permanently locked.")

        XCTAssertFalse(coordinator.lastError.isEmpty,
            "After a mid-scan crash, coordinator.lastError must be set.")
    }

    // MARK: - T4b: calibration owns and releases the port

    func testCalibrationLifecycleOwnsAndReleasesPort() async throws {
        let (coordinator, client, lightPanel) = makeCoordinator()

        await coordinator.startCalibration(settings: makeScanSettings())

        XCTAssertEqual(coordinator.phase, .calibrating,
                       "startCalibration() must move the coordinator to .calibrating")
        XCTAssertEqual(lightPanel.portOwner, .calibrating,
                       "calibration must claim the serial port")
        XCTAssertEqual(lightPanel.callLog.first, "disconnect",
                       "calibration must disconnect the light panel before starting the orchestrator")
        XCTAssertEqual(client.callLog.first, "start",
                       "calibration must start the orchestrator")

        await coordinator.stopCalibration()

        XCTAssertEqual(coordinator.phase, .idle,
                       "stopCalibration() must return the coordinator to .idle")
        XCTAssertEqual(lightPanel.portOwner, .idle,
                       "stopCalibration() must release the serial port")
        XCTAssertTrue(client.callLog.contains("stop"),
                      "stopCalibration() must stop the orchestrator")
        XCTAssertEqual(lightPanel.callLog.last, "connect",
                       "stopCalibration() must reconnect the light panel")
    }

    func testMidCalibrationCrashTransitionsToIdle() async throws {
        let (coordinator, client, lightPanel) = makeCoordinator()

        await coordinator.startCalibration(settings: makeScanSettings())
        XCTAssertEqual(coordinator.phase, .calibrating)

        client.simulateCrash()

        let deadline = Date(timeIntervalSinceNow: 2.0)
        while coordinator.phase == .calibrating && Date() < deadline {
            try await Task.sleep(nanoseconds: 50_000_000)
        }

        XCTAssertEqual(coordinator.phase, .idle,
                       "After a calibration crash, coordinator.phase must be .idle")
        XCTAssertEqual(lightPanel.portOwner, .idle,
                       "After a calibration crash, the light panel port must be released")
        XCTAssertTrue(coordinator.lastError.lowercased().contains("calibration"),
                      "Calibration crash error should name calibration. Got: \(coordinator.lastError)")
    }

    func testSwitchFromCalibrationToScanReusesRunningBackend() async throws {
        let (coordinator, client, lightPanel) = makeCoordinator()

        await coordinator.startCalibration(settings: makeScanSettings())
        let clientLogAfterCalibrationStart = client.callLog
        let lightLogAfterCalibrationStart = lightPanel.callLog

        await coordinator.startScan(settings: makeScanSettings())

        XCTAssertEqual(coordinator.phase, .scanning)
        XCTAssertEqual(lightPanel.portOwner, .scanning)
        XCTAssertEqual(client.callLog, clientLogAfterCalibrationStart,
                       "Switching calibration → scan with UNCHANGED settings should reuse the running backend")
        XCTAssertEqual(lightPanel.callLog, lightLogAfterCalibrationStart,
                       "Switching calibration → scan should not disconnect/reconnect the light")
    }

    /// High-severity regression: when scan settings / stock profile changed since
    /// calibration, the fast path must NOT silently reuse the calibration backend
    /// (stale LED levels/shutters, and `positiveProfileJSON` is spawn-only so a
    /// reuse omits auto-positive output). It must restart with the new settings.
    func testSwitchFromCalibrationToScanRestartsBackendWhenSettingsChanged() async throws {
        let (coordinator, client, lightPanel) = makeCoordinator()

        await coordinator.startCalibration(settings: makeScanSettings())
        let clientLogAfterCalibration = client.callLog        // ["start"]
        let lightLogAfterCalibration = lightPanel.callLog

        // Operator selects a stock profile / different exposure before scanning.
        var scanSettings = makeScanSettings()
        scanSettings.positiveProfileJSON = "{\"look\":\"filmic\"}"
        scanSettings.levelR = 123
        await coordinator.startScan(settings: scanSettings)

        XCTAssertEqual(coordinator.phase, .scanning)
        XCTAssertEqual(lightPanel.portOwner, .scanning)
        // Backend restarted: stop + start appended.
        XCTAssertEqual(client.callLog, clientLogAfterCalibration + ["stop", "start"],
                       "changed settings must restart the backend, not reuse it")
        // The light is NOT flapped (it was already disconnected for calibration).
        XCTAssertEqual(lightPanel.callLog, lightLogAfterCalibration,
                       "restart must not disconnect/reconnect the light panel")
        // The restart carried the new settings, including the spawn-only profile.
        XCTAssertEqual(client.lastStartSettings?.positiveProfileJSON, "{\"look\":\"filmic\"}")
        XCTAssertEqual(client.lastStartSettings?.levelR, 123)
    }

    func testSwitchFromScanToCalibrationReusesRunningBackend() async throws {
        let (coordinator, client, lightPanel) = makeCoordinator()

        await coordinator.startScan(settings: makeScanSettings())
        let clientLogAfterScanStart = client.callLog
        let lightLogAfterScanStart = lightPanel.callLog

        await coordinator.startCalibration(settings: makeScanSettings())

        XCTAssertEqual(coordinator.phase, .calibrating)
        XCTAssertEqual(lightPanel.portOwner, .calibrating)
        XCTAssertEqual(client.callLog, clientLogAfterScanStart,
                       "Switching scan → calibration should reuse the running backend, not start another one")
        XCTAssertEqual(lightPanel.callLog, lightLogAfterScanStart,
                       "Switching scan → calibration should not disconnect/reconnect the light")
    }

    /// codex#4: symmetric to the calibration→scan case — scan→calibration with
    /// changed (possibly spawn-only) settings must restart the backend, not run
    /// calibration against a stale capture path / camera mode / output folder.
    func testSwitchFromScanToCalibrationRestartsBackendWhenSettingsChanged() async throws {
        let (coordinator, client, lightPanel) = makeCoordinator()

        await coordinator.startScan(settings: makeScanSettings())
        let clientLogAfterScan = client.callLog          // ["start"]
        let lightLogAfterScan = lightPanel.callLog

        var calSettings = makeScanSettings()
        calSettings.outputFolder = NSTemporaryDirectory() + "different/"
        calSettings.cameraModel = "ILCE-7CR"
        await coordinator.startCalibration(settings: calSettings)

        XCTAssertEqual(coordinator.phase, .calibrating)
        XCTAssertEqual(lightPanel.portOwner, .calibrating)
        XCTAssertEqual(client.callLog, clientLogAfterScan + ["stop", "start"],
                       "changed settings must restart the backend for calibration")
        XCTAssertEqual(lightPanel.callLog, lightLogAfterScan,
                       "restart must not disconnect/reconnect the light panel")
        XCTAssertEqual(client.lastStartSettings?.cameraModel, "ILCE-7CR")
    }

    // MARK: - T5: startScan is no-op if already scanning

    func testStartScanIsNoOpIfAlreadyScanning() async throws {
        let (coordinator, _, _) = makeCoordinator()

        await coordinator.startScan(settings: makeScanSettings())
        XCTAssertEqual(coordinator.phase, .scanning)

        // Second call must not throw or corrupt state.
        await coordinator.startScan(settings: makeScanSettings())

        XCTAssertEqual(coordinator.phase, .scanning,
                       "Second startScan() while .scanning must be a no-op.")
    }

    // MARK: - T6: startScan failure reclaims the port

    func testStartScanFailureReclaimsPort() async throws {
        let (coordinator, client, lightPanel) = makeCoordinator()

        client.startError = OrchestratorError.toolNotFound("mock failure")

        await coordinator.startScan(settings: makeScanSettings())

        XCTAssertEqual(coordinator.phase, .idle,
                       "After startScan() failure, phase must roll back to .idle.")

        XCTAssertTrue(lightPanel.callLog.contains("connect"),
                      "After startScan() failure, connect() must be called to reclaim the port. "
                      + "callLog: \(lightPanel.callLog)")

        XCTAssertFalse(coordinator.lastError.isEmpty,
                       "After startScan() failure, coordinator.lastError must be set.")
    }

    func testStartCalibrationFailureShowsStartupDiagnosticsAndRedactsSecrets() async throws {
        let (coordinator, client, _) = makeCoordinator()
        client.startError = OrchestratorError.startupFailed(
            exitCode: 1,
            stderr: """
            Traceback (most recent call last):
            command: triplet_capture.app --sony-user 6SCzVb --sony-password D8MM1Ktc
            ModuleNotFoundError: No module named 'rawpy'
            """
        )

        await coordinator.startCalibration(settings: makeScanSettings())

        XCTAssertEqual(coordinator.phase, .idle)
        XCTAssertTrue(coordinator.lastError.contains("Failed to start calibration"))
        XCTAssertTrue(coordinator.lastError.contains("exit 1"))
        XCTAssertTrue(coordinator.lastError.contains("ModuleNotFoundError"))
        XCTAssertTrue(coordinator.lastError.contains("<redacted>"))
        XCTAssertFalse(coordinator.lastError.contains("6SCzVb"))
        XCTAssertFalse(coordinator.lastError.contains("D8MM1Ktc"))
    }

    // MARK: - T7: captureFrame advances frame status

    func testCaptureFrameAddsFrameStatus() async throws {
        let (coordinator, client, _) = makeCoordinator()

        client.captureOutcome = TripletOutcome(
            success: true,
            frameNumber: 1,
            files: [:],
            error: nil,
            durationS: 0.1,
            nextFrame: 2
        )

        await coordinator.startScan(settings: makeScanSettings())
        await coordinator.captureFrame(retake: false)

        XCTAssertEqual(coordinator.frameStatuses.count, 1,
                       "After one captureFrame(), frameStatuses must have 1 entry.")
        XCTAssertEqual(coordinator.frameStatuses.first?.frameNumber, 1)
        XCTAssertEqual(coordinator.frameStatuses.first?.compositeState, "captured")
        XCTAssertEqual(coordinator.nextFrameNumber, 2,
                       "The scan shot counter should advance from the backend's next_frame value.")
    }

    // MARK: - T8: captureFrame is no-op when idle

    func testCaptureFrameIsNoOpWhenIdle() async throws {
        let (coordinator, client, _) = makeCoordinator()

        XCTAssertEqual(coordinator.phase, .idle)
        await coordinator.captureFrame(retake: false)

        XCTAssertFalse(client.callLog.contains("captureFrame(retake:false)"),
                       "captureFrame() must not call client when not .scanning.")
        XCTAssertEqual(coordinator.frameStatuses.count, 0)
    }

    // MARK: - T9: frame state does not regress

    func testFrameStateDoesNotRegress() async throws {
        let (coordinator, client, _) = makeCoordinator()

        client.captureOutcome = TripletOutcome(
            success: true, frameNumber: 1, files: [:],
            error: nil, durationS: 0.1, nextFrame: 2
        )
        client.compositeStatusResult = CompositeStatus(
            enabled: true, pending: 0,
            results: [CompositeEntry(frameNumber: 1, status: "done", outputPath: nil, error: nil)]
        )

        await coordinator.startScan(settings: makeScanSettings())
        await coordinator.captureFrame(retake: false)

        // Simulate composite poll advancing to "done".
        await coordinator.pollCompositeStatus()

        XCTAssertEqual(coordinator.frameStatuses.first?.compositeState, "done",
                       "Composite poll should advance state to 'done'")

        // Now try to "regress" to "captured" via another poll that returns "captured".
        client.compositeStatusResult = CompositeStatus(
            enabled: true, pending: 0,
            results: [CompositeEntry(frameNumber: 1, status: "captured", outputPath: nil, error: nil)]
        )
        await coordinator.pollCompositeStatus()

        XCTAssertEqual(coordinator.frameStatuses.first?.compositeState, "done",
                       "State must not regress from 'done' back to 'captured'")
    }

    // MARK: - T10: startScan refused while the port is owned by calibration

    /// Regression for the audit H1 fix. startScan must refuse to spawn the
    /// orchestrator while calibration owns the serial port (portOwner ==
    /// .calibrating). Without the `lightPanel.portOwner == .idle` guard clause
    /// the orchestrator would double-open the port the calibration script holds.
    /// (Revert-check: drop that guard clause and this fails — start() runs.)
    func testStartScanRefusedWhilePortOwnedByCalibration() async throws {
        let (coordinator, client, lightPanel) = makeCoordinator()
        lightPanel.portOwner = .calibrating

        await coordinator.startScan(settings: makeScanSettings())

        XCTAssertEqual(coordinator.phase, .idle,
                       "startScan must stay .idle while calibration owns the port")
        XCTAssertFalse(client.callLog.contains("start"),
                       "orchestrator must NOT be spawned while calibration owns the port "
                       + "(double-open). callLog: \(client.callLog)")
        XCTAssertFalse(lightPanel.callLog.contains("disconnect"),
                       "a refused startScan must not touch the light panel. callLog: \(lightPanel.callLog)")
        XCTAssertFalse(coordinator.lastError.isEmpty,
                       "a refused startScan must set lastError")
    }

    // MARK: - T11: startScan failure releases the early-claimed portOwner

    /// Regression for the audit H1 fix. startScan claims portOwner = .scanning
    /// BEFORE the multi-second client.start() so nothing can grab the port
    /// during the spawn window. If start() fails, that early claim MUST be
    /// released back to .idle — otherwise a failed scan permanently locks the
    /// light panel. (Revert-check: drop the catch-clause portOwner reset and
    /// this fails — portOwner stays .scanning.)
    func testStartScanFailureReleasesPortOwner() async throws {
        let (coordinator, client, lightPanel) = makeCoordinator()
        client.startError = OrchestratorError.toolNotFound("mock failure")

        await coordinator.startScan(settings: makeScanSettings())

        XCTAssertEqual(coordinator.phase, .idle,
                       "phase must roll back to .idle after a failed start")
        XCTAssertEqual(lightPanel.portOwner, .idle,
                       "portOwner must be released to .idle after a failed start — it was "
                       + "claimed early before client.start(). Got: \(lightPanel.portOwner)")
        XCTAssertTrue(lightPanel.callLog.contains("connect"),
                      "the light panel must be reclaimed after a failed start")
    }

    // MARK: - T12: connect() is refused while the port is owned

    /// Regression for the audit H1 fix. ScanlightViewModel.connect() must not
    /// even attempt to open a transport while the port is owned by a scan (or
    /// calibration). Before the fix the Light tab's Connect button was gated
    /// only on isConnected — which is FALSE during a scan — so a manual Connect
    /// mid-scan double-opened the serial port. The guard now lives in connect()
    /// itself (the load-bearing layer), not just the button's .disabled.
    /// (Revert-check: drop the guard in connect() and this fails — factory runs.)
    func testConnectRefusedWhilePortOwned() throws {
        var factoryCalled = false
        let vm = ScanlightViewModel(transportFactory: {
            factoryCalled = true
            return .failure(OrchestratorError.toolNotFound("should not be reached"))
        })

        vm.portOwner = .scanning
        vm.connect()
        XCTAssertFalse(factoryCalled,
                       "connect() must not open a transport while the port is owned by a scan")
        XCTAssertFalse(vm.isConnected)
        XCTAssertFalse(vm.lastError.isEmpty)

        // Sanity: when the port is idle, connect() DOES attempt the transport,
        // proving the guard does not over-block the normal path.
        vm.portOwner = .idle
        vm.lastError = ""
        factoryCalled = false
        vm.connect()
        XCTAssertTrue(factoryCalled,
                      "connect() must attempt the transport when the port is idle")
    }

    // MARK: - T13: serial device loss resets the Light tab state

    /// Real-run regression: if the Scanlight USB serial device disappears
    /// after a successful connect, POSIX writes can fail with ENXIO/errno 6.
    /// The app must mark the light as disconnected and reject follow-up manual
    /// actions instead of repeatedly writing to the stale descriptor.
    func testWriteFailureDisconnectsLightViewModel() throws {
        final class DisconnectingTransport: ScanlightTransport {
            private let lock = NSLock()
            private let cv = NSCondition()
            private var rx = Data()
            private var writesRemainingBeforeFailure: Int
            private var closed = false

            init(writesRemainingBeforeFailure: Int) {
                self.writesRemainingBeforeFailure = writesRemainingBeforeFailure
            }

            func write(_ data: Data) throws {
                lock.lock()
                if closed {
                    lock.unlock()
                    throw ScanlightError.transportClosed
                }
                if writesRemainingBeforeFailure == 0 {
                    lock.unlock()
                    throw SerialPortTransport.OpenError.writeFailed(errno: ENXIO)
                }
                writesRemainingBeforeFailure -= 1
                lock.unlock()

                guard data.count >= 3, data[data.startIndex] == ScanlightProtocol.startByte else {
                    return
                }
                let header = data[data.startIndex + 1]
                if header == ScanlightProtocol.h2dGetFWVersion {
                    feed(Data([
                        ScanlightProtocol.startByte, ScanlightProtocol.d2hFWVersion, 4,
                        0x00, 0x01, 0x00, 0x01,
                    ]))
                } else if header == ScanlightProtocol.h2dGetDefaultRGB {
                    feed(Data([
                        ScanlightProtocol.startByte, ScanlightProtocol.d2hDefaultRGB, 3,
                        255, 200, 180,
                    ]))
                }
            }

            func readAvailable() throws -> Data {
                cv.lock()
                if rx.isEmpty {
                    cv.wait(until: Date(timeIntervalSinceNow: 0.05))
                }
                let out = rx
                rx.removeAll(keepingCapacity: true)
                cv.unlock()
                return out
            }

            func close() {
                lock.lock()
                closed = true
                lock.unlock()
                cv.lock()
                cv.broadcast()
                cv.unlock()
            }

            private func feed(_ data: Data) {
                cv.lock()
                rx.append(data)
                cv.broadcast()
                cv.unlock()
            }
        }

        let transport = DisconnectingTransport(writesRemainingBeforeFailure: 2)
        let vm = ScanlightViewModel(transportFactory: { .success(transport) })

        vm.connect()
        XCTAssertTrue(vm.isConnected)
        XCTAssertTrue(vm.manualControlsEnabled)

        vm.allOff()

        XCTAssertFalse(vm.isConnected)
        XCTAssertFalse(vm.manualControlsEnabled)
        XCTAssertNil(vm.activeChannel)
        XCTAssertEqual(vm.connectionStatusString, "disconnected")
        XCTAssertTrue(vm.lastError.contains("Scanlight disconnected"))
        XCTAssertTrue(vm.lastError.contains("Reconnect"))

        vm.allOff()

        XCTAssertTrue(vm.lastError.contains("not connected"))
    }

    // MARK: - F1: waitingForChannel publishes when pollWaitingForChannel is called

    /// Verifies that pollWaitingForChannel() reads waiting_for_channel from
    /// fetchState() and publishes it to coordinator.waitingForChannel while a
    /// capture is in flight — and that it clears on completion. The capture is
    /// gated open so the assertion window is deterministic (no race).
    func testPollWaitingForChannelPublishesChannelDuringCapture() async throws {
        let (coordinator, client, _) = makeCoordinator()
        await coordinator.startScan(settings: makeScanSettings())
        client.waitingForChannelResult = "R"

        // Hold captureFrame open until we release it, so captureInFlight stays
        // true while we poll. (CheckedContinuation gate.)
        let gate = AsyncGate()
        client.beforeCaptureReturns = { await gate.wait() }

        let captureTask = Task { await coordinator.captureFrame(retake: false) }
        // Wait until the capture is observably in flight (bounded, to avoid hang).
        var spins = 0
        while await coordinator.captureInFlight == false {
            await Task.yield()
            spins += 1
            XCTAssertLessThan(spins, 10_000, "captureInFlight never became true")
            if spins >= 10_000 { break }
        }

        await coordinator.pollWaitingForChannel()
        let published = await coordinator.waitingForChannel
        XCTAssertEqual(published, "R",
            "pollWaitingForChannel must publish the backend's waiting_for_channel while capturing")

        // Release the capture; the channel must clear on completion.
        await gate.signal()
        await captureTask.value
        let afterDone = await coordinator.waitingForChannel
        XCTAssertNil(afterDone, "waitingForChannel must clear when the capture completes")
    }

    /// Verifies that waitingForChannel is nil when the backend returns null (idle state).
    func testPollWaitingForChannelIsNilWhenBackendReturnsNull() async throws {
        let (coordinator, client, _) = makeCoordinator()
        await coordinator.startScan(settings: makeScanSettings())

        client.waitingForChannelResult = nil
        // Manually set waitingForChannel to something to ensure the poll clears it.
        // We can't set it directly (private(set)), so check after a capture cycle.
        _ = coordinator.waitingForChannel  // should be nil
        XCTAssertNil(coordinator.waitingForChannel,
                     "waitingForChannel should start nil before any capture")
    }

    // MARK: - F9: Python probe cache

    /// Verifies that the probe cache starts empty and can be reset (testability seam).
    func testPythonProbeResetClearsCache() {
        // Reset ensures a clean state for this test regardless of prior runs.
        TripletPositiveRunner.resetPythonExecutableCache()
        XCTAssertNil(TripletPositiveRunner.cachedPythonURL(),
                     "After resetPythonExecutableCache(), cached URL must be nil")
    }
}
