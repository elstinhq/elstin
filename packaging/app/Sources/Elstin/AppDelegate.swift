import AppKit
import ElstinCore
import os

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    private let options: LaunchOptions
    private let log = Logger(subsystem: "com.elstin.Elstin", category: "app")
    private var windowController: MainWindowController!
    private var supervisor: BackendSupervisor?
    private var monitor: StatusMonitor?
    private var signalSources: [DispatchSourceSignal] = []
    /// Set when the system or a signal asks to quit: nobody is there to answer a question.
    private var quitWithoutAsking = false
    private static let portKey = "lastBackendPort"

    init(options: LaunchOptions) {
        self.options = options
        super.init()
    }

    // MARK: launch

    func applicationDidFinishLaunching(_ notification: Notification) {
        guard ensureSingleInstance() else { return }
        NSApp.mainMenu = MenuBuilder.make(appName: "Elstin", delegate: self)
        windowController = MainWindowController(inspectable: options.inspectable)
        windowController.showMessage("Starting Elstin…")
        windowController.show()
        installSignalHandlers()
        Task { await startBackend() }
    }

    /// LaunchServices already starts one copy of a bundle; this is for the other ways in (running
    /// the executable twice). The older instance wins, and is brought to the front.
    private func ensureSingleInstance() -> Bool {
        guard let identifier = Bundle.main.bundleIdentifier else { return true }
        let me = ProcessInfo.processInfo.processIdentifier
        let older = NSRunningApplication.runningApplications(withBundleIdentifier: identifier)
            .filter { $0.processIdentifier != me && $0.processIdentifier < me }
        guard let existing = older.first else { return true }
        existing.activate()
        exit(0)
    }

    private func installSignalHandlers() {
        for number in [SIGTERM, SIGINT, SIGHUP] {
            signal(number, SIG_IGN)
            let source = DispatchSource.makeSignalSource(signal: number, queue: .main)
            source.setEventHandler { [weak self] in
                MainActor.assumeIsolated {
                    self?.quitWithoutAsking = true
                    self?.requestQuit()
                }
            }
            source.resume()
            signalSources.append(source)
        }
    }

    // MARK: the backend

    private func startBackend() async {
        windowController.showMessage("Starting Elstin…")
        // The environment the user's tools expect, not the near-empty one launchd gives an app.
        let shell = await ShellEnvironment.capture()
        if shell == nil { log.warning("could not read the login shell's environment; using the app's, plus the usual tool directories") }
        let environment = ShellEnvironment.merged(gui: ProcessInfo.processInfo.environment, shell: shell)

        var arguments = options.backendArguments
        let lastPort = UserDefaults.standard.integer(forKey: Self.portKey)
        if lastPort > 0 { arguments += ["--prefer-port", String(lastPort)] }  // same address, same saved UI state
        let backend = BackendSupervisor(.init(
            executable: AppPaths.backend(options), arguments: arguments,
            environment: environment, workingDirectory: AppPaths.home
        ))
        backend.onUnexpectedExit = { [weak self] status in
            Task { @MainActor in self?.backendEnded(status: status) }
        }
        supervisor = backend

        do {
            let ready = try await backend.start()
            UserDefaults.standard.set(ready.port, forKey: Self.portKey)
            log.info("backend ready on port \(ready.port, privacy: .public)")
            windowController.load(ready)
            monitor = StatusMonitor(ready: ready)
            monitor?.start()
        } catch {
            presentStartFailure(error)
        }
    }

    private func presentStartFailure(_ error: Error) {
        let anotherInstance = (error as? BackendSupervisor.Failure)?.isAnotherInstance == true
        windowController.showMessage(anotherInstance ? "Elstin is already running" : "Elstin could not start")
        while true {
            let alert = NSAlert()
            alert.alertStyle = .critical
            alert.messageText = anotherInstance ? "Elstin is already running" : "Elstin could not start"
            alert.informativeText = error.localizedDescription
            alert.addButton(withTitle: "Retry")
            alert.addButton(withTitle: "Show Log")
            alert.addButton(withTitle: "Quit")
            switch alert.runModal() {
            case .alertFirstButtonReturn:
                Task { await startBackend() }
                return
            case .alertSecondButtonReturn:
                showLog()
            default:
                quitWithoutAsking = true
                requestQuit()
                return
            }
        }
    }

    private func backendEnded(status: Int32) {
        log.error("backend ended unexpectedly, status \(status, privacy: .public)")
        monitor?.stop()
        monitor = nil
        windowController.showMessage("Elstin stopped")
        windowController.show()
        let alert = NSAlert()
        alert.alertStyle = .critical
        alert.messageText = "Elstin stopped unexpectedly"
        alert.informativeText = "Its background process ended (exit status \(status)). Work that was running is picked up again from its last checkpoint when Elstin restarts."
        alert.addButton(withTitle: "Restart")
        alert.addButton(withTitle: "Show Log")
        alert.addButton(withTitle: "Quit")
        switch alert.runModal() {
        case .alertFirstButtonReturn:
            Task { await startBackend() }
        case .alertSecondButtonReturn:
            showLog()
            backendEnded(status: status)
        default:
            quitWithoutAsking = true
            requestQuit()
        }
    }

    // MARK: quitting

    /// Every quit the app starts itself goes through here, never straight to `NSApp.terminate`.
    /// Called from a block on the main dispatch queue — a signal handler, a Task — `terminate`
    /// waits for `applicationShouldTerminate`'s answer in a nested run loop that cannot run
    /// the main-queue work which gives it (the block that called it has not returned), and the
    /// app hangs. A run-loop timer is not a block on that queue, so the quit starts from outside it.
    private func requestQuit() {
        NSApp.perform(#selector(NSApplication.terminate(_:)), with: nil, afterDelay: 0)
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        Task { @MainActor in
            if !quitWithoutAsking, let monitor {
                // Ask now, not from the last poll: a run may have started a moment ago.
                let status = await monitor.fetch() ?? monitor.latest
                if let prompt = status.quitPrompt, !confirmQuit(prompt) {
                    NSApp.reply(toApplicationShouldTerminate: false)
                    return
                }
            }
            monitor?.stop()
            await supervisor?.stop()
            NSApp.reply(toApplicationShouldTerminate: true)
        }
        return .terminateLater
    }

    private func confirmQuit(_ prompt: QuitPrompt) -> Bool {
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = prompt.title
        alert.informativeText = prompt.detail
        alert.addButton(withTitle: "Keep Running")  // the default: the safe answer
        alert.addButton(withTitle: "Quit Anyway")
        return alert.runModal() == .alertSecondButtonReturn
    }

    // MARK: the window, from the Dock and the menus

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        windowController?.show()
        return true
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    @objc func showWindow() { windowController.show() }
    @objc func reload() { windowController.reload() }
    @objc func zoomIn() { windowController.zoom(by: 1.1) }
    @objc func zoomOut() { windowController.zoom(by: 1 / 1.1) }
    @objc func resetZoom() { windowController.resetZoom() }

    @objc func showAbout() {
        NSApp.orderFrontStandardAboutPanel(options: [
            .credits: NSAttributedString(string: "Turns a JIRA epic into a pull request, on your own machine.\n\nYour data: \(AppPaths.dataDirectory.path)"),
        ])
    }

    @objc func showLog() {
        if FileManager.default.fileExists(atPath: AppPaths.logFile.path) {
            NSWorkspace.shared.activateFileViewerSelecting([AppPaths.logFile])
        } else {
            NSWorkspace.shared.open(AppPaths.logsDirectory.deletingLastPathComponent())
        }
    }

    @objc func showDataFolder() {
        let folder = AppPaths.dataDirectory
        if FileManager.default.fileExists(atPath: folder.path) {
            NSWorkspace.shared.open(folder)
        } else {
            NSWorkspace.shared.open(folder.deletingLastPathComponent())
        }
    }
}
