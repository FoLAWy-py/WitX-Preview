// swift-tools-version: 6.0
// The rules of the WitX app's downloads: pure, so `swift test` checks them on the Mac
// without a simulator.
import PackageDescription

let package = Package(
    name: "WitXDownloads",
    platforms: [.iOS(.v18), .macOS(.v15)],
    products: [.library(name: "WitXDownloads", targets: ["WitXDownloads"])],
    targets: [
        .target(name: "WitXDownloads"),
        .testTarget(name: "WitXDownloadsTests", dependencies: ["WitXDownloads"]),
    ]
)
