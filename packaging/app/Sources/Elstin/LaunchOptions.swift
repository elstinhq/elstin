import Foundation

/// Command-line options. A user never passes any: they are for developing the window and for
/// build scripts (`--check-backend`).
///
///     Elstin --backend build/dist/elstin-backend/elstin-backend --backend-arg --no-exit-with-parent
struct LaunchOptions {
    /// Use this backend instead of the one inside the app bundle.
    var backendPath: String?
    /// Passed to the backend as they are (repeatable): `--backend-arg --log-file --backend-arg /tmp/x.log`.
    var backendArguments: [String] = []
    /// Let Safari's Web Inspector attach to the page.
    var inspectable = false
    /// Run the bundled backend's `--self-test`, print the result and exit, without opening a window.
    var checkBackend = false

    static func parse(_ arguments: [String]) -> LaunchOptions {
        var options = LaunchOptions()
        var index = 1
        while index < arguments.count {
            switch arguments[index] {
            case "--backend" where index + 1 < arguments.count:
                index += 1
                options.backendPath = arguments[index]
            case "--backend-arg" where index + 1 < arguments.count:
                index += 1
                options.backendArguments.append(arguments[index])
            case "--inspectable":
                options.inspectable = true
            case "--check-backend":
                options.checkBackend = true
            default:
                break  // macOS adds arguments of its own (-psn_..., -NSDocumentRevisions...)
            }
            index += 1
        }
        return options
    }
}

enum AppPaths {
    static var home: URL { FileManager.default.homeDirectoryForCurrentUser }
    static var logsDirectory: URL { home.appendingPathComponent("Library/Logs/Elstin") }
    static var logFile: URL { logsDirectory.appendingPathComponent("elstin.log") }
    static var dataDirectory: URL { home.appendingPathComponent("Library/Application Support/Elstin") }

    /// The backend in the app bundle: Contents/Resources/backend/elstin-backend.
    static func backend(_ options: LaunchOptions) -> URL {
        if let path = options.backendPath { return URL(fileURLWithPath: (path as NSString).expandingTildeInPath) }
        return (Bundle.main.resourceURL ?? Bundle.main.bundleURL).appendingPathComponent("backend/elstin-backend")
    }
}
