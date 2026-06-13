// CalibrationWizardView — 4-step guided calibration wizard (Phase 14 R-29).
//
// Steps:
//   1 — Rig Check     (starts backend only when the operator runs the check)
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

import AppKit
import SwiftUI

// MARK: - Main wizard shell

struct CalibrationWizardView: View {

    @ObservedObject var viewModel: CalibrationWizardViewModel
    @ObservedObject var store: SettingsStore
    @ObservedObject var coordinator: ScanCoordinator
    @ObservedObject var cameraConnection: SonyCameraConnection

    var body: some View {
        ScrollView {
            VStack(spacing: Theme.Space.section) {

                if coordinator.phase == .calibrating {
                    Banner(kind: .info, text: "Calibration owns the Scanlight USB port; manual light controls stay locked until you stop calibration.")
                } else if !coordinator.lastError.isEmpty {
                    Banner(kind: .warning, text: coordinator.lastError)
                }

                // --- Sticky progress indicator ---
                WizardProgressView(currentStep: viewModel.currentStep)

                // --- Active step container ---
                Group {
                    switch viewModel.currentStep {
                    case 1:
                        Step1RigCheckView(
                            viewModel: viewModel,
                            store: store,
                            coordinator: coordinator,
                            cameraConnection: cameraConnection
                        )
                    case 2: Step2ExposureView(viewModel: viewModel, store: store)
                    case 3: Step3FlatFieldView(viewModel: viewModel, store: store)
                    default: Step4ResultsView(viewModel: viewModel)
                    }
                }
                .groupBoxStyle(PanelGroupBoxStyle())

                // --- Navigation footer ---
                WizardNavFooter(
                    viewModel: viewModel,
                    store: store,
                    coordinator: coordinator,
                    cameraConnection: cameraConnection
                )
            }
            .padding(Theme.Space.xl)
        }
    }
}

// MARK: - Navigation footer

private struct WizardNavFooter: View {

    @ObservedObject var viewModel: CalibrationWizardViewModel
    @ObservedObject var store: SettingsStore
    @ObservedObject var coordinator: ScanCoordinator
    @ObservedObject var cameraConnection: SonyCameraConnection

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

            if coordinator.phase == .calibrating {
                Button("Stop Calibration") {
                    Task { await coordinator.stopCalibration() }
                }
                .buttonStyle(.bordered)
                .disabled(coordinator.transitionInFlight)
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
                .disabled(viewModel.isRunning || primaryDisabled)
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
        case 1:
            await viewModel.prepareForCalibration(
                store: store,
                coordinator: coordinator,
                cameraConnection: cameraConnection
            )
        case 2:
            guard await ensureCalibrationBackend() else { return }
            await viewModel.triggerExposure(seed: exposureSeed, targetFraction: calibrationTargetFraction)
            viewModel.applyExposure(to: store)
        case 3:
            guard await ensureCalibrationBackend() else { return }
            await viewModel.triggerFFC()
        default:
            guard await ensureCalibrationBackend() else { return }
            await viewModel.triggerResultsCheck()
        }
    }

    private var primaryLabel: String {
        switch viewModel.currentStep {
        case 1: return viewModel.rigCheckResult != nil ? "Next" : "Start & Check Rig"
        case 2:
            if viewModel.exposureResult != nil { return "Next" }
            if viewModel.rebateRegion == nil { return "Select Region" }
            return "Run Exposure"
        case 3: return viewModel.ffcResult != nil ? "Use + Continue" : "Capture Flat Field"
        default: return "Done"
        }
    }

    private var primaryDisabled: Bool {
        viewModel.currentStep == 2 &&
            viewModel.exposureResult == nil &&
            viewModel.rebateRegion == nil
    }

    private var exposureSeed: ExposureCalibrationResult? {
        if let profile = store.stockCalibrationProfile(id: viewModel.selectedStockProfileID) {
            return profile.exposureResult
        }
        return viewModel.exposureResult
    }

    private var calibrationTargetFraction: Double {
        store.settings.calibrationTargetFraction ?? 0.80
    }

    private func primaryAction() async {
        switch viewModel.currentStep {
        case 1:
            if viewModel.rigCheckResult != nil {
                viewModel.currentStep = 2
            } else if await viewModel.prepareForCalibration(
                store: store,
                coordinator: coordinator,
                cameraConnection: cameraConnection
            ) {
                viewModel.currentStep = 2
            }
        case 2:
            if viewModel.exposureResult != nil {
                viewModel.currentStep = 3
            } else if viewModel.rebateRegion == nil {
                viewModel.lastError[2] = "Select a film-base sample in the preview before running exposure."
            } else {
                guard await ensureCalibrationBackend() else { return }
                await viewModel.triggerExposure(seed: exposureSeed, targetFraction: calibrationTargetFraction)
                viewModel.applyExposure(to: store)
            }
        case 3:
            if viewModel.ffcResult != nil {
                viewModel.applyCalibration(to: store)
                viewModel.currentStep = 4
            } else {
                guard await ensureCalibrationBackend() else { return }
                await viewModel.triggerFFC()
            }
        default:
            viewModel.reset()
        }
    }

    private func ensureCalibrationBackend() async -> Bool {
        if coordinator.phase == .calibrating, viewModel.orchestratorClient.isRunning {
            return true
        }

        if coordinator.phase == .calibrating, !viewModel.orchestratorClient.isRunning {
            await coordinator.stopCalibration()
        }

        if await viewModel.prepareForCalibration(
            store: store,
            coordinator: coordinator,
            cameraConnection: cameraConnection
        ) {
            return true
        }

        let message = viewModel.lastError[1]
            ?? (coordinator.lastError.isEmpty ? nil : coordinator.lastError)
            ?? "Calibration backend is not running. Run the rig check again before continuing."
        viewModel.lastError[viewModel.currentStep] = message
        return false
    }
}

// MARK: - Step 1: Rig Check

private struct Step1RigCheckView: View {
    @ObservedObject var viewModel: CalibrationWizardViewModel
    @ObservedObject var store: SettingsStore
    @ObservedObject var coordinator: ScanCoordinator
    @ObservedObject var cameraConnection: SonyCameraConnection

    var body: some View {
        GroupBox(label: Text("Rig Check").font(.headline)) {
            VStack(alignment: .leading, spacing: Theme.Space.md) {
                if viewModel.isRunning && viewModel.currentStep == 1 {
                    HStack(spacing: Theme.Space.sm) {
                        ProgressView().controlSize(.small)
                        Text(viewModel.rigCheckProgressText ?? "Starting rig check.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                } else if let state = viewModel.rigCheckResult {
                    rigCheckRows(state: state)
                } else {
                    rigCheckPreflightRows
                }

                if let err = viewModel.lastError[1] {
                    Banner(kind: .danger, text: err)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var rigCheckPreflightRows: some View {
        VStack(alignment: .leading, spacing: Theme.Space.md) {
            Banner(kind: .info, text: "Start & Check Rig starts the calibration backend, hands Scanlight control to Python, and verifies the settings below.")

            LabeledValue(label: "Backend") {
                Chip(text: backendPreflightText, tint: backendPreflightTint)
            }

            LabeledValue(label: "Roll") {
                Text(store.settings.rollName.isEmpty ? "Missing" : store.settings.rollName)
                    .foregroundStyle(store.settings.rollName.isEmpty ? Theme.State.danger : .primary)
            }

            LabeledValue(label: "Output base") {
                Text(store.settings.outputFolder.isEmpty ? "Missing" : store.settings.outputFolder)
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .foregroundStyle(store.settings.outputFolder.isEmpty ? Theme.State.danger : .primary)
            }

            LabeledValue(label: "Trigger") {
                Text(triggerSummary)
            }

            LabeledValue(label: "Scanlight USB") {
                Chip(
                    text: scanlightUSBReady ? "READY" : "NOT FOUND",
                    tint: scanlightUSBReady ? Theme.State.success : Theme.State.danger
                )
            }

            if !scanlightUSBReady {
                Text(scanlightUSBHint)
                    .font(.caption)
                    .foregroundStyle(Theme.State.warning)
                    .fixedSize(horizontal: false, vertical: true)
            } else if coordinator.phase == .calibrating {
                Text("Calibration is using the Scanlight port now. Manual light controls reconnect when you stop calibration.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            if store.settings.triggerMode == "sdk" {
                LabeledValue(label: "Sony credentials") {
                    Chip(
                        text: sonyCredentialsSaved ? "SAVED" : "MISSING",
                        tint: sonyCredentialsSaved ? Theme.State.info : Theme.State.danger
                    )
                }

                LabeledValue(label: "Sony camera") {
                    Chip(text: cameraConnection.chipText, tint: cameraConnection.tint)
                }

                Text(cameraConnection.detailText)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            } else if store.settings.triggerMode == "manual" || store.settings.triggerMode == "hw" {
                LabeledValue(label: "IED inbox") {
                    Text((store.settings.iedInbox ?? "").isEmpty ? "Missing" : store.settings.iedInbox!)
                        .lineLimit(1)
                        .truncationMode(.middle)
                        .foregroundStyle((store.settings.iedInbox ?? "").isEmpty ? Theme.State.danger : .primary)
                }
            }

            Text("After a successful check, this card will show the resolved roll folder and keep the backend running for exposure calibration.")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    @ViewBuilder
    private func rigCheckRows(state: OrchestratorState) -> some View {
        let backendReady = coordinator.phase == .calibrating && viewModel.orchestratorClient.isRunning
        let folderOK = !state.outputFolder.isEmpty
        let triggerText = triggerSummary

        LabeledValue(label: "Backend") {
            Chip(
                text: backendReady ? "RUNNING" : "IDLE",
                tint: backendReady ? Theme.State.success : Theme.State.warning
            )
            .accessibilityIdentifier(AccessibilityID.rigCheckLightLabel)
            .accessibilityValue(backendReady ? "RUNNING" : "IDLE")
        }

        LabeledValue(label: "Roll / frame") {
            Text("\(state.rollName) / \(state.frameNumber)")
                .accessibilityIdentifier(AccessibilityID.rigCheckFirmwareLabel)
                .accessibilityValue("\(state.rollName) / \(state.frameNumber)")
        }

        LabeledValue(label: "Trigger") {
            Text(triggerText)
            .accessibilityIdentifier(AccessibilityID.rigCheckCameraLabel)
            .accessibilityValue(triggerText)
        }

        if store.settings.triggerMode == "sdk" {
            LabeledValue(label: "Sony camera") {
                Chip(text: cameraConnection.chipText, tint: cameraConnection.tint)
            }
            Text(cameraConnection.detailText)
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }

        LabeledValue(label: "Scanlight USB") {
            Chip(
                text: backendReady ? "OWNED BY CALIBRATION" : "NOT CLAIMED",
                tint: backendReady ? Theme.State.success : Theme.State.warning
            )
        }

        LabeledValue(label: "Output folder") {
            Text(folderOK ? state.outputFolder : "Not configured")
                .lineLimit(1)
                .truncationMode(.middle)
            .accessibilityIdentifier(AccessibilityID.rigCheckFolderLabel)
            .accessibilityValue(folderOK ? state.outputFolder : "Not configured")
        }

        if !backendReady || !folderOK {
            Banner(kind: .warning, text: "Backend started, but one or more settings need attention before exposure.")
        } else {
            Banner(kind: .info, text: "Backend is ready. The Exposure step will refresh a live frame for selecting the film-base crop.")
        }
    }

    private var backendPreflightText: String {
        if coordinator.phase == .calibrating && viewModel.orchestratorClient.isRunning {
            return "RUNNING"
        }
        if coordinator.phase == .scanning {
            return "SCAN ACTIVE"
        }
        if coordinator.transitionInFlight {
            return "BUSY"
        }
        return "IDLE"
    }

    private var backendPreflightTint: Color {
        if coordinator.phase == .calibrating && viewModel.orchestratorClient.isRunning {
            return Theme.State.success
        }
        if coordinator.phase == .scanning || coordinator.transitionInFlight {
            return Theme.State.warning
        }
        return Theme.State.idle
    }

    private var sonyCredentialsSaved: Bool {
        // USB connects without IP / Access Auth — it's "ready" on its own.
        // Wi-Fi still needs an IP plus Access Auth user + password.
        if store.settings.usesSonyUSB { return true }
        return !(store.settings.sonyIpAddress ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && !(store.settings.sonyUser ?? "").isEmpty
            && !(store.settings.sonyPassword ?? "").isEmpty
    }

    private var scanlightUSBReady: Bool {
        !selectedScanlightPort.isEmpty || !detectedScanlightPorts.isEmpty
    }

    private var selectedScanlightPort: String {
        coordinator.scanlightPort.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private var detectedScanlightPorts: [String] {
        let entries = (try? FileManager.default.contentsOfDirectory(atPath: "/dev")) ?? []
        return entries
            .filter { $0.hasPrefix("cu.usbmodem") || $0.hasPrefix("cu.usbserial") }
            .sorted()
            .map { "/dev/\($0)" }
    }

    private var scanlightUSBHint: String {
        let visiblePorts = ((try? FileManager.default.contentsOfDirectory(atPath: "/dev")) ?? [])
            .filter { $0.hasPrefix("cu.") }
            .sorted()
            .map { "/dev/\($0)" }
            .joined(separator: ", ")
        let ports = visiblePorts.isEmpty ? "none" : visiblePorts
        return "macOS does not see a Scanlight serial port. Use the Scanlight's left USB-C data port, then confirm a /dev/cu.usbmodem* device appears. Visible ports: \(ports)"
    }

    private var triggerSummary: String {
        switch store.settings.triggerMode {
        case "sdk":
            if store.settings.usesSonyUSB {
                return "SDK USB"
            }
            let ip = store.settings.sonyIpAddress?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            return ip.isEmpty ? "SDK" : "SDK \(ip)"
        case "hw":
            return "Hardware pulse"
        default:
            return "Manual / IED"
        }
    }
}

// MARK: - Step 2: Exposure

private struct Step2ExposureView: View {
    @ObservedObject var viewModel: CalibrationWizardViewModel
    @ObservedObject var store: SettingsStore

    var body: some View {
        GroupBox(label: Text("Exposure").font(.headline)) {
            VStack(alignment: .leading, spacing: Theme.Space.md) {

                // Rebate picker row
                rebatePickerSection

                stockProfileSection

                Text(String(format: "Exposure target: %.0f%% of usable RAW range.", (store.settings.calibrationTargetFraction ?? 0.80) * 100.0))
                    .font(.caption)
                    .foregroundStyle(.secondary)

                if viewModel.isRunning && viewModel.exposureResult == nil {
                    HStack(spacing: Theme.Space.sm) {
                        ProgressView().controlSize(.small)
                        Text(viewModel.exposureProgressText ?? "Capturing dark frame first; RGB probes follow.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                } else if let result = viewModel.exposureResult {
                    Divider()
                    exposureResultSummary(result: result)
                    exposureResultRows(result: result)
                } else {
                    Banner(kind: .info, text: "Select a clean film-base crop in the preview before running exposure calibration.")
                }

                if let err = viewModel.lastError[2] {
                    Banner(kind: .danger, text: err)
                }

                CalibrationLogViewer(entries: viewModel.exposureLogEntries)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var stockProfileSection: some View {
        VStack(alignment: .leading, spacing: Theme.Space.sm) {
            Divider()

            Text("Stock RGB profile")
                .font(.headline)

            Text("Save the solved RGB exposure for this film stock, or load an existing stock profile and capture a fresh flat field before scanning.")
                .font(.caption)
                .foregroundStyle(.secondary)

            HStack(spacing: Theme.Space.sm) {
                TextField("Film stock, e.g. Portra 400", text: $viewModel.calibrationStockName)
                    .textFieldStyle(.roundedBorder)
                    .accessibilityIdentifier(AccessibilityID.stockProfileNameField)

                Button("Save Profile") {
                    viewModel.saveCurrentStockProfile(to: store)
                }
                .buttonStyle(.bordered)
                .disabled(
                    viewModel.exposureResult == nil ||
                    StockCalibrationProfile.normalizedStockName(viewModel.calibrationStockName).isEmpty
                )
                .accessibilityIdentifier(AccessibilityID.stockProfileSaveBtn)
            }

            if !store.stockCalibrationProfiles.isEmpty {
                HStack(spacing: Theme.Space.sm) {
                    Picker("Saved profile", selection: $viewModel.selectedStockProfileID) {
                        Text("Choose saved profile").tag(Optional<UUID>.none)
                        ForEach(store.stockCalibrationProfiles) { profile in
                            Text(profile.displayName).tag(Optional(profile.id))
                        }
                    }
                    .pickerStyle(.menu)
                    .accessibilityIdentifier(AccessibilityID.stockProfilePicker)

                    Button("Apply Profile") {
                        viewModel.applySelectedStockProfile(from: store)
                    }
                    .buttonStyle(.bordered)
                    .disabled(viewModel.selectedStockProfileID == nil)
                    .accessibilityIdentifier(AccessibilityID.stockProfileApplyBtn)
                }
            }

            if let message = viewModel.stockProfileMessage {
                Text(message)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }

    private var rebatePickerSection: some View {
        VStack(alignment: .leading, spacing: Theme.Space.sm) {
            LabeledValue(label: "Measurement region") {
                if let region = viewModel.rebateRegion {
                    Chip(text: "x \(region.x) y \(region.y) \(region.w)x\(region.h)", tint: Theme.State.success)
                        .accessibilityIdentifier(AccessibilityID.rebatePicker)
                        .accessibilityValue("x \(region.x) y \(region.y) width \(region.w) height \(region.h)")
                } else {
                    Chip(text: "No region selected", tint: Theme.State.warning)
                        .accessibilityIdentifier(AccessibilityID.rebatePicker)
                        .accessibilityValue("No region selected")
                }
            }

            Text("Start & Check Rig refreshes this live frame automatically. Click a clean film-base/rebate patch; exposure measures only the highlighted RAW crop.")
                .font(.caption)
                .foregroundStyle(.secondary)

            LiveViewRebatePicker(
                settings: store.settings,
                orchestratorClient: viewModel.orchestratorClient,
                selection: viewModel.rebateRegion,
                autoRefreshID: viewModel.previewRefreshGeneration,
                isCalibrationRunning: viewModel.isRunning
            ) { region in
                viewModel.rebateRegion = region
            }

            HStack {
                Button("Clear Selection") { viewModel.rebateRegion = nil }
                    .buttonStyle(.bordered)
                    .controlSize(.small)
                    .disabled(viewModel.rebateRegion == nil)
                    .accessibilityIdentifier(AccessibilityID.rebateClearBtn)
                Spacer()
            }

            if viewModel.rebateRegion == nil {
                Banner(kind: .warning, text: "Select a measurement region before running exposure calibration.")
            }
        }
    }

    @ViewBuilder
    private func exposureResultSummary(result: ExposureCalibrationResult) -> some View {
        let statuses = [
            result.r.exposureStatus?.lowercased(),
            result.g.exposureStatus?.lowercased(),
            result.b.exposureStatus?.lowercased(),
        ]
        let hasSlowShutter = [result.r, result.g, result.b].contains(where: usesSlowShutter(cal:))

        if statuses.contains("source_limited") {
            Banner(kind: .warning, text: "One or more channels hit source RAW clipping before the exposure target; the app kept the brightest clean setting.")
        } else if statuses.contains("clip_limited") {
            Banner(kind: .warning, text: "One or more channels hit a clean limit: the next brighter exposure clipped, so the app kept the brightest clean setting.")
        } else if hasSlowShutter {
            Banner(kind: .warning, text: "One or more channels needed slower than 1/8. That is usable on a rigid rig; re-check only if frames show vibration.")
        } else if statuses.contains("under") {
            Banner(kind: .warning, text: "One or more channels are light-limited: max LED could not reach the target without changing the light path or shutter range.")
        } else if statuses.contains("hot") {
            Banner(kind: .warning, text: "One or more channels are above target. Re-run exposure before scanning.")
        }
    }

    @ViewBuilder
    private func exposureResultRows(result: ExposureCalibrationResult) -> some View {
        // Column headers
        HStack {
            Text("Ch").font(.caption).foregroundStyle(.secondary).frame(width: 24, alignment: .leading)
            Text("Signal").font(.caption).foregroundStyle(.secondary).frame(maxWidth: .infinity, alignment: .trailing)
            Text("LED").font(.caption).foregroundStyle(.secondary).frame(maxWidth: .infinity, alignment: .trailing)
            Text("Shutter").font(.caption).foregroundStyle(.secondary).frame(maxWidth: .infinity, alignment: .trailing)
            Text("Status").font(.caption).foregroundStyle(.secondary).frame(maxWidth: .infinity, alignment: .trailing)
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

        let assessments = [result.r, result.g, result.b].map(exposureAssessment(cal:))
        let overallVerdict = overallExposureStatus(assessments)
        LabeledValue(label: "Overall") {
            Chip(
                text: overallVerdict.label,
                tint: overallVerdict.tint
            )
            .accessibilityIdentifier(AccessibilityID.exposureOverall)
            .accessibilityValue(overallVerdict.label)
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
        let assessment = exposureAssessment(cal: cal)

        HStack {
            // Channel initial — plain text, NO colored dot (SC-2)
            Text(ch)
                .font(.body.weight(.medium))
                .frame(width: 24, alignment: .leading)

            Spacer()

            Text(signalText(cal: cal))
                .font(.body.monospacedDigit())
                .accessibilityIdentifier(clipID)
                .accessibilityValue(signalText(cal: cal))

            Spacer()

            // LED level
            Text("\(cal.ledLevel)")
                .font(.body.monospacedDigit())
                .accessibilityIdentifier(levelID)
                .accessibilityValue("\(cal.ledLevel)")

            Spacer()

            Text((cal.shutterSpeed?.isEmpty == false) ? cal.shutterSpeed! : "Current")
                .font(.body.monospacedDigit())

            Spacer()

            Chip(text: assessment.label, tint: assessment.tint)
                .accessibilityIdentifier(verdictID)
                .accessibilityValue(assessment.label)
        }
    }

    private func signalText(cal: WizardChannelCalibration) -> String {
        guard let p99 = cal.p99, let target = cal.target, target > 0 else {
            return String(format: "%.1f%% clip", cal.clipFraction * 100.0)
        }
        return String(format: "%.0f%%", (p99 / target) * 100.0)
    }

    private func exposureAssessment(cal: WizardChannelCalibration) -> (label: String, tint: Color) {
        if cal.clipFraction > 0.005 {
            return ("CLIPPED", Theme.State.danger)
        }

        switch cal.exposureStatus?.lowercased() {
        case "under":
            return ("UNDER", Theme.State.warning)
        case "target":
            if usesSlowShutter(cal: cal) {
                return ("SLOW", Theme.State.warning)
            }
            return ("TARGET", Theme.State.success)
        case "hot":
            return ("HOT", Theme.State.warning)
        case "clipped":
            return ("CLIPPED", Theme.State.danger)
        case "source_limited":
            return ("RAW LIMIT", Theme.State.warning)
        case "clip_limited":
            return ("CLEAN MAX", Theme.State.warning)
        default:
            guard let p99 = cal.p99, let target = cal.target, target > 0 else {
                return cal.clipFraction < 0.05
                    ? ("TARGET", Theme.State.success)
                    : ("CLIPPED", Theme.State.danger)
            }
            let ratio = p99 / target
            if ratio < 0.99 { return ("UNDER", Theme.State.warning) }
            if ratio > 1.01 { return ("HOT", Theme.State.warning) }
            if usesSlowShutter(cal: cal) {
                return ("SLOW", Theme.State.warning)
            }
            return ("TARGET", Theme.State.success)
        }
    }

    private func usesSlowShutter(cal: WizardChannelCalibration) -> Bool {
        guard let seconds = shutterSeconds(cal.shutterSpeed) else { return false }
        return seconds > (1.0 / 8.0)
    }

    private func shutterSeconds(_ label: String?) -> Double? {
        guard var text = label?.trimmingCharacters(in: .whitespacesAndNewlines),
              !text.isEmpty else { return nil }
        text = text
            .replacingOccurrences(of: "sec", with: "")
            .replacingOccurrences(of: "s", with: "")
            .replacingOccurrences(of: "\"", with: "")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        if text.contains("/") {
            let parts = text.split(separator: "/", maxSplits: 1).compactMap { Double($0) }
            guard parts.count == 2, parts[1] != 0 else { return nil }
            return parts[0] / parts[1]
        }
        return Double(text)
    }

    private func overallExposureStatus(
        _ assessments: [(label: String, tint: Color)]
    ) -> (label: String, tint: Color) {
        let labels = assessments.map(\.label)
        if labels.contains("CLIPPED") {
            return ("CLIPPED", Theme.State.danger)
        }
        if labels.contains("HOT") {
            return ("HOT", Theme.State.warning)
        }
        if labels.contains("RAW LIMIT") {
            return ("RAW LIMIT", Theme.State.warning)
        }
        if labels.contains("CLEAN MAX") {
            return ("CLEAN MAX", Theme.State.warning)
        }
        if labels.contains("SLOW") {
            return ("SLOW", Theme.State.warning)
        }
        if labels.contains("UNDER") {
            return ("UNDER", Theme.State.warning)
        }
        return ("TARGET", Theme.State.success)
    }
}

private struct CalibrationLogViewer: View {
    let entries: [CalibrationProgressLogEntry]

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Space.sm) {
            Divider()

            HStack {
                Text("Calibration log")
                    .font(.headline)
                Spacer()
                Chip(text: entries.isEmpty ? "IDLE" : "\(entries.count) EVENTS", tint: entries.isEmpty ? Theme.State.idle : Theme.State.info)
            }

            if entries.isEmpty {
                Text("Run exposure to see each light, shutter, capture, and measurement attempt here.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } else {
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: Theme.Space.xs) {
                        ForEach(Array(entries.suffix(32))) { entry in
                            logRow(entry)
                        }
                    }
                    .padding(Theme.Space.sm)
                }
                .frame(maxHeight: 180)
                .background(Color.black.opacity(0.16), in: RoundedRectangle(cornerRadius: Theme.Radius.control, style: .continuous))
                .overlay(
                    RoundedRectangle(cornerRadius: Theme.Radius.control, style: .continuous)
                        .stroke(Color.white.opacity(0.08), lineWidth: 1)
                )
            }
        }
    }

    private func logRow(_ entry: CalibrationProgressLogEntry) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: Theme.Space.sm) {
            Text(timeText(entry.ts))
                .font(.caption.monospacedDigit())
                .foregroundStyle(.secondary)
                .frame(width: 72, alignment: .leading)

            Text(entry.message)
                .font(.caption)
                .foregroundStyle(logTint(entry))
                .fixedSize(horizontal: false, vertical: true)

            Spacer(minLength: 0)
        }
    }

    private func logTint(_ entry: CalibrationProgressLogEntry) -> Color {
        let event = entry.event.lowercased()
        let status = entry.exposureStatus?.lowercased() ?? ""
        if event.contains("fail") || event.contains("abort") || status == "clipped" {
            return Theme.State.danger
        }
        if status == "hot" || status == "under" || status == "clip_limited" || status == "source_limited" {
            return Theme.State.warning
        }
        if entry.converged == true || event.contains("complete") || event.contains("ok") {
            return Theme.State.success
        }
        return .secondary
    }

    private func timeText(_ raw: String?) -> String {
        guard let raw, !raw.isEmpty else { return "--:--:--" }
        let isoWithFraction = ISO8601DateFormatter()
        isoWithFraction.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let isoPlain = ISO8601DateFormatter()
        isoPlain.formatOptions = [.withInternetDateTime]

        guard let date = isoWithFraction.date(from: raw) ?? isoPlain.date(from: raw) else {
            return String(raw.prefix(8))
        }

        let formatter = DateFormatter()
        formatter.dateFormat = "HH:mm:ss"
        return formatter.string(from: date)
    }
}

private struct LiveViewRebatePicker: View {
    let settings: ScanSettings
    @ObservedObject var orchestratorClient: OrchestratorClient
    let selection: RebateRegion?
    let autoRefreshID: Int
    let isCalibrationRunning: Bool
    let onPick: (RebateRegion) -> Void

    @State private var previewImage: NSImage?
    @State private var statusText = "Live frame not loaded."
    @State private var statusTint = Theme.State.idle
    @State private var isRefreshing = false
    @State private var invertPreview = true
    @State private var mirrorPreview = false
    @State private var rotationDegrees = 0
    @State private var previewZoom: Double = 1.0
    @State private var previewPan: CGSize = .zero

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Space.sm) {
            HStack(spacing: Theme.Space.sm) {
                Button(isRefreshing ? "Refreshing..." : "Refresh Live Frame") {
                    refreshPreview()
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
                .disabled(isRefreshing || isCalibrationRunning)

                Spacer(minLength: 0)

                StatusDot(color: statusTint)
                Text(statusText)
                    .font(.caption)
                    .foregroundStyle(statusTint)
                    .lineLimit(1)
            }
            .zIndex(2)

            VStack(alignment: .leading, spacing: Theme.Space.sm) {
                HStack(spacing: Theme.Space.md) {
                    previewToggle("Invert", isOn: $invertPreview)
                    previewToggle("Mirror", isOn: $mirrorPreview)

                    Text("Rotate")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .fixedSize(horizontal: true, vertical: false)

                    Picker("Rotate", selection: $rotationDegrees) {
                        Text("0").tag(0)
                        Text("90").tag(90)
                        Text("180").tag(180)
                        Text("270").tag(270)
                    }
                    .labelsHidden()
                    .pickerStyle(.segmented)
                    .frame(width: 180)
                    .disabled(previewControlsDisabled)

                    Spacer(minLength: 0)
                }

                HStack(spacing: Theme.Space.md) {
                    Text("Zoom")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .fixedSize(horizontal: true, vertical: false)

                    Slider(value: $previewZoom, in: 1.0...6.0, step: 0.25) {
                        Text("Zoom")
                    }
                    .labelsHidden()
                    .disabled(previewControlsDisabled)

                    Text("\(Int(previewZoom * 100))%")
                        .font(.caption.monospacedDigit())
                        .foregroundStyle(.secondary)
                        .frame(width: 44, alignment: .trailing)

                    Button("Reset") {
                        previewZoom = 1.0
                        previewPan = .zero
                        invertPreview = true
                        mirrorPreview = false
                        rotationDegrees = 0
                    }
                    .buttonStyle(.bordered)
                    .controlSize(.small)
                    .disabled(previewControlsDisabled)
                }
            }
            .zIndex(2)

            LiveViewImagePickerSurface(
                image: previewImage,
                selection: selection,
                isInverted: invertPreview,
                isMirrored: mirrorPreview,
                rotationDegrees: rotationDegrees,
                zoom: CGFloat(previewZoom),
                pan: $previewPan,
                onPick: onPick
            )
            .accessibilityIdentifier(AccessibilityID.rebatePicker)
            .accessibilityLabel("Live-view rebate picker")
            .allowsHitTesting(!isCalibrationRunning)
            .frame(height: 340)
            .zIndex(0)
        }
        .onChange(of: isCalibrationRunning) { running in
            if running {
                statusText = "Calibration running; preview paused."
                statusTint = Theme.State.info
                Task { try? await orchestratorClient.setCalibrationPreviewLight(enabled: false) }
            } else if previewImage != nil {
                statusText = "Live frame ready."
                statusTint = Theme.State.success
            }
        }
        .onDisappear {
            Task { try? await orchestratorClient.setCalibrationPreviewLight(enabled: false) }
        }
        .task(id: autoRefreshID) {
            autoRefreshPreviewIfNeeded()
        }
    }

    private func autoRefreshPreviewIfNeeded() {
        guard autoRefreshID > 0, previewImage == nil, !isRefreshing, !isCalibrationRunning else {
            return
        }
        refreshPreview()
    }

    private var previewControlsDisabled: Bool {
        previewImage == nil || isCalibrationRunning
    }

    private func previewToggle(_ title: String, isOn: Binding<Bool>) -> some View {
        HStack(spacing: Theme.Space.xs) {
            Text(title)
                .font(.body)
                .lineLimit(1)
                .fixedSize(horizontal: true, vertical: false)

            Toggle(title, isOn: isOn)
                .labelsHidden()
                .toggleStyle(.switch)
                .controlSize(.small)
        }
        .disabled(previewControlsDisabled)
        .fixedSize(horizontal: true, vertical: false)
    }

    private func refreshPreview() {
        guard !isRefreshing else { return }
        isRefreshing = true
        statusText = "Refreshing live frame."
        statusTint = Theme.State.info

        Task {
            var lightWarning: String?
            do {
                try await orchestratorClient.setCalibrationPreviewLight(enabled: true, level: 200)
            } catch {
                lightWarning = "White light unavailable."
            }

            let frameDirectory = FileManager.default.temporaryDirectory
                .appendingPathComponent("scanlight-calibration-preview", isDirectory: true)
            do {
                try FileManager.default.createDirectory(
                    at: frameDirectory,
                    withIntermediateDirectories: true
                )
            } catch {
                await MainActor.run {
                    statusText = "Could not create preview cache."
                    statusTint = Theme.State.danger
                    isRefreshing = false
                }
                try? await orchestratorClient.setCalibrationPreviewLight(enabled: false)
                return
            }

            let frameURL = frameDirectory
                .appendingPathComponent("rebate-preview-\(UUID().uuidString).jpg")
            let result = await orchestratorClient.captureSonyLiveViewFrame(
                settings: settings,
                outputURL: frameURL
            )
            try? await orchestratorClient.setCalibrationPreviewLight(enabled: false)

            await MainActor.run {
                isRefreshing = false
                if result.success,
                   let imageURL = result.imageURL,
                   let imageData = try? Data(contentsOf: imageURL),
                   let image = NSImage(data: imageData) {
                    previewImage = image
                    statusText = lightWarning ?? "Live frame ready."
                    statusTint = lightWarning == nil ? Theme.State.success : Theme.State.warning
                } else {
                    statusText = result.message
                    statusTint = Theme.State.danger
                }
            }
        }
    }
}

private struct LiveViewImagePickerSurface: View {
    let image: NSImage?
    let selection: RebateRegion?
    let isInverted: Bool
    let isMirrored: Bool
    let rotationDegrees: Int
    let zoom: CGFloat
    @Binding var pan: CGSize
    let onPick: (RebateRegion) -> Void
    @GestureState private var dragDelta: CGSize = .zero

    var body: some View {
        GeometryReader { geo in
            ZStack {
                RoundedRectangle(cornerRadius: Theme.Radius.control, style: .continuous)
                    .fill(Color.black.opacity(0.34))
                    .overlay(
                        RoundedRectangle(cornerRadius: Theme.Radius.control, style: .continuous)
                            .stroke(Color.white.opacity(0.08), lineWidth: 1)
                    )

                if let image {
                    let sourceSize = pixelSize(for: image)
                    let imageSize = orientedImageSize(sourceSize)
                    let rect = fittedRect(imageSize: imageSize, containerSize: geo.size)
                    let displayRect = zoomedRect(baseRect: rect, zoom: zoom, pan: activePan)
                    let frameSize = unrotatedFrameSize(for: displayRect.size)

                    Image(nsImage: image)
                        .resizable()
                        .conditionalColorInvert(isInverted)
                        .frame(width: frameSize.width, height: frameSize.height)
                        .scaleEffect(x: isMirrored ? -1 : 1, y: 1)
                        .rotationEffect(.degrees(Double(normalizedRotationDegrees)))
                        .position(x: displayRect.midX, y: displayRect.midY)

                    if let selection {
                        selectionOverlay(selection, rect: displayRect)
                    } else {
                        Text("Click film base to set measurement crop")
                            .font(.caption.weight(.semibold))
                            .padding(.horizontal, Theme.Space.md)
                            .padding(.vertical, Theme.Space.xs)
                            .background(Color.black.opacity(0.62), in: Capsule())
                            .foregroundStyle(.white)
                    }

                    Rectangle()
                        .fill(Color.clear)
                        .contentShape(Rectangle())
                        .onTapGesture { location in
                            guard displayRect.contains(location) else { return }
                            let displayX = (location.x - displayRect.minX) / displayRect.width
                            let displayY = (location.y - displayRect.minY) / displayRect.height
                            let rawPoint = rawNormalizedPoint(displayX: displayX, displayY: displayY)
                            onPick(RebateRegion.centeredAtNormalized(
                                x: rawPoint.x,
                                y: rawPoint.y
                            ))
                        }
                        .gesture(
                            DragGesture(minimumDistance: 4)
                                .updating($dragDelta) { value, state, _ in
                                    state = value.translation
                                }
                                .onEnded { value in
                                    pan = clampedPan(
                                        CGSize(
                                            width: pan.width + value.translation.width,
                                            height: pan.height + value.translation.height
                                        ),
                                        baseRect: rect,
                                        containerSize: geo.size,
                                        zoom: zoom
                                    )
                                }
                        )
                } else {
                    VStack(spacing: Theme.Space.sm) {
                        Image(systemName: "camera.viewfinder")
                            .font(.system(size: 28, weight: .medium))
                            .foregroundStyle(.secondary)
                            .accessibilityHidden(true)
                        Text("No preview frame")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            }
            .clipped()
            .onChange(of: zoom) { newZoom in
                pan = clampedPan(
                    pan,
                    baseRect: fittedRect(
                        imageSize: image.map { orientedImageSize(pixelSize(for: $0)) } ?? .zero,
                        containerSize: geo.size
                    ),
                    containerSize: geo.size,
                    zoom: newZoom
                )
            }
            .onChange(of: rotationDegrees) { _ in
                pan = .zero
            }
            .onChange(of: isMirrored) { _ in
                pan = .zero
            }
        }
    }

    private var activePan: CGSize {
        CGSize(width: pan.width + dragDelta.width, height: pan.height + dragDelta.height)
    }

    private func selectionOverlay(_ selection: RebateRegion, rect: CGRect) -> some View {
        let displayPoint = displayNormalizedPoint(
            rawX: CGFloat(selection.centerX) / CGFloat(RebateRegion.rawWidth),
            rawY: CGFloat(selection.centerY) / CGFloat(RebateRegion.rawHeight)
        )
        let centerX = rect.minX + displayPoint.x * rect.width
        let centerY = rect.minY + displayPoint.y * rect.height
        let rotated = normalizedRotationDegrees == 90 || normalizedRotationDegrees == 270
        let widthFraction = rotated
            ? CGFloat(selection.h) / CGFloat(RebateRegion.rawHeight)
            : CGFloat(selection.w) / CGFloat(RebateRegion.rawWidth)
        let heightFraction = rotated
            ? CGFloat(selection.w) / CGFloat(RebateRegion.rawWidth)
            : CGFloat(selection.h) / CGFloat(RebateRegion.rawHeight)
        let width = max(18, widthFraction * rect.width)
        let height = max(18, heightFraction * rect.height)

        return ZStack {
            RoundedRectangle(cornerRadius: 2, style: .continuous)
                .stroke(Theme.accent, lineWidth: 2)
                .background(
                    RoundedRectangle(cornerRadius: 2, style: .continuous)
                        .fill(Theme.accent.opacity(0.12))
                )
                .frame(width: width, height: height)
                .position(x: centerX, y: centerY)

            Circle()
                .fill(Color.black.opacity(0.7))
                .overlay(Circle().strokeBorder(Theme.accent, lineWidth: 2))
                .frame(width: 10, height: 10)
                .position(x: centerX, y: centerY)
        }
        .allowsHitTesting(false)
    }

    private var normalizedRotationDegrees: Int {
        let normalized = rotationDegrees % 360
        return normalized >= 0 ? normalized : normalized + 360
    }

    private func orientedImageSize(_ sourceSize: CGSize) -> CGSize {
        if normalizedRotationDegrees == 90 || normalizedRotationDegrees == 270 {
            return CGSize(width: sourceSize.height, height: sourceSize.width)
        }
        return sourceSize
    }

    private func unrotatedFrameSize(for orientedSize: CGSize) -> CGSize {
        if normalizedRotationDegrees == 90 || normalizedRotationDegrees == 270 {
            return CGSize(width: orientedSize.height, height: orientedSize.width)
        }
        return orientedSize
    }

    private func displayNormalizedPoint(rawX: CGFloat, rawY: CGFloat) -> CGPoint {
        var x = min(1, max(0, rawX))
        let y = min(1, max(0, rawY))
        if isMirrored {
            x = 1 - x
        }

        switch normalizedRotationDegrees {
        case 90:
            return CGPoint(x: 1 - y, y: x)
        case 180:
            return CGPoint(x: 1 - x, y: 1 - y)
        case 270:
            return CGPoint(x: y, y: 1 - x)
        default:
            return CGPoint(x: x, y: y)
        }
    }

    private func rawNormalizedPoint(displayX: CGFloat, displayY: CGFloat) -> CGPoint {
        let dx = min(1, max(0, displayX))
        let dy = min(1, max(0, displayY))
        let unrotated: CGPoint

        switch normalizedRotationDegrees {
        case 90:
            unrotated = CGPoint(x: dy, y: 1 - dx)
        case 180:
            unrotated = CGPoint(x: 1 - dx, y: 1 - dy)
        case 270:
            unrotated = CGPoint(x: 1 - dy, y: dx)
        default:
            unrotated = CGPoint(x: dx, y: dy)
        }

        let rawX = isMirrored ? 1 - unrotated.x : unrotated.x
        return CGPoint(
            x: min(1, max(0, rawX)),
            y: min(1, max(0, unrotated.y))
        )
    }

    private func fittedRect(imageSize: CGSize, containerSize: CGSize) -> CGRect {
        guard imageSize.width > 0, imageSize.height > 0,
              containerSize.width > 0, containerSize.height > 0 else {
            return CGRect(origin: .zero, size: containerSize)
        }
        let scale = min(
            containerSize.width / imageSize.width,
            containerSize.height / imageSize.height
        )
        let width = imageSize.width * scale
        let height = imageSize.height * scale
        return CGRect(
            x: (containerSize.width - width) / 2,
            y: (containerSize.height - height) / 2,
            width: width,
            height: height
        )
    }

    private func zoomedRect(baseRect: CGRect, zoom: CGFloat, pan: CGSize) -> CGRect {
        let z = max(1, zoom)
        let width = baseRect.width * z
        let height = baseRect.height * z
        return CGRect(
            x: baseRect.midX - width / 2 + pan.width,
            y: baseRect.midY - height / 2 + pan.height,
            width: width,
            height: height
        )
    }

    private func clampedPan(
        _ proposed: CGSize,
        baseRect: CGRect,
        containerSize: CGSize,
        zoom: CGFloat
    ) -> CGSize {
        guard zoom > 1, baseRect.width > 0, baseRect.height > 0 else { return .zero }

        let overflowX = max(0, (baseRect.width * zoom - containerSize.width) / 2)
        let overflowY = max(0, (baseRect.height * zoom - containerSize.height) / 2)
        return CGSize(
            width: min(overflowX, max(-overflowX, proposed.width)),
            height: min(overflowY, max(-overflowY, proposed.height))
        )
    }

    private func pixelSize(for image: NSImage) -> CGSize {
        if let representation = image.representations.first {
            return CGSize(width: representation.pixelsWide, height: representation.pixelsHigh)
        }
        return image.size
    }
}

private extension View {
    @ViewBuilder
    func conditionalColorInvert(_ shouldInvert: Bool) -> some View {
        if shouldInvert {
            colorInvert()
        } else {
            self
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
                    flatFieldInstructions
                }

                if let err = viewModel.lastError[3] {
                    Banner(kind: .danger, text: err)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var flatFieldInstructions: some View {
        VStack(alignment: .leading, spacing: Theme.Space.md) {
            Banner(kind: .info, text: "Run this once per roll/session after exposure calibration. Re-run it when the film stock, holder position, lens/aperture, focus, camera position, diffuser, light path, or RGB exposure recipe changes.")

            VStack(alignment: .leading, spacing: Theme.Space.xs) {
                Text("How to capture it")
                    .font(.headline)
                Text("1. Put a clean, uniform film-base or blank leader area in the gate.")
                Text("2. Keep the camera, lens, focus, aperture, and Scanlight exactly as they will be used for the roll.")
                Text("3. Click Capture Flat Field. The app captures several R/G/B flat frames and checks illumination uniformity.")
                Text("4. If the result is clean, click Use + Continue to save it for scanning.")
            }
            .font(.caption)
            .foregroundStyle(.secondary)
            .fixedSize(horizontal: false, vertical: true)

            HStack(spacing: Theme.Space.sm) {
                Button("Skip Flat Field") {
                    viewModel.skipFFC(in: store)
                }
                .buttonStyle(.bordered)
                .disabled(viewModel.isRunning)

                Text("Skip only when the setup has not changed or you want to scan without FFC correction.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)

                Spacer(minLength: 0)
            }
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
        Button("Use + Continue") {
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
                if viewModel.ffcSkipped {
                    Banner(kind: .warning, text: "Flat field was skipped. This flow will not use a new FFC correction; choose one in Settings only if you intentionally want to reuse an older flat.")
                }

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

        // FIX-B: Phase 13 check_registration emits component keys (g_vs_r_dx/dy, b_vs_r_dx/dy).
        // Compute magnitude per pair; fall back to 0 so the format string stays numeric.
        let regDeltas = regCheck?.deltas ?? [:]
        let rgShift = regDeltas.isEmpty ? nil : hypot(regDeltas["g_vs_r_dx"] ?? 0.0, regDeltas["g_vs_r_dy"] ?? 0.0)
        let gbShift = regDeltas.isEmpty ? nil : hypot(regDeltas["b_vs_r_dx"] ?? 0.0, regDeltas["b_vs_r_dy"] ?? 0.0)
        // FIX-A: registration with empty deltas = "not available" (no captured frames yet).
        // Show a neutral info row rather than a 0.00 px PASS/FAIL Chip.
        let regPassed = regCheck?.passed ?? false
        let regAvailable = !regDeltas.isEmpty

        if regAvailable, let rgMag = rgShift, let gbMag = gbShift {
            LabeledValue(label: "G vs R shift") {
                Text(String(format: "%.2f px", rgMag))
                    .font(.body.monospacedDigit())
                    .accessibilityIdentifier(AccessibilityID.resultsShiftRG)
                    .accessibilityValue(String(format: "%.2f px", rgMag))
            }
            LabeledValue(label: "B vs R shift") {
                Text(String(format: "%.2f px", gbMag))
                    .font(.body.monospacedDigit())
                    .accessibilityIdentifier(AccessibilityID.resultsShiftGB)
                    .accessibilityValue(String(format: "%.2f px", gbMag))
            }
            LabeledValue(label: "Registration") {
                Chip(
                    text: regPassed ? "PASS" : "FAIL",
                    tint: regPassed ? Theme.State.success : Theme.State.danger
                )
                .accessibilityIdentifier(AccessibilityID.resultsRegVerdict)
                .accessibilityValue(regPassed ? "PASS" : "FAIL")
            }
        } else {
            // No captured frames yet — registration runs on hardware (M2) against a real triplet.
            LabeledValue(label: "G vs R shift") {
                Text("—")
                    .font(.body.monospacedDigit())
                    .foregroundStyle(.secondary)
                    .accessibilityIdentifier(AccessibilityID.resultsShiftRG)
                    .accessibilityValue("not available")
            }
            LabeledValue(label: "B vs R shift") {
                Text("—")
                    .font(.body.monospacedDigit())
                    .foregroundStyle(.secondary)
                    .accessibilityIdentifier(AccessibilityID.resultsShiftGB)
                    .accessibilityValue("not available")
            }
            LabeledValue(label: "Registration") {
                Text("Not available — needs captured frames (runs on hardware, M2)")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .accessibilityIdentifier(AccessibilityID.resultsRegVerdict)
                    .accessibilityValue("not available")
            }
        }

        Divider()

        // Sub-section B: Base Neutrality
        let baseDev = baseCheck?.deltas.values.max() ?? 0.0
        let baseDevPct = baseDev / 65535.0 * 100.0
        let basePassed = baseCheck?.passed ?? false

        LabeledValue(label: "Calibrated base spread") {
            Text(String(format: "%.1f%%", baseDevPct))
                .font(.body.monospacedDigit())
                .accessibilityIdentifier(AccessibilityID.resultsBaseDeviation)
                .accessibilityValue(String(format: "%.1f%%", baseDevPct))
        }
        LabeledValue(label: "RGB balance check") {
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
        // FIX-A: exclude checks with empty deltas ("not available") from the verdict.
        // Registration has empty deltas pre-hardware; base_neutrality always has real
        // deltas and is the effective gate. allSatisfy on an empty collection is true,
        // which is the correct neutral fallback when nothing is measurable yet.
        let rollPassed = checks.filter { !$0.deltas.isEmpty }.allSatisfy { $0.passed }
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
            Banner(kind: .info, text: "Calibration complete. The measured film-base RGB signal is balanced enough to apply to this roll.")
        } else {
            Banner(kind: .warning, text: "Measured film-base RGB is still uneven. Re-run exposure or accept the profile only if you expect to correct color later.")
        }
    }
}
