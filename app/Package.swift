// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "VoiceInputMac",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "VoiceInputMac", targets: ["VoiceInputMac"])
    ],
    targets: [
        .executableTarget(
            name: "VoiceInputMac",
            path: "Sources",
            swiftSettings: [.unsafeFlags(["-O"])]
        )
    ]
)
