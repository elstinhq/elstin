import AppKit
import ElstinCore
import os

/// Asks the backend, every few seconds, what it is working on. While it is working the Mac
/// is kept awake — a laptop that sleeps pauses every agent, and a run can take hours — and the
/// Dock icon shows how many things are running. Quitting asks it once more, fresh.
@MainActor
final class StatusMonitor {
    private let ready: BackendReady
    private let session: URLSession
    private var timer: Timer?
    private var assertion: NSObjectProtocol?
    private let log = Logger(subsystem: "com.elstin.Elstin", category: "status")
    private(set) var latest: WorkStatus = .idle

    init(ready: BackendReady) {
        self.ready = ready
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 3
        session = URLSession(configuration: configuration)
    }

    func start() {
        Task { await refresh() }
        timer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in
            Task { @MainActor in await self?.refresh() }
        }
    }

    func stop() {
        timer?.invalidate()
        timer = nil
        apply(.idle)
        session.invalidateAndCancel()
    }

    /// Nil if the backend cannot be reached.
    func fetch() async -> WorkStatus? {
        var request = URLRequest(url: ready.statusURL)
        request.setValue("Bearer \(ready.token)", forHTTPHeaderField: "Authorization")
        guard let (data, response) = try? await session.data(for: request),
              (response as? HTTPURLResponse)?.statusCode == 200
        else { return nil }
        return try? JSONDecoder().decode(WorkStatus.self, from: data)
    }

    @discardableResult
    func refresh() async -> WorkStatus {
        if let status = await fetch() { apply(status) }
        return latest
    }

    private func apply(_ status: WorkStatus) {
        latest = status
        let count = status.stories.count + status.poSessions + status.publishing
        NSApp.dockTile.badgeLabel = status.busy && count > 0 ? String(count) : nil
        if status.busy, assertion == nil {
            assertion = ProcessInfo.processInfo.beginActivity(options: [.idleSystemSleepDisabled, .userInitiated], reason: "Elstin is running agents")
            log.info("keeping the Mac awake: \(status.summary, privacy: .public)")
        } else if !status.busy, let held = assertion {
            ProcessInfo.processInfo.endActivity(held)
            assertion = nil
            log.info("nothing running; the Mac may sleep again")
        }
    }
}
