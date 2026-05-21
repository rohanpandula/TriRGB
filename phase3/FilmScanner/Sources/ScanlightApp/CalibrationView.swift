// CalibrationView — SwiftUI view for FFC calibration capture and result display.
//
// Observes CalibrationViewModel (injected or created fresh) via @StateObject.
// Also observes ScanlightViewModel for the serial-port guard: the "Capture
// Calibration" button is disabled when the Light panel is connected, preventing
// capture-calibration.sh (which opens the serial port via scanlightctl) from
// conflicting with the app's existing connection.
//
// Layout: PanelGroupBoxStyle sections (Calibration / Results / Apply). The
// Results and Apply sections render only when a CalibrationResult exists — that
// conditional, plus every AccessibilityID and the verdict accessibilityValue
// wiring, is preserved exactly (the automation contract depends on it).
//
// Injection pattern (mirrors OrchestratorClientTests URLSession injection):
//   CalibrationView(store:viewModel:calibrationViewModel:calVM)

import SwiftUI

struct CalibrationView: View {

    // MARK: - Dependencies

    /// Shared settings store — written by "Use this calibration".
    @ObservedObject var store: SettingsStore

    /// Observed for isConnected (serial-port guard).
    @ObservedObject var viewModel: ScanlightViewModel

    /// Calibration state machine + injectable runner.
    @StateObject private var calViewModel: CalibrationViewModel

    // MARK: - Init

    init(store: SettingsStore,
         viewModel: ScanlightViewModel,
         calibrationViewModel: CalibrationViewModel? = nil) {
        self.store = store
        self.viewModel = viewModel
        _calViewModel = StateObject(wrappedValue: calibrationViewModel ?? CalibrationViewModel())
    }

    // MARK: - Body

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Theme.Space.section) {
                calibrationSection
                if calViewModel.result != nil {
                    resultsSection
                    applySection
                }
            }
            .padding(Theme.Space.xl)
        }
        .groupBoxStyle(PanelGroupBoxStyle())
    }

    // MARK: - Sections

    /// GroupBox 1: Capture trigger, running indicator, error display, hint.
    private var calibrationSection: some View {
        GroupBox(label: Text("Calibration")) {
            VStack(alignment: .leading, spacing: Theme.Space.md) {
                // Serial-port guard: explain why capture is blocked while connected.
                if viewModel.portOwner == .scanning {
                    Banner(kind: .warning,
                           text: "A scan is using the serial port. Stop the scan before calibrating.")
                } else if viewModel.isConnected {
                    Banner(kind: .warning,
                           text: "Disconnect the Light panel before calibrating — the script needs the serial port.")
                }

                HStack(spacing: Theme.Space.md) {
                    Button("Capture Calibration") {
                        // Claim the serial port for the calibration script before
                        // launching it — capture-calibration.sh opens the port via
                        // scanlightctl, so the Light tab's Connect/channel controls
                        // (and Start Scan) must lock while it runs. Set synchronously
                        // here (not inside the Task) so a rapid double-tap can't pass
                        // this guard twice and spawn two runs. Released on completion.
                        guard viewModel.portOwner == .idle, !viewModel.isConnected, !calViewModel.isRunning else { return }
                        viewModel.portOwner = .calibrating
                        Task { @MainActor in
                            defer { viewModel.portOwner = .idle }
                            await calViewModel.runCalibration()
                        }
                    }
                    .accessibilityIdentifier(AccessibilityID.calCaptureBtn)
                    .buttonStyle(.borderedProminent)
                    .disabled(calViewModel.isRunning || viewModel.isConnected || viewModel.portOwner != .idle)

                    if calViewModel.isRunning {
                        ProgressView()
                            .controlSize(.small)
                        Text("Capturing flat-field frames\u{2026}")
                            .font(.callout)
                            .foregroundStyle(.secondary)
                    }
                }

                if let err = calViewModel.lastError {
                    Banner(kind: .danger, text: err)
                }

                if calViewModel.result == nil && !calViewModel.isRunning && calViewModel.lastError == nil {
                    Text("Capture a flat-field frame per channel to measure illumination falloff and uniformity. Results appear below.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        }
    }

    /// GroupBox 2: 3-row verdict table (R/G/B) + overall row. Shown only when
    /// a CalibrationResult is available.
    private var resultsSection: some View {
        Group {
            if let result = calViewModel.result {
                GroupBox(label: Text("Results")) {
                    VStack(spacing: Theme.Space.sm) {
                        // Column headers
                        HStack(spacing: Theme.Space.md) {
                            Text("")
                                .frame(width: 52, alignment: .leading)
                            Text("Falloff")
                                .frame(width: 72, alignment: .trailing)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                            Text("Uniformity")
                                .frame(width: 80, alignment: .trailing)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                            Text("Verdict")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                            Spacer(minLength: 0)
                        }

                        verdictRow(channel: "R", tint: Theme.Channel.red, stats: result.channelR)
                        verdictRow(channel: "G", tint: Theme.Channel.green, stats: result.channelG)
                        verdictRow(channel: "B", tint: Theme.Channel.blue, stats: result.channelB)

                        Divider().opacity(0.6)

                        // Overall verdict row
                        HStack(spacing: Theme.Space.md) {
                            Text("Overall")
                                .font(.subheadline.weight(.semibold))
                                .frame(width: 52, alignment: .leading)
                            Spacer(minLength: 0)
                            Chip(text: result.overall, tint: verdictTint(result.overall))
                                .accessibilityIdentifier(AccessibilityID.calOverallLabel)
                                .accessibilityValue(result.overall)
                        }
                    }
                }
            }
        }
    }

    /// GroupBox 3: "Use this calibration" button + FFC path confirmation.
    /// Shown only when a CalibrationResult is available.
    private var applySection: some View {
        Group {
            if calViewModel.result != nil {
                GroupBox(label: Text("Apply")) {
                    VStack(alignment: .leading, spacing: Theme.Space.sm) {
                        Button("Use this calibration") {
                            calViewModel.applyCalibration(to: store)
                        }
                        .accessibilityIdentifier(AccessibilityID.calUseBtn)
                        .buttonStyle(.borderedProminent)
                        .disabled(calViewModel.result == nil)

                        if let dir = calViewModel.lastCalDir {
                            HStack(spacing: Theme.Space.sm) {
                                Image(systemName: "checkmark.circle.fill")
                                    .foregroundStyle(Theme.State.success)
                                    .accessibilityHidden(true)
                                Text("FFC path set to \(dir.path)")
                                    .font(.system(.caption, design: .monospaced))
                                    .foregroundStyle(.secondary)
                                    .lineLimit(1)
                                    .truncationMode(.middle)
                            }
                        }
                    }
                }
            }
        }
    }

    // MARK: - Verdict helpers

    /// One data row in the verdict table: channel dot + falloff/uniformity + verdict chip.
    @ViewBuilder
    private func verdictRow(channel: String, tint: Color, stats: ChannelCalResult?) -> some View {
        if let stats = stats {
            HStack(spacing: Theme.Space.md) {
                HStack(spacing: Theme.Space.sm) {
                    Circle().fill(tint).frame(width: 9, height: 9).accessibilityHidden(true)
                    Text(channel).font(.subheadline).frame(width: 28, alignment: .leading)
                }
                .frame(width: 52, alignment: .leading)
                Text(String(format: "%.1f%%", stats.falloffPct))
                    .frame(width: 72, alignment: .trailing)
                    .monospacedDigit()
                    .accessibilityLabel("\(channel) falloff")
                    .accessibilityValue(String(format: "%.1f%%", stats.falloffPct))
                Text(String(format: "%.1f%%", stats.uniformityPct))
                    .frame(width: 80, alignment: .trailing)
                    .monospacedDigit()
                    .accessibilityLabel("\(channel) uniformity")
                    .accessibilityValue(String(format: "%.1f%%", stats.uniformityPct))
                Chip(text: stats.verdict, tint: verdictTint(stats.verdict))
                    .accessibilityIdentifier(axId(for: channel))
                    .accessibilityValue(stats.verdict)
                Spacer(minLength: 0)
            }
        }
    }

    /// Map a verdict string to its semantic tint.
    ///   clean → success, acceptable → warning, fail → danger.
    private func verdictTint(_ verdict: String) -> Color {
        switch verdict {
        case "clean": return Theme.State.success
        case "fail": return Theme.State.danger
        default: return Theme.State.warning
        }
    }

    /// Map channel name to its AccessibilityID constant.
    private func axId(for channel: String) -> String {
        switch channel {
        case "R": return AccessibilityID.calVerdictR
        case "G": return AccessibilityID.calVerdictG
        case "B": return AccessibilityID.calVerdictB
        default:  return "lbl-cal-verdict-\(channel.lowercased())"
        }
    }
}
