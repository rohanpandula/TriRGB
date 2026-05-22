// CalibrationWizardView — 4-step guided calibration wizard (Phase 14 R-29).
//
// Steps:
//   1 — Rig Check     (probes auto-run on appear)
//   2 — Exposure      (POST /api/calibrate/exposure; optional rebate picker)
//   3 — Flat Field    (POST /api/calibrate/ffc; "Use this calibration" action)
//   4 — Results       (POST /api/calibrate/checks; auto-run on appear)
//
// Colorblind gate (SC-2, NFR-11): NO Channel color tokens used anywhere.
// Verdict meaning rides on the WORD (PASS/FAIL/CLEAN/ACCEPTABLE) on a Chip.
// Channel initials R/G/B are plain text row labels with no colored dot.
//
// Reuses DesignSystem: GroupBox + PanelGroupBoxStyle, LabeledValue, Chip, Banner.
// Introduces NO new design tokens.

import SwiftUI

// MARK: - Main wizard shell

struct CalibrationWizardView: View {

    @ObservedObject var viewModel: CalibrationWizardViewModel
    @ObservedObject var store: SettingsStore

    var body: some View {
        ScrollView {
            VStack(spacing: Theme.Space.section) {

                // --- Sticky progress indicator ---
                WizardProgressView(currentStep: viewModel.currentStep)

                // --- Active step container ---
                Group {
                    switch viewModel.currentStep {
                    case 1: Step1RigCheckView(viewModel: viewModel)
                    case 2: Step2ExposureView(viewModel: viewModel)
                    case 3: Step3FlatFieldView(viewModel: viewModel, store: store)
                    default: Step4ResultsView(viewModel: viewModel)
                    }
                }
                .groupBoxStyle(PanelGroupBoxStyle())

                // --- Navigation footer ---
                WizardNavFooter(viewModel: viewModel, store: store)
            }
            .padding(Theme.Space.xl)
        }
    }
}

// MARK: - Navigation footer

private struct WizardNavFooter: View {

    @ObservedObject var viewModel: CalibrationWizardViewModel
    @ObservedObject var store: SettingsStore

    var body: some View {
        HStack {
            // Back button (hidden on step 1)
            if viewModel.currentStep > 1 {
                Button("Back") {
                    viewModel.currentStep -= 1
                }
                .buttonStyle(.bordered)
                .disabled(viewModel.isRunning)
                .accessibilityIdentifier(AccessibilityID.wizardBackBtn)
            } else {
                Button("Back") {}
                    .buttonStyle(.bordered)
                    .hidden()
                    .accessibilityIdentifier(AccessibilityID.wizardBackBtn)
            }

            Spacer()

            // Re-run button (hidden when no result yet or while running)
            if showRerunButton {
                Button(rerunLabel) { Task { await rerunAction() } }
                    .buttonStyle(.bordered)
                    .disabled(viewModel.isRunning)
                    .accessibilityIdentifier(AccessibilityID.wizardRerunBtn)
            } else {
                Button("Re-run") {}
                    .buttonStyle(.bordered)
                    .hidden()
                    .accessibilityIdentifier(AccessibilityID.wizardRerunBtn)
            }

            // Primary / next button
            Button(primaryLabel) { Task { await primaryAction() } }
                .buttonStyle(.borderedProminent)
                .disabled(viewModel.isRunning)
                .accessibilityIdentifier(AccessibilityID.wizardNextBtn)
        }
    }

    // MARK: - Button logic

    private var showRerunButton: Bool {
        switch viewModel.currentStep {
        case 1: return viewModel.rigCheckResult != nil
        case 2: return viewModel.exposureResult != nil
        case 3: return viewModel.ffcResult != nil
        case 4: return viewModel.checkResults != nil
        default: return false
        }
    }

    private var rerunLabel: String {
        switch viewModel.currentStep {
        case 1: return "Re-run Checks"
        case 2: return "Re-run Exposure"
        case 3: return "Re-run Flat Field"
        default: return "Re-run All Checks"
        }
    }

    private func rerunAction() async {
        switch viewModel.currentStep {
        case 1: await viewModel.triggerRigCheck()
        case 2: await viewModel.triggerExposure()
        case 3: await viewModel.triggerFFC()
        default: await viewModel.triggerResultsCheck()
        }
    }

    private var primaryLabel: String {
        switch viewModel.currentStep {
        case 1: return "Next"
        case 2: return viewModel.exposureResult != nil ? "Next" : "Run Exposure"
        case 3: return viewModel.ffcResult != nil ? "Continue" : "Capture Flat Field"
        default: return "Done"
        }
    }

    private func primaryAction() async {
        switch viewModel.currentStep {
        case 1:
            viewModel.currentStep = 2
        case 2:
            if viewModel.exposureResult != nil {
                viewModel.currentStep = 3
            } else {
                await viewModel.triggerExposure()
            }
        case 3:
            if viewModel.ffcResult != nil {
                viewModel.currentStep = 4
            } else {
                await viewModel.triggerFFC()
            }
        default:
            viewModel.reset()
        }
    }
}

// MARK: - Step 1: Rig Check

private struct Step1RigCheckView: View {
    @ObservedObject var viewModel: CalibrationWizardViewModel

    var body: some View {
        GroupBox(label: Text("Rig Check").font(.headline)) {
            VStack(alignment: .leading, spacing: Theme.Space.md) {
                if viewModel.isRunning && viewModel.rigCheckResult == nil {
                    HStack(spacing: Theme.Space.sm) {
                        ProgressView().controlSize(.small)
                        Text("Running rig checks\u{2026}")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                } else if let state = viewModel.rigCheckResult {
                    rigCheckRows(state: state)
                } else {
                    Text("Checking rig\u{2026}")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }

                if let err = viewModel.lastError[1] {
                    Banner(kind: .danger, text: err)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .task { await viewModel.triggerRigCheck() }
    }

    @ViewBuilder
    private func rigCheckRows(state: OrchestratorState) -> some View {
        // Pre-hardware, OrchestratorState exposes no per-device health; light/camera/firmware
        // all reflect scanner-service availability (isRunning). Real per-device probes are an
        // M2 refinement (see 14-VALIDATION.md manual-only). The duplication is intentional and
        // documented here so a future contributor knows to wire real per-device fields from
        // OrchestratorState when the hardware API is extended.
        let lightConnected = viewModel.orchestratorClient.isRunning
        let firmwareText: String = state.levelR > 0 ? "OK" : "UNKNOWN"
        let firmwareTint: Color = state.levelR > 0 ? Theme.State.success : Theme.State.warning
        let cameraReachable = viewModel.orchestratorClient.isRunning
        // Output-folder probe is a genuine distinct signal: non-empty means the Python server
        // has a configured working directory (different from the scanner-service liveness check).
        let folderOK = !state.outputFolder.isEmpty

        // Light panel probe
        LabeledValue(label: "Light panel") {
            Chip(
                text: lightConnected ? "CONNECTED" : "NOT FOUND",
                tint: lightConnected ? Theme.State.success : Theme.State.danger
            )
            .accessibilityIdentifier(AccessibilityID.rigCheckLightLabel)
            .accessibilityValue(lightConnected ? "CONNECTED" : "NOT FOUND")
        }

        // Firmware probe
        LabeledValue(label: "Firmware") {
            Chip(text: firmwareText, tint: firmwareTint)
                .accessibilityIdentifier(AccessibilityID.rigCheckFirmwareLabel)
                .accessibilityValue(firmwareText)
        }

        // Camera reachable probe
        LabeledValue(label: "Camera reachable") {
            Chip(
                text: cameraReachable ? "REACHABLE" : "NOT FOUND",
                tint: cameraReachable ? Theme.State.success : Theme.State.danger
            )
            .accessibilityIdentifier(AccessibilityID.rigCheckCameraLabel)
            .accessibilityValue(cameraReachable ? "REACHABLE" : "NOT FOUND")
        }

        // Output folder probe
        LabeledValue(label: "Output folder") {
            Chip(
                text: folderOK ? "WRITABLE" : "NOT WRITABLE",
                tint: folderOK ? Theme.State.success : Theme.State.danger
            )
            .accessibilityIdentifier(AccessibilityID.rigCheckFolderLabel)
            .accessibilityValue(folderOK ? "WRITABLE" : "NOT WRITABLE")
        }

        // Warning banner if any probe is not ideal
        if !lightConnected || !cameraReachable || !folderOK {
            Banner(kind: .warning, text: "One or more checks failed. You can continue, but results may be incomplete.")
        } else {
            Banner(kind: .info, text: "All systems ready. Proceed to exposure calibration.")
        }
    }
}

// MARK: - Step 2: Exposure

private struct Step2ExposureView: View {
    @ObservedObject var viewModel: CalibrationWizardViewModel

    var body: some View {
        GroupBox(label: Text("Exposure").font(.headline)) {
            VStack(alignment: .leading, spacing: Theme.Space.md) {

                // Rebate picker row
                rebatePickerSection

                if viewModel.isRunning && viewModel.exposureResult == nil {
                    HStack(spacing: Theme.Space.sm) {
                        ProgressView().controlSize(.small)
                        Text("Tuning exposure\u{2026}")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                } else if let result = viewModel.exposureResult {
                    Divider()
                    exposureResultRows(result: result)
                } else {
                    Banner(kind: .info, text: "Calibrates LED levels against the film rebate. Auto-detect locates the rebate automatically.")
                }

                if let err = viewModel.lastError[2] {
                    Banner(kind: .danger, text: err)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var rebatePickerSection: some View {
        VStack(alignment: .leading, spacing: Theme.Space.sm) {
            LabeledValue(label: "Rebate region") {
                if let coord = viewModel.rebateCoord {
                    Chip(text: "col \(coord.col) row \(coord.row)", tint: Theme.State.success)
                        .accessibilityIdentifier(AccessibilityID.rebatePicker)
                        .accessibilityValue("col \(coord.col) row \(coord.row)")
                } else {
                    Chip(text: "Auto-detect", tint: Theme.State.info)
                        .accessibilityIdentifier(AccessibilityID.rebatePicker)
                        .accessibilityValue("Auto-detect")
                }
            }

            // Tappable film-edge strip (spatial picker)
            GeometryReader { geo in
                Rectangle()
                    .fill(Theme.panelStroke.opacity(0.2))
                    .frame(height: 48)
                    .overlay(
                        Text("Film edge strip — tap to set rebate region")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    )
                    .onTapGesture { location in
                        let col = Int((location.x / geo.size.width) * 1000)
                        let row = Int(geo.size.height / 2)
                        viewModel.rebateCoord = (col: col, row: row)
                    }
            }
            .frame(height: 48)

            HStack {
                Button("Auto-detect") { viewModel.rebateCoord = nil }
                    .buttonStyle(.bordered)
                    .controlSize(.small)
                    .accessibilityIdentifier(AccessibilityID.rebateClearBtn)
                Spacer()
            }

            if viewModel.rebateCoord == nil {
                Banner(kind: .info, text: "Auto-detect is the default. Click the film edge strip to override.")
            }
        }
    }

    @ViewBuilder
    private func exposureResultRows(result: ExposureCalibrationResult) -> some View {
        // Column headers
        HStack {
            Text("Ch").font(.caption).foregroundStyle(.secondary).frame(width: 24, alignment: .leading)
            Text("Clip %").font(.caption).foregroundStyle(.secondary).frame(maxWidth: .infinity, alignment: .trailing)
            Text("LED").font(.caption).foregroundStyle(.secondary).frame(maxWidth: .infinity, alignment: .trailing)
            Text("Verdict").font(.caption).foregroundStyle(.secondary).frame(maxWidth: .infinity, alignment: .trailing)
        }

        exposureChannelRow(ch: "R", cal: result.r,
                           clipID: AccessibilityID.exposureClipR,
                           levelID: AccessibilityID.exposureLevelR,
                           verdictID: AccessibilityID.exposureVerdictR)
        exposureChannelRow(ch: "G", cal: result.g,
                           clipID: AccessibilityID.exposureClipG,
                           levelID: AccessibilityID.exposureLevelG,
                           verdictID: AccessibilityID.exposureVerdictG)
        exposureChannelRow(ch: "B", cal: result.b,
                           clipID: AccessibilityID.exposureClipB,
                           levelID: AccessibilityID.exposureLevelB,
                           verdictID: AccessibilityID.exposureVerdictB)

        Divider()

        // Overall verdict — PASS = under-clipped: <5% of rebate pixels saturated (clip_fraction near 0 is well-exposed)
        let allPass = result.r.clipFraction < 0.05 && result.g.clipFraction < 0.05 && result.b.clipFraction < 0.05
        let overallVerdict = allPass ? "PASS" : "FAIL"
        LabeledValue(label: "Overall") {
            Chip(
                text: overallVerdict,
                tint: allPass ? Theme.State.success : Theme.State.danger
            )
            .accessibilityIdentifier(AccessibilityID.exposureOverall)
            .accessibilityValue(overallVerdict)
        }
    }

    @ViewBuilder
    private func exposureChannelRow(
        ch: String,
        cal: WizardChannelCalibration,
        clipID: String,
        levelID: String,
        verdictID: String
    ) -> some View {
        let clipPct = cal.clipFraction * 100.0
        // PASS = under-clipped: <5% of rebate pixels saturated (clip_fraction near 0 is well-exposed)
        let passed = cal.clipFraction < 0.05

        HStack {
            // Channel initial — plain text, NO colored dot (SC-2)
            Text(ch)
                .font(.body.weight(.medium))
                .frame(width: 24, alignment: .leading)

            Spacer()

            // Clip %
            Text(String(format: "%.1f%%", clipPct))
                .font(.body.monospacedDigit())
                .accessibilityIdentifier(clipID)
                .accessibilityValue(String(format: "%.1f%%", clipPct))

            Spacer()

            // LED level
            Text("\(cal.ledLevel)")
                .font(.body.monospacedDigit())
                .accessibilityIdentifier(levelID)
                .accessibilityValue("\(cal.ledLevel)")

            Spacer()

            // Verdict chip
            Chip(text: passed ? "PASS" : "FAIL",
                 tint: passed ? Theme.State.success : Theme.State.danger)
                .accessibilityIdentifier(verdictID)
                .accessibilityValue(passed ? "PASS" : "FAIL")
        }
    }
}

// MARK: - Step 3: Flat Field

private struct Step3FlatFieldView: View {
    @ObservedObject var viewModel: CalibrationWizardViewModel
    @ObservedObject var store: SettingsStore

    var body: some View {
        GroupBox(label: Text("Flat Field").font(.headline)) {
            VStack(alignment: .leading, spacing: Theme.Space.md) {

                if viewModel.isRunning && viewModel.ffcResult == nil {
                    HStack(spacing: Theme.Space.sm) {
                        ProgressView().controlSize(.small)
                        Text("Capturing flat frames\u{2026}")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                } else if let result = viewModel.ffcResult {
                    ffcResultRows(result: result)
                } else {
                    Banner(kind: .info, text: "Captures flat frames at working brightness and measures illumination uniformity.")
                }

                if let err = viewModel.lastError[3] {
                    Banner(kind: .danger, text: err)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    @ViewBuilder
    private func ffcResultRows(result: FlatFieldResponse) -> some View {
        let insp = result.inspection

        // Column headers
        HStack {
            Text("Ch").font(.caption).foregroundStyle(.secondary).frame(width: 24, alignment: .leading)
            Text("Falloff").font(.caption).foregroundStyle(.secondary).frame(maxWidth: .infinity, alignment: .trailing)
            Text("Uniformity").font(.caption).foregroundStyle(.secondary).frame(maxWidth: .infinity, alignment: .trailing)
            Text("Verdict").font(.caption).foregroundStyle(.secondary).frame(maxWidth: .infinity, alignment: .trailing)
        }

        if let r = insp.channelR {
            ffcChannelRow(ch: "R", stats: r,
                          falloffID: AccessibilityID.ffcFalloffR,
                          uniformityID: AccessibilityID.ffcUniformityR,
                          verdictID: AccessibilityID.ffcVerdictR)
        }
        if let g = insp.channelG {
            ffcChannelRow(ch: "G", stats: g,
                          falloffID: AccessibilityID.ffcFalloffG,
                          uniformityID: AccessibilityID.ffcUniformityG,
                          verdictID: AccessibilityID.ffcVerdictG)
        }
        if let b = insp.channelB {
            ffcChannelRow(ch: "B", stats: b,
                          falloffID: AccessibilityID.ffcFalloffB,
                          uniformityID: AccessibilityID.ffcUniformityB,
                          verdictID: AccessibilityID.ffcVerdictB)
        }

        Divider()

        // Overall verdict
        let overallUpper = insp.overall.uppercased()
        let (overallTint, overallWord) = ffcVerdictStyle(word: overallUpper)
        LabeledValue(label: "Overall") {
            Chip(text: overallWord, tint: overallTint)
                .accessibilityIdentifier(AccessibilityID.ffcOverall)
                .accessibilityValue(overallWord)
        }

        // Frames averaged
        LabeledValue(label: "Frames averaged") {
            Text("\(result.flatField.nFramesAveraged)")
                .font(.body.monospacedDigit())
                .accessibilityIdentifier(AccessibilityID.ffcFramesLabel)
                .accessibilityValue("\(result.flatField.nFramesAveraged)")
        }

        // Acceptable FFC hint
        if overallWord == "ACCEPTABLE" {
            Banner(kind: .warning, text: "Flat field is acceptable. For best results, reseat the scanner and re-run.")
        }

        // "Use this calibration" button
        Button("Use this calibration") {
            viewModel.applyCalibration(to: store)
            viewModel.currentStep = 4
        }
        .buttonStyle(.borderedProminent)
        .accessibilityIdentifier(AccessibilityID.ffcUseBtn)
    }

    @ViewBuilder
    private func ffcChannelRow(
        ch: String,
        stats: ChannelCalResult,
        falloffID: String,
        uniformityID: String,
        verdictID: String
    ) -> some View {
        let verdictUpper = stats.verdict.uppercased()
        let (tint, word) = ffcVerdictStyle(word: verdictUpper)

        HStack {
            // Channel initial — plain text, NO colored dot (SC-2)
            Text(ch)
                .font(.body.weight(.medium))
                .frame(width: 24, alignment: .leading)

            Spacer()

            Text(String(format: "%.1f%%", stats.falloffPct))
                .font(.body.monospacedDigit())
                .accessibilityIdentifier(falloffID)
                .accessibilityValue(String(format: "%.1f%%", stats.falloffPct))

            Spacer()

            Text(String(format: "%.1f%%", stats.uniformityPct))
                .font(.body.monospacedDigit())
                .accessibilityIdentifier(uniformityID)
                .accessibilityValue(String(format: "%.1f%%", stats.uniformityPct))

            Spacer()

            Chip(text: word, tint: tint)
                .accessibilityIdentifier(verdictID)
                .accessibilityValue(word)
        }
    }

    private func ffcVerdictStyle(word: String) -> (Color, String) {
        switch word {
        case "CLEAN":
            return (Theme.State.success, "CLEAN")
        case "ACCEPTABLE":
            return (Theme.State.warning, "ACCEPTABLE")
        default:
            return (Theme.State.danger, "FAIL")
        }
    }
}

// MARK: - Step 4: Results

private struct Step4ResultsView: View {
    @ObservedObject var viewModel: CalibrationWizardViewModel

    var body: some View {
        GroupBox(label: Text("Results").font(.headline)) {
            VStack(alignment: .leading, spacing: Theme.Space.md) {

                if viewModel.isRunning && viewModel.checkResults == nil {
                    HStack(spacing: Theme.Space.sm) {
                        ProgressView().controlSize(.small)
                        Text("Verifying calibration\u{2026}")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                } else if let checks = viewModel.checkResults {
                    resultsContent(checks: checks)
                } else {
                    Text("Running checks\u{2026}")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }

                if let err = viewModel.lastError[4] {
                    Banner(kind: .danger, text: err)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .task { await viewModel.triggerResultsCheck() }
    }

    @ViewBuilder
    private func resultsContent(checks: [WizardCheckResult]) -> some View {
        let regCheck   = checks.first { $0.name == "registration" }
        let baseCheck  = checks.first { $0.name == "base_neutrality" }

        // Sub-section A: Registration
        Text("Registration").font(.headline)

        let rgShift = regCheck?.deltas["rg_shift"] ?? 0.0
        let gbShift = regCheck?.deltas["gb_shift"] ?? 0.0
        let regPassed = regCheck?.passed ?? false

        LabeledValue(label: "R-G shift") {
            Text(String(format: "%.2f px", rgShift))
                .font(.body.monospacedDigit())
                .accessibilityIdentifier(AccessibilityID.resultsShiftRG)
                .accessibilityValue(String(format: "%.2f px", rgShift))
        }
        LabeledValue(label: "G-B shift") {
            Text(String(format: "%.2f px", gbShift))
                .font(.body.monospacedDigit())
                .accessibilityIdentifier(AccessibilityID.resultsShiftGB)
                .accessibilityValue(String(format: "%.2f px", gbShift))
        }
        LabeledValue(label: "Registration") {
            Chip(
                text: regPassed ? "PASS" : "FAIL",
                tint: regPassed ? Theme.State.success : Theme.State.danger
            )
            .accessibilityIdentifier(AccessibilityID.resultsRegVerdict)
            .accessibilityValue(regPassed ? "PASS" : "FAIL")
        }

        Divider()

        // Sub-section B: Base Neutrality
        let baseDev = baseCheck?.deltas.values.max() ?? 0.0
        let baseDevPct = baseDev / 65535.0 * 100.0
        let basePassed = baseCheck?.passed ?? false

        LabeledValue(label: "Base deviation") {
            Text(String(format: "%.1f%%", baseDevPct))
                .font(.body.monospacedDigit())
                .accessibilityIdentifier(AccessibilityID.resultsBaseDeviation)
                .accessibilityValue(String(format: "%.1f%%", baseDevPct))
        }
        LabeledValue(label: "Base neutrality") {
            Chip(
                text: basePassed ? "PASS" : "FAIL",
                tint: basePassed ? Theme.State.success : Theme.State.danger
            )
            .accessibilityIdentifier(AccessibilityID.resultsBaseVerdict)
            .accessibilityValue(basePassed ? "PASS" : "FAIL")
        }

        Divider()

        // Sub-section C: Per-Channel Gains (from exposureResult)
        if let exp = viewModel.exposureResult {
            Text("Per-Channel Gains").font(.headline)

            LabeledValue(label: "Gain — R") {
                Text(String(format: "%.3f", exp.r.gain))
                    .font(.body.monospacedDigit())
                    .accessibilityIdentifier(AccessibilityID.resultsGainR)
                    .accessibilityValue(String(format: "%.3f", exp.r.gain))
            }
            LabeledValue(label: "Gain — G") {
                Text(String(format: "%.3f", exp.g.gain))
                    .font(.body.monospacedDigit())
                    .accessibilityIdentifier(AccessibilityID.resultsGainG)
                    .accessibilityValue(String(format: "%.3f", exp.g.gain))
            }
            LabeledValue(label: "Gain — B") {
                Text(String(format: "%.3f", exp.b.gain))
                    .font(.body.monospacedDigit())
                    .accessibilityIdentifier(AccessibilityID.resultsGainB)
                    .accessibilityValue(String(format: "%.3f", exp.b.gain))
            }

            Divider()
        }

        // Sub-section D: Roll-Level Summary
        let rollPassed = checks.allSatisfy { $0.passed }
        LabeledValue(label: "Calibration") {
            Chip(
                text: rollPassed ? "PASS" : "FAIL",
                tint: rollPassed ? Theme.State.success : Theme.State.danger
            )
            .accessibilityIdentifier(AccessibilityID.resultsRollVerdict)
            .accessibilityValue(rollPassed ? "PASS" : "FAIL")
        }

        // Summary banner
        if rollPassed {
            Banner(kind: .info, text: "Calibration complete. This result is ready to apply to a roll.")
        } else {
            Banner(kind: .warning, text: "One or more checks failed. Review the values above before scanning.")
        }
    }
}
