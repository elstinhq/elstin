import Foundation

/// Starts the backend, waits for its `ELSTIN_READY` line, and stops it again.
///
/// The backend is a child process of the window: it exits by itself if this process
/// disappears (elstin/launcher.py watches its parent), and the window ends it on Quit.
public final class BackendSupervisor: @unchecked Sendable {
    public struct Configuration: Sendable {
        public var executable: URL
        public var arguments: [String]
        public var environment: [String: String]
        public var workingDirectory: URL?

        public init(executable: URL, arguments: [String] = [], environment: [String: String] = [:], workingDirectory: URL? = nil) {
            self.executable = executable
            self.arguments = arguments
            self.environment = environment
            self.workingDirectory = workingDirectory
        }
    }

    public enum Failure: Error, Equatable, LocalizedError {
        case notFound(String)
        case couldNotStart(String)
        /// It ended before it was serving. `code` and `message` are its own explanation, if it gave one.
        case exited(status: Int32, code: String?, message: String?)
        case timedOut

        public var errorDescription: String? {
            switch self {
            case .notFound(let path): return "The backend is missing: \(path)"
            case .couldNotStart(let reason): return "The backend could not be started: \(reason)"
            case .exited(let status, _, let message): return message ?? "The backend stopped while starting (exit status \(status))."
            case .timedOut: return "The backend did not become ready in time."
            }
        }

        public var isAnotherInstance: Bool {
            if case .exited(_, let code, _) = self { return code == "already_running" }
            return false
        }
    }

    private let configuration: Configuration
    private let lock = NSLock()
    private var process: Process?
    private var stopping = false

    /// Called, off the main thread, if the backend ends on its own after it was serving.
    public var onUnexpectedExit: (@Sendable (Int32) -> Void)?

    public init(_ configuration: Configuration) {
        self.configuration = configuration
    }

    public var isRunning: Bool {
        lock.lock(); defer { lock.unlock() }
        return process?.isRunning ?? false
    }

    private final class StartState: @unchecked Sendable {
        let lock = NSLock()
        var finished = false
        var buffer = Data()
        var explanation: (code: String, message: String)?
        var ready = false
    }

    public func start(timeout: TimeInterval = 90) async throws -> BackendReady {
        guard FileManager.default.isExecutableFile(atPath: configuration.executable.path) else {
            throw Failure.notFound(configuration.executable.path)
        }
        let process = Process()
        process.executableURL = configuration.executable
        process.arguments = configuration.arguments
        process.environment = configuration.environment
        process.currentDirectoryURL = configuration.workingDirectory
        let output = Pipe()
        process.standardOutput = output
        // The backend logs to its own file; an unread pipe would fill and stop it.
        process.standardError = FileHandle.nullDevice
        process.standardInput = FileHandle.nullDevice
        let state = StartState()
        lock.withLock { self.process = process; stopping = false }

        return try await withCheckedThrowingContinuation { continuation in
            @Sendable func finish(_ result: Result<BackendReady, Failure>) {
                state.lock.lock()
                let first = !state.finished
                state.finished = true
                state.lock.unlock()
                if first { continuation.resume(with: result) }
            }

            output.fileHandleForReading.readabilityHandler = { handle in
                let data = handle.availableData
                if data.isEmpty { handle.readabilityHandler = nil; return }  // the backend closed its stdout
                state.lock.lock()
                state.buffer.append(data)
                var lines: [String] = []
                while let newline = state.buffer.firstIndex(of: 0x0A) {
                    lines.append(String(decoding: state.buffer[state.buffer.startIndex..<newline], as: UTF8.self))
                    state.buffer.removeSubrange(state.buffer.startIndex...newline)
                }
                state.lock.unlock()
                for line in lines {
                    switch BackendMessage.parse(line: line) {
                    case .ready(let ready):
                        state.lock.lock(); state.ready = true; state.lock.unlock()
                        finish(.success(ready))
                    case .failed(let code, let message):
                        state.lock.lock(); state.explanation = (code, message); state.lock.unlock()
                    case nil:
                        break  // anything else the backend prints is not ours to interpret
                    }
                }
            }

            process.terminationHandler = { [weak self] ended in
                state.lock.lock()
                let wasReady = state.ready
                let explanation = state.explanation
                state.lock.unlock()
                if !wasReady {
                    finish(.failure(.exited(status: ended.terminationStatus, code: explanation?.code, message: explanation?.message)))
                } else if let self, !self.isStopping {
                    self.onUnexpectedExit?(ended.terminationStatus)
                }
            }

            do {
                try process.run()
            } catch {
                finish(.failure(.couldNotStart(error.localizedDescription)))
                return
            }
            DispatchQueue.global().asyncAfter(deadline: .now() + timeout) {
                state.lock.lock(); let pending = !state.finished; state.lock.unlock()
                if pending {
                    if process.isRunning { process.terminate() }
                    finish(.failure(.timedOut))
                }
            }
        }
    }

    private var isStopping: Bool {
        lock.lock(); defer { lock.unlock() }
        return stopping
    }

    /// SIGTERM — the backend shuts down cleanly (cancels its runs, folds its database file) —
    /// and SIGKILL if it has not finished in `grace` seconds.
    public func stop(grace: TimeInterval = 15) async {
        let process: Process? = lock.withLock { stopping = true; return self.process }
        guard let process, process.isRunning else { return }
        process.terminate()
        let deadline = Date().addingTimeInterval(grace)
        while process.isRunning, Date() < deadline {
            try? await Task.sleep(nanoseconds: 50_000_000)
        }
        if process.isRunning {
            kill(process.processIdentifier, SIGKILL)
            while process.isRunning { try? await Task.sleep(nanoseconds: 20_000_000) }
        }
    }
}
