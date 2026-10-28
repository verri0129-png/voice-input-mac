import Foundation

/// One-shot Typeless-style learning: if the user hand-edits inserted text,
/// infer a narrow source→correction mapping and append it to the local terms.
enum EditLearning {
    static var delaySeconds: TimeInterval = 20

    private struct Pending {
        let originalField: String
        /// 貼完（含定版）當下的整份欄位內容。比對「有沒有手改」要拿它當基準——
        /// 空白欄位開新備忘錄直接講的情況，`originalField` 是空字串，不能拿來比。
        let baselineField: String
        let insertedText: String
        let before: String
        let after: String
        let bundleID: String
        let windowTitle: String
        let fieldRole: String
        let termsFile: URL
    }

    /// 20 秒後讀不到欄位時（使用者切去別的 App、Notes 的 AX 還沒穩）多重試幾次。
    private static let retryInterval: TimeInterval = 5
    private static let maxRetries = 12

    static func schedule(
        originalField: String?,
        baselineField: String?,
        insertedText: String,
        context: [String: Any],
        termsFile: URL
    ) {
        guard shouldLearn() else {
            Transcriber.log("[learn] 略過：VOICE_LEARN_EDITS=0")
            return
        }
        guard !insertedText.isEmpty else {
            Transcriber.log("[learn] 略過：注入文字為空")
            return
        }
        // 空白欄位也支援：貼上前抓不到文字時，用「貼完當下的欄位內容」當基準，
        // original_field 記成空字串交給 python 端（它支援空白起點）。
        let baseline = baselineField ?? originalField ?? ""
        guard !baseline.isEmpty else {
            Transcriber.log("[learn] 略過：貼完後也讀不到欄位內容")
            return
        }
        guard let bundleID = context["bundle_id"] as? String,
              let windowTitle = context["window_title"] as? String,
              let fieldRole = context["field_role"] as? String else {
            Transcriber.log("[learn] 略過：context 缺 bundle_id/window_title/field_role")
            return
        }
        let pending = Pending(
            originalField: originalField ?? "",
            baselineField: baseline,
            insertedText: insertedText,
            before: context["text_before"] as? String ?? "",
            after: context["text_after"] as? String ?? "",
            bundleID: bundleID,
            windowTitle: windowTitle,
            fieldRole: fieldRole,
            termsFile: termsFile
        )
        DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + delaySeconds) {
            check(pending: pending, attempt: 0)
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

    private static func check(pending: Pending, attempt: Int) {
        guard let current = ContextCapture.learningSnapshot() else {
            retry(pending: pending, attempt: attempt, why: "抓不到欄位（欄位不可讀）")
            return
        }
        guard current["bundle_id"] == pending.bundleID,
              current["window_title"] == pending.windowTitle,
              current["field_role"] == pending.fieldRole else {
            retry(
                pending: pending,
                attempt: attempt,
                why: "焦點在別的 App（app=\(current["bundle_id"] ?? "-")"
                    + " title=\(current["window_title"] ?? "-") role=\(current["field_role"] ?? "-")）"
            )
            return
        }
        guard let currentField = current["field_text"] as? String else {
            retry(pending: pending, attempt: attempt, why: "欄位讀不到文字")
            return
        }
        guard currentField != pending.baselineField else {
            retry(pending: pending, attempt: attempt, why: "文字跟貼完時一樣（還沒手動修改）")
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

    /// 讀不到可信的欄位就隔幾秒再看。使用者常常貼完就切去別的 App（或欄位一時讀不到），
    /// 只判斷一次等於大部分情況都學不到；預設多等 60 秒。
    private static func retry(pending: Pending, attempt: Int, why: String) {
        guard attempt < maxRetries else {
            Transcriber.log(
                "[learn] 放棄（再多等 \(Int(retryInterval * Double(maxRetries))) 秒仍不可用）：\(why)"
            )
            return
        }
        if attempt == 0 {
            Transcriber.log("[learn] 先不判斷（\(why)），每 \(Int(retryInterval)) 秒再看一次")
        }
        DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + retryInterval) {
            check(pending: pending, attempt: attempt + 1)
        }
    }
}
