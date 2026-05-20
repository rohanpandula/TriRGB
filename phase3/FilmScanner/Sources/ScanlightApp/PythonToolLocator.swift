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

    /// Search PATH for `toolName` and return its URL.
    ///
    /// Iterates each colon-separated directory in the `PATH` environment variable
    /// (falling back to `/usr/local/bin:/usr/bin:/bin` when PATH is unset) and
    /// checks `FileManager.isExecutableFile(atPath:)` for `<dir>/<toolName>`.
    /// Returns the first match as a `file://` URL.
    ///
    /// - Parameter toolName: The bare executable name (e.g. `"triplet-capture"`).
    /// - Returns: URL of the first executable match on PATH.
    /// - Throws: `PythonToolLocatorError.toolNotFound` when no match is found.
    static func resolve(_ toolName: String) throws -> URL {
        let pathEnv = ProcessInfo.processInfo.environment["PATH"]
            ?? "/usr/local/bin:/usr/bin:/bin"
        for dir in pathEnv.split(separator: ":").map(String.init) {
            let fullPath = "\(dir)/\(toolName)"
            if FileManager.default.isExecutableFile(atPath: fullPath) {
                return URL(fileURLWithPath: fullPath)
            }
        }
        throw PythonToolLocatorError.toolNotFound(
            "\(toolName) not found in PATH. Run: pip install -e <path/to/triplet-capture>"
        )
    }
}
