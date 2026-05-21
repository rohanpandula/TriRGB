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

                        HStack(spacing: Theme.Space.md) {
                            Button("Start Scan") {
                                Task { await coordinator.startScan(settings: store.settings) }
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
                            Text("Start a scan to hand the serial port to the orchestrator. The Light panel locks while scanning.")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                    }
                }

                // MARK: 2. Capture Controls (only shown while scanning)

                if coordinator.phase == .scanning {
                    GroupBox(label: Text("Capture")) {
                        VStack(alignment: .leading, spacing: Theme.Space.md) {
                            HStack(spacing: Theme.Space.md) {
                                Text("Frames")
                                    .font(.subheadline)
                                    .foregroundStyle(.secondary)
                                Text("\(coordinator.frameStatuses.count)")
                                    .accessibilityIdentifier(AccessibilityID.scanFrameCounterLabel)
                                    .accessibilityValue("\(coordinator.frameStatuses.count)")
                                    .font(.title3.weight(.semibold))
                                    .monospacedDigit()

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
                                .disabled(coordinator.captureInFlight || coordinator.transitionInFlight)

                                Button("Retake") {
                                    Task { await coordinator.captureFrame(retake: true) }
                                }
                                .accessibilityIdentifier(AccessibilityID.scanRetakeBtn)
                                .buttonStyle(.bordered)
                                .disabled(coordinator.captureInFlight
                                         || coordinator.transitionInFlight
                                         || coordinator.frameStatuses.isEmpty)

                                if coordinator.captureInFlight {
                                    ProgressView().controlSize(.small)
                                }
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
                        Text("Light panel is controlled by the active scan")
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
    }

    // MARK: - Helpers

    private var canStartScan: Bool {
        coordinator.phase == .idle && !coordinator.transitionInFlight
    }

    private var canStopScan: Bool {
        coordinator.phase == .scanning && !coordinator.transitionInFlight
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
