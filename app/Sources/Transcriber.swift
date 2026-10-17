import Foundation
import AppKit
import CoreGraphics
import ApplicationServices

enum Transcriber {
    static var pythonBin: String {
        let venv = (NSHomeDirectory() as NSString).appendingPathComponent("orca/voice-input-mac/.venv/bin/python")
        if FileManager.default.isExecutableFile(atPath: venv) { return venv }
        return "/opt/homebrew/bin/python3"
    }
    static var scriptPath: String {
        // 測試用：可覆寫成 stub（驗證 app 端對 exit 3/4 的處理，不必真的錄音）
        if let override = ProcessInfo.processInfo.environment["VOICE_INPUT_TRANSCRIBE_PY"],
           !override.isEmpty { return override }
        return (NSHomeDirectory() as NSString).appendingPathComponent("orca/voice-input-mac/transcribe.py")
    }
    static var logPath: String {
        (NSHomeDirectory() as NSString).appendingPathComponent("orca/voice-input-mac/.run.log")
    }

    // app 用 `open`/launchd 啟動時 stdout 沒人接，診斷一律寫檔
    static func log(_ message: String) {
        let ts = ISO8601DateFormatter().string(from: Date())
        let line = "[\(ts)] \(message)\n"
        let fm = FileManager.default
        if !fm.fileExists(atPath: logPath) { fm.createFile(atPath: logPath, contents: nil) }
        guard let h = FileHandle(forWritingAtPath: logPath) else { return }
        defer { try? h.close() }
        h.seekToEndOfFile()
        h.write(Data(line.utf8))
    }

    /// 回傳 (final 文字, suspect)。suspect = transcribe.py exit 4：有文字但疑似不是語音，
    /// app 端必須「不自動貼上」但保留文字（可手動貼）。
    static func transcribeOneShot(wavURL: URL) throws -> (text: String, suspect: Bool) {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: pythonBin)
        proc.arguments = [scriptPath, wavURL.path, "--copy"]
        let outPipe = Pipe()
        let errPipe = Pipe()
        proc.standardOutput = outPipe
        proc.standardError = errPipe
        try proc.run()

        // stderr 必須並行抽乾：whisper/mlx 的進度列很吵，pipe 滿了子行程會永遠卡住（waitUntilExit 不返回）
        var errData = Data()
        let errLock = NSLock()
        errPipe.fileHandleForReading.readabilityHandler = { h in
            let d = h.availableData
            guard !d.isEmpty else { h.readabilityHandler = nil; return }
            errLock.lock(); errData.append(d); errLock.unlock()
        }
        let outData = outPipe.fileHandleForReading.readDataToEndOfFile()
        proc.waitUntilExit()
        errPipe.fileHandleForReading.readabilityHandler = nil
        errLock.lock()
        let diag = (String(data: errData, encoding: .utf8) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        errLock.unlock()
        let out = (String(data: outData, encoding: .utf8) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)

        log("transcribe exit=\(proc.terminationStatus) wav=\(wavURL.lastPathComponent)")
        if !diag.isEmpty { log("  stderr: \(diag)") }
        log("  stdout: \(out)")

        // exit 4 = transcribe.py 判定「疑似不是語音」：文字有效但不自動貼（避免噪音幻覺
        // 被當成逐字稿貼出去）。用原始碼的 0.4s 去抖動同一個訊息通道送出去。
        if proc.terminationStatus == 4 {
            return (out, true)
        }
        guard proc.terminationStatus == 0 else {
            throw NSError(domain: "Transcriber", code: Int(proc.terminationStatus),
                          userInfo: [NSLocalizedDescriptionKey: "transcribe.py 失敗（exit \(proc.terminationStatus)）\n\(diag.suffix(500))"])
        }
        // 契約：transcribe.py 的 stdout 只放最終文字（診斷全走 stderr）。
        // 以前這裡會在取不到時退回「整份 stdout」，於是 "[raw] <簡體原文>" 被當成結果貼出去。
        guard !out.hasPrefix("[") else {
            throw NSError(domain: "Transcriber", code: -1,
                          userInfo: [NSLocalizedDescriptionKey: "輸出格式異常，取不到最終文字：\(out.prefix(200))"])
        }
        return (out, false)
    }

    // MARK: - 暖機伺服器：把「載入模型」的成本搬到「開始錄音」時
    //
    // 實測：一次性路徑每次都要重載 Whisper(1.6GB)+Qwen 7B 4bit(約4GB) → 冷啟動 4.26s；
    // 模型還在記憶體時只要 0.88s。所以在開始錄音時就把常駐的 python 拉起來暖機，
    // 使用者講話的那幾秒剛好拿來載模型，按下停止後只剩推論。
    // 任何失敗（啟動不了、逾時、回應壞掉）都靜默退回下面的一次性路徑，不會比現在差。

    private static let serverQueue = DispatchQueue(label: "local.hunghanchu.voice-input.serve")
    private static let serverLock = NSLock()
    private static var _server: Process?
    private static var _serverIn: FileHandle?
    private static var _serverOut: FileHandle?

    private static func serverHandles() -> (Process, FileHandle, FileHandle)? {
        serverLock.lock(); defer { serverLock.unlock() }
        guard let p = _server, p.isRunning, let i = _serverIn, let o = _serverOut else { return nil }
        return (p, i, o)
    }

    private static func setServer(_ p: Process?, _ i: FileHandle?, _ o: FileHandle?) {
        serverLock.lock(); _server = p; _serverIn = i; _serverOut = o; serverLock.unlock()
    }

    /// 開始錄音時呼叫：把模型載入成本移到「使用者正在講話」的那幾秒。
    static func startWarmServer() {
        serverQueue.async {
            if let (p, _, _) = serverHandles() { Transcriber.log("[serve] 已有暖機程序 pid=\(p.processIdentifier)，不重啟"); return }
            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: pythonBin)
            proc.arguments = [scriptPath, "--serve"]
            let inPipe = Pipe(), outPipe = Pipe(), errPipe = Pipe()
            proc.standardInput = inPipe
            proc.standardOutput = outPipe
            proc.standardError = errPipe
            do { try proc.run() } catch {
                Transcriber.log("[serve] 啟動失敗（之後走一次性路徑）: \(error.localizedDescription)")
                return
            }
            // stderr 必須抽乾：mlx 的進度列很吵，pipe 滿了子行程會卡住
            errPipe.fileHandleForReading.readabilityHandler = { h in
                let d = h.availableData
                guard !d.isEmpty else { h.readabilityHandler = nil; return }
                let s = (String(data: d, encoding: .utf8) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
                if !s.isEmpty { Transcriber.log("  serve: \(s.suffix(300))") }
            }
            setServer(proc, inPipe.fileHandleForWriting, outPipe.fileHandleForReading)
            Transcriber.log("[serve] 暖機程序啟動 pid=\(proc.processIdentifier)")
        }
    }

    private static func killServer(reason: String) {
        serverQueue.async {
            if let (p, i, _) = serverHandles() {
                Transcriber.log("[serve] 收掉暖機程序（\(reason)）pid=\(p.processIdentifier)")
                try? i.close()
                p.terminate()
            }
            setServer(nil, nil, nil)
        }
    }

    /// 讀一行（阻塞）。EOF 回 nil。
    private static func readLine(_ h: FileHandle) -> String? {
        var buf = Data()
        while true {
            let d = h.availableData
            if d.isEmpty { return nil }
            buf.append(d)
            if let s = String(data: buf, encoding: .utf8), let idx = s.firstIndex(of: "\n") {
                return String(s[s.startIndex..<idx])
            }
            if buf.count > 4 << 20 { return nil }   // 防呆：正常一行只有幾百 bytes
        }
    }

    private static func serverRequest(wavPath: String, timeout: TimeInterval) -> String? {
        guard let (_, stdin, stdout) = serverHandles() else { return nil }
        do {
            try stdin.write(contentsOf: Data((wavPath + "\n").utf8))
        } catch {
            Transcriber.log("[serve] 寫入請求失敗: \(error.localizedDescription)")
            killServer(reason: "寫入失敗")
            return nil
        }
        var line: String?
        let sem = DispatchSemaphore(value: 0)
        DispatchQueue.global(qos: .userInitiated).async {
            line = readLine(stdout)
            sem.signal()
        }
        if sem.wait(timeout: .now() + timeout) == .timedOut {
            Transcriber.log("[serve] 等 \(Int(timeout))s 沒回應 → 收掉暖機程序，退回一次性路徑")
            killServer(reason: "請求逾時")
            return nil
        }
        return line
    }

    /// 暖機路徑。回傳 nil = 這條路不能走（呼叫端退回一次性）。
    /// 沒有可用語音一樣用 throw code 3 表示（與一次性路徑的 exit 3 同語意）。
    private static func transcribeWarm(wavURL: URL) throws -> (text: String, suspect: Bool)? {
        guard let raw = serverRequest(wavPath: wavURL.path, timeout: 30) else { return nil }
        guard let data = raw.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            Transcriber.log("[serve] 回應不是 JSON，退回一次性路徑：\(raw.prefix(120))")
            return nil
        }
        let status = (obj["status"] as? String) ?? "error"
        let text = ((obj["text"] as? String) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        Transcriber.log("[serve] status=\(status) 文字=\(text)")
        switch status {
        case "ok":      return (text, false)
        case "suspect": return (text, true)
        case "silence": throw NSError(domain: "Transcriber", code: 3,
                                      userInfo: [NSLocalizedDescriptionKey: "沒有可用語音"])
        default:        return nil   // error → 退回一次性路徑（那條路徑的診斷比較完整）
        }
    }

    /// 對外入口：先走暖機路徑（快），不行才退回一次性路徑（慢但一定通）。
    static func transcribe(wavURL: URL) throws -> (text: String, suspect: Bool) {
        if let r = try transcribeWarm(wavURL: wavURL) {
            return r
        }
        return try transcribeOneShot(wavURL: wavURL)
    }

    /// 啟動時檢查貼上要用的「自動化」權限。
    ///
    /// 送 ⌘V 的 AppleScript 需要 `kTCCServiceAppleEvents`；授權是綁簽章的，重新簽章後
    /// 舊授權會失效，而失敗完全靜默（若只查 System Events 的版本這種無害查詢，不會送按鍵）。
    /// 這裡先問一次，缺權限時對話框會出現在啟動當下，而不是貼上當下。
    static func preflightAutomation() {
        DispatchQueue.global(qos: .utility).async {
            // 用自己的行程送（不是 spawn osascript，見 pasteViaAppleScript 的說明）
            guard let script = NSAppleScript(source: "tell application \"System Events\" to get version") else {
                log("[perm] 無法建立 AppleScript")
                return
            }
            var err: NSDictionary?
            _ = script.executeAndReturnError(&err)
            if let err = err {
                log("[perm] 自動化權限不可用（貼上會失敗，文字仍會留在剪貼簿）: \(err)")
            } else {
                log("[perm] 自動化權限 OK（貼上可用）")
            }
        }
    }

    /// 把文字送進最前面的 app。
    ///
    /// 實測（2026-10-17）：這台機器上**所有帶 ⌘ 的合成按鍵都會被吃掉**——純打字成功、
    /// ⌘V／⌘A／⌘X 全部無效（System Events 回報成功但目標 app 什麼都沒收到；
    /// 機器上裝了一堆事件工具：Karabiner / WindowsShortcuts / 各家 computer-use）。
    /// 所以主要路徑改成 CGEvent unicode 注入（等同使用者打字），⌘V 只當保底。
    /// 剪貼簿一定也放一份，使用者手動 ⌘V 隨時可用。
    static func insertText(_ text: String) {
        guard !text.isEmpty else { return }
        // TUI／終端機會把換行當成「送出」：逐字稿不需要換行，一律換成空格
        let safe = text
            .replacingOccurrences(of: "\r\n", with: " ")
            .replacingOccurrences(of: "\n", with: " ")
            .replacingOccurrences(of: "\r", with: " ")
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(safe, forType: .string)
        log("[insert] 文字已放進剪貼簿（\(safe.count) 字），開始注入")
        if typeViaCGEvent(safe) { return }
        if pasteViaAppleScript() { return }
        if pasteViaCGEvent() { return }
        log("[insert] ❌ 三條路都不通 → 文字只在剪貼簿裡（手動按 ⌘V 可以貼）")
    }

    /// CGEvent unicode 注入：把字串當成鍵盤輸入打進最前面的 app。
    /// 不需要 ⌘ 修飾鍵（修飾鍵在本機的合成事件會被吃掉），但需要「輔助使用」權限。
    private static func typeViaCGEvent(_ text: String) -> Bool {
        guard AXIsProcessTrusted() else {
            log("[insert] 輔助使用權限未生效（AXIsProcessTrusted=false）→ 無法注入文字")
            return false
        }
        guard let src = CGEventSource(stateID: .combinedSessionState) else {
            log("[insert] CGEventSource 建立失敗")
            return false
        }
        let units = Array(text.utf16)
        let chunkSize = 20          // 一次送太長，終端機／TUI 會掉字
        var i = 0
        var chunks = 0
        while i < units.count {
            let end = min(i + chunkSize, units.count)
            let chunk = Array(units[i..<end])
            guard let down = CGEvent(keyboardEventSource: src, virtualKey: 0, keyDown: true),
                  let up = CGEvent(keyboardEventSource: src, virtualKey: 0, keyDown: false) else {
                log("[insert] CGEvent 建立失敗")
                return false
            }
            // 字串只掛在 keyDown：掛兩邊有些 app 會重複插入
            chunk.withUnsafeBufferPointer { p in
                down.keyboardSetUnicodeString(stringLength: chunk.count, unicodeString: p.baseAddress)
            }
            down.post(tap: CGEventTapLocation.cgAnnotatedSessionEventTap)
            up.post(tap: CGEventTapLocation.cgAnnotatedSessionEventTap)
            i = end
            chunks += 1
            if i < units.count { usleep(8000) }   // 8ms：讓目標 app 跟上
        }
        log("[insert] ✅ CGEvent 注入 \(units.count) 字（\(chunks) 塊）")
        return true
    }

    private static func pasteViaAppleScript() -> Bool {
        let t0 = Date()
        // 用自己行程送 AppleEvent（不是 spawn osascript）。TCC 的「自動化」授權綁「哪個行程送的」：
        // 用 osascript 時送按鍵的是 osascript（錯誤訊息就是寫它），授權／對話框不一定對得上我們的
        // bundle id；用 NSAppleScript 才保證是 local.hunghanchu.voice-input-mac 在送。
        guard let script = NSAppleScript(source: "tell application \"System Events\" to keystroke \"v\" using command down") else {
            log("[paste] 無法建立 AppleScript")
            return false
        }
        var err: NSDictionary?
        _ = script.executeAndReturnError(&err)
        let dt = Date().timeIntervalSince(t0)
        if let err = err {
            // 常見：-1743 / 1002 = 沒有自動化權限；-600 = System Events 沒起來
            log("[paste] AppleScript 失敗（耗時 \(String(format: "%.2f", dt))s）: \(err)")
            return false
        }
        log("[paste] ✅ AppleScript 送出 ⌘V（耗時 \(String(format: "%.2f", dt))s）")
        return true
    }

    private static func pasteViaCGEvent() -> Bool {
        guard AXIsProcessTrusted() else {
            log("[paste] 輔助使用權限未生效（AXIsProcessTrusted=false）→ CGEvent 保底也不可用")
            return false
        }
        guard let src = CGEventSource(stateID: .combinedSessionState),
              let down = CGEvent(keyboardEventSource: src, virtualKey: 9 /* kVK_ANSI_V */, keyDown: true),
              let up = CGEvent(keyboardEventSource: src, virtualKey: 9, keyDown: false) else {
            log("[paste] CGEvent 建立失敗")
            return false
        }
        down.flags = .maskCommand
        up.flags = .maskCommand
        down.post(tap: CGEventTapLocation.cgAnnotatedSessionEventTap)
        up.post(tap: CGEventTapLocation.cgAnnotatedSessionEventTap)
        log("[paste] ✅ CGEvent 送出 ⌘V")
        return true
    }
}
