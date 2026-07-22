// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "OmniCraftNotch",
    platforms: [.macOS(.v14)],
    dependencies: [
        .package(path: "../OmniCraftPets")  // pets compartilhados com os widgets
    ],
    targets: [
        .executableTarget(
            name: "OmniCraftNotch",
            dependencies: ["OmniCraftPets"],
            path: "Sources/OmniCraftNotch"
        ),
        .testTarget(
            name: "OmniCraftNotchTests",
            dependencies: ["OmniCraftNotch"],
            path: "Tests/OmniCraftNotchTests"
        ),
    ]
)
