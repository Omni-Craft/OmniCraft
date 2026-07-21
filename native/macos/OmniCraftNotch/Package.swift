// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "OmniCraftNotch",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(name: "OmniCraftNotch", path: "Sources/OmniCraftNotch"),
        .testTarget(
            name: "OmniCraftNotchTests",
            dependencies: ["OmniCraftNotch"],
            path: "Tests/OmniCraftNotchTests"
        ),
    ]
)
