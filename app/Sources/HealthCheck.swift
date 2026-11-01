import Foundation

/// 用完才檢查：這套輸入沒有固定排程、App 是「要用才開」，所以健康訊號只在
/// 「真的用它」的時候才有意義。這裡不判斷好壞，只負責叫 `healthcheck.py`：
///
/// - App 啟動時跑一次（正要開始用的那一刻，順便抓跨次的問題：熱鍵 agent 死掉、
///   上一次以非零結束）。
/// - 每一次轉寫結束都跑一次（成功、空白、失敗都算），因為「按了卻沒拿到字」才是
///   使用者的痛點，那個答案只有 `.run.log` 有。
///
/// 要不要開 GitHub issue 由 python 端決定（`--emit --apply`）；健康時它一個字都不會印。
/// 子行程是 detached 的，不阻塞錄音與貼字，輸出另外寫檔，不污染 `.run.log`。
enum HealthCheck {
    private static let lock = NSLock()
    private static var lastRun = Date.distantPast
    /// 一次操作可能連續觸發（結果行、exit 4…），10 秒內只跑一次。
    private static let minInterval: TimeInterval = 10

    /// 檢查的台詞寫在這裡（health.json 之外的人看得懂的輸出）。
    static var logPath: String {
        let dir = (NSHomeDirectory() as NSString).appendingPathComponent("orca/state/voice-input")
        return (dir as NSString).appendingPathComponent("healthcheck.log")
    }

    static func run(reason: String) {
        let script = Transcriber.healthCheckPath
        guard FileManager.default.fileExists(atPath: script) else { return }

        lock.lock()
        let now = Date()
        guard now.timeIntervalSince(lastRun) >= minInterval else { lock.unlock(); return }
        lastRun = now
        lock.unlock()

        Transcriber.log("[health] 檢查觸發（reason=\(reason)）")
        DispatchQueue.global(qos: .utility).async {
            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: Transcriber.pythonBin)
            proc.arguments = [script, "--emit", "--apply"]
            // gh 在 /opt/homebrew/bin：GUI 啟動的 App 沒有登入 shell 的 PATH。
            var env = ProcessInfo.processInfo.environment
            let extra = "/opt/homebrew/bin:/usr/local/bin"
            env["PATH"] = extra + ":" + (env["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin")
            proc.environment = env
            proc.currentDirectoryURL = URL(fileURLWithPath: (script as NSString).deletingLastPathComponent)

            let fm = FileManager.default
            try? fm.createDirectory(atPath: (logPath as NSString).deletingLastPathComponent,
                                    withIntermediateDirectories: true)
            if !fm.fileExists(atPath: logPath) { _ = fm.createFile(atPath: logPath, contents: nil) }
            let handle = FileHandle(forWritingAtPath: logPath)
            handle?.seekToEndOfFile()
            handle?.write(Data("[\(ISO8601DateFormatter().string(from: Date()))] 觸發 reason=\(reason)\n".utf8))
            if let h = handle { proc.standardOutput = h; proc.standardError = h }

            do {
                try proc.run()
            } catch {
                Transcriber.log("[health] 跑不起來：\(error.localizedDescription)")
                try? handle?.close()
                return
            }
            proc.waitUntilExit()
            try? handle?.close()
            if proc.terminationStatus != 0 {
                Transcriber.log("[health] exit=\(proc.terminationStatus)（詳見 \(logPath)）")
            }
        }
    }
}
