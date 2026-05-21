// ScanSettingsView — capture-settings form bound to SettingsStore.
//
// Validation: store.validate() mirrors CaptureSettings.__post_init__ rules.
// Inline errors appear as a danger Banner below the offending control.
//
// Folder pickers delegate to store.folderPicker (injectable in tests to avoid
// real NSOpenPanel in headless runs).
//
// Visual language: shared PanelGroupBoxStyle + Theme tokens (see DesignSystem).
// All AccessibilityIDs, element types, and the conditional rendering of the
// HW-trigger inbox row and the composite-format picker are preserved exactly —
// the AX-ID coverage gate (SettingsCalibrationUITests) depends on them.

import SwiftUI

struct ScanSettingsView: View {
    @ObservedObject var store: SettingsStore
    /// Injected from ScanlightAppMain. Used to push live settings when the
    /// orchestrator is running (R-20). Not @StateObject here — AppMain owns
    /// the lifetime.
    @ObservedObject var orchestratorClient: OrchestratorClient

    /// Local state for camera model "Custom…" text entry.
    @State private var customCameraModel: String = ""

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
                        ) { url in store.settings.outputFolder = url.path }
                        validationError("outputFolder")
                    }
                }

                // MARK: 3. Trigger

                GroupBox(label: Text("Trigger")) {
                    VStack(alignment: .leading, spacing: Theme.Space.md) {
                        Picker("Trigger", selection: $store.settings.triggerMode) {
                            Text("Hardware").tag("hw")
                            Text("SDK").tag("sdk")
                        }
                        .pickerStyle(.segmented)
                        .labelsHidden()
                        .accessibilityIdentifier(AccessibilityID.settingsTriggerModePicker)

                        Text(store.settings.triggerMode == "hw"
                             ? "Scanlight fires the shutter over the 3.5 mm jack; RAWs arrive in the IED inbox."
                             : "The Sony SDK fires the shutter over USB tether.")
                            .font(.caption)
                            .foregroundStyle(.secondary)

                        if store.settings.triggerMode == "hw" {
                            VStack(alignment: .leading, spacing: Theme.Space.sm) {
                                folderRow(
                                    label: "IED inbox",
                                    buttonId: AccessibilityID.settingsPickInboxBtn,
                                    pathId: AccessibilityID.settingsInboxPathLabel,
                                    path: store.settings.iedInbox ?? "",
                                    placeholder: "No folder selected",
                                    prompt: "Select IED inbox folder"
                                ) { url in store.settings.iedInbox = url.path }
                                validationError("iedInbox")
                            }
                        }
                    }
                }

                // MARK: 4. Levels

                GroupBox(label: Text("Levels")) {
                    VStack(spacing: Theme.Space.md) {
                        settingsChannelRow(
                            label: "Red", tint: Theme.Channel.red,
                            level: Binding(get: { store.settings.levelR }, set: { store.settings.levelR = $0 }),
                            sliderId: AccessibilityID.settingsLevelRSlider
                        )
                        settingsChannelRow(
                            label: "Green", tint: Theme.Channel.green,
                            level: Binding(get: { store.settings.levelG }, set: { store.settings.levelG = $0 }),
                            sliderId: AccessibilityID.settingsLevelGSlider
                        )
                        settingsChannelRow(
                            label: "Blue", tint: Theme.Channel.blue,
                            level: Binding(get: { store.settings.levelB }, set: { store.settings.levelB = $0 }),
                            sliderId: AccessibilityID.settingsLevelBSlider
                        )
                    }
                }

                // MARK: 5. Timing

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
                        Spacer()
                    }
                }

                // MARK: 6. Calibration (FFC dir)

                GroupBox(label: Text("Calibration")) {
                    VStack(alignment: .leading, spacing: Theme.Space.sm) {
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

                // MARK: 7. Camera

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
                    }
                }

                // MARK: 8. Composite

                GroupBox(label: Text("Composite")) {
                    VStack(alignment: .leading, spacing: Theme.Space.md) {
                        Toggle("Stream composite", isOn: $store.settings.streamComposite)
                            .accessibilityIdentifier(AccessibilityID.settingsStreamToggle)

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

                // MARK: 9. Actions

                GroupBox(label: Text("Save")) {
                    VStack(alignment: .leading, spacing: Theme.Space.sm) {
                        Button("Save Settings") {
                            Task { await saveSettings() }
                        }
                        .accessibilityIdentifier(AccessibilityID.settingsSaveBtn)
                        .buttonStyle(.borderedProminent)

                        if !store.validationErrors.isEmpty {
                            Banner(kind: .danger, text: "Please fix the errors above.")
                        } else {
                            Text("Settings apply on the next scan, or live when the orchestrator is running.")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                    }
                }
            }
            .padding(Theme.Space.xl)
        }
        .groupBoxStyle(PanelGroupBoxStyle())
    }

    // MARK: - Actions

    /// Execute the Save Settings action: validate then push live settings when
    /// the orchestrator is running (R-20). Async so callers can await the push.
    /// Exposed as internal (not private) so unit tests can call it directly to
    /// verify the wiring without launching the app or clicking a SwiftUI button.
    @MainActor
    func saveSettings() async {
        store.validationErrors = store.validate()
        // R-20: push live settings when the orchestrator is running.
        // If not running, just store — start() applies them later (Phase 7).
        if store.validationErrors.isEmpty && orchestratorClient.isRunning {
            do {
                try await orchestratorClient.applyRuntimeSettings(store.settings)
            } catch {
                store.validationErrors["_runtime"] =
                    "Failed to push settings: \(error.localizedDescription)"
            }
        }
    }

    // MARK: - Private helpers

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

    /// Channel row for the Levels GroupBox: a channel-tinted slider bound to an
    /// Int field on ScanSettings (levelR/G/B). No "On" button — these are saved
    /// capture levels, not live light controls.
    @ViewBuilder
    private func settingsChannelRow(label: String, tint: Color, level: Binding<Int>, sliderId: String) -> some View {
        HStack(spacing: Theme.Space.md) {
            HStack(spacing: Theme.Space.sm) {
                Circle()
                    .fill(tint)
                    .frame(width: 9, height: 9)
                    .accessibilityHidden(true)
                Text(label)
                    .font(.subheadline)
                    .frame(width: 46, alignment: .leading)
            }
            Slider(
                value: Binding(
                    get: { Double(level.wrappedValue) },
                    set: { level.wrappedValue = Int($0) }
                ),
                in: 0...255
            )
            .accessibilityIdentifier(sliderId)
            .tint(tint)
            .frame(minWidth: 140)
            Text("\(level.wrappedValue)")
                .font(.body)
                .monospacedDigit()
                .foregroundStyle(tint)
                .frame(width: 36, alignment: .trailing)
        }
    }
}
