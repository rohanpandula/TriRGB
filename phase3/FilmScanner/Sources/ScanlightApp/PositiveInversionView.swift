import AppKit
import Foundation
import SwiftUI
import UniformTypeIdentifiers

// MARK: - View model

@MainActor
final class PositiveInversionViewModel: ObservableObject {
    @Published var selectedFiles: [URL] = []
    @Published var outputFolder: URL?
    @Published var isRunning = false
    @Published var statusText = "Idle"
    @Published var lastError: String?
    @Published var result: TripletPositiveProcessResult?
    @Published var logLines: [String] = []
    @Published var selectedLook: PositiveRenderLook = .standard
    @Published var isPreviewRunning = false
    @Published var previewResult: TripletPreviewProcessResult?
    @Published var previewImage: NSImage?
    @Published var selectedBaseRegion: TripletBaseRegionInput?

    /// Holds the running python inversion process so it can be terminated on
    /// cancel / view-disappear instead of orphaning a child on app quit.
    private let runningProcess = TripletProcessBox()
    private var activeTask: Task<Void, Never>?

    nonisolated static var allowedInputContentTypes: [UTType] {
        [
            .tiff,
            UTType(filenameExtension: "arw") ?? .data,
            UTType(filenameExtension: "raf") ?? .data,
            UTType(filenameExtension: "dng") ?? .data,
            UTType(filenameExtension: "raw") ?? .data,
        ]
    }

    var canRun: Bool {
        selectedFiles.count == 3
            && outputFolder != nil
            && !isRunning
            && !isPreviewRunning
    }

    var canGeneratePreview: Bool {
        selectedFiles.count == 3 && !isRunning && !isPreviewRunning
    }

    func chooseFiles() {
        let panel = NSOpenPanel()
        panel.title = "Choose three R/G/B files (named _R, _G, _B)"
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = true
        panel.allowedContentTypes = Self.allowedInputContentTypes
        if panel.runModal() == .OK {
            selectedFiles = Array(panel.urls.prefix(3))
            if outputFolder == nil, let parent = selectedFiles.first?.deletingLastPathComponent() {
                outputFolder = parent.appendingPathComponent("ScanlightPositive")
            }
            result = nil
            lastError = nil
            previewResult = nil
            previewImage = nil
            selectedBaseRegion = nil
            appendLog("Selected \(selectedFiles.count) input file(s).")
        }
    }

    func chooseOutputFolder() {
        let panel = NSOpenPanel()
        panel.title = "Choose output folder"
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        if panel.runModal() == .OK {
            outputFolder = panel.url
            lastError = nil
            appendLog("Output folder set to \(panel.url?.path ?? "").")
        }
    }

    func generatePreview() async {
        guard selectedFiles.count == 3 else {
            lastError = "Choose exactly three files from one RGB-lit frame."
            return
        }

        isPreviewRunning = true
        statusText = "Generating preview..."
        lastError = nil
        previewResult = nil
        previewImage = nil
        selectedBaseRegion = nil
        appendLog("Generating visual base-patch preview.")

        do {
            let folder = outputFolder ?? FileManager.default.temporaryDirectory
            let preview = try await TripletPositiveRunner.preview(
                inputFiles: selectedFiles,
                outputFolder: folder,
                look: selectedLook,
                register: { [runningProcess] in runningProcess.store($0) }
            )
            previewResult = preview
            if let loadedImage = NSImage(contentsOfFile: preview.previewPath) {
                previewImage = loadedImage
                statusText = "Preview ready"
                appendLog("Preview ready. Click or drag clean film base.")
            } else {
                let message = "Preview was generated, but macOS could not display it: \(preview.previewPath)"
                lastError = message
                statusText = "Preview failed"
                appendLog(message)
            }
        } catch {
            let message = (error as? LocalizedError)?.errorDescription ?? String(describing: error)
            lastError = message
            statusText = "Failed"
            appendLog("Preview failed: \(message)")
        }

        isPreviewRunning = false
    }

    func renderPositive() async {
        guard selectedFiles.count == 3 else {
            lastError = "Choose exactly three files from one RGB-lit frame."
            return
        }
        guard let outputFolder else {
            lastError = "Choose an output folder."
            return
        }

        isRunning = true
        statusText = "Rendering..."
        lastError = nil
        result = nil
        appendLog("Starting triplet inversion with \(selectedLook.rawValue) curve.")

        do {
            let rendered = try await TripletPositiveRunner.run(
                inputFiles: selectedFiles,
                outputFolder: outputFolder,
                look: selectedLook,
                baseRegion: selectedBaseRegion,
                register: { [runningProcess] in runningProcess.store($0) }
            )
            result = rendered
            statusText = "Rendered positive"
            appendLog("Detected \(rendered.mappingSummary).")
            appendLog("Positive written to \(rendered.positivePath).")
        } catch {
            let message = (error as? LocalizedError)?.errorDescription ?? String(describing: error)
            lastError = message
            statusText = "Failed"
            appendLog("Failed: \(message)")
        }

        isRunning = false
    }

    func setBaseRegion(_ region: TripletBaseRegionInput) {
        selectedBaseRegion = region
        appendLog("Selected base patch x \(region.x) y \(region.y) \(region.w)x\(region.h).")
    }

    func clearBaseRegion() {
        selectedBaseRegion = nil
        appendLog("Cleared manual base patch; auto base will be used.")
    }

    func openPositive() {
        guard let path = result?.positivePath else { return }
        NSWorkspace.shared.open(URL(fileURLWithPath: path))
    }

    func revealPositive() {
        guard let path = result?.positivePath else { return }
        NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: path)])
    }

    func startPreview() {
        activeTask?.cancel()
        activeTask = Task { [weak self] in await self?.generatePreview() }
    }

    func startRender() {
        activeTask?.cancel()
        activeTask = Task { [weak self] in await self?.renderPositive() }
    }

    /// Cancel any in-flight preview/render and terminate the python child so it
    /// is never orphaned on tab-switch or app shutdown. Wired to `onDisappear`.
    func cancel() {
        activeTask?.cancel()
        activeTask = nil
        runningProcess.terminate()
        if isRunning || isPreviewRunning {
            isRunning = false
            isPreviewRunning = false
            statusText = "Cancelled"
            appendLog("Cancelled in-flight render.")
        }
    }

    private func appendLog(_ text: String) {
        let formatter = DateFormatter()
        formatter.dateFormat = "HH:mm:ss"
        logLines.append("\(formatter.string(from: Date()))  \(text)")
        if logLines.count > 200 {
            logLines.removeFirst(logLines.count - 200)
        }
    }
}

// MARK: - Process runner

enum PositiveRenderLook: String, CaseIterable, Identifiable {
    case flat
    case standard
    case punchy
    case filmic

    var id: String { rawValue }

    var label: String {
        switch self {
        case .flat: return "Flat"
        case .standard: return "Standard"
        case .punchy: return "Punchy"
        case .filmic: return "Filmic"
        }
    }

    var help: String {
        switch self {
        case .flat:
            return "Gentle workprint with minimal added contrast."
        case .standard:
            return "Deeper automatic black/white points plus an S-curve."
        case .punchy:
            return "Strongest automatic curve for quick review."
        case .filmic:
            return "Filmic sigmoid curve for a finished review look."
        }
    }
}

struct TripletBaseRegionInput: Equatable {
    let x: Int
    let y: Int
    let w: Int
    let h: Int

    var cliValue: String {
        "\(x),\(y),\(w),\(h)"
    }
}

struct TripletPositiveProcessResult: Decodable {
    struct Mapping: Decodable, Identifiable {
        var id: String { channel }
        let channel: String
        let channelIndex: Int
        let path: String
        let score: Double
        let confidence: Double

        enum CodingKeys: String, CodingKey {
            case channel
            case channelIndex = "channel_index"
            case path
            case score
            case confidence
        }
    }

    struct BaseRegion: Decodable {
        let x: Int
        let y: Int
        let w: Int
        let h: Int
        let baseRgb: [Double]
        let uniformityCv: Double
        let source: String

        enum CodingKeys: String, CodingKey {
            case x, y, w, h
            case baseRgb = "base_rgb"
            case uniformityCv = "uniformity_cv"
            case source
        }
    }

    let positivePath: String
    let compositePath: String
    let reportPath: String
    let mapping: [Mapping]
    let baseRegion: BaseRegion

    enum CodingKeys: String, CodingKey {
        case positivePath = "positive_path"
        case compositePath = "composite_path"
        case reportPath = "report_path"
        case mapping
        case baseRegion = "base_region"
    }

    var mappingSummary: String {
        mapping
            .sorted { $0.channelIndex < $1.channelIndex }
            .map { "\($0.channel)=\(URL(fileURLWithPath: $0.path).lastPathComponent)" }
            .joined(separator: ", ")
    }
}

struct TripletPreviewProcessResult: Decodable {
    let previewPath: String
    let fullWidth: Int
    let fullHeight: Int
    let previewWidth: Int
    let previewHeight: Int
    let mapping: [TripletPositiveProcessResult.Mapping]
    let autoBaseRegion: TripletPositiveProcessResult.BaseRegion

    enum CodingKeys: String, CodingKey {
        case previewPath = "preview_path"
        case fullWidth = "full_width"
        case fullHeight = "full_height"
        case previewWidth = "preview_width"
        case previewHeight = "preview_height"
        case mapping
        case autoBaseRegion = "auto_base_region"
    }
}

enum TripletPositiveRunnerError: LocalizedError {
    case repositoryRootNotFound
    case pythonNotFound
    case processFailed(Int32, String)
    case invalidJSON(String)

    var errorDescription: String? {
        switch self {
        case .repositoryRootNotFound:
            return "Could not locate this checkout. Run the app from the project folder."
        case .pythonNotFound:
            return "Could not find a python3 with numpy, tifffile, rawpy, and Pillow. Set SCANLIGHT_PYTHON to your interpreter, or launch the app from a shell where the project Python is on PATH."
        case .processFailed(let code, let message):
            return "Triplet inversion failed (exit \(code)): \(message)"
        case .invalidJSON(let output):
            return "Triplet inversion finished but returned unreadable output: \(output)"
        }
    }
}

/// Thread-safe holder for the currently-running inversion `Process` so the
/// MainActor view model can terminate it on cancel / view-disappear / app
/// shutdown from any thread, without orphaning a python child.
final class TripletProcessBox: @unchecked Sendable {
    private let lock = NSLock()
    private var process: Process?

    func store(_ process: Process?) {
        lock.lock(); self.process = process; lock.unlock()
    }

    func terminate() {
        lock.lock(); let process = self.process; lock.unlock()
        guard let process, process.isRunning else { return }
        process.terminate()
    }
}

enum TripletPositiveRunner {
    static func run(
        inputFiles: [URL],
        outputFolder: URL,
        look: PositiveRenderLook = .standard,
        baseRegion: TripletBaseRegionInput? = nil,
        register: @escaping @Sendable (Process?) -> Void = { _ in }
    ) async throws -> TripletPositiveProcessResult {
        var arguments = [
            "-m", "rgb_composite.triplet_positive",
            "--out-dir", outputFolder.path,
            "--look", look.rawValue,
        ]
        if let baseRegion {
            arguments += ["--base-region", baseRegion.cliValue]
        }
        return try await runTripletProcess(
            arguments: arguments + inputFiles.map(\.path),
            register: register
        )
    }

    static func preview(
        inputFiles: [URL],
        outputFolder: URL,
        look: PositiveRenderLook = .standard,
        register: @escaping @Sendable (Process?) -> Void = { _ in }
    ) async throws -> TripletPreviewProcessResult {
        let previewURL = outputFolder
            .appendingPathComponent("scanlight-triplet-preview-\(UUID().uuidString).png")
        return try await runTripletProcess(
            arguments: [
                "-m", "rgb_composite.triplet_positive",
                "--out-dir", outputFolder.path,
                "--look", look.rawValue,
                "--preview-out", previewURL.path,
            ] + inputFiles.map(\.path),
            register: register
        )
    }

    private static func runTripletProcess<T: Decodable>(
        arguments: [String],
        register: @escaping @Sendable (Process?) -> Void = { _ in }
    ) async throws -> T {
        try await Task.detached(priority: .userInitiated) {
            let root = try repositoryRoot()
            let python = try pythonExecutableWithCache()  // F9: cached after first probe
            let pythonPath = [
                root.appendingPathComponent("phase2/rgb-composite").path,
                root.appendingPathComponent("phase2/c41-core").path,
            ].joined(separator: ":")

            var environment = ProcessInfo.processInfo.environment
            let existingPythonPath = environment["PYTHONPATH"].map { ":\($0)" } ?? ""
            environment["PYTHONPATH"] = pythonPath + existingPythonPath
            environment["PATH"] = [
                "/opt/homebrew/bin",
                "/opt/homebrew/opt/python@3.14/bin",
                "/usr/local/bin",
                "/usr/bin",
                "/bin",
                environment["PATH"] ?? "",
            ].joined(separator: ":")

            let process = Process()
            process.executableURL = python
            process.currentDirectoryURL = root
            process.environment = environment
            process.arguments = arguments

            let stdout = Pipe()
            let stderr = Pipe()
            process.standardOutput = stdout
            process.standardError = stderr

            try process.run()
            // Publish the running process so the caller can terminate it on
            // cancel / disappear, then clear the slot on every exit path.
            register(process)
            defer { register(nil) }

            // Close our parent-side write ends so the drainers observe EOF when
            // the child (and only the child) closes its end. Without this a
            // read-to-EOF can hang forever on Darwin. Mirrors
            // OrchestratorClient.runSonyProbeProcess.
            try? stdout.fileHandleForWriting.close()
            try? stderr.fileHandleForWriting.close()

            // Drain both pipes incrementally on detached tasks (reusing the
            // shared PipeDrainer) so the child never blocks on a full pipe and
            // we never call the deadlock-prone readDataToEndOfFile.
            let stdoutCollector = PipeDrainer()
            let stderrCollector = PipeDrainer()
            let stdoutTask = Task.detached(priority: .userInitiated) {
                stdoutCollector.drain(handle: stdout.fileHandleForReading)
            }
            let stderrTask = Task.detached(priority: .userInitiated) {
                stderrCollector.drain(handle: stderr.fileHandleForReading)
            }

            process.waitUntilExit()

            // Bounded grace window for in-flight output, then stop the drainers.
            // A stuck pipe (inherited FD) must never pin this task.
            let collectionDeadline = Date(timeIntervalSinceNow: 1.0)
            while !(stdoutCollector.isFinished && stderrCollector.isFinished),
                  Date() < collectionDeadline {
                try? await Task.sleep(nanoseconds: 25_000_000)
            }
            stdoutTask.cancel()
            stderrTask.cancel()
            try? stdout.fileHandleForReading.close()
            try? stderr.fileHandleForReading.close()

            let stdoutText = stdoutCollector.collectedString
            let stderrText = stderrCollector.collectedString

            guard process.terminationStatus == 0 else {
                let message = stderrText.isEmpty ? stdoutText : stderrText
                throw TripletPositiveRunnerError.processFailed(process.terminationStatus, message.trimmingCharacters(in: .whitespacesAndNewlines))
            }

            guard let data = stdoutText.data(using: .utf8) else {
                throw TripletPositiveRunnerError.invalidJSON(stdoutText)
            }
            do {
                return try JSONDecoder().decode(T.self, from: data)
            } catch {
                throw TripletPositiveRunnerError.invalidJSON(stdoutText)
            }
        }.value
    }

    private static func repositoryRoot() throws -> URL {
        let fileManager = FileManager.default
        let binaryDir = URL(fileURLWithPath: CommandLine.arguments[0]).deletingLastPathComponent()
        let cwd = URL(fileURLWithPath: fileManager.currentDirectoryPath)
        let candidates = [
            cwd,
            cwd.appendingPathComponent(".."),
            cwd.appendingPathComponent("../.."),
            cwd.appendingPathComponent("../../.."),
            cwd.appendingPathComponent("../../../.."),
            binaryDir,
            binaryDir.appendingPathComponent("../../.."),
            binaryDir.appendingPathComponent("../../../../"),
            binaryDir.appendingPathComponent("../../../../../"),
            binaryDir.appendingPathComponent("../../../../../../"),
        ]
        for candidate in candidates {
            let standardized = candidate.standardizedFileURL
            let marker = standardized
                .appendingPathComponent("phase2/rgb-composite/rgb_composite/triplet_positive.py")
                .path
            if fileManager.fileExists(atPath: marker) {
                return standardized
            }
        }
        throw TripletPositiveRunnerError.repositoryRootNotFound
    }

    private static func pythonExecutable() throws -> URL {
        let fileManager = FileManager.default
        let env = ProcessInfo.processInfo.environment

        // 1. Explicit override wins: `SCANLIGHT_PYTHON=/path/to/python3`. This
        //    is the supported way to point the app at a specific interpreter
        //    (e.g. a project venv) without baking any path into the binary. An
        //    override that doesn't work fails loudly rather than silently
        //    falling through to some other interpreter.
        if let override = env["SCANLIGHT_PYTHON"], !override.isEmpty {
            let url = URL(fileURLWithPath: override).standardizedFileURL
            if fileManager.isExecutableFile(atPath: url.path),
               pythonSupportsTripletStack(url) {
                return url
            }
            throw TripletPositiveRunnerError.pythonNotFound
        }

        // 2. PATH search — the common case (homebrew / pyenv / venv on PATH).
        let pathEnv = env["PATH"] ?? ""
        let interpreterNames = ["python3", "python3.14", "python3.13", "python3.12", "python3.11"]
        let pathCandidates = pathEnv
            .split(separator: ":")
            .flatMap { dir in
                interpreterNames.map {
                    URL(fileURLWithPath: String(dir)).appendingPathComponent($0)
                }
            }

        // 3. Per-user pip-install dir derived from $HOME at RUNTIME (never a
        //    hardcoded home path), then standard system locations.
        var userCandidates: [URL] = []
        if let home = env["HOME"], !home.isEmpty {
            let localBin = URL(fileURLWithPath: home).appendingPathComponent(".local/bin")
            userCandidates = interpreterNames.map { localBin.appendingPathComponent($0) }
        }
        let systemCandidates = [
            "/opt/homebrew/bin/python3",
            "/opt/homebrew/opt/python@3.14/bin/python3.14",
            "/opt/homebrew/bin/python3.14",
            "/usr/local/bin/python3",
            "/usr/bin/python3",
        ].map(URL.init(fileURLWithPath:))

        var seen = Set<String>()
        for candidate in pathCandidates + userCandidates + systemCandidates {
            let standardized = candidate.standardizedFileURL
            guard seen.insert(standardized.path).inserted else { continue }
            if fileManager.isExecutableFile(atPath: standardized.path),
               pythonSupportsTripletStack(standardized) {
                return standardized
            }
        }
        throw TripletPositiveRunnerError.pythonNotFound
    }

    // MARK: - Python probe cache (F9)

    /// Thread-safe lock for the probe cache.
    private static let probeCacheLock = NSLock()
    /// Resolved winning interpreter URL, cached after the first successful probe.
    /// Nil means "not yet resolved or explicitly reset". The cache is per-session:
    /// a different interpreter cannot appear at the same path during one run.
    private static var cachedPythonExecutable: URL? = nil

    /// Reset the cached probe result. Intended for tests that need to re-probe
    /// (e.g. after injecting a stub python via SCANLIGHT_PYTHON override).
    static func resetPythonExecutableCache() {
        probeCacheLock.lock()
        cachedPythonExecutable = nil
        probeCacheLock.unlock()
    }

    /// Return the cached resolved Python URL without running a new probe, or nil
    /// if no successful probe has completed yet.
    static func cachedPythonURL() -> URL? {
        probeCacheLock.lock(); defer { probeCacheLock.unlock() }
        return cachedPythonExecutable
    }

    /// Resolve the Python executable, using the per-session cache so the import
    /// probe runs at most once regardless of how many times pythonExecutable() is
    /// called (e.g. on every render/preview click). F9.
    private static func pythonExecutableWithCache() throws -> URL {
        // Fast path: cache already populated.
        probeCacheLock.lock()
        if let cached = cachedPythonExecutable {
            probeCacheLock.unlock()
            return cached
        }
        probeCacheLock.unlock()

        // Slow path: run the probe, then populate the cache.
        let resolved = try pythonExecutable()
        probeCacheLock.lock()
        // Double-check: another concurrent call may have cached a result while we
        // were probing. Both results are valid; keep whichever arrived first.
        if cachedPythonExecutable == nil {
            cachedPythonExecutable = resolved
        }
        probeCacheLock.unlock()
        return resolved
    }

    private static func pythonSupportsTripletStack(_ executable: URL) -> Bool {
        let process = Process()
        process.executableURL = executable
        process.arguments = [
            "-c",
            "import numpy, tifffile, rawpy; from PIL import Image",
        ]
        process.standardOutput = Pipe()
        process.standardError = Pipe()

        do {
            try process.run()
            process.waitUntilExit()
            return process.terminationStatus == 0
        } catch {
            return false
        }
    }
}

// MARK: - View

struct PositiveInversionView: View {
    @ObservedObject var viewModel: PositiveInversionViewModel

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Theme.Space.section) {
                GroupBox(label: Text("Triplet Input")) {
                    VStack(alignment: .leading, spacing: Theme.Space.md) {
                        Banner(kind: .info, text: "Choose the three files from one RGB-lit frame. _R/_G/_B filename suffixes are preferred; generic camera names are auto-detected from RAW channel dominance when confidence is high. The matching channels are isolated and a positive TIFF is written.")

                        HStack(spacing: Theme.Space.md) {
                            Button("Choose Files...") {
                                viewModel.chooseFiles()
                            }
                            .accessibilityIdentifier(AccessibilityID.invertPickFilesButton)

                            Text("\(viewModel.selectedFiles.count)/3 selected")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                            Spacer(minLength: 0)
                        }

                        VStack(alignment: .leading, spacing: Theme.Space.xs) {
                            ForEach(Array(viewModel.selectedFiles.enumerated()), id: \.offset) { index, url in
                                HStack(spacing: Theme.Space.sm) {
                                    Text("\(index + 1)")
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                        .frame(width: 18, alignment: .leading)
                                    Text(url.lastPathComponent)
                                        .lineLimit(1)
                                        .truncationMode(.middle)
                                    Spacer(minLength: 0)
                                }
                            }
                            if viewModel.selectedFiles.isEmpty {
                                Text("No files selected")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                        }
                    }
                }

                GroupBox(label: Text("Output")) {
                    VStack(alignment: .leading, spacing: Theme.Space.md) {
                        HStack(spacing: Theme.Space.md) {
                            Button("Choose Folder...") {
                                viewModel.chooseOutputFolder()
                            }
                            .accessibilityIdentifier(AccessibilityID.invertPickOutputButton)

                            Text(viewModel.outputFolder?.path ?? "No output folder selected")
                                .font(.system(.body, design: .monospaced))
                                .lineLimit(1)
                                .truncationMode(.middle)
                            Spacer(minLength: 0)
                        }
                    }
                }

                GroupBox(label: Text("Balance and Curve")) {
                    VStack(alignment: .leading, spacing: Theme.Space.md) {
                        HStack(spacing: Theme.Space.md) {
                            Text("Curve")
                                .font(.subheadline)
                                .foregroundStyle(.secondary)
                                .frame(width: 96, alignment: .leading)
                            Picker("Curve", selection: $viewModel.selectedLook) {
                                ForEach(PositiveRenderLook.allCases) { look in
                                    Text(look.label).tag(look)
                                }
                            }
                            .pickerStyle(.segmented)
                            .labelsHidden()
                            Text(viewModel.selectedLook.help)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                            Spacer(minLength: 0)
                        }

                        Divider()

                        Text("The base patch is the white-balance source. Generate a preview below, then click or drag clean unexposed film base. If no patch is selected, the renderer auto-picks from the frame edge.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }

                GroupBox(label: Text("Base Patch Preview")) {
                    VStack(alignment: .leading, spacing: Theme.Space.md) {
                        HStack(spacing: Theme.Space.md) {
                            Button(viewModel.isPreviewRunning ? "Generating..." : "Generate Preview") {
                                viewModel.startPreview()
                            }
                            .disabled(!viewModel.canGeneratePreview)

                            if viewModel.isPreviewRunning {
                                ProgressView().controlSize(.small)
                            }

                            if let region = viewModel.selectedBaseRegion {
                                Chip(text: "selected x \(region.x) y \(region.y) \(region.w)x\(region.h)", tint: Theme.State.success)
                                Button("Clear Selection") {
                                    viewModel.clearBaseRegion()
                                }
                                .buttonStyle(.bordered)
                            } else if let auto = viewModel.previewResult?.autoBaseRegion {
                                Chip(text: "auto x \(auto.x) y \(auto.y) \(auto.w)x\(auto.h)", tint: Theme.State.info)
                            }
                            Spacer(minLength: 0)
                        }

                        if let image = viewModel.previewImage, let preview = viewModel.previewResult {
                            Banner(kind: .info, text: "Pick the film base on the image below. Click once for a small patch, or drag a box if the clean border is narrow.")
                            SelectableTripletPreview(
                                image: image,
                                preview: preview,
                                selection: viewModel.selectedBaseRegion,
                                onSelection: { region in viewModel.setBaseRegion(region) }
                            )
                            .frame(height: 460)
                        } else if let preview = viewModel.previewResult {
                            Banner(kind: .danger, text: "Preview file exists but could not be displayed: \(preview.previewPath)")
                        } else {
                            Text("Generate a preview after choosing the three files. Click the image for a small base patch, or drag over a clean strip of unexposed film border.")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                    }
                }

                GroupBox(label: Text("Render")) {
                    VStack(alignment: .leading, spacing: Theme.Space.md) {
                        HStack(spacing: Theme.Space.md) {
                            Button("Render Positive") {
                                viewModel.startRender()
                            }
                            .buttonStyle(.borderedProminent)
                            .disabled(!viewModel.canRun)
                            .accessibilityIdentifier(AccessibilityID.invertRunButton)

                            if viewModel.isRunning {
                                ProgressView().controlSize(.small)
                            }

                            HStack(spacing: Theme.Space.sm) {
                                StatusDot(color: statusColor)
                                Text(viewModel.statusText)
                                    .font(.caption)
                                    .foregroundStyle(statusColor)
                                    .accessibilityIdentifier(AccessibilityID.invertStatusLabel)
                                    .accessibilityValue(viewModel.statusText)
                            }
                            Spacer(minLength: 0)
                        }

                        if let error = viewModel.lastError {
                            Banner(kind: .danger, text: error)
                        }
                    }
                }

                if let result = viewModel.result {
                    resultGroup(result)
                }

                GroupBox(label: Text("Log")) {
                    ScrollView {
                        VStack(alignment: .leading, spacing: Theme.Space.xs) {
                            ForEach(Array(viewModel.logLines.enumerated()), id: \.offset) { _, line in
                                Text(line)
                                    .font(.system(.caption, design: .monospaced))
                                    .foregroundStyle(.secondary)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                            }
                            if viewModel.logLines.isEmpty {
                                Text("No log entries yet")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                        }
                    }
                    .frame(minHeight: 96, maxHeight: 180)
                    .accessibilityIdentifier(AccessibilityID.invertLogScrollView)
                }
            }
            .padding(Theme.Space.xl)
        }
        .groupBoxStyle(PanelGroupBoxStyle())
        .onDisappear { viewModel.cancel() }
    }

    private var statusColor: Color {
        if viewModel.isRunning { return Theme.State.info }
        if viewModel.lastError != nil { return Theme.State.danger }
        if viewModel.result != nil { return Theme.State.success }
        return Theme.State.idle
    }

    private func resultGroup(_ result: TripletPositiveProcessResult) -> some View {
        GroupBox(label: Text("Result")) {
            VStack(alignment: .leading, spacing: Theme.Space.md) {
                VStack(alignment: .leading, spacing: Theme.Space.xs) {
                    ForEach(result.mapping.sorted { $0.channelIndex < $1.channelIndex }) { item in
                        HStack(spacing: Theme.Space.md) {
                            Chip(text: item.channel, tint: channelTint(item.channel))
                            Text(URL(fileURLWithPath: item.path).lastPathComponent)
                                .lineLimit(1)
                                .truncationMode(.middle)
                            Spacer(minLength: 0)
                            Text(String(format: "%.1fx", item.confidence))
                                .font(.caption.monospacedDigit())
                                .foregroundStyle(.secondary)
                        }
                    }
                }

                Divider()

                LabeledValue(label: "Base patch") {
                    Text("\(result.baseRegion.source)  x \(result.baseRegion.x) y \(result.baseRegion.y) \(result.baseRegion.w)x\(result.baseRegion.h)")
                        .foregroundStyle(.secondary)
                }

                LabeledValue(label: "Positive") {
                    Text(result.positivePath)
                        .accessibilityIdentifier(AccessibilityID.invertOutputPathLabel)
                        .accessibilityValue(result.positivePath)
                        .lineLimit(1)
                        .truncationMode(.middle)
                }

                HStack(spacing: Theme.Space.md) {
                    Button("Open Positive") {
                        viewModel.openPositive()
                    }
                    Button("Reveal in Finder") {
                        viewModel.revealPositive()
                    }
                }
            }
        }
    }

    private func channelTint(_ channel: String) -> Color {
        switch channel {
        case "R": return Theme.Channel.red
        case "G": return Theme.Channel.green
        case "B": return Theme.Channel.blue
        default: return Theme.State.idle
        }
    }

    private func numericField(_ label: String, text: Binding<String>) -> some View {
        HStack(spacing: Theme.Space.xs) {
            Text(label)
                .font(.caption)
                .foregroundStyle(.secondary)
            TextField("0", text: text)
                .textFieldStyle(.roundedBorder)
                .frame(width: 82)
        }
    }
}

private struct SelectableTripletPreview: View {
    let image: NSImage
    let preview: TripletPreviewProcessResult
    let selection: TripletBaseRegionInput?
    let onSelection: (TripletBaseRegionInput) -> Void

    @State private var dragStart: CGPoint?
    @State private var dragCurrent: CGPoint?

    var body: some View {
        GeometryReader { proxy in
            let fitted = fittedRect(in: proxy.size)
            ZStack(alignment: .topLeading) {
                RoundedRectangle(cornerRadius: Theme.Radius.control, style: .continuous)
                    .fill(Color.black.opacity(0.28))

                Image(nsImage: image)
                    .resizable()
                    .interpolation(.high)
                    .frame(width: fitted.width, height: fitted.height)
                    .position(x: fitted.midX, y: fitted.midY)

                if let selectionRect = displayRect(for: selection, fitted: fitted) {
                    Rectangle()
                        .strokeBorder(Theme.accent, lineWidth: 2)
                        .background(Theme.accent.opacity(0.12))
                        .frame(width: selectionRect.width, height: selectionRect.height)
                        .position(x: selectionRect.midX, y: selectionRect.midY)
                }

                if let dragRect = currentDragRect(fitted: fitted) {
                    Rectangle()
                        .strokeBorder(Theme.accent, style: StrokeStyle(lineWidth: 2, dash: [6, 4]))
                        .background(Theme.accent.opacity(0.08))
                        .frame(width: dragRect.width, height: dragRect.height)
                        .position(x: dragRect.midX, y: dragRect.midY)
                }

                Color.clear
                    .contentShape(Rectangle())
                    .gesture(selectionGesture(fitted: fitted))
            }
            .clipShape(RoundedRectangle(cornerRadius: Theme.Radius.control, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: Theme.Radius.control, style: .continuous)
                    .strokeBorder(Theme.panelStroke, lineWidth: 1)
            )
        }
    }

    private func selectionGesture(fitted: CGRect) -> some Gesture {
        DragGesture(minimumDistance: 0)
            .onChanged { value in
                dragStart = dragStart ?? clamped(value.startLocation, to: fitted)
                dragCurrent = clamped(value.location, to: fitted)
            }
            .onEnded { value in
                let start = clamped(value.startLocation, to: fitted)
                let end = clamped(value.location, to: fitted)
                let region: TripletBaseRegionInput
                if hypot(end.x - start.x, end.y - start.y) < 8 {
                    region = centeredRegion(at: start, fitted: fitted, size: 128)
                } else {
                    region = draggedRegion(from: start, to: end, fitted: fitted)
                }
                dragStart = nil
                dragCurrent = nil
                onSelection(region)
            }
    }

    private func fittedRect(in container: CGSize) -> CGRect {
        let imageSize = CGSize(
            width: max(1, preview.previewWidth),
            height: max(1, preview.previewHeight)
        )
        let scale = min(container.width / imageSize.width, container.height / imageSize.height)
        let width = imageSize.width * scale
        let height = imageSize.height * scale
        return CGRect(
            x: (container.width - width) / 2,
            y: (container.height - height) / 2,
            width: width,
            height: height
        )
    }

    private func displayRect(for region: TripletBaseRegionInput?, fitted: CGRect) -> CGRect? {
        guard let region else { return nil }
        let x = fitted.minX + CGFloat(region.x) / CGFloat(max(1, preview.fullWidth)) * fitted.width
        let y = fitted.minY + CGFloat(region.y) / CGFloat(max(1, preview.fullHeight)) * fitted.height
        let width = CGFloat(region.w) / CGFloat(max(1, preview.fullWidth)) * fitted.width
        let height = CGFloat(region.h) / CGFloat(max(1, preview.fullHeight)) * fitted.height
        return CGRect(x: x, y: y, width: max(4, width), height: max(4, height))
    }

    private func currentDragRect(fitted: CGRect) -> CGRect? {
        guard let dragStart, let dragCurrent else { return nil }
        let start = clamped(dragStart, to: fitted)
        let end = clamped(dragCurrent, to: fitted)
        guard hypot(end.x - start.x, end.y - start.y) >= 8 else {
            return displayRect(for: centeredRegion(at: start, fitted: fitted, size: 128), fitted: fitted)
        }
        return CGRect(
            x: min(start.x, end.x),
            y: min(start.y, end.y),
            width: max(4, abs(end.x - start.x)),
            height: max(4, abs(end.y - start.y))
        )
    }

    private func centeredRegion(
        at point: CGPoint,
        fitted: CGRect,
        size requestedSize: Int
    ) -> TripletBaseRegionInput {
        let full = fullPoint(from: point, fitted: fitted)
        let width = min(requestedSize, max(1, preview.fullWidth))
        let height = min(requestedSize, max(1, preview.fullHeight))
        let x = clamp(Int(round(full.x)) - width / 2, min: 0, max: max(0, preview.fullWidth - width))
        let y = clamp(Int(round(full.y)) - height / 2, min: 0, max: max(0, preview.fullHeight - height))
        return TripletBaseRegionInput(x: x, y: y, w: width, h: height)
    }

    private func draggedRegion(
        from start: CGPoint,
        to end: CGPoint,
        fitted: CGRect
    ) -> TripletBaseRegionInput {
        let a = fullPoint(from: start, fitted: fitted)
        let b = fullPoint(from: end, fitted: fitted)
        let x0 = clamp(Int(floor(min(a.x, b.x))), min: 0, max: max(0, preview.fullWidth - 1))
        let y0 = clamp(Int(floor(min(a.y, b.y))), min: 0, max: max(0, preview.fullHeight - 1))
        let x1 = clamp(Int(ceil(max(a.x, b.x))), min: x0 + 1, max: preview.fullWidth)
        let y1 = clamp(Int(ceil(max(a.y, b.y))), min: y0 + 1, max: preview.fullHeight)
        return TripletBaseRegionInput(x: x0, y: y0, w: x1 - x0, h: y1 - y0)
    }

    private func fullPoint(from point: CGPoint, fitted: CGRect) -> CGPoint {
        let localX = (clamp(point.x, min: fitted.minX, max: fitted.maxX) - fitted.minX)
            / max(1, fitted.width)
        let localY = (clamp(point.y, min: fitted.minY, max: fitted.maxY) - fitted.minY)
            / max(1, fitted.height)
        return CGPoint(
            x: localX * CGFloat(max(1, preview.fullWidth)),
            y: localY * CGFloat(max(1, preview.fullHeight))
        )
    }

    private func clamped(_ point: CGPoint, to rect: CGRect) -> CGPoint {
        CGPoint(
            x: clamp(point.x, min: rect.minX, max: rect.maxX),
            y: clamp(point.y, min: rect.minY, max: rect.maxY)
        )
    }

    private func clamp<T: Comparable>(_ value: T, min lower: T, max upper: T) -> T {
        Swift.min(Swift.max(value, lower), upper)
    }
}
