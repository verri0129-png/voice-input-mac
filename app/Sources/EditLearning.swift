import Foundation

/// One-shot Typeless-style learning: if the user hand-edits inserted text,
/// infer a narrow source→correction mapping and append it to the local terms.
enum EditLearning {
    static var delaySeconds: TimeInterval = 20

    private struct Pending {
        let originalField: String
        let insertedText: String
        let before: String
        let after: String
        let bundleID: String
        let windowTitle: String
        let fieldRole: String
        let termsFile: URL
    }

    static func schedule(
        originalField: String?,
        insertedText: String,
        context: [String: Any],
        termsFile: URL
    ) {
        guard shouldLearn() else {
            Transcriber.log("[learn] 略過：VOICE_LEARN_EDITS=0")
            return
        }
        guard let originalField, !originalField.isEmpty else {
            Transcriber.log("[learn] 略過：貼上前抓不到欄位文字（learningSnapshot 失敗或欄位為空）")
            return
        }
        guard !insertedText.isEmpty else {
            Transcriber.log("[learn] 略過：注入文字為空")
            return
        }
        guard let bundleID = context["bundle_id"] as? String,
              let windowTitle = context["window_title"] as? String,
              let fieldRole = context["field_role"] as? String else {
            Transcriber.log("[learn] 略過：context 缺 bundle_id/window_title/field_role")
            return
        }
        let pending = Pending(
            originalField: originalField,
            insertedText: insertedText,
            before: context["text_before"] as? String ?? "",
            after: context["text_after"] as? String ?? "",
            bundleID: bundleID,
            windowTitle: windowTitle,
            fieldRole: fieldRole,
            termsFile: termsFile
        )
        DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + delaySeconds) {
            check(pending: pending)
        }
        Transcriber.log("[learn] 已排程：\(Int(delaySeconds)) 秒後比對欄位（app=\(bundleID)）")
    }

    static func shouldLearn() -> Bool {
        localValue("VOICE_LEARN_EDITS") != "0"
    }

    static func localValue(_ name: String) -> String? {
        if let value = ProcessInfo.processInfo.environment[name] {
            return value
        }
        let directory = URL(fileURLWithPath: Transcriber.scriptPath)
            .deletingLastPathComponent()
        let file = directory.appendingPathComponent("local.env")
        guard let text = try? String(contentsOf: file, encoding: .utf8) else {
            return nil
        }
        for rawLine in text.components(separatedBy: .newlines) {
            let code = String(rawLine.split(separator: "#", maxSplits: 1).first ?? "")
                .trimmingCharacters(in: .whitespacesAndNewlines)
            let pair = code.split(separator: "=", maxSplits: 1).map(String.init)
            guard pair.count == 2, pair[0].trimmingCharacters(in: .whitespaces) == name else {
                continue
            }
            return pair[1].trimmingCharacters(in: .whitespacesAndNewlines)
                .trimmingCharacters(in: CharacterSet(charactersIn: "\"'"))
        }
        return nil
    }

    static func termsFileURL() -> URL {
        if let override = localValue("VOICE_REWRITE_TERMS_FILE"), !override.isEmpty {
            let expanded = NSString(string: override).expandingTildeInPath
            let url = URL(fileURLWithPath: expanded)
            if expanded.hasPrefix("/") {
                return url
            }
            return URL(fileURLWithPath: Transcriber.scriptPath)
                .deletingLastPathComponent()
                .appendingPathComponent(expanded)
        }
        return URL(fileURLWithPath: NSHomeDirectory())
            .appendingPathComponent("orca/voice-input-mac/rewrite_terms.json")
    }

    private static func check(pending: Pending) {
        guard let current = ContextCapture.learningSnapshot() else {
            Transcriber.log("[learn] 略過：20 秒後抓不到欄位（換了 App 或欄位不可讀）")
            return
        }
        guard current["bundle_id"] == pending.bundleID,
              current["window_title"] == pending.windowTitle,
              current["field_role"] == pending.fieldRole else {
            Transcriber.log(
                "[learn] 略過：焦點已改變 app=\(current["bundle_id"] ?? "-")"
                + " title=\(current["window_title"] ?? "-") role=\(current["field_role"] ?? "-")"
            )
            return
        }
        guard let currentField = current["field_text"] else {
            Transcriber.log("[learn] 略過：欄位讀不到文字")
            return
        }
        guard currentField != pending.originalField else {
            Transcriber.log("[learn] 略過：文字跟貼上時一樣（沒有手動修改）")
            return
        }
        let payload: [String: String] = [
            "original_field": pending.originalField,
            "inserted_text": pending.insertedText,
            "text_before": pending.before,
            "text_after": pending.after,
            "current_field": currentField
        ]
        let file = FileManager.default.temporaryDirectory
            .appendingPathComponent("voice-learn-\(UUID().uuidString).json")
        do {
            let data = try JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys])
            try data.write(to: file, options: .atomic)
            defer { try? FileManager.default.removeItem(at: file) }
            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: Transcriber.pythonBin)
            proc.arguments = [
                Transcriber.scriptPath,
                "--learn-edit-json", file.path,
                "--terms-file", pending.termsFile.path
            ]
            let outPipe = Pipe()
            let errPipe = Pipe()
            proc.standardOutput = outPipe
            proc.standardError = errPipe
            try proc.run()
            let sem = DispatchSemaphore(value: 0)
            DispatchQueue.global(qos: .utility).async {
                proc.waitUntilExit()
                sem.signal()
            }
            guard sem.wait(timeout: .now() + 30) == .success else {
                proc.terminate()
                Transcriber.log("[learn] 檢查逾時，已放棄")
                return
            }
            let summary = String(
                data: outPipe.fileHandleForReading.readDataToEndOfFile(),
                encoding: .utf8
            )?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            let diagnostics = String(
                data: errPipe.fileHandleForReading.readDataToEndOfFile(),
                encoding: .utf8
            )?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            Transcriber.log("[learn] exit=\(proc.terminationStatus) \(summary)")
            if !diagnostics.isEmpty {
                Transcriber.log("[learn] stderr: \(diagnostics.suffix(300))")
            }
        } catch {
            Transcriber.log("[learn] 檢查失敗：\(error.localizedDescription)")
        }
    }
}
