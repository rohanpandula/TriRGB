import SwiftUI

// MARK: - Session

struct SessionView: View {
    @Binding var selectedTab: ProductTab
    @ObservedObject var store: SettingsStore
    @ObservedObject var coordinator: ScanCoordinator
    @ObservedObject var orchestratorClient: OrchestratorClient
    @ObservedObject var lightViewModel: ScanlightViewModel
    @ObservedObject var cameraConnection: SonyCameraConnection

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Theme.Space.section) {
                GroupBox(label: Text("Roll Session")) {
                    VStack(alignment: .leading, spacing: Theme.Space.md) {
                        HStack(alignment: .top, spacing: Theme.Space.lg) {
                            VStack(alignment: .leading, spacing: Theme.Space.sm) {
                                Text(store.settings.rollName.isEmpty ? "Untitled roll" : store.settings.rollName)
                                    .font(.title2.weight(.semibold))
                                    .lineLimit(1)
                                Text(sessionSubtitle)
                                    .font(.callout)
                                    .foregroundStyle(.secondary)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                            Spacer(minLength: 0)
                            Chip(text: phaseText, tint: phaseTint, filled: coordinator.phase != .idle)
                        }

                        if !blockingIssues.isEmpty {
                            Banner(kind: .warning, text: blockingIssues.joined(separator: " "))
                        } else if let caution = readinessCaution {
                            Banner(kind: .warning, text: caution)
                        } else {
                            Banner(kind: .info, text: "Ready to calibrate or scan. Calibration is still recommended at the start of each roll.")
                        }

                        HStack(spacing: Theme.Space.md) {
                            Button(primaryActionTitle) {
                                primaryAction()
                            }
                            .buttonStyle(.borderedProminent)
                            .disabled(primaryActionDisabled)

                            if primaryActionTitle != "Connect Scanlight" {
                                Button("Connect Scanlight") {
                                    connectScanlight()
                                }
                                .buttonStyle(.bordered)
                                .disabled(coordinator.phase != .idle || lightViewModel.portOwner != .idle)
                            }

                            Button("Set Up") {
                                selectedTab = .setup
                            }
                            .buttonStyle(.bordered)
                        }
                    }
                }

                GroupBox(label: Text("Readiness")) {
                    VStack(spacing: Theme.Space.sm) {
                        SessionStatusRow(
                            label: "Output",
                            value: outputValue,
                            tint: store.settings.outputFolder.isEmpty ? Theme.State.warning : Theme.State.success
                        )
                        SessionStatusRow(
                            label: "Trigger",
                            value: triggerValue,
                            tint: triggerTint
                        )
                        SessionStatusRow(
                            label: "Scanlight",
                            value: scanlightValue,
                            tint: lightViewModel.isConnected ? Theme.State.success : Theme.State.idle
                        )
                        SessionStatusRow(
                            label: "Camera",
                            value: cameraValue,
                            tint: cameraConnection.tint
                        )
                        SessionStatusRow(
                            label: "Stock profile",
                            value: stockProfileValue,
                            tint: store.stockCalibrationProfiles.isEmpty ? Theme.State.idle : Theme.State.success
                        )
                        SessionStatusRow(
                            label: "Flat field",
                            value: flatFieldValue,
                            tint: store.settings.ffcCalibration == nil ? Theme.State.idle : Theme.State.success
                        )
                    }
                }

                GroupBox(label: Text("Workflow")) {
                    VStack(alignment: .leading, spacing: Theme.Space.md) {
                        SessionActionRow(
                            step: "1",
                            title: "Set up the roll",
                            detail: "Name the roll, choose output, confirm trigger and camera auth.",
                            action: "Open setup"
                        ) {
                            selectedTab = .setup
                        }

                        SessionActionRow(
                            step: "2",
                            title: "Calibrate film base",
                            detail: "Pick a clean base patch, solve R/G/B exposure, then capture or skip flat field.",
                            action: "Calibrate"
                        ) {
                            selectedTab = .calibrate
                        }

                        SessionActionRow(
                            step: "3",
                            title: "Scan the roll",
                            detail: "Use the saved stock profile, frame with live view, then capture each RGB triplet.",
                            action: "Scan"
                        ) {
                            selectedTab = .scan
                        }

                        SessionActionRow(
                            step: "4",
                            title: "Develop positives",
                            detail: "Render positives from finished RGB triplets and pick a base patch if needed.",
                            action: "Develop"
                        ) {
                            selectedTab = .develop
                        }
                    }
                }
            }
            .padding(Theme.Space.xl)
        }
        .groupBoxStyle(PanelGroupBoxStyle())
    }

    private var sessionSubtitle: String {
        if store.settings.outputFolder.isEmpty {
            return "Choose an output folder before capture."
        }
        return store.settings.outputFolder
    }

    private var blockingIssues: [String] {
        var issues: [String] = []
        if store.settings.outputFolder.isEmpty {
            issues.append("Output folder is missing.")
        }
        if ["manual", "hw"].contains(store.settings.triggerMode),
           (store.settings.iedInbox ?? "").isEmpty {
            issues.append("IED inbox is missing for this trigger mode.")
        }
        if store.settings.triggerMode == "sdk" {
            if !store.settings.usesSonyUSB, (store.settings.sonyIpAddress ?? "").isEmpty {
                issues.append("Sony camera IP is missing.")
            }
            if (store.settings.sonyUser ?? "").isEmpty || (store.settings.sonyPassword ?? "").isEmpty {
                issues.append("Sony credentials are missing.")
            }
        }
        return issues
    }

    private var primaryActionTitle: String {
        if store.settings.outputFolder.isEmpty {
            return "Choose Output"
        }
        if coordinator.phase == .scanning {
            return "Go to Scan"
        }
        if coordinator.phase == .calibrating {
            return "Continue Calibration"
        }
        if !lightViewModel.isConnected && coordinator.phase == .idle {
            return "Connect Scanlight"
        }
        if store.settings.triggerMode == "sdk", !cameraConnection.isOnline {
            return "Check Camera"
        }
        if store.stockCalibrationProfiles.isEmpty {
            return "Start Calibration"
        }
        return "Open Scan"
    }

    private var primaryActionDisabled: Bool {
        if primaryActionTitle == "Connect Scanlight" {
            return coordinator.transitionInFlight || lightViewModel.portOwner != .idle
        }
        return coordinator.transitionInFlight || cameraConnection.isChecking
    }

    private func primaryAction() {
        if store.settings.outputFolder.isEmpty {
            chooseOutputFolder()
            return
        }
        if coordinator.phase == .scanning {
            selectedTab = .scan
            return
        }
        if !lightViewModel.isConnected && coordinator.phase == .idle {
            connectScanlight()
            return
        }
        if store.settings.triggerMode == "sdk", !cameraConnection.isOnline {
            Task { await checkSonyConnection() }
            return
        }
        if coordinator.phase == .calibrating || store.stockCalibrationProfiles.isEmpty {
            selectedTab = .calibrate
            return
        }
        selectedTab = .scan
    }

    private var readinessCaution: String? {
        if !lightViewModel.isConnected && coordinator.phase == .idle {
            return "Scanlight is disconnected. Connect the rig before calibration or capture."
        }
        if store.settings.triggerMode == "sdk", !cameraConnection.isOnline {
            return "Sony SDK mode is selected. Check the camera connection before capture."
        }
        return nil
    }

    private func chooseOutputFolder() {
        store.folderPicker("Select output folder") { url in
            if let url {
                store.settings.outputFolder = url.path
                _ = store.validate()
            }
        }
    }

    /// Only touches the USB Scanlight rig. Never invokes the Sony SDK
    /// probe — that lives behind the explicit "Check Camera" path
    /// (`checkSonyConnection`) and the Set Up tab's dedicated button.
    private func connectScanlight() {
        if !lightViewModel.isConnected {
            lightViewModel.connect()
        }
    }

    private func checkSonyConnection() async {
        _ = await cameraConnection.check(store: store, orchestratorClient: orchestratorClient)
    }

    private var phaseText: String {
        switch coordinator.phase {
        case .idle:
            return "idle"
        case .calibrating:
            return "calibrating"
        case .scanning:
            return "scanning"
        }
    }

    private var phaseTint: Color {
        switch coordinator.phase {
        case .idle:
            return Theme.State.idle
        case .calibrating:
            return Theme.State.info
        case .scanning:
            return Theme.State.success
        }
    }

    private var outputValue: String {
        store.settings.outputFolder.isEmpty ? "not selected" : store.settings.outputFolder
    }

    private var triggerReady: Bool {
        if store.settings.triggerMode == "sdk" {
            return (store.settings.usesSonyUSB || !(store.settings.sonyIpAddress ?? "").isEmpty)
                && !(store.settings.sonyUser ?? "").isEmpty
                && !(store.settings.sonyPassword ?? "").isEmpty
        }
        if ["manual", "hw"].contains(store.settings.triggerMode) {
            return !(store.settings.iedInbox ?? "").isEmpty
        }
        return true
    }

    private var triggerTint: Color {
        if store.settings.triggerMode == "sdk" {
            return triggerReady ? Theme.State.info : Theme.State.warning
        }
        return triggerReady ? Theme.State.success : Theme.State.warning
    }

    private var triggerValue: String {
        switch store.settings.triggerMode {
        case "sdk":
            let transport = store.settings.usesSonyUSB ? "USB" : "Wi-Fi PC Remote"
            return triggerReady ? "Sony SDK (\(transport))" : "Sony SDK missing fields"
        case "hw":
            return "hardware pulse + IED inbox"
        default:
            return "manual IED capture"
        }
    }

    private var scanlightValue: String {
        if lightViewModel.isConnected {
            return "connected \(lightViewModel.scanlightPort.isEmpty ? "" : lightViewModel.scanlightPort)"
        }
        if coordinator.phase != .idle {
            return "owned by \(phaseText)"
        }
        return "disconnected"
    }

    private var cameraValue: String {
        if store.settings.triggerMode != "sdk" {
            return "not used by current trigger"
        }
        return cameraConnection.statusText
    }

    private var stockProfileValue: String {
        if store.stockCalibrationProfiles.isEmpty {
            return "none saved"
        }
        return "\(store.stockCalibrationProfiles.count) saved"
    }

    private var flatFieldValue: String {
        if let ffc = store.settings.ffcCalibration, !ffc.isEmpty {
            return ffc
        }
        return "none selected"
    }
}

private struct SessionStatusRow: View {
    let label: String
    let value: String
    let tint: Color

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: Theme.Space.md) {
            StatusDot(color: tint)
            Text(label)
                .font(.subheadline.weight(.medium))
                .foregroundStyle(.secondary)
                .frame(width: 104, alignment: .leading)
            Text(value)
                .lineLimit(1)
                .truncationMode(.middle)
                .frame(maxWidth: .infinity, alignment: .trailing)
        }
    }
}

private struct SessionActionRow: View {
    let step: String
    let title: String
    let detail: String
    let action: String
    let onAction: () -> Void

    var body: some View {
        HStack(alignment: .center, spacing: Theme.Space.md) {
            Text(step)
                .font(.caption.weight(.bold))
                .monospacedDigit()
                .frame(width: 28, height: 28)
                .background(
                    Circle()
                        .fill(Theme.accent.opacity(0.18))
                )
                .foregroundStyle(Theme.accent)

            VStack(alignment: .leading, spacing: Theme.Space.xs) {
                Text(title)
                    .font(.subheadline.weight(.semibold))
                Text(detail)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            Spacer(minLength: Theme.Space.md)

            Button(action) {
                onAction()
            }
            .buttonStyle(.bordered)
        }
        .padding(.vertical, Theme.Space.xs)
    }
}

// MARK: - Set Up

private enum SetupSection: String, CaseIterable, Identifiable {
    case setup
    case filmStocks
    case diagnostics

    var id: String { rawValue }

    var label: String {
        switch self {
        case .setup:
            return "Roll Setup"
        case .filmStocks:
            return "Film Stocks"
        case .diagnostics:
            return "Diagnostics"
        }
    }
}

struct SetupView: View {
    @ObservedObject var store: SettingsStore
    @ObservedObject var orchestratorClient: OrchestratorClient
    @ObservedObject var lightViewModel: ScanlightViewModel
    @ObservedObject var cameraConnection: SonyCameraConnection

    @State private var section: SetupSection = .setup

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Space.md) {
            HStack(spacing: Theme.Space.md) {
                Picker("Set Up section", selection: $section) {
                    ForEach(SetupSection.allCases) { section in
                        Text(section.label).tag(section)
                    }
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .frame(width: 460)

                Text(section == .setup
                     ? "Roll paths, trigger mode, camera connection, and capture output."
                     : section == .filmStocks
                        ? "Rename, edit, and delete saved per-stock RGB exposure recipes."
                        : "Manual light controls and live-view tools for debugging and recovery.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 0)
            }
            .padding(.horizontal, Theme.Space.xl)
            .padding(.top, Theme.Space.xl)

            Divider()
                .padding(.horizontal, Theme.Space.xl)

            switch section {
            case .setup:
                ScanSettingsView(
                    store: store,
                    orchestratorClient: orchestratorClient,
                    lightViewModel: lightViewModel,
                    cameraConnection: cameraConnection
                )
            case .filmStocks:
                FilmStockManagerView(store: store)
            case .diagnostics:
                ScanlightView(viewModel: lightViewModel)
            }
        }
    }
}
