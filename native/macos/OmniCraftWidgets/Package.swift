// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "OmniCraftWidgets",
    platforms: [.macOS(.v14)],
    dependencies: [
        .package(path: "../OmniCraftPets")   // mesmos pets do notch
    ],
    targets: [
        .executableTarget(
            name: "OmniCraftWidgets",
            dependencies: ["OmniCraftPets"],
            path: "Sources/OmniCraftWidgets"
        )
    ]
)
