import AppKit

/// The menu bar. WKWebView does its own editing; these items only route the standard
/// shortcuts (⌘C, ⌘V, ⌘A ...) to it through the responder chain, which is what makes them
/// work at all in an app without a standard nib.
@MainActor
enum MenuBuilder {
    static func make(appName: String, delegate: AppDelegate) -> NSMenu {
        let main = NSMenu()

        let app = submenu(of: main, title: appName)
        app.addItem(withTitle: "About \(appName)", action: #selector(AppDelegate.showAbout), keyEquivalent: "").target = delegate
        app.addItem(.separator())
        app.addItem(withTitle: "Hide \(appName)", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        let hideOthers = app.addItem(withTitle: "Hide Others", action: #selector(NSApplication.hideOtherApplications(_:)), keyEquivalent: "h")
        hideOthers.keyEquivalentModifierMask = [.command, .option]
        app.addItem(withTitle: "Show All", action: #selector(NSApplication.unhideAllApplications(_:)), keyEquivalent: "")
        app.addItem(.separator())
        app.addItem(withTitle: "Quit \(appName)", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")

        let file = submenu(of: main, title: "File")
        file.addItem(withTitle: "Close Window", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")

        let edit = submenu(of: main, title: "Edit")
        edit.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        let redo = edit.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "z")
        redo.keyEquivalentModifierMask = [.command, .shift]
        edit.addItem(.separator())
        edit.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")

        let view = submenu(of: main, title: "View")
        view.addItem(withTitle: "Reload", action: #selector(AppDelegate.reload), keyEquivalent: "r").target = delegate
        view.addItem(.separator())
        view.addItem(withTitle: "Actual Size", action: #selector(AppDelegate.resetZoom), keyEquivalent: "0").target = delegate
        view.addItem(withTitle: "Zoom In", action: #selector(AppDelegate.zoomIn), keyEquivalent: "=").target = delegate
        view.addItem(withTitle: "Zoom Out", action: #selector(AppDelegate.zoomOut), keyEquivalent: "-").target = delegate
        view.addItem(.separator())
        let fullScreen = view.addItem(withTitle: "Enter Full Screen", action: #selector(NSWindow.toggleFullScreen(_:)), keyEquivalent: "f")
        fullScreen.keyEquivalentModifierMask = [.command, .control]

        let window = submenu(of: main, title: "Window")
        window.addItem(withTitle: "Minimize", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        window.addItem(withTitle: "Zoom", action: #selector(NSWindow.performZoom(_:)), keyEquivalent: "")
        window.addItem(.separator())
        window.addItem(withTitle: appName, action: #selector(AppDelegate.showWindow), keyEquivalent: "0").target = delegate
        window.items.last?.keyEquivalentModifierMask = [.command, .option]
        window.addItem(.separator())
        window.addItem(withTitle: "Bring All to Front", action: #selector(NSApplication.arrangeInFront(_:)), keyEquivalent: "")
        NSApp.windowsMenu = window

        let help = submenu(of: main, title: "Help")
        help.addItem(withTitle: "Show Log in Finder", action: #selector(AppDelegate.showLog), keyEquivalent: "").target = delegate
        help.addItem(withTitle: "Show Data Folder in Finder", action: #selector(AppDelegate.showDataFolder), keyEquivalent: "").target = delegate
        NSApp.helpMenu = help

        return main
    }

    private static func submenu(of parent: NSMenu, title: String) -> NSMenu {
        let item = parent.addItem(withTitle: title, action: nil, keyEquivalent: "")
        let menu = NSMenu(title: title)
        parent.setSubmenu(menu, for: item)
        return menu
    }
}
