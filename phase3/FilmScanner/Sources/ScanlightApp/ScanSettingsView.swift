// ScanSettingsView — capture-settings form bound to SettingsStore.
//
// Validation: store.validate() mirrors CaptureSettings.__post_init__ rules.
// Inline errors appear as a danger Banner below the offending control.
// There is no Save button: controls update SettingsStore immediately, and
// runtime-safe settings are pushed to a running orchestrator as they change.
//
// Folder pickers delegate to store.folderPicker (injectable in tests to avoid
// real NSOpenPanel in headless runs).
//
// Visual language: shared PanelGroupBoxStyle + Theme tokens (see DesignSystem).
// All AccessibilityIDs and conditional rendering for the IED inbox row and
// composite-format picker are kept stable for automation.

import SwiftUI

struct ScanSettingsView: View {
    @ObservedObject var store: SettingsStore
    /// Injected from ScanlightAppMain. Used to push live settings when the
    /// orchestrator is running (R-20). Not @StateObject here — AppMain owns
    /// the lifetime.
    @ObservedObject var orchestratorClient: OrchestratorClient
    @ObservedObject var lightViewModel: ScanlightViewModel
    @ObservedObject var cameraConnection: SonyCameraConnection

    /// Local state for camera model "Custom…" text entry.
    @State private var customCameraModel: String = ""
    @State private var sonyLiveViewActive: Bool = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Theme.Space.section) {

                // MARK: 1. Roll

                GroupBox(label: Text("Roll")) {
                    VStack(alignment: .leading, spacing: Theme.Space.sm) {
                        HStack(spacing: Theme.Space.md) {
                            Text("Name")
                                .font(.subheadline)
                                .foregroundStyle(.secondary)
                                .frame(width: 96, alignment: .leading)
                            TextField("Roll001", text: $store.settings.rollName)
                                .accessibilityIdentifier(AccessibilityID.settingsRollNameField)
                                .textFieldStyle(.roundedBorder)
                                .onChange(of: store.settings.rollName) { _ in
                                    refreshValidationIfNeeded()
                                }
                        }
                        validationError("rollName")
                    }
                }

                // MARK: 2. Output

                GroupBox(label: Text("Output")) {
                    VStack(alignment: .leading, spacing: Theme.Space.sm) {
                        folderRow(
                            label: "Folder",
                            buttonId: AccessibilityID.settingsPickOutputBtn,
                            pathId: AccessibilityID.settingsOutputPathLabel,
                            path: store.settings.outputFolder,
                            placeholder: "No folder selected",
                            prompt: "Select output folder"
                        ) { url in
                            store.settings.outputFolder = url.path
                            refreshValidationIfNeeded()
                        }
                        validationError("outputFolder")
                    }
                }

                // MARK: 3. Trigger

                GroupBox(label: Text("Trigger")) {
                    VStack(alignment: .leading, spacing: Theme.Space.md) {
                        Picker("Trigger", selection: $store.settings.triggerMode) {
                            Text("Manual").tag("manual")
                            Text("Pulse").tag("hw")
                            Text("SDK").tag("sdk")
                        }
                        .pickerStyle(.segmented)
                        .labelsHidden()
                        .accessibilityIdentifier(AccessibilityID.settingsTriggerModePicker)
                        .onChange(of: store.settings.triggerMode) { _ in
                            refreshValidationIfNeeded()
                        }

                        Text(triggerModeHelp)
                            .font(.caption)
                            .foregroundStyle(.secondary)

                        if store.settings.triggerMode == "sdk" {
                            HStack(spacing: Theme.Space.md) {
                                Button(cameraConnection.isChecking ? "Checking..." : "Check Camera") {
                                    Task { await checkSonyConnection() }
                                }
                                .buttonStyle(.borderedProminent)
                                .disabled(cameraConnection.isChecking || orchestratorClient.isRunning || sonyLiveViewActive)
                                .accessibilityIdentifier(AccessibilityID.settingsSonyConnectButton)

                                HStack(spacing: Theme.Space.sm) {
                                    StatusDot(color: cameraConnection.tint)
                                    Text(cameraConnection.statusText)
                                        .font(.caption)
                                        .foregroundStyle(cameraConnection.tint)
                                        .lineLimit(2)
                                        .fixedSize(horizontal: false, vertical: true)
                                }
                                .accessibilityIdentifier(AccessibilityID.settingsSonyConnectionStatusLabel)
                                .accessibilityLabel(cameraConnection.statusText)

                                Spacer(minLength: 0)
                            }

                            Text(cameraConnection.detailText)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)

                            SonyLiveViewPanel(
                                settings: store.settings,
                                orchestratorClient: orchestratorClient,
                                lightViewModel: lightViewModel,
                                isLiveViewActive: $sonyLiveViewActive
                            )
                        }

                        if usesIedInbox {
                            VStack(alignment: .leading, spacing: Theme.Space.sm) {
                                folderRow(
                                    label: "IED inbox",
                                    buttonId: AccessibilityID.settingsPickInboxBtn,
                                    pathId: AccessibilityID.settingsInboxPathLabel,
                                    path: store.settings.iedInbox ?? "",
                                    placeholder: "No folder selected",
                                    prompt: "Select IED inbox folder"
                                ) { url in
                                    store.settings.iedInbox = url.path
                                    refreshValidationIfNeeded()
                                }
                                validationError("iedInbox")
                            }
                        }
                    }
                }

                // MARK: 4. Timing

                GroupBox(label: Text("Timing")) {
                    HStack(spacing: Theme.Space.md) {
                        Text("Settle")
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                        Stepper("\(store.settings.settleMs) ms",
                                value: $store.settings.settleMs,
                                in: 0...9999,
                                step: 10)
                            .accessibilityIdentifier(AccessibilityID.settingsSettleStepper)
                            .monospacedDigit()
                            .fixedSize()
                            .onChange(of: store.settings.settleMs) { _ in
                                Task { await applyRuntimeSettingsIfRunning() }
                            }
                        Spacer()
                    }
                }

                // MARK: 5. Calibration (FFC dir)

                GroupBox(label: Text("Calibration")) {
                    VStack(alignment: .leading, spacing: Theme.Space.sm) {
                        HStack(spacing: Theme.Space.md) {
                            Text("Target")
                                .font(.subheadline)
                                .foregroundStyle(.secondary)
                                .frame(width: 96, alignment: .leading)
                            Picker("Exposure target", selection: calibrationTargetPercentBinding) {
                                Text("75%").tag(75)
                                Text("80%").tag(80)
                                Text("85%").tag(85)
                            }
                            .pickerStyle(.segmented)
                            .labelsHidden()
                            .frame(maxWidth: 260)
                            Spacer()
                        }
                        Text("Lower targets keep more RAW headroom. 80% is the default for this rig.")
                            .font(.caption)
                            .foregroundStyle(.secondary)

                        folderRow(
                            label: "FFC dir",
                            buttonId: AccessibilityID.settingsPickFfcBtn,
                            pathId: AccessibilityID.settingsFfcPathLabel,
                            path: store.settings.ffcCalibration ?? "",
                            placeholder: "No calibration selected",
                            prompt: "Select FFC calibration folder"
                        ) { url in store.settings.ffcCalibration = url.path }

                        if let lastDir = store.lastCalibrationDir {
                            Button("Use last calibration") {
                                store.settings.ffcCalibration = lastDir
                            }
                            .buttonStyle(.link)
                        }
                    }
                }

                // MARK: 6. Camera

                GroupBox(label: Text("Camera")) {
                    VStack(alignment: .leading, spacing: Theme.Space.sm) {
                        let cameraOptions = ["Sony ILCE-7CR", "FUJIFILM GFX100 II", "Custom\u{2026}"]
                        let cameraBinding = Binding<String>(
                            get: {
                                let current = store.settings.cameraModel ?? "Sony ILCE-7CR"
                                return cameraOptions.contains(current) ? current : "Custom\u{2026}"
                            },
                            set: { newValue in
                                if newValue == "Custom\u{2026}" {
                                    store.settings.cameraModel = customCameraModel.isEmpty
                                        ? nil : customCameraModel
                                } else {
                                    store.settings.cameraModel = newValue
                                    customCameraModel = ""
                                }
                            }
                        )

                        HStack(spacing: Theme.Space.md) {
                            Text("Model")
                                .font(.subheadline)
                                .foregroundStyle(.secondary)
                                .frame(width: 96, alignment: .leading)
                            Picker("Camera", selection: cameraBinding) {
                                ForEach(cameraOptions, id: \.self) { option in
                                    Text(option).tag(option)
                                }
                            }
                            .pickerStyle(.menu)
                            .labelsHidden()
                            .accessibilityIdentifier(AccessibilityID.settingsCameraModelPicker)
                            Spacer()
                        }

                        if cameraBinding.wrappedValue == "Custom\u{2026}" {
                            TextField("Custom camera model", text: $customCameraModel)
                                .textFieldStyle(.roundedBorder)
                                .onChange(of: customCameraModel) { newValue in
                                    store.settings.cameraModel = newValue.isEmpty ? nil : newValue
                                }
                                .onAppear {
                                    let current = store.settings.cameraModel ?? ""
                                    if !["Sony ILCE-7CR", "FUJIFILM GFX100 II"].contains(current) {
                                        customCameraModel = current
                                    }
                                }
                        }

                        if store.settings.triggerMode == "sdk" {
                            Divider()
                                .padding(.vertical, Theme.Space.xs)

                            Text("Sony SDK fires the camera and saves each ARW directly to the roll folder. Use Wi-Fi for PC Remote, or USB when the camera enumerates through Sony's USB SDK path.")
                                .font(.caption)
                                .foregroundStyle(.secondary)

                            HStack(spacing: Theme.Space.md) {
                                Text("Transport")
                                    .font(.subheadline)
                                    .foregroundStyle(.secondary)
                                    .frame(width: 96, alignment: .leading)
                                Picker("Sony transport", selection: sonyTransportBinding) {
                                    Text("Wi-Fi").tag("wifi")
                                    Text("USB").tag("usb")
                                }
                                .pickerStyle(.segmented)
                                .labelsHidden()
                                .frame(maxWidth: 260)
                                Spacer(minLength: 0)
                            }

                            if !store.settings.usesSonyUSB {
                                settingsTextFieldRow(
                                    label: "IP",
                                    placeholder: "Camera IP, e.g. 10.0.0.x",
                                    id: AccessibilityID.settingsSonyIpField,
                                    text: optionalStringBinding(\.sonyIpAddress)
                                )
                                validationError("sonyIpAddress")

                                settingsTextFieldRow(
                                    label: "MAC",
                                    placeholder: "Camera MAC from Access Auth screen",
                                    id: AccessibilityID.settingsSonyMacField,
                                    text: optionalStringBinding(\.sonyMacAddress)
                                )
                            } else {
                                Text("USB mode uses Sony SDK USB enumeration. Leave the camera connected by USB; IP and MAC are ignored for capture, calibration, and live view.")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }

                            settingsTextFieldRow(
                                label: "User",
                                placeholder: "Access Auth user",
                                id: AccessibilityID.settingsSonyUserField,
                                text: optionalStringBinding(\.sonyUser)
                            )
                            validationError("sonyUser")

                            HStack(spacing: Theme.Space.md) {
                                Text("Password")
                                    .font(.subheadline)
                                    .foregroundStyle(.secondary)
                                    .frame(width: 96, alignment: .leading)
                                SecureField("Access Auth password", text: optionalStringBinding(\.sonyPassword))
                                    .textFieldStyle(.roundedBorder)
                                    .accessibilityIdentifier(AccessibilityID.settingsSonyPasswordField)
                                Spacer(minLength: 0)
                            }
                            validationError("sonyPassword")
                        }
                    }
                }

                // MARK: 7. Composite

                GroupBox(label: Text("Composite")) {
                    VStack(alignment: .leading, spacing: Theme.Space.md) {
                        Toggle("Stream composite", isOn: $store.settings.streamComposite)
                            .accessibilityIdentifier(AccessibilityID.settingsStreamToggle)

                        Text("Build each frame's RGB composite in the background as captures finish. DNG is best for RAW-editor workflows; TIFF is the compatibility fallback.")
                            .font(.caption)
                            .foregroundStyle(.secondary)

                        if store.settings.streamComposite {
                            HStack(spacing: Theme.Space.md) {
                                Text("Format")
                                    .font(.subheadline)
                                    .foregroundStyle(.secondary)
                                    .frame(width: 96, alignment: .leading)
                                Picker("Format", selection: $store.settings.compositeFormat) {
                                    Text("DNG").tag("dng")
                                    Text("TIFF").tag("tiff")
                                    Text("Both").tag("both")
                                }
                                .pickerStyle(.segmented)
                                .labelsHidden()
                                .accessibilityIdentifier(AccessibilityID.settingsCompositeFormat)
                            }
                        }
                    }
                }
            }
            .padding(Theme.Space.xl)
        }
        .groupBoxStyle(PanelGroupBoxStyle())
    }

    // MARK: - Actions

    /// Push runtime-safe settings when the orchestrator is running.
    /// Exposed as internal so tests can verify the wiring without launching the app.
    @MainActor
    func applyRuntimeSettingsIfRunning() async {
        refreshValidationIfNeeded()
        guard orchestratorClient.isRunning else { return }
        store.validationErrors = store.validate()
        guard store.validationErrors.isEmpty else { return }
        do {
            try await orchestratorClient.applyRuntimeSettings(store.settings)
        } catch {
            store.validationErrors["_runtime"] =
                "Failed to push settings: \(error.localizedDescription)"
        }
    }

    @MainActor
    private func checkSonyConnection() async {
        guard store.settings.triggerMode == "sdk" else { return }

        if orchestratorClient.isRunning {
            cameraConnection.markOffline(
                "Stop scan or calibration before checking the Sony SDK connection.",
                settings: store.settings
            )
            return
        }
        if sonyLiveViewActive {
            cameraConnection.markOffline(
                "Close Sony live view before checking the connection.",
                settings: store.settings
            )
            return
        }

        _ = await cameraConnection.check(store: store, orchestratorClient: orchestratorClient)
    }

    // MARK: - Private helpers

    private var usesIedInbox: Bool {
        store.settings.triggerMode == "hw" || store.settings.triggerMode == "manual"
    }

    private var triggerModeHelp: String {
        switch store.settings.triggerMode {
        case "manual":
            return "You fire each channel in Imaging Edge Desktop; the app sets R/G/B and waits for each RAW in the inbox."
        case "hw":
            return "Scanlight fires the shutter over the 3.5 mm jack; RAWs arrive in the IED inbox."
        default:
            return store.settings.usesSonyUSB
                ? "Sony SDK fires the camera over USB and downloads each ARW directly."
                : "Sony SDK fires the camera over Wi-Fi and downloads each ARW directly."
        }
    }

    private var sonyTransportBinding: Binding<String> {
        Binding(
            get: { store.settings.sonyTransportMode },
            set: { newValue in
                store.settings.sonyTransport = newValue == "usb" ? "usb" : "wifi"
                refreshValidationIfNeeded()
            }
        )
    }

    private var calibrationTargetPercentBinding: Binding<Int> {
        Binding(
            get: {
                let fraction = store.settings.calibrationTargetFraction ?? 0.80
                return Int((fraction * 100.0).rounded())
            },
            set: { newValue in
                store.settings.calibrationTargetFraction = Double(newValue) / 100.0
            }
        )
    }

    func refreshValidationIfNeeded() {
        if !store.validationErrors.isEmpty {
            _ = store.validate()
        }
    }

    private func optionalStringBinding(_ keyPath: WritableKeyPath<ScanSettings, String?>) -> Binding<String> {
        Binding(
            get: { store.settings[keyPath: keyPath] ?? "" },
            set: { newValue in
                store.settings[keyPath: keyPath] = newValue.isEmpty ? nil : newValue
                refreshValidationIfNeeded()
            }
        )
    }

    /// Inline validation message for a given field key, rendered as a danger Banner.
    @ViewBuilder
    private func validationError(_ key: String) -> some View {
        if let error = store.validationErrors[key] {
            Banner(kind: .danger, text: error)
        }
    }

    /// A folder-picker row: leading label, "Choose…" button, and a truncating
    /// monospaced path. Shared shape for output / inbox / FFC so the three read
    /// identically (consistent component vocabulary).
    @ViewBuilder
    private func folderRow(
        label: String,
        buttonId: String,
        pathId: String,
        path: String,
        placeholder: String,
        prompt: String,
        onPick: @escaping (URL) -> Void
    ) -> some View {
        HStack(spacing: Theme.Space.md) {
            Text(label)
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .frame(width: 96, alignment: .leading)
            Button("Choose\u{2026}") {
                store.folderPicker(prompt) { url in
                    if let url = url { onPick(url) }
                }
            }
            .accessibilityIdentifier(buttonId)
            .buttonStyle(.bordered)
            Text(path.isEmpty ? placeholder : path)
                .accessibilityIdentifier(pathId)
                .font(.system(.caption, design: .monospaced))
                .foregroundStyle(path.isEmpty ? .secondary : .primary)
                .lineLimit(1)
                .truncationMode(.middle)
            Spacer(minLength: 0)
        }
    }

    @ViewBuilder
    private func settingsTextFieldRow(
        label: String,
        placeholder: String,
        id: String,
        text: Binding<String>
    ) -> some View {
        HStack(spacing: Theme.Space.md) {
            Text(label)
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .frame(width: 96, alignment: .leading)
            TextField(placeholder, text: text)
                .textFieldStyle(.roundedBorder)
                .accessibilityIdentifier(id)
            Spacer(minLength: 0)
        }
    }

}
