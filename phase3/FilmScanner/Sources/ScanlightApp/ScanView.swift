// ScanView — Phase 07 scan-loop UI.
//
// Layout: ScrollView root, VStack(spacing:16), GroupBox sections —
// matching ScanlightView's style exactly.
//
// Sections:
//   1. Session control — Start Scan / Stop Scan with phase badge.
//   2. Capture controls — Capture Frame, Retake, frame counter, composite queue.
//   3. Frame status list — per-frame captured→compositing→done→failed rows.
//   4. Light-locked overlay — shown when coordinator.phase != .idle.
//   5. Port-reclaim error — "Reconnect Light" when reconnectNeeded.
//   6. Error — last error.
//
// All state flows from ScanCoordinator (injected, @ObservedObject).
// ScanView contains zero direct calls to OrchestratorClient or
// ScanlightViewModel — all mutations route through ScanCoordinator.

import SwiftUI

struct ScanView: View {
    @ObservedObject var coordinator: ScanCoordinator
    @ObservedObject var store: SettingsStore

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {

                // MARK: 1. Session Control

                GroupBox(label: Text("Scan Session").font(.headline)) {
                    VStack(alignment: .leading, spacing: 8) {
                        // Phase indicator
                        HStack {
                            Text("Phase:")
                                .frame(width: 80, alignment: .trailing)
                            phaseChip(coordinator.phase)
                        }

                        HStack(spacing: 12) {
                            Button("Start Scan") {
                                Task { await coordinator.startScan(settings: store.settings) }
                            }
                            .accessibilityIdentifier(AccessibilityID.scanStartBtn)
                            .disabled(!canStartScan)

                            Button("Stop Scan") {
                                Task { await coordinator.stopScan() }
                            }
                            .accessibilityIdentifier(AccessibilityID.scanStopBtn)
                            .disabled(!canStopScan)
                        }
                    }
                    .padding(.top, 4)
                }

                // MARK: 2. Capture Controls (only shown while scanning)

                if coordinator.phase == .scanning {
                    GroupBox(label: Text("Capture").font(.headline)) {
                        VStack(alignment: .leading, spacing: 8) {
                            HStack(spacing: 8) {
                                // Frame counter
                                Text("Frames:")
                                    .frame(width: 80, alignment: .trailing)
                                Text("\(coordinator.frameStatuses.count)")
                                    .accessibilityIdentifier(AccessibilityID.scanFrameCounterLabel)
                                    .accessibilityValue("\(coordinator.frameStatuses.count)")
                                    .monospacedDigit()
                                    .frame(minWidth: 30, alignment: .leading)

                                Spacer()

                                // Composite queue depth badge
                                if coordinator.compositePending > 0 {
                                    Text("Compositing: \(coordinator.compositePending)")
                                        .accessibilityIdentifier(AccessibilityID.scanCompositeQueueLabel)
                                        .accessibilityValue("\(coordinator.compositePending)")
                                        .font(.caption)
                                        .padding(.horizontal, 8)
                                        .padding(.vertical, 2)
                                        .background(Color.orange.opacity(0.2))
                                        .cornerRadius(4)
                                } else {
                                    // Always render the element so the AX-ID is always
                                    // accessible. Set to 0 when idle.
                                    Text("Compositing: 0")
                                        .accessibilityIdentifier(AccessibilityID.scanCompositeQueueLabel)
                                        .accessibilityValue("0")
                                        .font(.caption)
                                        .foregroundColor(.secondary)
                                }
                            }

                            HStack(spacing: 12) {
                                Button("Capture Frame") {
                                    Task { await coordinator.captureFrame(retake: false) }
                                }
                                .accessibilityIdentifier(AccessibilityID.scanCaptureFrameBtn)
                                .disabled(coordinator.captureInFlight || coordinator.transitionInFlight)

                                Button("Retake") {
                                    Task { await coordinator.captureFrame(retake: true) }
                                }
                                .accessibilityIdentifier(AccessibilityID.scanRetakeBtn)
                                .disabled(coordinator.captureInFlight
                                         || coordinator.transitionInFlight
                                         || coordinator.frameStatuses.isEmpty)

                                if coordinator.captureInFlight {
                                    ProgressView()
                                        .scaleEffect(0.7)
                                }
                            }
                        }
                        .padding(.top, 4)
                    }
                }

                // MARK: 3. Frame Status List

                if !coordinator.frameStatuses.isEmpty {
                    GroupBox(label: Text("Frame Status").font(.headline)) {
                        LazyVStack(alignment: .leading, spacing: 4) {
                            ForEach(coordinator.frameStatuses) { fs in
                                frameStatusRow(fs)
                            }
                        }
                        .accessibilityIdentifier(AccessibilityID.scanFrameStatusList)
                        .padding(.top, 4)
                    }
                }

                // MARK: 4. Light-locked overlay (when not idle)

                if coordinator.phase != .idle {
                    GroupBox {
                        HStack(spacing: 8) {
                            Image(systemName: "lock.fill")
                                .foregroundColor(.orange)
                            Text("Light panel is controlled by active scan")
                                .accessibilityIdentifier(AccessibilityID.scanLightLockedLabel)
                                .foregroundColor(.orange)
                        }
                        .padding(.top, 4)
                    }
                }

                // MARK: 5. Port-reclaim error + Reconnect button

                if coordinator.reconnectNeeded {
                    GroupBox(label: Text("Light Panel").font(.headline)) {
                        VStack(alignment: .leading, spacing: 6) {
                            Text("Light panel failed to reconnect after scan.")
                                .foregroundColor(.red)
                                .font(.caption)

                            Button("Reconnect Light") {
                                coordinator.reconnectLight()
                            }
                            .accessibilityIdentifier(AccessibilityID.scanReconnectLightBtn)
                        }
                        .padding(.top, 4)
                    }
                }

                // MARK: 6. Error display

                if !coordinator.lastError.isEmpty {
                    GroupBox(label: Text("Last Error").font(.headline)) {
                        Text(coordinator.lastError)
                            .foregroundColor(.red)
                            .font(.caption)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(.top, 4)
                    }
                }

            }
            .padding()
        }
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
        let (label, bg, fg): (String, Color, Color) = switch phase {
        case .idle:        ("Idle",        Color.secondary.opacity(0.15), .primary)
        case .calibrating: ("Calibrating", Color.orange.opacity(0.2),     .orange)
        case .scanning:    ("Scanning",    Color.green.opacity(0.2),      .green)
        }
        Text(label)
            .font(.caption)
            .padding(.horizontal, 8)
            .padding(.vertical, 2)
            .background(bg)
            .foregroundColor(fg)
            .cornerRadius(4)
    }

    @ViewBuilder
    private func frameStatusRow(_ fs: FrameStatus) -> some View {
        HStack(spacing: 8) {
            Text("Frame \(fs.frameNumber)")
                .frame(width: 72, alignment: .leading)
                .monospacedDigit()
                .font(.system(.body, design: .monospaced))
            statusChip(fs.compositeState)
        }
    }

    @ViewBuilder
    private func statusChip(_ state: String) -> some View {
        let (bg, fg): (Color, Color) = switch state {
        case "captured":    (Color.blue.opacity(0.15),   .blue)
        case "compositing": (Color.orange.opacity(0.15), .orange)
        case "done":        (Color.green.opacity(0.15),  .green)
        case "failed":      (Color.red.opacity(0.15),    .red)
        default:            (Color.secondary.opacity(0.1), .secondary)
        }
        Text(state)
            .font(.caption)
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(bg)
            .foregroundColor(fg)
            .cornerRadius(4)
    }
}
