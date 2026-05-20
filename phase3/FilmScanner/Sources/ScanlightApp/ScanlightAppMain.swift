// scanlight-app — SwiftUI macOS app for Scanlight light source control.
//
// Entry point. Wires the ViewModel factory to FakeBridge.makeTransport
// so XCTest UI and cua-driver automation can pass -FakeTransport YES to
// bypass hardware. The ViewModel calls the factory on each connect() so
// a fresh transport is created per connection attempt.
//
// Phase 06 four-tab shell:
//   [0] Light     — ScanlightView (unchanged)
//   [1] Settings  — ScanSettingsView
//   [2] Calibrate — CalibrationView (plan 06-03)
//   [3] Scan      — Text placeholder (Phase 07)
//
// SettingsStore is @StateObject at the App level so ScanSettingsView and
// CalibrationView share the same settings instance.
//
// scanlightViewModel is @StateObject at the App level so CalibrationView
// can observe isConnected for the serial-port guard (phase 07 delivers the
// full port-handoff state machine; here we only prevent the double-open).
//
// No business logic lives here — this file only wires View + ViewModel.

import SwiftUI

@main
struct ScanlightAppMain: App {
    @StateObject private var settingsStore = SettingsStore()
    @StateObject private var scanlightViewModel = ScanlightViewModel(
        transportFactory: FakeBridge.makeTransport
    )

    var body: some Scene {
        WindowGroup {
            TabView {
                ScanlightView(viewModel: scanlightViewModel)
                    .tabItem { Label("Light", systemImage: "lightbulb") }

                ScanSettingsView(store: settingsStore)
                    .tabItem { Label("Settings", systemImage: "gearshape") }

                CalibrationView(store: settingsStore, viewModel: scanlightViewModel)
                    .tabItem { Label("Calibrate", systemImage: "scope") }

                Text("Scan \u{2014} coming in Phase 7")
                    .foregroundColor(.secondary)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .tabItem { Label("Scan", systemImage: "camera") }
            }
            .frame(
                minWidth: 720, idealWidth: 900, maxWidth: .infinity,
                minHeight: 480, idealHeight: 640, maxHeight: .infinity
            )
        }
    }
}
