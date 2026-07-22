// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "OmniCraftPets",
    platforms: [.macOS(.v14)],
    products: [
        .library(name: "OmniCraftPets", targets: ["OmniCraftPets"])
    ],
    targets: [
        .target(
            name: "OmniCraftPets",
            path: "Sources/OmniCraftPets",
            resources: [.copy("Resources/Pets")]
        )
    ]
)
