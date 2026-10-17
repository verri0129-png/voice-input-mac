import Foundation

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

    static func pasteViaClipboard() {
        let t0 = Date()
        let src = Process()
        src.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        src.arguments = ["-e", "tell application \"System Events\" to keystroke \"v\" using command down"]
        try? src.run()
        src.waitUntilExit()
        // 貼上要等 System Events（有時還要等自動化權限），慢的話體感就是「等很久才出現文字」
        let dt = Date().timeIntervalSince(t0)
        if dt > 0.3 { log("[paste] osascript 花了 \(String(format: "%.2f", dt))s") }
    }
}
