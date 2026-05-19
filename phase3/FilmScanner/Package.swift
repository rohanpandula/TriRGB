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
        .testTarget(
            name: "ScanlightSwiftTests",
            dependencies: ["ScanlightSwift"],
            path: "Tests/ScanlightSwiftTests"
        ),
    ]
)
