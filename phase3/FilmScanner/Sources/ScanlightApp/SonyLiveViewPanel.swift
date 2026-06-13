// SonyLiveViewPanel — persistent SDK live-view preview for the Sony SDK path.
//
// The preview starts one `sony-capture --live-view-stream-out` process. That
// process keeps a single SDK connection open, refreshes one JPEG file in the
// temp directory, and exits when the user stops preview.

import AppKit
import Darwin
import SwiftUI

private let defaultPreviewWhiteLevel: Double = 64

private enum SonyLiveViewState: Equatable {
    case idle
    case starting
    case running(String)
    case failed(String)

    var text: String {
        switch self {
        case .idle:
            return "Preview stopped"
        case .starting:
            return "Opening live view... this can take 10-30 seconds."
        case .running(let message), .failed(let message):
            return message
        }
    }

    var tint: Color {
        switch self {
        case .idle:
            return Theme.State.idle
        case .starting:
            return Theme.State.info
        case .running:
            return Theme.State.success
        case .failed:
            return Theme.State.danger
        }
    }

    var isStarting: Bool {
        if case .starting = self { return true }
        return false
    }
}

struct SonyLiveViewPanel: View {
    let settings: ScanSettings
    @ObservedObject var orchestratorClient: OrchestratorClient
    @ObservedObject var lightViewModel: ScanlightViewModel
    @Binding var isLiveViewActive: Bool
    var allowsWhileBackendRunning: Bool = false
    var isTemporarilyUnavailable: Bool = false
    var unavailableReason: String? = nil
    var startButtonID: String = AccessibilityID.settingsSonyLiveViewStartButton
    var stopButtonID: String = AccessibilityID.settingsSonyLiveViewStopButton
    var statusLabelID: String = AccessibilityID.settingsSonyLiveViewStatusLabel
    var imageID: String = AccessibilityID.settingsSonyLiveViewImage
    var invertToggleID: String = AccessibilityID.settingsSonyLiveViewInvertToggle
    var mirrorToggleID: String = AccessibilityID.settingsSonyLiveViewMirrorToggle
    var flipToggleID: String = AccessibilityID.settingsSonyLiveViewFlipToggle
    var rotatePickerID: String = AccessibilityID.settingsSonyLiveViewRotatePicker
    var zoomSliderID: String = AccessibilityID.settingsSonyLiveViewZoomSlider
    var whiteLightToggleID: String = AccessibilityID.settingsSonyLiveViewWhiteLightToggle

    @State private var liveViewTask: Task<Void, Never>?
    @State private var liveViewProcess: Process?
    @State private var liveViewState: SonyLiveViewState = .idle
    @State private var liveViewImage: NSImage?
    @State private var imageRefreshID = UUID()
    @State private var invertPreview = false
    @State private var mirrorPreview = false
    @State private var flipPreview = false
    @State private var rotationDegrees = 0
    @State private var previewZoom: Double = 1.0
    @State private var previewPan: CGSize = .zero
    @State private var previewWhiteEnabled = false
    @State private var previewWhiteInFlight = false
    @State private var previewWhiteMessage: String?

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Space.sm) {
            Divider()
                .padding(.vertical, Theme.Space.xs)

            HStack(spacing: Theme.Space.md) {
                Text("Live View")
                    .font(.subheadline.weight(.semibold))

                Spacer(minLength: 0)

                Button(liveViewState.isStarting ? "Opening..." : "Open Live View") {
                    startLiveView()
                }
                .accessibilityIdentifier(startButtonID)
                .buttonStyle(.bordered)
                .disabled(liveViewProcess != nil || liveViewUnavailable || liveViewState.isStarting)

                Button("Close") {
                    stopLiveView(clearImage: false, state: .idle)
                }
                .accessibilityIdentifier(stopButtonID)
                .buttonStyle(.bordered)
                .disabled(liveViewProcess == nil)
            }

            Text(liveViewHelpText)
                .font(.caption)
                .foregroundStyle(.secondary)

            HStack(spacing: Theme.Space.md) {
                Toggle("White light", isOn: whiteLightBinding)
                    .toggleStyle(.switch)
                    .controlSize(.small)
                    .disabled(previewWhiteInFlight || !canControlPreviewWhiteLight)
                    .accessibilityIdentifier(whiteLightToggleID)

                if previewWhiteInFlight {
                    ProgressView().controlSize(.small)
                }

                Text(previewWhiteStatusText)
                    .font(.caption)
                    .foregroundStyle(previewWhiteStatusTint)
                    .lineLimit(1)

                Spacer(minLength: 0)
            }

            HStack(spacing: Theme.Space.md) {
                Toggle("Invert", isOn: $invertPreview)
                    .toggleStyle(.switch)
                    .controlSize(.small)
                    .disabled(liveViewImage == nil)
                    .accessibilityIdentifier(invertToggleID)

                Toggle("Mirror", isOn: $mirrorPreview)
                    .toggleStyle(.switch)
                    .controlSize(.small)
                    .disabled(liveViewImage == nil)
                    .accessibilityIdentifier(mirrorToggleID)

                Toggle("Flip", isOn: $flipPreview)
                    .toggleStyle(.switch)
                    .controlSize(.small)
                    .disabled(liveViewImage == nil)
                    .accessibilityIdentifier(flipToggleID)

                Text("Rotate")
                    .font(.caption)
                    .foregroundStyle(.secondary)

                Picker("Rotate", selection: $rotationDegrees) {
                    Text("0").tag(0)
                    Text("90").tag(90)
                    Text("180").tag(180)
                    Text("270").tag(270)
                }
                .labelsHidden()
                .pickerStyle(.segmented)
                .frame(width: 180)
                .disabled(liveViewImage == nil)
                .accessibilityIdentifier(rotatePickerID)

                Text("Zoom")
                    .font(.caption)
                    .foregroundStyle(.secondary)

                Slider(value: $previewZoom, in: 1.0...6.0, step: 0.25) {
                    Text("Zoom")
                }
                .labelsHidden()
                .disabled(liveViewImage == nil)
                .accessibilityIdentifier(zoomSliderID)

                Text("\(Int(previewZoom * 100))%")
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(.secondary)
                    .frame(width: 44, alignment: .trailing)

                Button("Reset") {
                    resetPreviewTransforms()
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
                .disabled(liveViewImage == nil)
            }

            TransformedSonyLiveViewSurface(
                image: liveViewImage,
                isInverted: invertPreview,
                isMirrored: mirrorPreview,
                isFlipped: flipPreview,
                rotationDegrees: rotationDegrees,
                zoom: CGFloat(previewZoom),
                pan: $previewPan
            )
            .id(imageRefreshID)
            .frame(maxWidth: .infinity)
            .frame(height: 260)
            .accessibilityIdentifier(imageID)
            .accessibilityLabel("Sony live view preview")

            HStack(spacing: Theme.Space.sm) {
                StatusDot(color: liveViewState.tint)
                Text(liveViewState.text)
                    .font(.caption)
                    .foregroundStyle(liveViewState.tint)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .accessibilityIdentifier(statusLabelID)
            .accessibilityLabel(liveViewState.text)
        }
        .onChange(of: settings.triggerMode) { _ in
            stopLiveView(clearImage: true, state: .idle)
        }
        .onChange(of: settings.sonyTransport ?? "") { _ in
            stopLiveView(clearImage: true, state: .idle)
        }
        .onChange(of: settings.sonyIpAddress ?? "") { _ in
            stopLiveView(clearImage: true, state: .idle)
        }
        .onChange(of: settings.sonyMacAddress ?? "") { _ in
            stopLiveView(clearImage: true, state: .idle)
        }
        .onChange(of: settings.sonyUser ?? "") { _ in
            stopLiveView(clearImage: true, state: .idle)
        }
        .onChange(of: settings.sonyPassword ?? "") { _ in
            stopLiveView(clearImage: true, state: .idle)
        }
        .onChange(of: orchestratorClient.isRunning) { isRunning in
            if isRunning && !allowsWhileBackendRunning {
                stopLiveView(clearImage: false, state: .idle)
            }
        }
        .onChange(of: isTemporarilyUnavailable) { unavailable in
            if unavailable {
                stopLiveView(clearImage: false, state: .idle)
            }
        }
        .onDisappear {
            stopLiveView(clearImage: false, state: .idle)
        }
    }

    private var backendBlocksLiveView: Bool {
        orchestratorClient.isRunning && !allowsWhileBackendRunning
    }

    private var liveViewUnavailable: Bool {
        backendBlocksLiveView || isTemporarilyUnavailable
    }

    private var liveViewHelpText: String {
        if let unavailableReason, isTemporarilyUnavailable {
            return unavailableReason
        }
        if backendBlocksLiveView {
            return "Live view is unavailable while scan or calibration owns the backend."
        }
        if allowsWhileBackendRunning {
            return "Use this for framing. Close live view before capture; Sony capture uses its own SDK session."
        }
        return "One Sony SDK session stays open until you close live view. Opening live view turns on a low white preview light."
    }

    private var canControlPreviewWhiteLight: Bool {
        if isTemporarilyUnavailable {
            return false
        }
        return orchestratorClient.isRunning || lightViewModel.manualControlsEnabled
    }

    private var whiteLightBinding: Binding<Bool> {
        Binding(
            get: { previewWhiteEnabled },
            set: { enabled in
                setPreviewWhiteLight(enabled: enabled)
            }
        )
    }

    private var previewWhiteStatusText: String {
        if let previewWhiteMessage {
            return previewWhiteMessage
        }
        if previewWhiteEnabled {
            return "White light on for framing."
        }
        if previewWhiteInFlight {
            return "Updating light..."
        }
        if isTemporarilyUnavailable {
            return "Light is busy."
        }
        if orchestratorClient.isRunning {
            return "Uses backend light control."
        }
        if lightViewModel.manualControlsEnabled {
            return "Uses connected light panel."
        }
        return "Connect the light or start the backend first."
    }

    private var previewWhiteStatusTint: Color {
        if previewWhiteMessage != nil {
            return previewWhiteEnabled ? Theme.State.success : Theme.State.warning
        }
        if previewWhiteEnabled {
            return Theme.State.success
        }
        if canControlPreviewWhiteLight {
            return Theme.State.idle
        }
        return Theme.State.warning
    }

    private func resetPreviewTransforms() {
        invertPreview = false
        mirrorPreview = false
        flipPreview = false
        rotationDegrees = 0
        previewZoom = 1.0
        previewPan = .zero
    }

    @MainActor
    private func setPreviewWhiteLight(enabled: Bool) {
        guard !previewWhiteInFlight else { return }
        guard canControlPreviewWhiteLight else {
            previewWhiteEnabled = false
            previewWhiteMessage = previewWhiteStatusText
            return
        }

        previewWhiteInFlight = true
        previewWhiteMessage = nil

        Task { @MainActor in
            if orchestratorClient.isRunning {
                do {
                    try await orchestratorClient.setCalibrationPreviewLight(
                        enabled: enabled,
                        level: Int(defaultPreviewWhiteLevel)
                    )
                    previewWhiteEnabled = enabled
                    previewWhiteMessage = enabled
                        ? "White light on via backend."
                        : "White light off."
                } catch {
                    previewWhiteEnabled = false
                    previewWhiteMessage = "White light failed: \(concise(error.localizedDescription))"
                }
            } else {
                if enabled {
                    if lightViewModel.whiteLevel == 0 {
                        lightViewModel.whiteLevel = defaultPreviewWhiteLevel
                    }
                    lightViewModel.turnOnWhite()
                } else {
                    lightViewModel.allOff()
                }
                let failed = !lightViewModel.lastError.isEmpty
                    && lightViewModel.logLines.first?.contains("error") == true
                previewWhiteEnabled = enabled && !failed
                previewWhiteMessage = failed
                    ? "White light failed: \(concise(lightViewModel.lastError))"
                    : (enabled ? "White light on." : "White light off.")
            }
            previewWhiteInFlight = false
        }
    }

    @MainActor
    private func turnOffPreviewWhiteLightIfNeeded() {
        guard previewWhiteEnabled || previewWhiteInFlight else { return }
        previewWhiteInFlight = false
        previewWhiteEnabled = false
        previewWhiteMessage = nil

        if orchestratorClient.isRunning {
            Task { try? await orchestratorClient.setCalibrationPreviewLight(enabled: false) }
        } else if lightViewModel.manualControlsEnabled || lightViewModel.activeChannel == .white {
            lightViewModel.allOff()
        }
    }

    @MainActor
    private func startLiveView() {
        guard liveViewProcess == nil else { return }
        guard !backendBlocksLiveView else {
            liveViewState = .failed("Stop scan or calibration before opening live view.")
            return
        }
        guard !isTemporarilyUnavailable else {
            liveViewState = .failed(unavailableReason ?? "Live view is temporarily unavailable.")
            return
        }
        if let error = sonyCredentialValidationError() {
            liveViewState = .failed(error)
            return
        }

        let frameDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("scanlight-sony-live-view", isDirectory: true)
        do {
            try FileManager.default.createDirectory(
                at: frameDirectory,
                withIntermediateDirectories: true
            )
        } catch {
            liveViewState = .failed("Could not create live-view cache: \(error.localizedDescription)")
            return
        }

        let frameURL = frameDirectory
            .appendingPathComponent("live-view-stream-\(UUID().uuidString).jpg")

        do {
            let command = try orchestratorClient.buildSonyLiveViewStreamCommand(
                settings: orchestratorClient.settingsWithResolvedSonyIP(settings),
                outputURL: frameURL,
                intervalMs: 250,
                timeoutSeconds: 30
            )

            let proc = Process()
            proc.executableURL = command.executableURL
            proc.arguments = command.arguments
            proc.environment = command.environment

            let stdoutPipe = Pipe()
            let stderrPipe = Pipe()
            proc.standardOutput = stdoutPipe
            proc.standardError = stderrPipe

            try proc.run()
            // Close our parent-side write ends so the drainers see EOF when the
            // child exits. Darwin keeps the parent's dup'd write FD open
            // otherwise, which would hang a read-to-EOF forever and leak the
            // monitor task — mirrors OrchestratorClient.runSonyProbeProcess.
            try? stdoutPipe.fileHandleForWriting.close()
            try? stderrPipe.fileHandleForWriting.close()
            liveViewProcess = proc
            isLiveViewActive = true
            liveViewState = .starting
            if canControlPreviewWhiteLight {
                setPreviewWhiteLight(enabled: true)
            }

            liveViewTask = Task {
                await monitorLiveViewProcess(
                    proc,
                    frameURL: frameURL,
                    stdoutPipe: stdoutPipe,
                    stderrPipe: stderrPipe
                )
            }
        } catch PythonToolLocatorError.toolNotFound(let message) {
            liveViewState = .failed(message)
        } catch {
            liveViewState = .failed("Could not open Sony live view: \(error.localizedDescription)")
        }
    }

    private func monitorLiveViewProcess(
        _ proc: Process,
        frameURL: URL,
        stdoutPipe: Pipe,
        stderrPipe: Pipe
    ) async {
        // Drain both pipes incrementally on detached tasks (reusing the shared
        // PipeDrainer, the same primitive OrchestratorClient uses) rather than a
        // bare readDataToEndOfFile: the parent write ends are closed in
        // startLiveView, but a Sony SDK background thread can still hold a dup'd
        // FD, so a blocking read-to-EOF could hang forever and leak this task.
        let stdoutCollector = PipeDrainer()
        let stderrCollector = PipeDrainer()
        let stdoutTask = Task.detached(priority: .utility) {
            stdoutCollector.drain(handle: stdoutPipe.fileHandleForReading)
        }
        let stderrTask = Task.detached(priority: .utility) {
            stderrCollector.drain(handle: stderrPipe.fileHandleForReading)
        }
        defer {
            stdoutTask.cancel()
            stderrTask.cancel()
            // Closing the read ends interrupts any pending availableData read so
            // the drainer tasks exit their loop instead of lingering.
            try? stdoutPipe.fileHandleForReading.close()
            try? stderrPipe.fileHandleForReading.close()
        }

        let firstFrameDeadline = Date(timeIntervalSinceNow: 45)
        var lastModified: Date?
        var sawFrame = false

        while !Task.isCancelled {
            if let modified = modificationDate(frameURL),
               modified != lastModified,
               let imageData = try? Data(contentsOf: frameURL),
               let image = NSImage(data: imageData) {
                lastModified = modified
                sawFrame = true
                await MainActor.run {
                    liveViewImage = image
                    imageRefreshID = UUID()
                    liveViewState = .running("Live view open.")
                }
            }

            if !proc.isRunning {
                let stderr = concise(stderrCollector.collectedString.trimmingCharacters(in: .whitespacesAndNewlines))
                await MainActor.run {
                    if liveViewProcess === proc {
                        liveViewProcess = nil
                        liveViewTask = nil
                        isLiveViewActive = false
                    }
                    if !sawFrame {
                        liveViewState = .failed(stderr.isEmpty
                            ? "Sony live view exited before the first frame."
                            : "Sony live view failed: \(stderr)")
                    } else if !stderr.isEmpty {
                        liveViewState = .failed("Sony live view closed: \(stderr)")
                    } else {
                        liveViewState = .idle
                    }
                }
                return
            }

            if !sawFrame && Date() > firstFrameDeadline {
                await MainActor.run {
                    if liveViewProcess === proc {
                        requestStopLiveViewProcess(proc)
                        liveViewProcess = nil
                        liveViewTask = nil
                        isLiveViewActive = false
                    }
                    liveViewState = .failed("Timed out opening Sony live view after 45 seconds. Check camera power, SDK remote mode, and whether another Sony app is connected.")
                }
                return
            }

            try? await Task.sleep(nanoseconds: 120_000_000)
        }
    }

    @MainActor
    private func stopLiveView(clearImage: Bool, state: SonyLiveViewState) {
        liveViewTask?.cancel()
        liveViewTask = nil
        if let liveViewProcess {
            requestStopLiveViewProcess(liveViewProcess)
        }
        liveViewProcess = nil
        isLiveViewActive = false
        liveViewState = state
        turnOffPreviewWhiteLightIfNeeded()
        if clearImage {
            liveViewImage = nil
            imageRefreshID = UUID()
        }
    }

    @MainActor
    private func requestStopLiveViewProcess(_ proc: Process) {
        if proc.isRunning {
            proc.terminate()
        }
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 1_000_000_000)
            if proc.isRunning {
                kill(proc.processIdentifier, SIGKILL)
            }
        }
    }

    private func modificationDate(_ url: URL) -> Date? {
        let attrs = try? FileManager.default.attributesOfItem(atPath: url.path)
        return attrs?[.modificationDate] as? Date
    }

    private func concise(_ text: String) -> String {
        guard text.count > 240 else { return text }
        return "...\(text.suffix(240))"
    }

    private func sonyCredentialValidationError() -> String? {
        // USB connects without IP / Access Auth; only gate those for Wi-Fi.
        if !settings.usesSonyUSB {
            if missing(settings.sonyIpAddress) {
                return "Enter the Sony IP address first."
            }
            if missing(settings.sonyUser) {
                return "Enter the Sony Access Auth user first."
            }
            if missing(settings.sonyPassword) {
                return "Enter the Sony Access Auth password first."
            }
        }
        return nil
    }

    private func missing(_ value: String?) -> Bool {
        value?.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ?? true
    }
}

private struct TransformedSonyLiveViewSurface: View {
    let image: NSImage?
    let isInverted: Bool
    let isMirrored: Bool
    let isFlipped: Bool
    let rotationDegrees: Int
    let zoom: CGFloat
    @Binding var pan: CGSize
    @GestureState private var dragDelta: CGSize = .zero

    var body: some View {
        GeometryReader { geo in
            ZStack {
                RoundedRectangle(cornerRadius: Theme.Radius.control, style: .continuous)
                    .fill(Color.black.opacity(0.22))
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
                        .scaleEffect(x: isMirrored ? -1 : 1, y: isFlipped ? -1 : 1)
                        .rotationEffect(.degrees(Double(normalizedRotationDegrees)))
                        .position(x: displayRect.midX, y: displayRect.midY)
                } else {
                    VStack(spacing: Theme.Space.sm) {
                        Image(systemName: "camera.viewfinder")
                            .font(.system(size: 28, weight: .medium))
                            .foregroundStyle(.secondary)
                            .accessibilityHidden(true)
                        Text("No live-view frame")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }

                Rectangle()
                    .fill(Color.clear)
                    .contentShape(Rectangle())
                    .gesture(
                        DragGesture(minimumDistance: 4)
                            .updating($dragDelta) { value, state, _ in
                                state = value.translation
                            }
                            .onEnded { value in
                                guard image != nil else { return }
                                let sourceSize = image.map(pixelSize(for:)) ?? .zero
                                let baseRect = fittedRect(
                                    imageSize: orientedImageSize(sourceSize),
                                    containerSize: geo.size
                                )
                                pan = clampedPan(
                                    CGSize(
                                        width: pan.width + value.translation.width,
                                        height: pan.height + value.translation.height
                                    ),
                                    baseRect: baseRect,
                                    containerSize: geo.size,
                                    zoom: zoom
                                )
                            }
                    )
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
            .onChange(of: rotationDegrees) { _ in pan = .zero }
            .onChange(of: isMirrored) { _ in pan = .zero }
            .onChange(of: isFlipped) { _ in pan = .zero }
        }
    }

    private var activePan: CGSize {
        CGSize(width: pan.width + dragDelta.width, height: pan.height + dragDelta.height)
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
