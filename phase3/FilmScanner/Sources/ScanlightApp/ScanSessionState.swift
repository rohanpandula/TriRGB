// ScanSessionState — Phase 07 serial-port-ownership state machine.
//
// ScanPhase is the exclusive-ownership invariant:
//   .idle       → app's light panel owns the serial port; manual controls live.
//   .calibrating → triplet-capture owns the port for calibration; manual controls disabled.
//   .scanning   → orchestrator owns the port; manual controls disabled with
//                 the "controlled by active scan" overlay.
//
// ScanCoordinator drives transitions and enforces the release/reclaim ordering:
//   idle→scanning: lightViewModel.disconnect() BEFORE client.start() (so the
//     orchestrator's Python driver never double-opens the serial port).
//   scanning→idle: client.stop() BEFORE lightViewModel.connect() (so the
//     orchestrator fully releases before we reopen).
//
// Port-reclaim failure: if connect() fails after a scan, ScanCoordinator
//   sets lastError and exposes reconnectNeeded = true (a "Reconnect Light"
//   button appears in ScanView rather than silently leaving the panel dead).
//
// Guards: manual light actions (connect/disconnect/setChannel/pulse) are
//   rejected (no-op + portOwnerError set) when coordinator.phase != .idle.
//   The guard lives on ScanlightViewModel.portOwner — checked inside each
//   action. ScanCoordinator sets/clears it on transitions.
//
// Crash recovery: OrchestratorClient.isRunning flips false when the child
//   exits for any reason (Phase 05 termination handler). ScanCoordinator
//   observes isRunning via Combine and transitions scanning/calibrating→idle
//   on crash, then attempts to reconnect the light panel.
//
// Composite polling: a Task loops every 1s while .scanning, cancelled on
//   transition to .idle. Merged frame-status list is exposed via @Published
//   frameStatuses: [FrameStatus].
//
// Testability: ScanCoordinator accepts OrchestratorClientProtocol and
// LightPanelProtocol — thin protocols over OrchestratorClient and
// ScanlightViewModel that let tests inject recording fakes without
// subclassing either `final class`.
//
// Pattern mirrors ScanlightViewModel exactly:
//   @MainActor final class ... : ObservableObject + @Published + Combine.
//   NOT the Observable macro.

import AppKit
import Combine
import Foundation

// MARK: - PortOwner

/// Sent from ScanCoordinator to ScanlightViewModel to enforce the light-panel guard.
/// When not .idle, ScanlightViewModel rejects manual light actions.
enum PortOwner: Equatable {
    case idle
    case scanning
    case calibrating
}

// MARK: - ScanPhase

/// The three phases of the scan-hub state machine. Determines which process
/// owns the Scanlight serial port at any given moment.
enum ScanPhase: Equatable {
    case idle
    case calibrating
    case scanning
}

// MARK: - FrameStatus

/// Per-frame composite status, merged from capture results + composite polling.
struct FrameStatus: Identifiable {
    var id: Int { frameNumber }
    let frameNumber: Int
    /// "captured" | "compositing" | "done" | "failed"
    var compositeState: String
}

// MARK: - OrchestratorClientProtocol

/// Minimal protocol over OrchestratorClient used by ScanCoordinator.
/// Enables test injection without subclassing the `final` OrchestratorClient.
@MainActor
protocol OrchestratorClientProtocol: AnyObject {
    /// Published. Observed by ScanCoordinator for crash recovery.
    var isRunning: Bool { get }
    /// Published. Observed for crash error messages.
    var lastError: String { get }
    /// Publisher for isRunning (Combine monitoring).
    var isRunningPublisher: AnyPublisher<Bool, Never> { get }

    func start(settings: ScanSettings) async throws
    func stop() async
    func captureFrame(retake: Bool) async throws -> TripletOutcome
    func compositeStatus() async throws -> CompositeStatus
    /// Fetch the current orchestrator state for channel-prompt polling (F1).
    func fetchState() async throws -> OrchestratorState
}

// MARK: OrchestratorClient conformance

extension OrchestratorClient: OrchestratorClientProtocol {
    var isRunningPublisher: AnyPublisher<Bool, Never> {
        $isRunning.eraseToAnyPublisher()
    }
}

// MARK: - LightPanelProtocol

/// Minimal protocol over ScanlightViewModel used by ScanCoordinator.
/// Enables test injection without subclassing the `final` ScanlightViewModel.
@MainActor
protocol LightPanelProtocol: AnyObject {
    var isConnected: Bool { get }
    var portOwner: PortOwner { get set }
    var scanlightPort: String { get }
    func connect()
    func disconnect()
}

// MARK: ScanlightViewModel conformance

extension ScanlightViewModel: LightPanelProtocol {}

private extension ScanSettings {
    func withScanlightPort(_ port: String) -> ScanSettings {
        let trimmed = port.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return self }
        var copy = self
        copy.scanlightPort = trimmed
        return copy
    }
}

// MARK: - ScanCoordinator

/// @MainActor state machine that owns the Scanlight serial-port handoff.
///
/// All public async methods are MainActor-isolated. The phase property is the
/// single source of truth for port ownership. Transitions are atomic and
/// re-entrant (a transition in flight blocks re-entry via `transitionInFlight`).
@MainActor
final class ScanCoordinator: ObservableObject {

    // MARK: - Published state

    /// Current scan phase. Drives UI enabled/disabled states throughout the app.
    @Published private(set) var phase: ScanPhase = .idle

    /// Whether a state transition is currently in progress (prevents re-entry).
    @Published private(set) var transitionInFlight: Bool = false

    /// Last error message — set on transition failures (start timeout, crash, etc.).
    @Published var lastError: String = ""

    /// Whether reconnecting the light panel failed after a scan stop. When true,
    /// ScanView shows a "Reconnect Light" button.
    @Published private(set) var reconnectNeeded: Bool = false

    /// Ordered list of frame statuses (frame counter and composite state).
    /// Updated on capture events and composite-status polls.
    @Published private(set) var frameStatuses: [FrameStatus] = []

    /// Composite queue depth (pending jobs), from /api/composite-status polling.
    @Published private(set) var compositePending: Int = 0

    /// True while a captureFrame round-trip is in progress.
    @Published private(set) var captureInFlight: Bool = false

    /// Next frame number the backend will shoot. Updated from capture outcomes.
    @Published private(set) var nextFrameNumber: Int = 1

    /// The channel the operator must fire next in manual/hw mode (F1).
    /// "R" | "G" | "B" while a triplet capture awaits the IED shot; nil when idle.
    /// Sourced from GET /api/state `waiting_for_channel` and polled concurrently
    /// with the captureFrame HTTP call so the UI updates during the blocking wait.
    @Published private(set) var waitingForChannel: String? = nil

    // MARK: - Dependencies (injected via protocol)

    private let client: any OrchestratorClientProtocol
    private let lightPanel: any LightPanelProtocol

    var scanlightPort: String {
        lightPanel.scanlightPort
    }

    // MARK: - Privates

    /// Cancellable for the isRunning Combine sink (crash monitor).
    private var isRunningCancellable: AnyCancellable?
    /// Task for the 1s composite-status polling loop (non-nil while .scanning).
    private var compositePollingTask: Task<Void, Never>?
    /// Task for the 1s channel-prompt polling loop (non-nil during captureInFlight).
    /// Polls GET /api/state for `waiting_for_channel` so the banner updates while
    /// the captureFrame HTTP call is blocked waiting for the operator (F1).
    private var channelPromptTask: Task<Void, Never>?

    /// The settings the running backend was last started with (port-normalized).
    /// Used by the calibration→scan fast path to decide whether the backend can
    /// be reused as-is or must be restarted to pick up changed scan settings /
    /// stock profile (`positiveProfileJSON` is spawn-only, so a stale reuse would
    /// silently scan with the wrong exposure recipe or omit auto-positive output).
    private var backendSettings: ScanSettings?

    // MARK: - Init

    /// Production init: pass the concrete OrchestratorClient and ScanlightViewModel.
    convenience init(client: OrchestratorClient, lightViewModel: ScanlightViewModel) {
        self.init(clientProto: client, lightPanelProto: lightViewModel)
    }

    /// Testable init: accepts protocol types so tests can inject recording fakes.
    init(clientProto: any OrchestratorClientProtocol, lightPanelProto: any LightPanelProtocol) {
        self.client = clientProto
        self.lightPanel = lightPanelProto
        // Install the crash monitor. OrchestratorClient.isRunning flips false
        // when the child exits for any reason (Phase 05 termination handler).
        // We observe it to transition out of .scanning on an unexpected crash.
        isRunningCancellable = clientProto.isRunningPublisher
            .removeDuplicates()
            .sink { [weak self] isRunning in
                guard let self else { return }
                // Act ONLY on an UNEXPECTED false-flip mid-owned-run — i.e. a
                // real orchestrator crash. Intentional stopScan()/stopCalibration()
                // also flip isRunning false while the phase still owns the port;
                // without the !transitionInFlight guard that would mislabel a
                // normal stop as a crash and double-connect the light panel.
                if !isRunning
                    && (self.phase == .scanning || self.phase == .calibrating)
                    && !self.transitionInFlight {
                    Task { @MainActor in
                        await self.handleOrchestratorCrash()
                    }
                }
            }
    }

    deinit {
        isRunningCancellable?.cancel()
        compositePollingTask?.cancel()
        // Normal stop paths cancel this, but a deinit while a capture is in
        // flight would otherwise leave the 1s prompt poll waking until exit.
        channelPromptTask?.cancel()
    }

    // MARK: - Public transitions

    /// Transition idle → scanning.
    ///
    /// ORDER (the port-ownership invariant):
    ///   1. lightPanel.disconnect()       — release the serial port from the app.
    ///   2. client.start(settings:)       — orchestrator grabs the port.
    ///   3. phase = .scanning             — guards enabled (light panel locked).
    ///   4. start composite polling loop.
    ///
    /// On failure at step 2, reconnect the light panel before re-throwing.
    func startScan(settings: ScanSettings) async {
        if phase == .calibrating, !transitionInFlight, client.isRunning {
            guard !captureInFlight else {
                lastError = "Cannot start scan: capture in progress"
                return
            }
            transitionInFlight = true
            defer { transitionInFlight = false }
            lastError = ""
            reconnectNeeded = false
            frameStatuses = []
            compositePending = 0
            nextFrameNumber = 1

            let wanted = settings.withScanlightPort(lightPanel.scanlightPort)
            // Reuse the running backend ONLY when it was started with exactly
            // these settings. Otherwise restart it: scan settings / stock profile
            // may have changed since calibration, and `positiveProfileJSON`,
            // `ffcCalibration`, `cameraModel` are spawn-only args — a settings
            // push (POST /api/settings) cannot carry them, so a stale reuse would
            // scan with the wrong exposure recipe or silently skip auto-positive.
            if backendSettings == wanted {
                lightPanel.portOwner = .scanning
                phase = .scanning
                startCompositePolling()
                return
            }

            // Restart the backend with the scan settings. Keep the port claimed
            // (.scanning) across the restart so a manual Connect / calibration
            // can't grab the serial port during the respawn window.
            lightPanel.portOwner = .scanning
            await client.stop()
            do {
                try await client.start(settings: wanted)
            } catch {
                backendSettings = nil
                lightPanel.portOwner = .idle
                lightPanel.connect()
                phase = .idle
                lastError = "Failed to switch to scan: \(error.localizedDescription)"
                return
            }
            backendSettings = wanted
            phase = .scanning
            startCompositePolling()
            return
        }

        guard phase == .idle, !transitionInFlight, lightPanel.portOwner == .idle else {
            let reason: String
            if phase != .idle { reason = "not idle" }
            else if transitionInFlight { reason = "transition in flight" }
            else { reason = "serial port is busy (calibration in progress)" }
            lastError = "Cannot start scan: \(reason)"
            return
        }
        transitionInFlight = true
        defer { transitionInFlight = false }

        // Clear previous error + frame history for the new scan.
        lastError = ""
        reconnectNeeded = false
        frameStatuses = []
        compositePending = 0
        nextFrameNumber = 1

        // Step 1: Claim port ownership for the scan, THEN release the panel's
        // connection. portOwner is set BEFORE the (multi-second) client.start()
        // so a manual Connect or a calibration run — both gated on
        // portOwner == .idle — cannot grab the serial port during the spawn
        // window. disconnect() is intentionally not portOwner-guarded.
        lightPanel.portOwner = .scanning
        lightPanel.disconnect()

        // Step 2: Spawn the orchestrator (it grabs the serial port).
        let wanted = settings.withScanlightPort(lightPanel.scanlightPort)
        do {
            try await client.start(settings: wanted)
        } catch {
            // Orchestrator failed to start — release ownership and reclaim the port.
            backendSettings = nil
            lightPanel.portOwner = .idle
            lightPanel.connect()
            lastError = "Failed to start scan: \(error.localizedDescription)"
            return
        }
        backendSettings = wanted

        // Step 3: Mark as scanning (portOwner already claimed in Step 1).
        phase = .scanning

        // Step 4: Start polling composite status every 1s.
        startCompositePolling()
    }

    /// Transition scanning → idle.
    ///
    /// ORDER (the port-ownership invariant):
    ///   1. stopCompositePolling()         — cancel the polling loop.
    ///   2. client.stop()                  — SIGTERM the orchestrator.
    ///   3. phase = .idle                  — unlock the light panel.
    ///   4. lightPanel.connect()           — reclaim the serial port.
    ///
    /// If step 4 fails, set reconnectNeeded = true instead of silently
    /// leaving the panel dead (per the SPEC's port-reclaim failure rule).
    func stopScan() async {
        guard phase == .scanning, !transitionInFlight else { return }
        transitionInFlight = true
        defer { transitionInFlight = false }

        // Step 1: Cancel the composite and channel-prompt polling loops.
        stopCompositePolling()
        stopChannelPromptPolling()
        waitingForChannel = nil

        // Step 2: Stop the orchestrator (releases the serial port on the Python side).
        await client.stop()

        // Step 3: Unlock the light panel (phase must be .idle before connect()).
        phase = .idle
        backendSettings = nil
        lightPanel.portOwner = .idle

        // Step 4: Reclaim the serial port.
        lightPanel.connect()
        // Check if the reconnect succeeded. ScanlightViewModel.connect() is
        // synchronous and sets isConnected = true on success.
        if !lightPanel.isConnected {
            reconnectNeeded = true
            lastError = "Light panel failed to reconnect. Use 'Reconnect Light' to retry."
        }
    }

    /// Transition idle → calibrating.
    ///
    /// Calibration uses the same `triplet-capture` child process as scanning,
    /// because the exposure/FFC routes reuse the orchestrator capture loop. The
    /// port handoff mirrors startScan(): claim ownership first, disconnect the
    /// manual light panel, then spawn the Python server.
    func startCalibration(settings: ScanSettings) async {
        if phase == .scanning, !transitionInFlight, client.isRunning {
            guard !captureInFlight else {
                lastError = "Cannot start calibration: capture in progress"
                return
            }
            transitionInFlight = true
            defer { transitionInFlight = false }
            stopCompositePolling()
            lastError = ""
            reconnectNeeded = false
            compositePending = 0
            lightPanel.portOwner = .calibrating
            phase = .calibrating
            return
        }

        guard phase == .idle, !transitionInFlight, lightPanel.portOwner == .idle else {
            let reason: String
            if phase == .scanning { reason = "scan in progress" }
            else if phase == .calibrating { reason = "calibration already running" }
            else if transitionInFlight { reason = "transition in flight" }
            else { reason = "serial port is busy" }
            lastError = "Cannot start calibration: \(reason)"
            return
        }
        transitionInFlight = true
        defer { transitionInFlight = false }

        lastError = ""
        reconnectNeeded = false

        lightPanel.portOwner = .calibrating
        lightPanel.disconnect()

        let wanted = settings.withScanlightPort(lightPanel.scanlightPort)
        do {
            try await client.start(settings: wanted)
        } catch {
            backendSettings = nil
            lightPanel.portOwner = .idle
            lightPanel.connect()
            lastError = "Failed to start calibration: \(error.localizedDescription)"
            return
        }
        backendSettings = wanted

        phase = .calibrating
    }

    /// Transition calibrating → idle and reclaim the light panel.
    func stopCalibration() async {
        guard phase == .calibrating, !transitionInFlight else { return }
        transitionInFlight = true
        defer { transitionInFlight = false }

        await client.stop()

        phase = .idle
        backendSettings = nil
        lightPanel.portOwner = .idle
        lightPanel.connect()
        if !lightPanel.isConnected {
            reconnectNeeded = true
            lastError = "Light panel failed to reconnect after calibration. Use 'Reconnect Light' to retry."
        }
    }

    /// Manual reconnect after a failed port-reclaim. Shown in ScanView when
    /// reconnectNeeded == true.
    func reconnectLight() {
        guard phase == .idle else { return }
        lightPanel.connect()
        if lightPanel.isConnected {
            reconnectNeeded = false
            lastError = ""
        } else {
            lastError = "Reconnect failed. Check the Scanlight connection."
        }
    }

    /// Trigger a triplet capture. No-op unless .scanning.
    ///
    /// Updates frameStatuses with "captured" on success so the list shows
    /// immediate feedback before the composite-status poll catches up.
    ///
    /// Concurrently polls GET /api/state at 1s intervals while the capture HTTP
    /// call is in flight, so `waitingForChannel` updates reach the UI during the
    /// blocking wait for the operator to fire the IED (F1).
    func captureFrame(retake: Bool) async {
        guard phase == .scanning, !captureInFlight else { return }
        captureInFlight = true
        waitingForChannel = nil

        // Start concurrent channel-prompt polling while the HTTP call blocks.
        startChannelPromptPolling()
        defer {
            captureInFlight = false
            stopChannelPromptPolling()
            waitingForChannel = nil
        }

        do {
            let outcome = try await client.captureFrame(retake: retake)
            let frameNum = outcome.frameNumber
            nextFrameNumber = outcome.nextFrame
            if outcome.success {
                // A retake re-shoots an existing frame: reset its row to
                // "captured" even if a prior composite advanced it to
                // "done"/"failed", so the UI reflects the in-progress recapture.
                upsertFrameStatus(frameNum, compositeState: "captured", resetProgress: retake)
            } else {
                upsertFrameStatus(frameNum, compositeState: "failed")
                lastError = outcome.error ?? "Capture failed"
            }
        } catch {
            lastError = "Capture error: \(error.localizedDescription)"
        }
    }

    // MARK: - Private: crash recovery

    /// Called when OrchestratorClientProtocol.isRunning flips false during a scan.
    /// Transitions .scanning → .idle and tries to reclaim the light panel.
    private func handleOrchestratorCrash() async {
        guard phase == .scanning || phase == .calibrating else { return }
        let crashedPhase = phase
        // The orchestrator has already exited — no need to call client.stop().
        stopCompositePolling()
        phase = .idle
        backendSettings = nil
        lightPanel.portOwner = .idle
        let noun = crashedPhase == .calibrating ? "calibration" : "scan"
        lastError = "Orchestrator crashed — \(noun) stopped. "
            + (client.lastError.isEmpty ? "" : client.lastError)

        // Attempt to reclaim the serial port.
        lightPanel.connect()
        if !lightPanel.isConnected {
            reconnectNeeded = true
            lastError += " Failed to reconnect light panel."
        }
    }

    // MARK: - Private: composite polling

    private func startCompositePolling() {
        compositePollingTask?.cancel()
        compositePollingTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.pollCompositeStatus()
                try? await Task.sleep(nanoseconds: 1_000_000_000) // 1s
            }
        }
    }

    private func stopCompositePolling() {
        compositePollingTask?.cancel()
        compositePollingTask = nil
    }

    /// Internal so tests can invoke it directly without waiting 1s per poll cycle.
    @MainActor
    internal func pollCompositeStatus() async {
        guard phase == .scanning else { return }
        do {
            let status = try await client.compositeStatus()
            compositePending = status.pending ?? 0
            // Merge results into frameStatuses.
            for entry in status.results ?? [] {
                upsertFrameStatus(entry.frameNumber, compositeState: entry.status)
            }
        } catch {
            // Polling failure is non-fatal — we'll retry next second.
            // Don't overwrite lastError with transient network noise.
        }
    }

    // MARK: - Private: channel-prompt polling (F1)

    private func startChannelPromptPolling() {
        channelPromptTask?.cancel()
        channelPromptTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.pollWaitingForChannel()
                try? await Task.sleep(nanoseconds: 1_000_000_000)  // 1s
            }
        }
    }

    private func stopChannelPromptPolling() {
        channelPromptTask?.cancel()
        channelPromptTask = nil
    }

    /// Poll GET /api/state and update `waitingForChannel`.
    /// Internal so tests can drive it directly.
    @MainActor
    internal func pollWaitingForChannel() async {
        guard captureInFlight else { return }
        do {
            let state = try await client.fetchState()
            let newChannel = state.waitingForChannel
            // Sound on channel change (F1 optional: always-on for v1).
            if newChannel != waitingForChannel, newChannel != nil {
                NSSound.beep()
            }
            waitingForChannel = newChannel
        } catch {
            // Non-fatal — the poll loop retries next second.
        }
    }

    // MARK: - Private: frame status helpers

    @MainActor
    private func upsertFrameStatus(_ frameNumber: Int, compositeState: String, resetProgress: Bool = false) {
        if let idx = frameStatuses.firstIndex(where: { $0.frameNumber == frameNumber }) {
            if resetProgress {
                // Retake: force the state regardless of rank so a re-shoot of a
                // "done" frame shows the recapture, not the stale prior result.
                frameStatuses[idx].compositeState = compositeState
                return
            }
            // Only advance state — never regress "done"/"failed" back to "captured"
            // on an out-of-order composite-status poll.
            let current = frameStatuses[idx].compositeState
            let rank: [String: Int] = ["captured": 0, "compositing": 1, "done": 2, "failed": 2]
            let currentRank = rank[current] ?? 0
            let newRank = rank[compositeState] ?? 0
            if newRank >= currentRank {
                frameStatuses[idx].compositeState = compositeState
            }
        } else {
            frameStatuses.append(FrameStatus(frameNumber: frameNumber, compositeState: compositeState))
            frameStatuses.sort { $0.frameNumber < $1.frameNumber }
        }
    }
}
