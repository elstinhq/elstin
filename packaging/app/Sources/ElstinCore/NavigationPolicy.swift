import Foundation

public enum NavigationDecision: Equatable, Sendable {
    case allow
    case openExternally(URL)
    case cancel
}

/// What the window is allowed to show. The web view is elstin's own page and nothing else:
/// a link to JIRA or a GitHub pull request opens in the user's browser, and anything that
/// is not a web address (file:, javascript:, custom schemes) goes nowhere.
public enum NavigationPolicy {
    public static func decide(url: URL?, backendPort: Int?) -> NavigationDecision {
        guard let url, let scheme = url.scheme?.lowercased() else { return .cancel }
        switch scheme {
        case "about":
            return url.absoluteString == "about:blank" ? .allow : .cancel
        case "http", "https":
            if scheme == "http", url.host == "127.0.0.1", url.port == backendPort { return .allow }
            return .openExternally(url)
        case "mailto":
            return .openExternally(url)
        default:
            return .cancel
        }
    }
}
