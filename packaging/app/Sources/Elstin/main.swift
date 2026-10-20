import AppKit

let options = LaunchOptions.parse(CommandLine.arguments)
if options.checkBackend { exit(BundleCheck.run(options)) }

// Held for the life of the process: NSApplication keeps only a weak reference to its delegate.
let delegate = MainActor.assumeIsolated { AppDelegate(options: options) }
MainActor.assumeIsolated {
    let application = NSApplication.shared
    application.delegate = delegate
    application.setActivationPolicy(.regular)
    application.run()
}
