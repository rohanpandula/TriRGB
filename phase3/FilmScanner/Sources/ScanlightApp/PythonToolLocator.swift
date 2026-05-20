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

    /// Search PATH (then a scripts/ fallback) for `toolName` and return its URL.
    ///
    /// Step 1 — PATH search:
    /// Iterates each colon-separated directory in the `PATH` environment variable
    /// (falling back to `/usr/local/bin:/usr/bin:/bin` when PATH is unset) and
    /// checks `FileManager.isExecutableFile(atPath:)` for `<dir>/<toolName>`.
    /// Returns the first match as a `file://` URL.
    ///
    /// Step 2 — scripts/ fallback (Open Question 2+3 from 06-RESEARCH.md):
    /// When the tool is not on PATH, searches for it in a scripts/ directory
    /// relative to the running binary. This lets capture-calibration.sh and
    /// inspect-calibration.py resolve in a normal dev checkout where scripts/ is
    /// not on PATH. Three candidate directories are checked (binary-local, two
    /// common relative depths from the SwiftPM build tree).
    ///
    /// - Parameter toolName: The bare executable name (e.g. `"capture-calibration.sh"`).
    /// - Returns: URL of the first executable match (PATH first, then scripts/).
    /// - Throws: `PythonToolLocatorError.toolNotFound` when no match is found in either location.
    static func resolve(_ toolName: String) throws -> URL {
        let pathEnv = ProcessInfo.processInfo.environment["PATH"]
            ?? "/usr/local/bin:/usr/bin:/bin"
        for dir in pathEnv.split(separator: ":").map(String.init) {
            let fullPath = "\(dir)/\(toolName)"
            if FileManager.default.isExecutableFile(atPath: fullPath) {
                return URL(fileURLWithPath: fullPath)
            }
        }

        // scripts/ fallback: check paths relative to the running binary so that
        // capture-calibration.sh / inspect-calibration.py resolve in a dev
        // checkout where scripts/ is not on PATH (T-06-09: all candidates are
        // constructed from fixed relative components — no user-supplied path).
        let binaryDir = URL(fileURLWithPath: CommandLine.arguments[0])
            .deletingLastPathComponent()
        let scriptsDirCandidates: [URL] = [
            binaryDir.appendingPathComponent("scripts"),
            binaryDir.appendingPathComponent("../../..").appendingPathComponent("scripts"),
            binaryDir.appendingPathComponent("../../../../scripts"),
        ]
        for scriptsDir in scriptsDirCandidates {
            let candidate = scriptsDir.appendingPathComponent(toolName)
            if FileManager.default.isExecutableFile(atPath: candidate.path) {
                return candidate
            }
        }

        throw PythonToolLocatorError.toolNotFound(
            "\(toolName) not found in PATH. Run: pip install -e <path/to/triplet-capture>"
        )
    }
}
