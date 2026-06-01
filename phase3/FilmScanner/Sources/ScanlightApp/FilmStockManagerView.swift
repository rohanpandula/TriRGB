import SwiftUI

struct FilmStockManagerView: View {
    @ObservedObject var store: SettingsStore

    @State private var selectedProfileID: UUID?
    @State private var draft = StockProfileDraft()
    @State private var message: String?
    @State private var messageKind: Banner.Kind = .info
    @State private var showingDeleteConfirmation = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Theme.Space.section) {
                GroupBox(label: Text("Film Stocks")) {
                    VStack(alignment: .leading, spacing: Theme.Space.lg) {
                        intro

                        if store.stockCalibrationProfiles.isEmpty {
                            Banner(kind: .info, text: "No saved film stocks yet. Run exposure calibration, name the stock, then Save Profile.")
                        } else {
                            selector
                            editor
                        }
                    }
                }
            }
            .padding(Theme.Space.xl)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .groupBoxStyle(PanelGroupBoxStyle())
        .onAppear {
            syncSelectionIfNeeded()
        }
        .onChange(of: store.stockCalibrationProfiles.map(\.id)) { _ in
            syncSelectionIfNeeded()
        }
        .onChange(of: selectedProfileID) { _ in
            loadSelectedProfile()
        }
        .confirmationDialog(
            "Delete \(draft.name)?",
            isPresented: $showingDeleteConfirmation,
            titleVisibility: .visible
        ) {
            Button("Delete Film Stock", role: .destructive) {
                deleteSelectedProfile()
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("This removes the saved RGB exposure recipe. Existing scans and rendered files are not deleted.")
        }
    }

    private var intro: some View {
        VStack(alignment: .leading, spacing: Theme.Space.xs) {
            Text("Saved stock profiles are RGB exposure recipes.")
                .font(.subheadline.weight(.semibold))
            Text("Rename or adjust a recipe here, then apply it from Scan before capturing that film stock. Flat fields are still captured per roll/session.")
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private var selector: some View {
        VStack(alignment: .leading, spacing: Theme.Space.sm) {
            HStack(alignment: .firstTextBaseline, spacing: Theme.Space.md) {
                Picker("Saved stock", selection: $selectedProfileID) {
                    ForEach(store.stockCalibrationProfiles) { profile in
                        Text(profile.stockName).tag(Optional(profile.id))
                    }
                }
                .pickerStyle(.menu)
                .frame(maxWidth: 420, alignment: .leading)
                .accessibilityIdentifier(AccessibilityID.stockManagerList)

                Chip(text: "\(store.stockCalibrationProfiles.count) saved", tint: Theme.State.idle)

                Spacer(minLength: 0)
            }

            if let selectedProfile {
                Text(recipeSummary(selectedProfile))
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private var editor: some View {
        VStack(alignment: .leading, spacing: Theme.Space.md) {
            Divider()

            HStack(alignment: .firstTextBaseline, spacing: Theme.Space.md) {
                Text("Edit Recipe")
                    .font(.headline)
                Spacer(minLength: 0)
                if let profile = selectedProfile {
                    Text("Updated \(Self.dateFormatter.string(from: profile.updatedAt))")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
            }

            VStack(alignment: .leading, spacing: Theme.Space.xs) {
                Text("Name")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                TextField("Film stock name", text: $draft.name)
                    .textFieldStyle(.roundedBorder)
                    .frame(maxWidth: 520)
                    .accessibilityIdentifier(AccessibilityID.stockManagerNameField)
            }

            channelGrid

            if let profile = selectedProfile {
                metadata(profile)
            }

            actions

            if let message {
                Banner(kind: messageKind, text: message)
                    .accessibilityIdentifier(AccessibilityID.stockManagerStatusLabel)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var channelGrid: some View {
        Grid(alignment: .leading, horizontalSpacing: Theme.Space.lg, verticalSpacing: Theme.Space.sm) {
            GridRow {
                Text("Channel")
                Text("LED")
                Text("Shutter")
            }
            .font(.caption.weight(.semibold))
            .foregroundStyle(.secondary)

            stockChannelEditor(label: "Red", tint: Theme.Channel.red, led: $draft.ledR, shutter: $draft.shutterR)
            stockChannelEditor(label: "Green", tint: Theme.Channel.green, led: $draft.ledG, shutter: $draft.shutterG)
            stockChannelEditor(label: "Blue", tint: Theme.Channel.blue, led: $draft.ledB, shutter: $draft.shutterB)
        }
        .padding(Theme.Space.md)
        .background(
            RoundedRectangle(cornerRadius: Theme.Radius.control, style: .continuous)
                .fill(Color.primary.opacity(0.03))
        )
    }

    private func stockChannelEditor(
        label: String,
        tint: Color,
        led: Binding<Int>,
        shutter: Binding<String>
    ) -> some View {
        GridRow {
            HStack(spacing: Theme.Space.sm) {
                StatusDot(color: tint)
                Text(label)
                    .font(.callout.weight(.medium))
            }
            .frame(width: 96, alignment: .leading)

            Stepper(value: led, in: 0...255) {
                Text("\(led.wrappedValue)")
                    .monospacedDigit()
                    .frame(width: 42, alignment: .leading)
            }
            .frame(width: 140, alignment: .leading)

            TextField("current", text: shutter)
                .textFieldStyle(.roundedBorder)
                .monospacedDigit()
                .frame(width: 110)
        }
    }

    private func metadata(_ profile: StockCalibrationProfile) -> some View {
        VStack(alignment: .leading, spacing: Theme.Space.sm) {
            Divider()
            let base = profile.exposureResult.baseRegion
            LabeledValue(label: "Base patch") {
                Text("x \(base.x) y \(base.y) \(base.w)x\(base.h)")
                    .foregroundStyle(.secondary)
            }
            LabeledValue(label: "Camera") {
                Text(profile.cameraModel ?? "unspecified")
                    .foregroundStyle(.secondary)
            }
        }
        .frame(maxWidth: 560, alignment: .leading)
    }

    private var actions: some View {
        HStack(spacing: Theme.Space.sm) {
            Button("Save Changes") {
                saveDraft()
            }
            .buttonStyle(.borderedProminent)
            .disabled(!draft.canSave)
            .accessibilityIdentifier(AccessibilityID.stockManagerSaveButton)

            Button("Use Current Settings") {
                loadCurrentSettingsIntoDraft()
            }
            .buttonStyle(.bordered)
            .accessibilityIdentifier(AccessibilityID.stockManagerUseCurrentButton)

            Button("Revert") {
                loadSelectedProfile()
                setMessage("Reverted unsaved changes.", kind: .info)
            }
            .buttonStyle(.bordered)

            Spacer(minLength: Theme.Space.md)

            Button("Delete") {
                showingDeleteConfirmation = true
            }
            .buttonStyle(.bordered)
            .foregroundStyle(Theme.State.danger)
            .disabled(selectedProfileID == nil)
            .accessibilityIdentifier(AccessibilityID.stockManagerDeleteButton)
        }
        .frame(maxWidth: 720, alignment: .leading)
    }

    private var selectedProfile: StockCalibrationProfile? {
        store.stockCalibrationProfile(id: selectedProfileID)
    }

    private func syncSelectionIfNeeded() {
        if let selectedProfileID,
           store.stockCalibrationProfile(id: selectedProfileID) != nil {
            return
        }
        selectedProfileID = store.stockCalibrationProfiles.first?.id
        loadSelectedProfile()
    }

    private func loadSelectedProfile() {
        guard let selectedProfile else {
            draft = StockProfileDraft()
            return
        }
        draft = StockProfileDraft(profile: selectedProfile)
    }

    private func saveDraft() {
        guard let selectedProfileID else {
            setMessage("Choose a film stock before saving.", kind: .warning)
            return
        }

        do {
            let updated = try store.updateStockCalibrationProfile(
                id: selectedProfileID,
                stockName: draft.name,
                ledR: draft.ledR,
                ledG: draft.ledG,
                ledB: draft.ledB,
                shutterR: draft.shutterR,
                shutterG: draft.shutterG,
                shutterB: draft.shutterB
            )
            self.selectedProfileID = updated.id
            draft = StockProfileDraft(profile: updated)
            setMessage("Saved \(updated.stockName). Apply it in Scan before capturing this stock.", kind: .info)
        } catch let error as StockProfileEditError {
            setMessage(error.localizedDescription, kind: .danger)
        } catch {
            setMessage(error.localizedDescription, kind: .danger)
        }
    }

    private func loadCurrentSettingsIntoDraft() {
        draft.ledR = store.settings.levelR
        draft.ledG = store.settings.levelG
        draft.ledB = store.settings.levelB
        draft.shutterR = store.settings.shutterR ?? ""
        draft.shutterG = store.settings.shutterG ?? ""
        draft.shutterB = store.settings.shutterB ?? ""
        setMessage("Loaded the current RGB levels and shutter speeds into the editor. Save Changes to update the profile.", kind: .info)
    }

    private func deleteSelectedProfile() {
        guard let selectedProfileID else { return }
        let oldName = draft.name
        if store.deleteStockCalibrationProfile(id: selectedProfileID) {
            self.selectedProfileID = store.stockCalibrationProfiles.first?.id
            loadSelectedProfile()
            setMessage("Deleted \(oldName). Existing scan files were not touched.", kind: .warning)
        } else {
            setMessage("That film stock profile no longer exists.", kind: .danger)
        }
    }

    private func setMessage(_ text: String, kind: Banner.Kind) {
        message = text
        messageKind = kind
    }

    private func recipeSummary(_ profile: StockCalibrationProfile) -> String {
        let r = profile.exposureResult.r
        let g = profile.exposureResult.g
        let b = profile.exposureResult.b
        return "LED \(r.ledLevel)/\(g.ledLevel)/\(b.ledLevel), shutter \(r.shutterSpeed ?? "current")/\(g.shutterSpeed ?? "current")/\(b.shutterSpeed ?? "current")"
    }

    private static let dateFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.dateStyle = .short
        formatter.timeStyle = .short
        return formatter
    }()
}

private struct StockProfileDraft: Equatable {
    var name = ""
    var ledR = 0
    var ledG = 0
    var ledB = 0
    var shutterR = ""
    var shutterG = ""
    var shutterB = ""

    init() {}

    init(profile: StockCalibrationProfile) {
        name = profile.stockName
        ledR = profile.exposureResult.r.ledLevel
        ledG = profile.exposureResult.g.ledLevel
        ledB = profile.exposureResult.b.ledLevel
        shutterR = profile.exposureResult.r.shutterSpeed ?? ""
        shutterG = profile.exposureResult.g.shutterSpeed ?? ""
        shutterB = profile.exposureResult.b.shutterSpeed ?? ""
    }

    var canSave: Bool {
        !StockCalibrationProfile.normalizedStockName(name).isEmpty
    }
}
