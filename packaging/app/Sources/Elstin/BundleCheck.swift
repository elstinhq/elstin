import Foundation

/// `Elstin --check-backend`: runs the bundled backend's `--self-test` and exits with its status, with
/// no window — the build script's proof that the app was assembled with a working backend inside.
enum BundleCheck {
    static func run(_ options: LaunchOptions) -> Int32 {
        let backend = AppPaths.backend(options)
        guard FileManager.default.isExecutableFile(atPath: backend.path) else {
            FileHandle.standardError.write(Data("The backend is missing from the app: \(backend.path)\n".utf8))
            return 2
        }
        let process = Process()
        process.executableURL = backend
        process.arguments = ["--self-test"]
        do { try process.run() } catch {
            FileHandle.standardError.write(Data("Could not run \(backend.path): \(error.localizedDescription)\n".utf8))
            return 2
        }
        process.waitUntilExit()
        return process.terminationStatus
    }
}
