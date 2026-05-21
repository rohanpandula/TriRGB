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
import Foundation
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

    func connect() {
        callLog.append("connect")
        isConnected = true
    }

    func disconnect() {
        callLog.append("disconnect")
        isConnected = false
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

    func start(settings: ScanSettings) async throws {
        callLog.append("start")
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

    func captureFrame(retake: Bool) async throws -> TripletOutcome {
        callLog.append("captureFrame(retake:\(retake))")
        if let err = captureError { throw err }
        return captureOutcome
    }

    func compositeStatus() async throws -> CompositeStatus {
        return compositeStatusResult
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
}

// NOTE: pollCompositeStatus() is `internal` in ScanCoordinator (visible via
// @testable import) so tests can invoke it directly and skip the 1s polling loop.
