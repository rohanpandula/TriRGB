// CalibrationView — SwiftUI view for FFC calibration capture and result display.
//
// Observes CalibrationViewModel (injected or created fresh) via @StateObject.
// Also observes ScanlightViewModel for the serial-port guard: the "Capture
// Calibration" button is disabled when the Light panel is connected, preventing
// capture-calibration.sh (which opens the serial port via scanlightctl) from
// conflicting with the app's existing connection.
//
// Layout: three GroupBox sections (Calibration / Results / Apply), matching the
// ScanlightView GroupBox + ScrollView + VStack pattern exactly.
//
// Injection pattern (mirrors OrchestratorClientTests URLSession injection):
//   CalibrationView(store:viewModel:calibrationViewModel:calVM)
// Tests pass a CalibrationViewModel with a stub runner. Production code passes
// nil and the View creates its own fresh CalibrationViewModel.
//
// Serial-port guard note: Phase 07 delivers the full port-handoff state machine.
// Phase 06 only prevents the double-open via .disabled(viewModel.isConnected).

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

    /// Create CalibrationView.
    ///
    /// - Parameters:
    ///   - store: The shared SettingsStore (must be @StateObject at the app level).
    ///   - viewModel: ScanlightViewModel for the serial-port guard.
    ///   - calibrationViewModel: Optional pre-built CalibrationViewModel for tests.
    ///     When nil (default), a fresh CalibrationViewModel is created.
    init(store: SettingsStore,
         viewModel: ScanlightViewModel,
         calibrationViewModel: CalibrationViewModel? = nil) {
        self.store = store
        self.viewModel = viewModel
        // Inject or create. @StateObject requires the initial value at init time.
        // This is the standard injection pattern for @StateObject in SwiftUI tests.
        _calViewModel = StateObject(wrappedValue: calibrationViewModel ?? CalibrationViewModel())
    }

    // MARK: - Body

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                calibrationSection
                if calViewModel.result != nil {
                    resultsSection
                    applySection
                }
            }
            .padding()
        }
    }

    // MARK: - Sections

    /// GroupBox 1: Capture trigger, running indicator, error display.
    private var calibrationSection: some View {
        GroupBox(label: Text("Calibration").font(.headline)) {
            VStack(alignment: .leading, spacing: 6) {
                // Serial-port guard: show explanatory label when connected.
                if viewModel.isConnected {
                    Text("Disconnect the Light panel before calibrating.")
                        .foregroundColor(.red)
                        .font(.caption)
                }

                Button("Capture Calibration") {
                    Task { await calViewModel.runCalibration() }
                }
                .accessibilityIdentifier(AccessibilityID.calCaptureBtn)
                .disabled(calViewModel.isRunning || viewModel.isConnected)

                if calViewModel.isRunning {
                    ProgressView()
                }

                if let err = calViewModel.lastError {
                    Text(err)
                        .foregroundColor(.red)
                        .font(.caption)
                }
            }
            .padding(.top, 4)
        }
    }

    /// GroupBox 2: 3-row verdict table (R/G/B) + overall row. Shown only when
    /// a CalibrationResult is available.
    private var resultsSection: some View {
        Group {
            if let result = calViewModel.result {
                GroupBox(label: Text("Results").font(.headline)) {
                    VStack(spacing: 6) {
                        // Column headers
                        HStack {
                            Text("").frame(width: 60, alignment: .trailing)
                            Text("Falloff")
                                .frame(width: 64, alignment: .trailing)
                                .font(.caption)
                                .foregroundColor(.secondary)
                            Text("Uniformity")
                                .frame(width: 64, alignment: .trailing)
                                .font(.caption)
                                .foregroundColor(.secondary)
                            Text("Verdict")
                                .font(.caption)
                                .foregroundColor(.secondary)
                        }

                        verdictRow(channel: "R", stats: result.channelR)
                        verdictRow(channel: "G", stats: result.channelG)
                        verdictRow(channel: "B", stats: result.channelB)

                        Divider()

                        // Overall verdict row
                        HStack {
                            Text("Overall").frame(width: 60, alignment: .trailing)
                            verdictChip(verdict: result.overall)
                                .accessibilityIdentifier(AccessibilityID.calOverallLabel)
                                .accessibilityValue(result.overall)
                        }
                    }
                    .padding(.top, 4)
                }
            }
        }
    }

    /// GroupBox 3: "Use this calibration" button + FFC path confirmation.
    /// Shown only when a CalibrationResult is available.
    private var applySection: some View {
        Group {
            if calViewModel.result != nil {
                GroupBox(label: Text("Apply").font(.headline)) {
                    VStack(alignment: .leading, spacing: 6) {
                        Button("Use this calibration") {
                            calViewModel.applyCalibration(to: store)
                        }
                        .accessibilityIdentifier(AccessibilityID.calUseBtn)
                        .disabled(calViewModel.result == nil)

                        if let dir = calViewModel.lastCalDir {
                            Text("FFC path set to: \(dir.path)")
                                .font(.system(.caption, design: .monospaced))
                                .foregroundColor(.secondary)
                        }
                    }
                    .padding(.top, 4)
                }
            }
        }
    }

    // MARK: - Verdict helpers

    /// One data row in the verdict table.
    @ViewBuilder
    private func verdictRow(channel: String, stats: ChannelCalResult?) -> some View {
        if let stats = stats {
            HStack(spacing: 8) {
                Text(channel).frame(width: 60, alignment: .trailing)
                Text(String(format: "%.1f%%", stats.falloffPct))
                    .frame(width: 64, alignment: .trailing)
                    .monospacedDigit()
                Text(String(format: "%.1f%%", stats.uniformityPct))
                    .frame(width: 64, alignment: .trailing)
                    .monospacedDigit()
                verdictChip(verdict: stats.verdict)
                    .accessibilityIdentifier(axId(for: channel))
                    .accessibilityValue(stats.verdict)
            }
        }
    }

    /// Colored chip badge for a verdict string.
    ///
    /// Colors per 06-UI-SPEC.md:
    ///   clean      → green background,  white text
    ///   acceptable → yellow background, black text (contrast)
    ///   fail       → red background,    white text
    @ViewBuilder
    private func verdictChip(verdict: String) -> some View {
        let bg: Color = verdict == "clean" ? .green : verdict == "fail" ? .red : .yellow
        let fg: Color = verdict == "acceptable" ? .black : .white
        Text(verdict)
            .font(.caption)
            .padding(.horizontal, 8)
            .padding(.vertical, 2)
            .background(bg)
            .foregroundColor(fg)
            .cornerRadius(4)
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
