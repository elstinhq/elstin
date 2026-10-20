// swift-tools-version: 6.0
import PackageDescription

// The Mac app's window. ElstinCore is everything that can be tested without a screen
// (parsing the backend's ready line, the login-shell environment, the navigation
// policy, supervising the backend process); Elstin is the AppKit layer on top.
// No dependencies: SwiftPM, XCTest and the system frameworks all ship with Xcode.
let package = Package(
    name: "Elstin",
    platforms: [.macOS(.v15)],
    products: [.executable(name: "Elstin", targets: ["Elstin"])],
    targets: [
        .target(name: "ElstinCore"),
        .executableTarget(name: "Elstin", dependencies: ["ElstinCore"]),
        .testTarget(name: "ElstinCoreTests", dependencies: ["ElstinCore"]),
    ],
    swiftLanguageModes: [.v5]
)
