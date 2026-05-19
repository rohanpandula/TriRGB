// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "FilmScanner",
    platforms: [.macOS(.v13)],
    products: [
        // The Scanlight driver — usable on its own from any Swift host.
        // The full SwiftUI app and the Sony SDK Obj-C++ bridge live elsewhere
        // and are deferred until the Phase 1 hardware verification passes.
        .library(name: "ScanlightSwift", targets: ["ScanlightSwift"]),
        // Minimal CLI executable for plug-in-day verification of the Swift
        // serial path. Mirrors `scanlightctl` (Python) at a basic level so
        // an operator can confirm the Swift Scanlight driver talks to real
        // hardware before any UI work depends on it.
        .executable(name: "scanlight-swift-cli", targets: ["ScanlightSwiftCLI"]),
        // SwiftUI macOS app with full AX-ID coverage. Used by XCTest UI
        // (Plan 03) and cua-driver harness (Plan 04) for end-to-end
        // GUI-surface verification without hardware.
        .executable(name: "scanlight-app", targets: ["ScanlightApp"]),
    ],
    targets: [
        .target(
            name: "ScanlightSwift",
            path: "Sources/ScanlightSwift"
        ),
        .executableTarget(
            name: "ScanlightSwiftCLI",
            dependencies: ["ScanlightSwift"],
            path: "Sources/ScanlightSwiftCLI"
        ),
        .executableTarget(
            name: "ScanlightApp",
            dependencies: ["ScanlightSwift"],
            path: "Sources/ScanlightApp"
        ),
        .testTarget(
            name: "ScanlightSwiftTests",
            dependencies: ["ScanlightSwift"],
            path: "Tests/ScanlightSwiftTests"
        ),
        .testTarget(
            name: "ScanlightAppUITests",
            dependencies: ["ScanlightApp", "ScanlightSwift"],
            path: "Tests/ScanlightAppUITests"
        ),
    ]
)
