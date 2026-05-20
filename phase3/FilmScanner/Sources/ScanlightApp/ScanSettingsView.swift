// ScanSettingsView — nine-section settings form bound to SettingsStore.
//
// Layout mirrors ScanlightView exactly: ScrollView root, VStack(spacing:16),
// GroupBox sections with HStack(spacing:8) rows and .padding(.top, 4) inside
// each GroupBox content block.
//
// Validation: store.validate() mirrors CaptureSettings.__post_init__ rules.
// Inline errors appear as red .caption Text below the offending control.
//
// Folder pickers delegate to store.folderPicker (injectable in tests to avoid
// real NSOpenPanel in headless runs).

import SwiftUI

struct ScanSettingsView: View {
    @ObservedObject var store: SettingsStore

    /// Local state for camera model "Custom…" text entry.
    @State private var customCameraModel: String = ""

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {

                // MARK: 1. Roll

                GroupBox(label: Text("Roll").font(.headline)) {
                    VStack(alignment: .leading, spacing: 4) {
                        HStack(spacing: 8) {
                            Text("Name:")
                                .frame(width: 120, alignment: .trailing)
                            TextField("Roll001", text: $store.settings.rollName)
                                .accessibilityIdentifier(AccessibilityID.settingsRollNameField)
                                .textFieldStyle(.roundedBorder)
                        }
                        if let error = store.validationErrors["rollName"] {
                            Text(error)
                                .foregroundColor(.red)
                                .font(.caption)
                        }
                    }
                    .padding(.top, 4)
                }

                // MARK: 2. Output

                GroupBox(label: Text("Output").font(.headline)) {
                    VStack(alignment: .leading, spacing: 4) {
                        HStack(spacing: 8) {
                            Text("Folder:")
                                .frame(width: 120, alignment: .trailing)
                            Button("Choose\u{2026}") {
                                store.folderPicker("Select output folder") { url in
                                    if let url = url {
                                        store.settings.outputFolder = url.path
                                    }
                                }
                            }
                            .accessibilityIdentifier(AccessibilityID.settingsPickOutputBtn)
                            Text(store.settings.outputFolder.isEmpty
                                 ? "No folder selected"
                                 : store.settings.outputFolder)
                                .accessibilityIdentifier(AccessibilityID.settingsOutputPathLabel)
                                .font(.system(.caption, design: .monospaced))
                                .foregroundColor(store.settings.outputFolder.isEmpty ? .secondary : .primary)
                        }
                        if let error = store.validationErrors["outputFolder"] {
                            Text(error)
                                .foregroundColor(.red)
                                .font(.caption)
                        }
                    }
                    .padding(.top, 4)
                }

                // MARK: 3. Trigger

                GroupBox(label: Text("Trigger").font(.headline)) {
                    VStack(alignment: .leading, spacing: 4) {
                        Picker("Trigger", selection: $store.settings.triggerMode) {
                            Text("HW").tag("hw")
                            Text("SDK").tag("sdk")
                        }
                        .pickerStyle(.segmented)
                        .accessibilityIdentifier(AccessibilityID.settingsTriggerModePicker)

                        if store.settings.triggerMode == "hw" {
                            VStack(alignment: .leading, spacing: 4) {
                                HStack(spacing: 8) {
                                    Text("IED Inbox:")
                                        .frame(width: 120, alignment: .trailing)
                                    Button("Choose\u{2026}") {
                                        store.folderPicker("Select IED inbox folder") { url in
                                            if let url = url {
                                                store.settings.iedInbox = url.path
                                            }
                                        }
                                    }
                                    .accessibilityIdentifier(AccessibilityID.settingsPickInboxBtn)
                                    Text(store.settings.iedInbox ?? "No folder selected")
                                        .accessibilityIdentifier(AccessibilityID.settingsInboxPathLabel)
                                        .font(.system(.caption, design: .monospaced))
                                        .foregroundColor(
                                            (store.settings.iedInbox ?? "").isEmpty ? .secondary : .primary
                                        )
                                }
                                if let error = store.validationErrors["iedInbox"] {
                                    Text(error)
                                        .foregroundColor(.red)
                                        .font(.caption)
                                }
                            }
                        }
                    }
                    .padding(.top, 4)
                }

                // MARK: 4. Levels

                GroupBox(label: Text("Levels").font(.headline)) {
                    VStack(spacing: 8) {
                        settingsChannelRow(
                            label: "R",
                            level: Binding(
                                get: { store.settings.levelR },
                                set: { store.settings.levelR = $0 }
                            ),
                            sliderId: AccessibilityID.settingsLevelRSlider
                        )
                        settingsChannelRow(
                            label: "G",
                            level: Binding(
                                get: { store.settings.levelG },
                                set: { store.settings.levelG = $0 }
                            ),
                            sliderId: AccessibilityID.settingsLevelGSlider
                        )
                        settingsChannelRow(
                            label: "B",
                            level: Binding(
                                get: { store.settings.levelB },
                                set: { store.settings.levelB = $0 }
                            ),
                            sliderId: AccessibilityID.settingsLevelBSlider
                        )
                    }
                    .padding(.top, 4)
                }

                // MARK: 5. Timing

                GroupBox(label: Text("Timing").font(.headline)) {
                    // Stepper step:10 is the sole constraint on settle ms —
                    // settle_ms has no __post_init__ validation rule; see
                    // SettingsStore.validate() comment.
                    Stepper("\(store.settings.settleMs) ms",
                            value: $store.settings.settleMs,
                            in: 0...9999,
                            step: 10)
                        .accessibilityIdentifier(AccessibilityID.settingsSettleStepper)
                    .padding(.top, 4)
                }

                // MARK: 6. Calibration (FFC dir)

                GroupBox(label: Text("Calibration").font(.headline)) {
                    VStack(alignment: .leading, spacing: 4) {
                        HStack(spacing: 8) {
                            Text("FFC Dir:")
                                .frame(width: 120, alignment: .trailing)
                            Button("Choose\u{2026}") {
                                store.folderPicker("Select FFC calibration folder") { url in
                                    if let url = url {
                                        store.settings.ffcCalibration = url.path
                                    }
                                }
                            }
                            .accessibilityIdentifier(AccessibilityID.settingsPickFfcBtn)
                            Text(store.settings.ffcCalibration ?? "No calibration selected")
                                .accessibilityIdentifier(AccessibilityID.settingsFfcPathLabel)
                                .font(.system(.caption, design: .monospaced))
                                .foregroundColor(
                                    (store.settings.ffcCalibration ?? "").isEmpty ? .secondary : .primary
                                )
                        }
                        if let lastDir = store.lastCalibrationDir {
                            Button("Use last calibration") {
                                store.settings.ffcCalibration = lastDir
                            }
                        }
                    }
                    .padding(.top, 4)
                }

                // MARK: 7. Camera

                GroupBox(label: Text("Camera").font(.headline)) {
                    VStack(alignment: .leading, spacing: 4) {
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

                        Picker("Camera", selection: cameraBinding) {
                            ForEach(cameraOptions, id: \.self) { option in
                                Text(option).tag(option)
                            }
                        }
                        .pickerStyle(.menu)
                        .accessibilityIdentifier(AccessibilityID.settingsCameraModelPicker)

                        if cameraBinding.wrappedValue == "Custom\u{2026}" {
                            TextField("Custom camera model", text: $customCameraModel)
                                .textFieldStyle(.roundedBorder)
                                .onChange(of: customCameraModel) { newValue in
                                    store.settings.cameraModel = newValue.isEmpty ? nil : newValue
                                }
                                .onAppear {
                                    // Pre-populate the text field if current value is custom
                                    let current = store.settings.cameraModel ?? ""
                                    if !["Sony ILCE-7CR", "FUJIFILM GFX100 II"].contains(current) {
                                        customCameraModel = current
                                    }
                                }
                        }
                    }
                    .padding(.top, 4)
                }

                // MARK: 8. Composite

                GroupBox(label: Text("Composite").font(.headline)) {
                    VStack(alignment: .leading, spacing: 4) {
                        Toggle("Stream composite", isOn: $store.settings.streamComposite)
                            .accessibilityIdentifier(AccessibilityID.settingsStreamToggle)

                        if store.settings.streamComposite {
                            Picker("Format", selection: $store.settings.compositeFormat) {
                                Text("DNG").tag("dng")
                                Text("TIFF").tag("tiff")
                                Text("Both").tag("both")
                            }
                            .pickerStyle(.segmented)
                            .accessibilityIdentifier(AccessibilityID.settingsCompositeFormat)
                        }
                    }
                    .padding(.top, 4)
                }

                // MARK: 9. Actions

                GroupBox(label: Text("Actions").font(.headline)) {
                    VStack(alignment: .leading, spacing: 4) {
                        Button("Save Settings") {
                            store.validationErrors = store.validate()
                        }
                        .accessibilityIdentifier(AccessibilityID.settingsSaveBtn)

                        if !store.validationErrors.isEmpty {
                            Text("Please fix the errors above.")
                                .foregroundColor(.red)
                                .font(.caption)
                        }
                    }
                    .padding(.top, 4)
                }

            }
            .padding()
        }
    }

    // MARK: - Private helpers

    /// Channel row for the Levels GroupBox: slider bound to an Int field on
    /// ScanSettings (levelR/G/B). Uses Int↔Double conversion for the Slider.
    /// No "On" button — levels here are settings, not live light controls.
    @ViewBuilder
    private func settingsChannelRow(label: String, level: Binding<Int>, sliderId: String) -> some View {
        HStack(spacing: 8) {
            Text(label)
                .frame(width: 44, alignment: .trailing)
            Slider(
                value: Binding(
                    get: { Double(level.wrappedValue) },
                    set: { level.wrappedValue = Int($0) }
                ),
                in: 0...255
            )
            .accessibilityIdentifier(sliderId)
            .frame(minWidth: 120)
            Text("\(level.wrappedValue)")
                .frame(width: 30, alignment: .trailing)
                .monospacedDigit()
        }
    }
}
