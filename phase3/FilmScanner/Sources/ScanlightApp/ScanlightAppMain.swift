// scanlight-app — SwiftUI macOS app for Scanlight light source control.
//
// Entry point. Wires the ViewModel factory to FakeBridge.makeTransport
// so XCTest UI and cua-driver automation can pass -FakeTransport YES to
// bypass hardware. The ViewModel calls the factory on each connect() so
// a fresh transport is created per connection attempt.
//
// Product shell:
//   [0] Session   — roll readiness, rig status, and next actions
//   [1] Calibrate — guided RGB exposure + flat-field workflow
//   [2] Scan      — roll capture loop
//   [3] Develop   — manual RGB triplet → positive TIFF
//   [4] Set Up    — roll/camera setup, diagnostics, and manual light controls
//
// All shared objects live in ScanHubRootView as @StateObjects so every tab
// shares the same instance. The App's body just presents ScanHubRootView.
//
// Design note: ScanCoordinator and CalibrationWizardViewModel both reference
// the SAME OrchestratorClient (created first in init) so the wizard and the
// scan loop share the same server connection and _lock.
//
// No business logic lives here — this file only wires View + ViewModel.

import SwiftUI
import AppKit

enum ProductTab: Hashable {
    case session
    case calibrate
    case scan
    case develop
    case setup
}

@main
struct ScanlightAppMain: App {
    @NSApplicationDelegateAdaptor(ScanlightAppDelegate.self) private var appDelegate

    init() {
        if let repoRoot = Bundle.main.object(forInfoDictionaryKey: "ScanlightRepoRoot") as? String,
           !repoRoot.isEmpty {
            FileManager.default.changeCurrentDirectoryPath(repoRoot)
        }

        // Launched as a bare SwiftPM executable (no .app bundle), macOS defaults
        // the process to a .prohibited activation policy: the WindowGroup window
        // is created but never shown by the window server, and can't be brought
        // forward externally. Promote to a regular foreground app so the window
        // actually appears (and gets a Dock icon) on a direct `swift run` launch.
        // Harmless under the AX-driven test harness (-FakeTransport), which never
        // relied on the window being frontmost.
        NSApplication.shared.setActivationPolicy(.regular)
        DispatchQueue.main.async {
            NSApplication.shared.activate(ignoringOtherApps: true)
        }
    }

    var body: some Scene {
        WindowGroup {
            ScanHubRootView()
                .frame(
                    minWidth: 680, idealWidth: 800, maxWidth: .infinity,
                    minHeight: 520, idealHeight: 700, maxHeight: .infinity
                )
        }
    }
}

@MainActor
private final class ScanlightAppDelegate: NSObject, NSApplicationDelegate {
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false
    }

    func applicationWillTerminate(_ notification: Notification) {
        ScanlightRuntimeRegistry.orchestratorClient?.terminateChildForAppShutdown()
    }
}

@MainActor
private enum ScanlightRuntimeRegistry {
    static weak var orchestratorClient: OrchestratorClient?
}

/// Root view that owns all shared state objects and passes them into each tab.
///
/// Putting @StateObjects here (rather than on App) lets ScanCoordinator be
/// initialized with the live OrchestratorClient and ScanlightViewModel, since
/// SwiftUI processes @StateObject declarations top-to-bottom within a single
/// view initializer, and our custom init creates the coordinator last.
struct ScanHubRootView: View {
    // Double-allocation fix: @StateObject properties that have inline defaults
    // AND are overwritten in init() allocate two objects (the default is
    // constructed first, then immediately discarded). Remove inline initializers
    // for the five properties that init() rewrites so only one instance is ever
    // created. Properties that init() does NOT touch (positiveInversionViewModel,
    // sonyCameraConnection, selectedTab) keep their inline defaults.
    @StateObject private var settingsStore: SettingsStore
    @StateObject private var orchestratorClient: OrchestratorClient
    @StateObject private var scanlightViewModel: ScanlightViewModel
    // ScanCoordinator and CalibrationWizardViewModel must be constructed AFTER
    // the two objects they depend on (client, lightVM). We use explicit init()
    // to guarantee the construction order.
    @StateObject private var scanCoordinator: ScanCoordinator
    @StateObject private var calibrationWizardViewModel: CalibrationWizardViewModel
    @StateObject private var positiveInversionViewModel = PositiveInversionViewModel()
    @StateObject private var sonyCameraConnection = SonyCameraConnection()
    @State private var selectedTab: ProductTab = .session

    init() {
        // Build shared objects first so they can be passed to the coordinators.
        let client  = OrchestratorClient()
        let lightVM = ScanlightViewModel(transportFactory: FakeBridge.makeTransport)
        let coordinator       = ScanCoordinator(client: client, lightViewModel: lightVM)
        let wizardVM          = CalibrationWizardViewModel(orchestratorClient: client)
        ScanlightRuntimeRegistry.orchestratorClient = client

        _settingsStore                = StateObject(wrappedValue: SettingsStore())
        _orchestratorClient           = StateObject(wrappedValue: client)
        _scanlightViewModel           = StateObject(wrappedValue: lightVM)
        _scanCoordinator              = StateObject(wrappedValue: coordinator)
        _calibrationWizardViewModel   = StateObject(wrappedValue: wizardVM)
    }

    var body: some View {
        TabView(selection: $selectedTab) {
            SessionView(
                selectedTab: $selectedTab,
                store: settingsStore,
                coordinator: scanCoordinator,
                orchestratorClient: orchestratorClient,
                lightViewModel: scanlightViewModel,
                cameraConnection: sonyCameraConnection
            )
                .tabItem { Label("Session", systemImage: "rectangle.stack") }
                .tag(ProductTab.session)

            CalibrationWizardView(
                viewModel: calibrationWizardViewModel,
                store: settingsStore,
                coordinator: scanCoordinator,
                cameraConnection: sonyCameraConnection
            )
                .tabItem { Label("Calibrate", systemImage: "scope") }
                .tag(ProductTab.calibrate)

            ScanView(
                coordinator: scanCoordinator,
                store: settingsStore,
                orchestratorClient: orchestratorClient,
                lightViewModel: scanlightViewModel
            )
                .tabItem { Label("Scan", systemImage: "camera") }
                .tag(ProductTab.scan)

            PositiveInversionView(viewModel: positiveInversionViewModel)
                .tabItem { Label("Develop", systemImage: "wand.and.stars") }
                .tag(ProductTab.develop)

            SetupView(
                store: settingsStore,
                orchestratorClient: orchestratorClient,
                lightViewModel: scanlightViewModel,
                cameraConnection: sonyCameraConnection
            )
                .tabItem { Label("Set Up", systemImage: "slider.horizontal.3") }
                .tag(ProductTab.setup)
        }
        .tint(Theme.accent)
        .onAppear {
            sonyCameraConnection.update(for: settingsStore.settings)
        }
        .onChange(of: settingsStore.settings.triggerMode) { _ in
            sonyCameraConnection.update(for: settingsStore.settings)
        }
        .onChange(of: settingsStore.settings.sonyTransport) { _ in
            sonyCameraConnection.update(for: settingsStore.settings)
        }
        .onChange(of: settingsStore.settings.sonyIpAddress) { _ in
            sonyCameraConnection.update(for: settingsStore.settings)
        }
        .onChange(of: settingsStore.settings.sonyMacAddress) { _ in
            sonyCameraConnection.update(for: settingsStore.settings)
        }
        .onChange(of: settingsStore.settings.sonyUser) { _ in
            sonyCameraConnection.update(for: settingsStore.settings)
        }
        .onChange(of: settingsStore.settings.sonyPassword) { _ in
            sonyCameraConnection.update(for: settingsStore.settings)
        }
    }
}
