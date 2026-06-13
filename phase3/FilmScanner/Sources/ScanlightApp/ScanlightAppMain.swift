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
                // Width budget: ~200pt sidebar + the detail panes, which were
                // designed for ~680pt of content. The old TabView gave content
                // the full window; the sidebar now needs its share on top.
                .frame(
                    minWidth: 880, idealWidth: 1040, maxWidth: .infinity,
                    minHeight: 560, idealHeight: 720, maxHeight: .infinity
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
    @State private var nav: NavItem = .setup

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
        NavigationSplitView {
            List(selection: $nav) {
                Section("Workflow") {
                    sidebarRow(.setup)
                    sidebarRow(.calibrate)
                    sidebarRow(.scan)
                    sidebarRow(.develop)
                }
                Section("Utilities") {
                    sidebarRow(.diagnostics)
                    sidebarRow(.filmStocks)
                }
            }
            .listStyle(.sidebar)
            .navigationSplitViewColumnWidth(min: 188, ideal: 200, max: 230)
        } detail: {
            VStack(spacing: 0) {
                ReadinessStrip(
                    store: settingsStore,
                    light: scanlightViewModel,
                    camera: sonyCameraConnection,
                    client: orchestratorClient,
                    coordinator: scanCoordinator,
                    onConnectLight: {
                        // Send the operator to the panel that owns the serial
                        // connection (so they can pick a port / see status) and
                        // kick an auto-discovery connect.
                        nav = .diagnostics
                        scanlightViewModel.connect()
                    }
                )
                Divider()
                detailContent
                    .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
            }
            .navigationTitle(nav.title)
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

    @ViewBuilder
    private func sidebarRow(_ item: NavItem) -> some View {
        HStack(spacing: 9) {
            if let n = item.stepNumber {
                Text("\(n)")
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(nav == item ? Color.accentColor : .secondary)
                    .frame(width: 20, height: 20)
                    .background(
                        Circle().fill(nav == item
                                      ? Theme.accent.opacity(0.20)
                                      : Color.primary.opacity(0.06))
                    )
            } else if let icon = item.utilityIcon {
                Image(systemName: icon)
                    .frame(width: 20)
                    .foregroundStyle(.secondary)
            }
            Text(item.title)
        }
        .tag(item)
        .accessibilityIdentifier(item.axID)
    }

    @ViewBuilder
    private var detailContent: some View {
        switch nav {
        case .setup:
            ScanSettingsView(
                store: settingsStore,
                orchestratorClient: orchestratorClient,
                lightViewModel: scanlightViewModel,
                cameraConnection: sonyCameraConnection
            )
        case .calibrate:
            CalibrationWizardView(
                viewModel: calibrationWizardViewModel,
                store: settingsStore,
                coordinator: scanCoordinator,
                cameraConnection: sonyCameraConnection
            )
        case .scan:
            ScanView(
                coordinator: scanCoordinator,
                store: settingsStore,
                orchestratorClient: orchestratorClient,
                lightViewModel: scanlightViewModel
            )
        case .develop:
            PositiveInversionView(viewModel: positiveInversionViewModel)
        case .diagnostics:
            ScanlightView(viewModel: scanlightViewModel)
        case .filmStocks:
            FilmStockManagerView(store: settingsStore)
        }
    }
}
