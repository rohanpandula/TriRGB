// scanlight-app — SwiftUI macOS app for Scanlight light source control.
//
// Entry point. Wires the ViewModel factory to FakeBridge.makeTransport
// so XCTest UI and cua-driver automation can pass -FakeTransport YES to
// bypass hardware. The ViewModel calls the factory on each connect() so
// a fresh transport is created per connection attempt.
//
// No business logic lives here — this file only wires View + ViewModel.

import SwiftUI

@main
struct ScanlightAppMain: App {
    var body: some Scene {
        WindowGroup {
            ScanlightView(viewModel: ScanlightViewModel(transportFactory: FakeBridge.makeTransport))
                .frame(
                    minWidth: 720, idealWidth: 900, maxWidth: .infinity,
                    minHeight: 480, idealHeight: 640, maxHeight: .infinity
                )
        }
    }
}
