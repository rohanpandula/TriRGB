// scanlight-app — SwiftUI macOS app for Scanlight light source control.
//
// Entry point. Wires the ViewModel factory to FakeBridge.makeTransport
// so XCTest UI and cua-driver automation can pass -FakeTransport YES to
// bypass hardware. The ViewModel calls the factory on each connect() so
// a fresh transport is created per connection attempt.
//
// Phase 06 introduces a four-tab shell:
//   [0] Light     — ScanlightView (unchanged)
//   [1] Settings  — ScanSettingsView (new)
//   [2] Calibrate — Text placeholder (CalibrationView arrives in plan 06-03)
//   [3] Scan      — Text placeholder (Phase 07)
//
// SettingsStore is @StateObject at the App level so ScanSettingsView and
// CalibrationView (06-03) share the same settings instance.
//
// No business logic lives here — this file only wires View + ViewModel.

import SwiftUI

@main
struct ScanlightAppMain: App {
    @StateObject private var settingsStore = SettingsStore()

    var body: some Scene {
        WindowGroup {
            TabView {
                ScanlightView(viewModel: ScanlightViewModel(transportFactory: FakeBridge.makeTransport))
                    .tabItem { Label("Light", systemImage: "lightbulb") }

                ScanSettingsView(store: settingsStore)
                    .tabItem { Label("Settings", systemImage: "gearshape") }

                // CalibrationView is introduced in plan 06-03.
                // A plain Text placeholder is used here so that 06-03 can
                // declare the canonical CalibrationView type without conflict.
                Text("Calibrate \u{2014} coming soon")
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
