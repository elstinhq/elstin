import Foundation

/// The environment the user's tools expect.
///
/// An app opened from Finder or the Dock is started by launchd, whose environment is
/// almost empty: PATH is /usr/bin:/bin:/usr/sbin:/sbin, so the agents' `git` works
/// (macOS ships a shim) and `node`, `flutter`, `mvn`, `java`, Homebrew's anything do
/// not — "command not found", far from the cause. The user's own environment is what
/// their login shell builds from ~/.zprofile and ~/.zshrc, so ask it, the way VS Code
/// does: run it as a login, interactive shell and read `env` back.
public enum ShellEnvironment {
    static let begin = "__ELSTIN_ENV_BEGIN__"
    static let end = "__ELSTIN_ENV_END__"

    /// Directories a developer Mac's tools live in, appended to PATH if the shell did not
    /// already list them (an rc file that fails, a shell that has none).
    public static let commonToolDirectories = ["/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin", "/usr/local/sbin"]

    /// Never taken from the shell. DATABASE_URL names where elstin's data lives and an old
    /// export of it (this project's docs once said to `export` a Postgres URL) must not
    /// steer the app; the rest are a terminal session's, not an app's.
    static let neverImported: Set<String> = [
        "DATABASE_URL", "SHLVL", "_", "PWD", "OLDPWD", "TERM_SESSION_ID", "SECURITYSESSIONID",
        "ITERM_SESSION_ID", "TERM_PROGRAM", "TERM_PROGRAM_VERSION", "COLORTERM",
    ]

    static func script() -> String {
        "printf '%s\\n' \(begin); /usr/bin/env -0; printf '%s\\n' \(end)"
    }

    /// The variables between the markers. The text around them is whatever the shell's rc
    /// files print — banners, `neofetch`, a fortune — and is ignored.
    public static func parse(output: Data) -> [String: String]? {
        guard let start = output.range(of: Data((begin + "\n").utf8)),
              let stop = output.range(of: Data((end + "\n").utf8), options: .backwards),
              start.upperBound <= stop.lowerBound
        else { return nil }
        var variables: [String: String] = [:]
        for entry in output[start.upperBound..<stop.lowerBound].split(separator: 0) {
            guard let text = String(data: Data(entry), encoding: .utf8), let equals = text.firstIndex(of: "=") else { continue }
            let name = String(text[..<equals])
            guard !name.isEmpty else { continue }
            variables[name] = String(text[text.index(after: equals)...])
        }
        return variables.isEmpty ? nil : variables
    }

    /// What the backend — and through it every command an agent runs — is started with:
    /// the app's own environment, overlaid with the shell's, with PATH assembled from the
    /// shell's entries first, then what the app had, then the usual tool directories.
    public static func merged(gui: [String: String], shell: [String: String]?) -> [String: String] {
        var environment = gui
        for (name, value) in shell ?? [:] where !neverImported.contains(name) { environment[name] = value }
        var seen = Set<String>()
        let entries = [shell?["PATH"], gui["PATH"], commonToolDirectories.joined(separator: ":")]
            .compactMap { $0 }
            .flatMap { $0.split(separator: ":").map(String.init) }
            .filter { !$0.isEmpty && seen.insert($0).inserted }
        environment["PATH"] = entries.joined(separator: ":")
        return environment
    }

    /// Runs `shell` as a login, interactive shell and reads its environment. Nil if it does
    /// not answer in `timeout` seconds (an rc file waiting for input) or at all.
    public static func capture(shell: String? = nil, timeout: TimeInterval = 10) async -> [String: String]? {
        let path = shell ?? ProcessInfo.processInfo.environment["SHELL"] ?? "/bin/zsh"
        return await withCheckedContinuation { continuation in
            DispatchQueue.global(qos: .userInitiated).async {
                let process = Process()
                process.executableURL = URL(fileURLWithPath: path)
                process.arguments = ["-ilc", script()]
                let pipe = Pipe()
                process.standardOutput = pipe
                process.standardError = FileHandle.nullDevice
                process.standardInput = FileHandle.nullDevice
                do { try process.run() } catch { continuation.resume(returning: nil); return }
                let watchdog = DispatchWorkItem { if process.isRunning { process.terminate() } }
                DispatchQueue.global().asyncAfter(deadline: .now() + timeout, execute: watchdog)
                let output = pipe.fileHandleForReading.readDataToEndOfFile()
                process.waitUntilExit()
                watchdog.cancel()
                continuation.resume(returning: parse(output: output))
            }
        }
    }
}
