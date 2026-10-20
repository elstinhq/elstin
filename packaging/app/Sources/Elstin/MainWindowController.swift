import AppKit
import ElstinCore
import WebKit

/// The one window: a web view showing elstin's own page and nothing else.
///
/// Closing the window hides it, it does not quit: runs go on for hours, and the app is
/// still there in the Dock, to bring the window back.
@MainActor
final class MainWindowController: NSObject, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate {
    let window: NSWindow
    let webView: WKWebView
    private(set) var backendPort: Int?
    private var titleObservation: NSKeyValueObservation?
    private static let zoomKey = "pageZoom"

    init(inspectable: Bool) {
        let configuration = WKWebViewConfiguration()
        configuration.applicationNameForUserAgent = "Elstin"
        configuration.websiteDataStore = .default()  // the UI keeps its theme and sidebar state in localStorage
        webView = WKWebView(frame: .zero, configuration: configuration)
        webView.isInspectable = inspectable
        webView.underPageBackgroundColor = .windowBackgroundColor
        let saved = UserDefaults.standard.double(forKey: Self.zoomKey)
        webView.pageZoom = saved > 0 ? saved : 1.0

        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1280, height: 820),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered, defer: false
        )
        super.init()
        window.title = "Elstin"
        window.contentView = webView
        window.minSize = NSSize(width: 900, height: 600)
        window.isReleasedWhenClosed = false
        window.tabbingMode = .disallowed
        window.setFrameAutosaveName("ElstinMainWindow")
        if !window.setFrameUsingName("ElstinMainWindow") { window.center() }
        window.delegate = self
        webView.navigationDelegate = self
        webView.uiDelegate = self
        titleObservation = webView.observe(\.title, options: [.new]) { [weak self] view, _ in
            MainActor.assumeIsolated { self?.window.title = view.title.flatMap { $0.isEmpty ? nil : $0 } ?? "Elstin" }
        }
    }

    // MARK: showing things

    func show() {
        window.makeKeyAndOrderFront(nil)
        NSApp.activate()
    }

    func showMessage(_ message: String, detail: String = "") {
        backendPort = nil
        let escape = { (text: String) in text.replacingOccurrences(of: "&", with: "&amp;").replacingOccurrences(of: "<", with: "&lt;") }
        webView.loadHTMLString("""
            <!doctype html><meta charset="utf-8"><meta name="color-scheme" content="light dark">
            <body style="font: 15px -apple-system, sans-serif; display: flex; height: 100vh; margin: 0; align-items: center; justify-content: center; flex-direction: column; color: gray">
            <div style="font-size: 17px">\(escape(message))</div><div style="margin-top: 8px; font-size: 13px; max-width: 480px; text-align: center">\(escape(detail))</div>
            </body>
            """, baseURL: nil)
    }

    func load(_ ready: BackendReady) {
        backendPort = ready.port
        webView.load(URLRequest(url: ready.pageURL))
    }

    func reload() { webView.reload() }

    func zoom(by factor: Double) { setZoom(webView.pageZoom * factor) }
    func resetZoom() { setZoom(1.0) }

    private func setZoom(_ value: Double) {
        webView.pageZoom = min(max(value, 0.5), 3.0)
        UserDefaults.standard.set(webView.pageZoom, forKey: Self.zoomKey)
    }

    // MARK: NSWindowDelegate

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        sender.orderOut(nil)
        return false
    }

    // MARK: WKNavigationDelegate

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction, decisionHandler: @escaping @MainActor @Sendable (WKNavigationActionPolicy) -> Void) {
        switch NavigationPolicy.decide(url: navigationAction.request.url, backendPort: backendPort) {
        case .allow:
            decisionHandler(.allow)
        case .openExternally(let url):
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
        case .cancel:
            decisionHandler(.cancel)
        }
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        webView.reload()  // WebKit's page process died (memory pressure, a crash); the app did not
    }

    // MARK: WKUIDelegate — links that open a new window, and the page's own dialogs

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration, for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        // target="_blank": JIRA and GitHub links. Elstin's own pages stay in this window.
        switch NavigationPolicy.decide(url: navigationAction.request.url, backendPort: backendPort) {
        case .openExternally(let url): NSWorkspace.shared.open(url)
        case .allow: webView.load(navigationAction.request)
        case .cancel: break
        }
        return nil
    }

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping @MainActor @Sendable () -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.addButton(withTitle: "OK")
        alert.beginSheetModal(for: window) { _ in completionHandler() }
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping @MainActor @Sendable (Bool) -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: "Cancel")
        alert.beginSheetModal(for: window) { completionHandler($0 == .alertFirstButtonReturn) }
    }

    func webView(_ webView: WKWebView, runJavaScriptTextInputPanelWithPrompt prompt: String, defaultText: String?, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping @MainActor @Sendable (String?) -> Void) {
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 260, height: 24))
        field.stringValue = defaultText ?? ""
        let alert = NSAlert()
        alert.messageText = prompt
        alert.accessoryView = field
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: "Cancel")
        alert.beginSheetModal(for: window) { completionHandler($0 == .alertFirstButtonReturn ? field.stringValue : nil) }
    }

    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping @MainActor @Sendable ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.canChooseDirectories = parameters.allowsDirectories
        panel.beginSheetModal(for: window) { completionHandler($0 == .OK ? panel.urls : nil) }
    }
}
