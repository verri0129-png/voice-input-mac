import Foundation

enum Transcriber {
    static var pythonBin: String {
        let venv = (NSHomeDirectory() as NSString).appendingPathComponent("orca/voice-input-mac/.venv/bin/python")
        if FileManager.default.isExecutableFile(atPath: venv) { return venv }
        return "/opt/homebrew/bin/python3"
    }
    static var scriptPath: String {
        (NSHomeDirectory() as NSString).appendingPathComponent("orca/voice-input-mac/transcribe.py")
    }

    static func transcribe(wavURL: URL) throws -> String {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: pythonBin)
        proc.arguments = [scriptPath, wavURL.path, "--copy"]
        let pipe = Pipe()
        proc.standardOutput = pipe
        proc.standardError = Pipe()
        try proc.run()
        proc.waitUntilExit()
        let out = String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
        guard proc.terminationStatus == 0 else {
            throw NSError(domain: "Transcriber", code: Int(proc.terminationStatus), userInfo: [NSLocalizedDescriptionKey: out])
        }
        let lines = out.components(separatedBy: .newlines).map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
        let polished = lines.last(where: { !$0.hasPrefix("[") }) ?? out.trimmingCharacters(in: .whitespacesAndNewlines)
        return polished
    }

    static func pasteViaClipboard() {
        let src = Process()
        src.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        src.arguments = ["-e", "tell application \"System Events\" to keystroke \"v\" using command down"]
        try? src.run()
        src.waitUntilExit()
    }
}
