import Foundation

public struct QuitPrompt: Equatable, Sendable {
    public let title: String
    public let detail: String
}

/// What the backend is working on (GET /api/status): the window asks before quitting stops
/// it, and keeps the Mac awake while there is any.
public struct WorkStatus: Decodable, Equatable, Sendable {
    public let busy: Bool
    public let stories: [String]
    public let poSessions: Int
    public let publishing: Int

    enum CodingKeys: String, CodingKey { case busy, stories, publishing, poSessions = "po_sessions" }

    public init(busy: Bool, stories: [String], poSessions: Int, publishing: Int) {
        self.busy = busy
        self.stories = stories
        self.poSessions = poSessions
        self.publishing = publishing
    }

    public static let idle = WorkStatus(busy: false, stories: [], poSessions: 0, publishing: 0)

    /// "3 stories (ENG-1, ENG-2, ENG-3), a PO draft and a publish to JIRA"
    public var summary: String {
        var parts: [String] = []
        if !stories.isEmpty {
            let shown = stories.prefix(3).joined(separator: ", ")
            let more = stories.count > 3 ? " and \(stories.count - 3) more" : ""
            parts.append(stories.count == 1 ? "1 story (\(shown))" : "\(stories.count) stories (\(shown)\(more))")
        }
        if poSessions > 0 { parts.append(poSessions == 1 ? "a PO draft" : "\(poSessions) PO drafts") }
        if publishing > 0 { parts.append(publishing == 1 ? "a publish to JIRA" : "\(publishing) publishes to JIRA") }
        switch parts.count {
        case 0: return "nothing"
        case 1: return parts[0]
        default: return parts.dropLast().joined(separator: ", ") + " and " + parts.last!
        }
    }

    /// Nil when quitting costs nothing.
    public var quitPrompt: QuitPrompt? {
        guard busy else { return nil }
        return QuitPrompt(
            title: "Elstin is still working",
            detail: "It is working on \(summary).\n\nQuitting stops it. Stories are picked up again from their last checkpoint the next time Elstin starts, but the step in progress is repeated. Closing the window instead leaves everything running."
        )
    }
}
