// scanlight-app — SwiftUI macOS app for Scanlight light source control.
//
// Entry point. Wires the ViewModel factory to FakeBridge.makeTransport
// so XCTest UI and cua-driver automation can pass -FakeTransport YES to
// bypass hardware. The ViewModel calls the factory on each connect() so
// a fresh transport is created per connection attempt.
//
// Phase 14 four-tab shell:
//   [0] Light     — ScanlightView (unchanged)
//   [1] Settings  — ScanSettingsView
//   [2] Calibrate — CalibrationWizardView (Phase 14: 4-step guided calibration wizard)
//   [3] Scan      — ScanView (Phase 07: scanning loop UI + port-state machine)
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
    @StateObject private var settingsStore              = SettingsStore()
    @StateObject private var orchestratorClient         = OrchestratorClient()
    @StateObject private var scanlightViewModel         = ScanlightViewModel(
        transportFactory: FakeBridge.makeTransport
    )
    // ScanCoordinator and CalibrationWizardViewModel must be constructed AFTER
    // the two objects they depend on (client, lightVM). We use explicit init()
    // to guarantee the construction order.
    @StateObject private var scanCoordinator: ScanCoordinator
    @StateObject private var calibrationWizardViewModel: CalibrationWizardViewModel

    init() {
        // Build shared objects first so they can be passed to the coordinators.
        let client  = OrchestratorClient()
        let lightVM = ScanlightViewModel(transportFactory: FakeBridge.makeTransport)
        let coordinator       = ScanCoordinator(client: client, lightViewModel: lightVM)
        let wizardVM          = CalibrationWizardViewModel(orchestratorClient: client)

        _settingsStore                = StateObject(wrappedValue: SettingsStore())
        _orchestratorClient           = StateObject(wrappedValue: client)
        _scanlightViewModel           = StateObject(wrappedValue: lightVM)
        _scanCoordinator              = StateObject(wrappedValue: coordinator)
        _calibrationWizardViewModel   = StateObject(wrappedValue: wizardVM)
    }

    var body: some View {
        TabView {
            ScanlightView(viewModel: scanlightViewModel)
                .tabItem { Label("Light", systemImage: "lightbulb") }

            ScanSettingsView(store: settingsStore, orchestratorClient: orchestratorClient)
                .tabItem { Label("Settings", systemImage: "gearshape") }

            CalibrationWizardView(viewModel: calibrationWizardViewModel, store: settingsStore)
                .tabItem { Label("Calibrate", systemImage: "scope") }

            ScanView(coordinator: scanCoordinator, store: settingsStore)
                .tabItem { Label("Scan", systemImage: "camera") }
        }
        .tint(Theme.accent)
    }
}
