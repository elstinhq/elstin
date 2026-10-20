import Foundation

/// What the backend tells the window once it is serving: the port it picked, the
/// access token for this launch, and its process id. See elstin/launcher.py.
public struct BackendReady: Equatable, Sendable {
    public let port: Int
    public let token: String
    public let pid: Int32

    public init(port: Int, token: String, pid: Int32) {
        self.port = port
        self.token = token
        self.pid = pid
    }

    /// The first request the window makes: the token travels in the address once, the
    /// server answers with a cookie and a redirect that drops it (elstin/api/access_guard.py).
    public var pageURL: URL {
        var parts = URLComponents()
        parts.scheme = "http"
        parts.host = "127.0.0.1"
        parts.port = port
        parts.path = "/"
        parts.queryItems = [URLQueryItem(name: "token", value: token)]
        return parts.url!
    }

    public var statusURL: URL { URL(string: "http://127.0.0.1:\(port)/api/status")! }
}

/// One line the backend writes to stdout. Nothing else is written there.
public enum BackendMessage: Equatable, Sendable {
    case ready(BackendReady)
    case failed(code: String, message: String)

    private struct ReadyPayload: Decodable { let port: Int; let token: String; let pid: Int32 }
    private struct ErrorPayload: Decodable { let error: String; let message: String }

    public static func parse(line: String) -> BackendMessage? {
        if let json = payload(of: line, prefix: "ELSTIN_READY "),
           let ready = try? JSONDecoder().decode(ReadyPayload.self, from: json) {
            return .ready(BackendReady(port: ready.port, token: ready.token, pid: ready.pid))
        }
        if let json = payload(of: line, prefix: "ELSTIN_ERROR "),
           let failure = try? JSONDecoder().decode(ErrorPayload.self, from: json) {
            return .failed(code: failure.error, message: failure.message)
        }
        return nil
    }

    private static func payload(of line: String, prefix: String) -> Data? {
        guard line.hasPrefix(prefix) else { return nil }
        return String(line.dropFirst(prefix.count)).data(using: .utf8)
    }
}
