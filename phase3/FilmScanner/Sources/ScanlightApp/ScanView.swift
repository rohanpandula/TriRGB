// ScanView — Phase 07 scan-loop UI.
//
// Sections:
//   1. Scan Session — Start/Stop Scan with a phase chip.
//   2. Capture — Capture Frame / Retake, frame counter, composite queue (scanning only).
//   3. Frame Status — per-frame captured→compositing→done→failed rows.
//   4. Light-locked banner — shown when coordinator.phase != .idle.
//   5. Port-reclaim error — "Reconnect Light" when reconnectNeeded.
//   6. Error — last error.
//
// All state flows from ScanCoordinator (injected, @ObservedObject). Every
// conditional-render predicate and `.disabled` guard below is preserved from the
// original verbatim — they encode the serial-port-ownership state machine. Only
// presentation changed: shared PanelGroupBoxStyle, Chip, and Banner vocabulary.

import SwiftUI

struct ScanView: View {
    @ObservedObject var coordinator: ScanCoordinator
    @ObservedObject var store: SettingsStore
    @ObservedObject var orchestratorClient: OrchestratorClient
    @ObservedObject var lightViewModel: ScanlightViewModel

    @State private var selectedStockProfileID: UUID? = nil
    @State private var appliedStockProfileID: UUID? = nil
    @State private var stockProfileMessage: String? = nil
    @State private var scanLiveViewActive = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Theme.Space.section) {

                // MARK: 1. Session Control

                GroupBox(label: Text("Scan Session")) {
                    VStack(alignment: .leading, spacing: Theme.Space.md) {
                        HStack(spacing: Theme.Space.md) {
                            Text("Phase")
                                .font(.subheadline)
                                .foregroundStyle(.secondary)
                            phaseChip(coordinator.phase)
                                .animation(.easeOut(duration: 0.2), value: coordinator.phase)
                            Spacer(minLength: 0)
                        }

                        stockProfileSection

                        HStack(spacing: Theme.Space.md) {
                            Button(scanStartLabel) {
                                Task { await startScan() }
                            }
                            .accessibilityIdentifier(AccessibilityID.scanStartBtn)
                            .buttonStyle(.borderedProminent)
                            .disabled(!canStartScan)

                            Button("Stop Scan") {
                                Task { await coordinator.stopScan() }
                            }
                            .accessibilityIdentifier(AccessibilityID.scanStopBtn)
                            .buttonStyle(.bordered)
                            .tint(Theme.State.danger)
                            .disabled(!canStopScan)

                            if coordinator.transitionInFlight {
                                ProgressView().controlSize(.small)
                            }
                        }

                        if coordinator.phase == .idle && !coordinator.reconnectNeeded {
                            Text("Start a scan when the roll is positioned. The app owns the light while scanning.")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        } else if coordinator.phase == .calibrating {
                            Text("Switching to Scan reuses the running backend; no reconnect is needed.")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }

                        if !store.validationErrors.isEmpty {
                            Banner(kind: .danger, text: "Choose the required folders before scanning.")
                        }
                    }
                }

                // MARK: 2. SDK Live View

                if store.settings.triggerMode == "sdk" {
                    GroupBox(label: Text("Camera Live View")) {
                        VStack(alignment: .leading, spacing: Theme.Space.md) {
                            SonyLiveViewPanel(
                                settings: store.settings,
                                orchestratorClient: orchestratorClient,
                                lightViewModel: lightViewModel,
                                isLiveViewActive: $scanLiveViewActive,
                                allowsWhileBackendRunning: true,
                                isTemporarilyUnavailable: coordinator.captureInFlight || coordinator.transitionInFlight,
                                unavailableReason: "Live view pauses while the camera is capturing or the backend is changing state.",
                                startButtonID: AccessibilityID.scanSonyLiveViewStartButton,
                                stopButtonID: AccessibilityID.scanSonyLiveViewStopButton,
                                statusLabelID: AccessibilityID.scanSonyLiveViewStatusLabel,
                                imageID: AccessibilityID.scanSonyLiveViewImage,
                                invertToggleID: AccessibilityID.scanSonyLiveViewInvertToggle,
                                mirrorToggleID: AccessibilityID.scanSonyLiveViewMirrorToggle,
                                flipToggleID: AccessibilityID.scanSonyLiveViewFlipToggle,
                                rotatePickerID: AccessibilityID.scanSonyLiveViewRotatePicker,
                                zoomSliderID: AccessibilityID.scanSonyLiveViewZoomSlider,
                                whiteLightToggleID: AccessibilityID.scanSonyLiveViewWhiteLightToggle
                            )

                            if scanLiveViewActive {
                                Banner(kind: .warning, text: "Close live view before Capture Frame. The camera can only service one Sony SDK capture session reliably.")
                            }
                        }
                    }
                }

                // MARK: 2. Capture Controls (only shown while scanning)

                if coordinator.phase == .scanning {
                    GroupBox(label: Text("Capture")) {
                        VStack(alignment: .leading, spacing: Theme.Space.md) {
                            HStack(spacing: Theme.Space.md) {
                                scanMetric(
                                    label: "Captured",
                                    value: "\(coordinator.frameStatuses.count)",
                                    accessibilityID: AccessibilityID.scanFrameCounterLabel
                                )

                                scanMetric(
                                    label: "Next shot",
                                    value: "\(coordinator.nextFrameNumber)",
                                    accessibilityID: AccessibilityID.scanNextFrameLabel
                                )

                                Spacer()

                                // Composite queue depth badge. Both branches carry the
                                // same AX-ID + value so automation reads it consistently.
                                if coordinator.compositePending > 0 {
                                    Chip(text: "Compositing \(coordinator.compositePending)",
                                         tint: Theme.State.warning)
                                        .accessibilityIdentifier(AccessibilityID.scanCompositeQueueLabel)
                                        .accessibilityValue("\(coordinator.compositePending)")
                                } else {
                                    Text("Compositing 0")
                                        .accessibilityIdentifier(AccessibilityID.scanCompositeQueueLabel)
                                        .accessibilityValue("0")
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                }
                            }

                            HStack(spacing: Theme.Space.md) {
                                Button("Capture Frame") {
                                    Task { await coordinator.captureFrame(retake: false) }
                                }
                                .accessibilityIdentifier(AccessibilityID.scanCaptureFrameBtn)
                                .buttonStyle(.borderedProminent)
                                .disabled(captureControlsDisabled)

                                Button("Retake") {
                                    Task { await coordinator.captureFrame(retake: true) }
                                }
                                .accessibilityIdentifier(AccessibilityID.scanRetakeBtn)
                                .buttonStyle(.bordered)
                                .disabled(coordinator.captureInFlight
                                         || coordinator.transitionInFlight
                                         || scanLiveViewActive
                                         || coordinator.frameStatuses.isEmpty)

                                if coordinator.captureInFlight {
                                    ProgressView().controlSize(.small)
                                }
                            }

                            if scanLiveViewActive {
                                Text("Close live view to shoot; the live stream is for framing, then capture takes the RAW triplet.")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                        }
                    }
                }

                // MARK: 3. Frame Status List

                if !coordinator.frameStatuses.isEmpty {
                    GroupBox(label: Text("Frame Status")) {
                        LazyVStack(alignment: .leading, spacing: Theme.Space.sm) {
                            ForEach(coordinator.frameStatuses) { fs in
                                frameStatusRow(fs)
                            }
                        }
                        .accessibilityIdentifier(AccessibilityID.scanFrameStatusList)
                    }
                }

                // MARK: 4. Light-locked banner (when not idle)

                if coordinator.phase != .idle {
                    HStack(alignment: .firstTextBaseline, spacing: Theme.Space.sm) {
                        Image(systemName: "lock.fill")
                            .foregroundStyle(Theme.State.warning)
                            .accessibilityHidden(true)
                        Text(coordinator.phase == .calibrating
                             ? "Light is controlled by calibration"
                             : "Light is controlled by the active scan")
                            .accessibilityIdentifier(AccessibilityID.scanLightLockedLabel)
                            .font(.callout)
                            .foregroundStyle(Theme.State.warning)
                        Spacer(minLength: 0)
                    }
                    .padding(.horizontal, Theme.Space.md)
                    .padding(.vertical, Theme.Space.sm)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(
                        RoundedRectangle(cornerRadius: Theme.Radius.control, style: .continuous)
                            .fill(Theme.State.warning.opacity(0.12))
                    )
                }

                // MARK: 5. Port-reclaim error + Reconnect button

                if coordinator.reconnectNeeded {
                    GroupBox(label: Text("Light Panel")) {
                        VStack(alignment: .leading, spacing: Theme.Space.md) {
                            Banner(kind: .danger, text: "Light panel failed to reconnect after the scan.")
                            Button("Reconnect Light") {
                                coordinator.reconnectLight()
                            }
                            .accessibilityIdentifier(AccessibilityID.scanReconnectLightBtn)
                            .buttonStyle(.borderedProminent)
                        }
                    }
                }

                // MARK: 6. Error display

                if !coordinator.lastError.isEmpty {
                    GroupBox(label: Text("Last Error")) {
                        Banner(kind: .danger, text: coordinator.lastError)
                    }
                }
            }
            .padding(Theme.Space.xl)
        }
        .groupBoxStyle(PanelGroupBoxStyle())
        .onAppear {
            ensureSelectedStockProfile()
        }
        .onChange(of: store.stockCalibrationProfiles.map(\.id)) { _ in
            ensureSelectedStockProfile()
        }
    }

    // MARK: - Helpers

    @MainActor
    private func startScan() async {
        let errors = store.validate()
        guard errors.isEmpty else { return }
        applySelectedStockProfileForScan()
        await coordinator.startScan(settings: store.settings)
    }

    private var canStartScan: Bool {
        (coordinator.phase == .idle || coordinator.phase == .calibrating)
            && !coordinator.transitionInFlight
    }

    private var scanStartLabel: String {
        coordinator.phase == .calibrating ? "Switch to Scan" : "Start Scan"
    }

    private var canStopScan: Bool {
        coordinator.phase == .scanning && !coordinator.transitionInFlight
    }

    private var captureControlsDisabled: Bool {
        coordinator.captureInFlight || coordinator.transitionInFlight || scanLiveViewActive
    }

    private var stockProfileStatusText: String {
        if store.stockCalibrationProfiles.isEmpty {
            return "No saved film stock profiles yet. Use Calibrate > Exposure, then Save Profile."
        }
        if let profile = store.stockCalibrationProfile(id: appliedStockProfileID) {
            return "Using \(profile.displayName). Capture or intentionally skip a fresh flat field for this roll."
        }
        if coordinator.phase == .scanning {
            return "Scanning with the current RGB/shutter settings. Stop scan to change film stock profile."
        }
        if selectedStockProfileID != nil {
            return "Start Scan applies this stock profile before the roll begins."
        }
        return "Choose the film stock you calibrated against before starting this roll."
    }

    private var stockProfileSelectionDisabled: Bool {
        coordinator.phase == .scanning || coordinator.transitionInFlight
    }

    @ViewBuilder
    private var stockProfileSection: some View {
        VStack(alignment: .leading, spacing: Theme.Space.sm) {
            Divider()
                .padding(.vertical, Theme.Space.xs)

            Text("Film Stock")
                .font(.subheadline.weight(.semibold))

            HStack(spacing: Theme.Space.md) {
                Picker("Film stock", selection: $selectedStockProfileID) {
                    Text("Current settings").tag(Optional<UUID>.none)
                    ForEach(store.stockCalibrationProfiles) { profile in
                        Text(profile.displayName).tag(Optional(profile.id))
                    }
                }
                .accessibilityIdentifier(AccessibilityID.scanStockProfilePicker)
                .pickerStyle(.menu)
                .disabled(stockProfileSelectionDisabled || store.stockCalibrationProfiles.isEmpty)

                Button("Apply Profile") {
                    applySelectedStockProfile()
                }
                .accessibilityIdentifier(AccessibilityID.scanStockProfileApplyBtn)
                .buttonStyle(.bordered)
                .disabled(stockProfileSelectionDisabled || selectedStockProfileID == nil)
            }

            Text(stockProfileMessage ?? stockProfileStatusText)
                .accessibilityIdentifier(AccessibilityID.scanStockProfileStatus)
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    @ViewBuilder
    private func scanMetric(label: String, value: String, accessibilityID: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(label)
                .font(.caption)
                .foregroundStyle(.secondary)
            Text(value)
                .accessibilityIdentifier(accessibilityID)
                .accessibilityValue(value)
                .font(.title3.weight(.semibold))
                .monospacedDigit()
        }
        .frame(minWidth: 96, alignment: .leading)
    }

    private func ensureSelectedStockProfile() {
        if let selectedStockProfileID,
           store.stockCalibrationProfile(id: selectedStockProfileID) != nil {
            return
        }
        selectedStockProfileID = store.stockCalibrationProfiles.first?.id
        if let appliedStockProfileID,
           store.stockCalibrationProfile(id: appliedStockProfileID) == nil {
            self.appliedStockProfileID = nil
            stockProfileMessage = nil
        }
    }

    private func applySelectedStockProfile() {
        guard let profile = store.stockCalibrationProfile(id: selectedStockProfileID) else {
            appliedStockProfileID = nil
            store.settings.positiveProfileJSON = nil
            stockProfileMessage = "No stock profile selected."
            return
        }
        store.applyStockCalibrationProfile(profile)
        store.settings.positiveProfileJSON = profile.positiveProfileJSON
        appliedStockProfileID = profile.id
        stockProfileMessage = "Applied \(profile.displayName). Scans will also write auto-positive TIFFs from this film-base profile."
    }

    private func applySelectedStockProfileForScan() {
        guard coordinator.phase == .idle || coordinator.phase == .calibrating else {
            return
        }
        guard let profile = store.stockCalibrationProfile(id: selectedStockProfileID) else {
            store.settings.positiveProfileJSON = nil
            appliedStockProfileID = nil
            return
        }
        store.applyStockCalibrationProfile(profile)
        store.settings.positiveProfileJSON = profile.positiveProfileJSON
        appliedStockProfileID = profile.id
        stockProfileMessage = "Using \(profile.displayName) for this scan. Auto-positive TIFFs will be written next to the linear composites."
    }

    @ViewBuilder
    private func phaseChip(_ phase: ScanPhase) -> some View {
        let (label, tint): (String, Color) = switch phase {
        case .idle:        ("Idle", Theme.State.idle)
        case .calibrating: ("Calibrating", Theme.State.warning)
        case .scanning:    ("Scanning", Theme.State.success)
        }
        Chip(text: label, tint: tint, filled: phase == .scanning)
    }

    @ViewBuilder
    private func frameStatusRow(_ fs: FrameStatus) -> some View {
        HStack(spacing: Theme.Space.md) {
            Text("Frame \(fs.frameNumber)")
                .frame(width: 96, alignment: .leading)
                .monospacedDigit()
                .font(.system(.body, design: .monospaced))
            Chip(text: fs.compositeState, tint: statusTint(fs.compositeState))
            Spacer(minLength: 0)
        }
    }

    /// Map a composite state string to its semantic tint.
    private func statusTint(_ state: String) -> Color {
        switch state {
        case "captured":    return Theme.State.info
        case "compositing": return Theme.State.warning
        case "done":        return Theme.State.success
        case "failed":      return Theme.State.danger
        default:            return Theme.State.idle
        }
    }
}
