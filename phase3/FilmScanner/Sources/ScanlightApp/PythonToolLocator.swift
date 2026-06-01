// PythonToolLocator — static PATH-search utility for Python entry-point tools.
//
// Searches the inherited PATH for a named executable and returns its URL.
// Throws a typed error with an actionable message (pip install command)
// when the tool is absent, so the caller can surface it to the user before
// attempting to spawn a child process.

import Foundation

// MARK: - Error

enum PythonToolLocatorError: Error {
    /// The named tool was not found in any directory on PATH.
    /// The associated value contains a human-readable message with the fix command.
    case toolNotFound(String)
}

// MARK: - Locator

enum PythonToolLocator {

    /// Search for `toolName` and return its URL.
    ///
    /// Step 1 — project-owned tool fallback:
    /// For tools maintained in this checkout, search the local scripts/build
    /// paths first so the Swift app does not accidentally launch a stale editable
    /// install from `~/.local/bin`.
    ///
    /// Step 2 — PATH search:
    /// Iterates each colon-separated directory in the `PATH` environment variable
    /// (falling back to `/usr/local/bin:/usr/bin:/bin` when PATH is unset) and
    /// checks `FileManager.isExecutableFile(atPath:)` for `<dir>/<toolName>`.
    /// Returns the first match as a `file://` URL.
    ///
    /// Step 3 — project fallbacks (Open Question 2+3 from 06-RESEARCH.md):
    /// When the tool is not on PATH, searches for it in a scripts/ directory
    /// relative to the running binary. This lets capture-calibration.sh and
    /// inspect-calibration.py resolve in a normal dev checkout where scripts/ is
    /// not on PATH. For `sony-capture`, also checks the Phase 1 build directory.
    ///
    /// - Parameter toolName: The bare executable name (e.g. `"capture-calibration.sh"`).
    /// - Returns: URL of the first executable match.
    /// - Throws: `PythonToolLocatorError.toolNotFound` when no match is found in either location.
    static func resolve(_ toolName: String) throws -> URL {
        // For repo-owned tools, prefer the current checkout before PATH. The
        // developer machine may have stale editable installs in ~/.local/bin; if
        // Swift launches those, the UI and backend silently run different code.
        if isProjectOwnedTool(toolName),
           let localTool = resolveProjectFallback(toolName) {
            return localTool
        }

        let pathEnv = ProcessInfo.processInfo.environment["PATH"]
            ?? "/usr/local/bin:/usr/bin:/bin"
        for dir in pathEnv.split(separator: ":").map(String.init) {
            let fullPath = "\(dir)/\(toolName)"
            if FileManager.default.isExecutableFile(atPath: fullPath) {
                return URL(fileURLWithPath: fullPath)
            }
        }

        if let localTool = resolveProjectFallback(toolName) {
            return localTool
        }

        throw PythonToolLocatorError.toolNotFound(
            "\(toolName) not found in PATH. Run: pip install -e <path/to/triplet-capture>"
        )
    }

    private static func isProjectOwnedTool(_ toolName: String) -> Bool {
        switch toolName {
        case "triplet-capture", "sony-capture", "scanlightctl",
             "capture-calibration.sh", "inspect-calibration.py":
            return true
        default:
            return false
        }
    }

    private static func resolveProjectFallback(_ toolName: String) -> URL? {
        // Project fallback: check paths relative to the running binary and cwd so
        // scripts and the Phase 1 sony-capture binary resolve in a dev checkout
        // where neither directory is on PATH. Candidates are fixed relative
        // components — no user-supplied path is joined here.
        let binaryDir = URL(fileURLWithPath: CommandLine.arguments[0])
            .deletingLastPathComponent()
        let cwd = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
        let baseCandidates: [URL] = [
            binaryDir,
            binaryDir.appendingPathComponent("../../.."),
            binaryDir.appendingPathComponent("../../../../"),
            cwd,
            cwd.appendingPathComponent(".."),
            cwd.appendingPathComponent("../.."),
            cwd.appendingPathComponent("../../.."),
            cwd.appendingPathComponent("../../../.."),
            cwd.appendingPathComponent("../../../../.."),
            binaryDir.appendingPathComponent("../../../../../"),
            binaryDir.appendingPathComponent("../../../../../../"),
        ]
        let relativeToolCandidates: [String] = [
            "scripts/\(toolName)",
            "phase1/sony-capture/build/\(toolName)",
        ]
        for base in baseCandidates {
            for relativePath in relativeToolCandidates {
                let candidate = base.appendingPathComponent(relativePath)
                if FileManager.default.isExecutableFile(atPath: candidate.standardizedFileURL.path) {
                    return candidate.standardizedFileURL
                }
            }
        }
        return nil
    }
}
