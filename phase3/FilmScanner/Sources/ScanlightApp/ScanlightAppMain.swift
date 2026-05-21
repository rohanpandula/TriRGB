// scanlight-app — SwiftUI macOS app for Scanlight light source control.
//
// Entry point. Wires the ViewModel factory to FakeBridge.makeTransport
// so XCTest UI and cua-driver automation can pass -FakeTransport YES to
// bypass hardware. The ViewModel calls the factory on each connect() so
// a fresh transport is created per connection attempt.
//
// Phase 07 four-tab shell:
//   [0] Light     — ScanlightView (unchanged)
//   [1] Settings  — ScanSettingsView
//   [2] Calibrate — CalibrationView
//   [3] Scan      — ScanView (Phase 07: scanning loop UI + port-state machine)
//
// All shared objects live in ScanHubRootView as @StateObjects so every tab
// shares the same instance. The App's body just presents ScanHubRootView.
//
// Design note: ScanCoordinator references OrchestratorClient + ScanlightViewModel
// passed at init time. Both are created first and passed in. SwiftUI guarantees
// @StateObjects in a View are initialized before body is evaluated, so the
// dependency order is safe.
//
// No business logic lives here — this file only wires View + ViewModel.

import SwiftUI

@main
struct ScanlightAppMain: App {
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

/// Root view that owns all shared state objects and passes them into each tab.
///
/// Putting @StateObjects here (rather than on App) lets ScanCoordinator be
/// initialized with the live OrchestratorClient and ScanlightViewModel, since
/// SwiftUI processes @StateObject declarations top-to-bottom within a single
/// view initializer, and our custom init creates the coordinator last.
struct ScanHubRootView: View {
    @StateObject private var settingsStore    = SettingsStore()
    @StateObject private var orchestratorClient = OrchestratorClient()
    @StateObject private var scanlightViewModel = ScanlightViewModel(
        transportFactory: FakeBridge.makeTransport
    )
    // ScanCoordinator must be constructed AFTER the two objects it owns. We
    // use a StateObject with a wrappedValue closure so Swift evaluates all three
    // let-bindings in order before the StateObject's constructor runs. In a
    // struct's synthesised memberwise init, @StateObject closures are evaluated
    // lazily in property-declaration order, so scanCoordinator is created last.
    @StateObject private var scanCoordinator: ScanCoordinator

    init() {
        // Build the shared objects here so we can pass them into ScanCoordinator.
        let client = OrchestratorClient()
        let lightVM = ScanlightViewModel(transportFactory: FakeBridge.makeTransport)
        let coordinator = ScanCoordinator(client: client, lightViewModel: lightVM)

        _settingsStore        = StateObject(wrappedValue: SettingsStore())
        _orchestratorClient   = StateObject(wrappedValue: client)
        _scanlightViewModel   = StateObject(wrappedValue: lightVM)
        _scanCoordinator      = StateObject(wrappedValue: coordinator)
    }

    var body: some View {
        TabView {
            ScanlightView(viewModel: scanlightViewModel)
                .tabItem { Label("Light", systemImage: "lightbulb") }

            ScanSettingsView(store: settingsStore, orchestratorClient: orchestratorClient)
                .tabItem { Label("Settings", systemImage: "gearshape") }

            CalibrationView(store: settingsStore, viewModel: scanlightViewModel)
                .tabItem { Label("Calibrate", systemImage: "scope") }

            ScanView(coordinator: scanCoordinator, store: settingsStore)
                .tabItem { Label("Scan", systemImage: "camera") }
        }
        .tint(Theme.accent)
    }
}
