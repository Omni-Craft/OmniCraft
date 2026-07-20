// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "OmniCraftWidgets",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(name: "OmniCraftWidgets", path: "Sources/OmniCraftWidgets")
    ]
)
