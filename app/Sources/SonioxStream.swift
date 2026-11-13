import Foundation

/// Soniox 即時 STT 的 App 端接線。
///
/// 為什麼不讓 python 代收音訊：現有 serve 協定是「一行請求、一行回應」，塞進
/// 沒有回應的音訊訊息會讓排隊邏輯（預覽／正式請求共用一條管子）讀錯回應。
/// 所以音訊這一小段在 App 內直連 Soniox，停止後只把「一段文字」交回 python
/// 做繁化＋詞庫＋收尾——協定完全不變，Breeze 路線也一行沒動。
enum SonioxProvider {
    /// local.env 在 repo 根目錄（App 由 open 啟動，拿不到 shell 的環境變數）
    private static var envFile: [String: String] {
        let path = (NSHomeDirectory() as NSString)
            .appendingPathComponent("orca/voice-input-mac/local.env")
        guard let text = try? String(contentsOfFile: path, encoding: .utf8) else { return [:] }
        var map: [String: String] = [:]
        for raw in text.split(separator: "\n") {
            let line = raw.split(separator: "#", maxSplits: 1).first.map(String.init) ?? ""
            guard let eq = line.firstIndex(of: "=") else { continue }
            let key = line[..<eq].trimmingCharacters(in: .whitespaces)
            let value = line[line.index(after: eq)...]
                .trimmingCharacters(in: .whitespaces)
                .trimmingCharacters(in: CharacterSet(charactersIn: "\"'"))
            if !key.isEmpty { map[key] = value }
        }
        return map
    }

    private static func setting(_ name: String) -> String? {
        if let value = ProcessInfo.processInfo.environment[name], !value.isEmpty { return value }
        if let value = envFile[name], !value.isEmpty { return value }
        return nil
    }

    /// 目前是不是 Soniox 模式（local.env 的 VOICE_STT=soniox）
    static var enabled: Bool {
        (setting("VOICE_STT") ?? "breeze").lowercased() == "soniox"
    }

    static var model: String { setting("VOICE_SONIOX_MODEL") ?? "stt-rt-v5" }

    /// 金鑰：環境變數優先，其次沿用字幕專案那一份 secrets 檔
    static var apiKey: String? {
        for name in ["VOICE_SONIOX_API_KEY", "SONIOX_API_KEY"] {
            if let value = ProcessInfo.processInfo.environment[name], !value.isEmpty { return value }
        }
        let path = (NSHomeDirectory() as NSString)
            .appendingPathComponent(".config/agent-secrets/soniox.env")
        guard let text = try? String(contentsOfFile: path, encoding: .utf8) else { return nil }
        for raw in text.split(separator: "\n") {
            let line = raw.trimmingCharacters(in: .whitespaces)
            guard !line.hasPrefix("#"), let eq = line.firstIndex(of: "=") else { continue }
            let key = line[..<eq].trimmingCharacters(in: .whitespaces)
            if key == "SONIOX_API_KEY" || key == "VOICE_SONIOX_API_KEY" {
                let value = line[line.index(after: eq)...]
                    .trimmingCharacters(in: .whitespaces)
                    .trimmingCharacters(in: CharacterSet(charactersIn: "\"'"))
                if !value.isEmpty { return value }
            }
        }
        return nil
    }

    /// 熱詞：rewrite_terms.json 的「正字」＋ VOICE_SONIOX_TERMS（逗號分隔）
    static var terms: [String] {
        var words: [String] = []
        let path = (NSHomeDirectory() as NSString)
            .appendingPathComponent("orca/voice-input-mac/rewrite_terms.json")
        if let data = try? Data(contentsOf: URL(fileURLWithPath: path)),
           let doc = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let rows = doc["terms"] as? [Any] {
            for row in rows {
                if let dict = row as? [String: Any], let text = dict["text"] as? String, !text.isEmpty {
                    words.append(text)
                } else if let text = row as? String, !text.isEmpty {
                    words.append(text)
                }
            }
        }
        if let extra = setting("VOICE_SONIOX_TERMS") {
            for piece in extra.split(whereSeparator: { ",，、".contains($0) }) {
                let word = piece.trimmingCharacters(in: .whitespaces)
                if !word.isEmpty { words.append(word) }
            }
        }
        var unique: [String] = []
        for word in words where !unique.contains(word) { unique.append(word) }
        // 長詞先送：模型比較不會把長專有名詞拆成短詞
        unique.sort { $0.count > $1.count }
        return Array(unique.prefix(120))
    }
}

/// 一條 WebSocket = 一次錄音。錄音中一直送音訊，停止時送 finalize 收尾。
final class SonioxStream {
    private let task: URLSessionWebSocketTask
    private let session: URLSession
    private let lock = NSLock()
    private let sendQueue = DispatchQueue(label: "local.hunghanchu.voice-input.soniox.send")
    private let finished = DispatchSemaphore(value: 0)

    private var finals: [String] = []
    private var confidenceSum = 0.0
    private var confidenceCount = 0
    private var pendingAudio: [Data] = []
    private var ready = false
    private var finReceived = false
    private var sentBytes = 0
    private var closed = false
    private(set) var failure: String?
    private let startedAt = Date()

    init?(model: String, terms: [String], language: String = "zh") {
        guard let key = SonioxProvider.apiKey else {
            Transcriber.log("[soniox] 沒有 API key（~/.config/agent-secrets/soniox.env）→ 不走 Soniox")
            return nil
        }
        guard let url = URL(string: "wss://stt-rt.soniox.com/transcribe-websocket") else { return nil }
        var request = URLRequest(url: url)
        request.setValue("Bearer \(key)", forHTTPHeaderField: "Authorization")
        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = 15
        session = URLSession(configuration: config)
        task = session.webSocketTask(with: request)
        task.resume()
        Transcriber.log("[soniox] ws 連線（model=\(model) 熱詞=\(terms.count)）")

        var payload: [String: Any] = [
            "model": model,
            "audio_format": "pcm_s16le",
            "sample_rate": 16000,
            "num_channels": 1,
            "language_hints": [language],
            "language_hints_strict": true,
            "enable_endpoint_detection": true,
        ]
        if !terms.isEmpty { payload["context"] = ["terms": terms] }
        guard let data = try? JSONSerialization.data(withJSONObject: payload),
              let text = String(data: data, encoding: .utf8) else { return nil }
        task.send(.string(text)) { [weak self] error in
            guard let self else { return }
            self.lock.lock()
            if let error {
                self.failure = error.localizedDescription
                self.lock.unlock()
                Transcriber.log("[soniox] config 送不出去：\(error.localizedDescription)")
                return
            }
            let queued = self.pendingAudio
            self.pendingAudio = []
            self.ready = true
            self.lock.unlock()
            // 連線建立前（按下 ⌘E 到握手完成）收到的音訊補送，開頭不會少字
            for chunk in queued { self.task.send(.data(chunk)) { _ in } }
        }
        receive()
    }

    private func receive() {
        task.receive { [weak self] result in
            guard let self else { return }
            switch result {
            case .failure(let error):
                self.lock.lock()
                if !self.finReceived { self.failure = error.localizedDescription }
                let alreadyDone = self.finReceived
                self.lock.unlock()
                if !alreadyDone {
                    Transcriber.log("[soniox] 連線結束：\(error.localizedDescription)")
                    self.finished.signal()
                }
            case .success(let message):
                switch message {
                case .string(let text): self.handle(text)
                case .data(let data):
                    if let text = String(data: data, encoding: .utf8) { self.handle(text) }
                @unknown default: break
                }
                self.receive()
            }
        }
    }

    private func handle(_ text: String) {
        guard let data = text.data(using: .utf8),
              let doc = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
        if let code = doc["error_code"] as? Int {
            let message = (doc["error_message"] as? String) ?? ""
            lock.lock(); failure = "Soniox \(code): \(message)"; lock.unlock()
            Transcriber.log("[soniox] 錯誤 \(code)：\(message)")
            finished.signal()
            return
        }
        guard let tokens = doc["tokens"] as? [[String: Any]] else { return }
        lock.lock()
        for token in tokens {
            let piece = (token["text"] as? String) ?? ""
            if piece == "<fin>" {
                finReceived = true
                continue
            }
            // 控制標記（<end> 等）是給客戶端的訊號，不是要貼的字
            if piece.hasPrefix("<") && piece.hasSuffix(">") { continue }
            guard (token["is_final"] as? Bool) == true else { continue }
            finals.append(piece)
            if let confidence = token["confidence"] as? Double {
                confidenceSum += confidence
                confidenceCount += 1
            }
        }
        let done = finReceived
        lock.unlock()
        if done { finished.signal() }
    }

    /// 錄音中每個音訊塊呼叫（16k mono Int16 LE）
    func send(_ pcm: Data) {
        guard !pcm.isEmpty else { return }
        sendQueue.async { [weak self] in
            guard let self else { return }
            self.lock.lock()
            let isReady = self.ready
            let failed = self.failure != nil
            if !isReady && !failed { self.pendingAudio.append(pcm) }
            self.sentBytes += pcm.count
            self.lock.unlock()
            guard isReady, !failed else { return }
            self.task.send(.data(pcm)) { _ in }
        }
    }

    /// 停止錄音：補 200ms 靜音、送 finalize、等 <fin>（最多 timeout 秒）
    func finish(timeout: TimeInterval = 5) -> (text: String, confidence: Double?, seconds: Double) {
        let stopAt = Date()
        var shouldAsk = false
        lock.lock()
        if !closed && failure == nil {
            closed = true
            shouldAsk = true
        }
        lock.unlock()
        if shouldAsk {
            let silence = Data(repeating: 0, count: 3200)   // 16k * 2 * 0.1s
            task.send(.data(silence)) { _ in }
            task.send(.string("{\"type\":\"finalize\"}")) { _ in }
            _ = finished.wait(timeout: .now() + timeout)
        }
        lock.lock()
        let text = finals.joined().trimmingCharacters(in: .whitespacesAndNewlines)
        let confidence = confidenceCount > 0 ? confidenceSum / Double(confidenceCount) : nil
        let seconds = Date().timeIntervalSince(stopAt)
        let audioSeconds = Double(sentBytes) / 2.0 / 16000.0
        lock.unlock()
        task.cancel(with: .goingAway, reason: nil)
        session.finishTasksAndInvalidate()
        Transcriber.log(String(format: "[soniox] 停止→文字 %.2fs 音訊 %.1fs 信心 %@ 字數 %d%@",
                               seconds, audioSeconds,
                               confidence.map { String(format: "%.3f", $0) } ?? "-",
                               text.count,
                               failure.map { " 失敗：\($0)" } ?? ""))
        return (text, confidence, seconds)
    }

    func cancel() {
        lock.lock(); closed = true; lock.unlock()
        task.cancel(with: .goingAway, reason: nil)
        session.finishTasksAndInvalidate()
    }
}

/// 錄音期間持有 SonioxStream；停止後由 `Transcriber.transcribe` 取走文字。
/// 放在這裡而不是 App.swift：App 只負責「開始送音訊」，要不要等、等多久留在語音層。
enum SonioxCoordinator {
    static let enabled = SonioxProvider.enabled
    private static let lock = NSLock()
    private static var stream: SonioxStream?

    static func begin() {
        lock.lock()
        stream = SonioxStream(model: SonioxProvider.model, terms: SonioxProvider.terms)
        let ok = stream != nil
        lock.unlock()
        Transcriber.log("[soniox] 錄音開始：串流\(ok ? "已建立" : "建不起來（這次走 Breeze）")")
    }

    static func audio(_ data: Data) {
        lock.lock()
        let current = stream
        lock.unlock()
        current?.send(data)
    }

    /// 停止錄音 → 等 final → 回文字（沒有文字回 nil，呼叫端退回原本路徑）
    static func finishAndTakeText() -> String? {
        lock.lock()
        let current = stream
        stream = nil
        lock.unlock()
        guard let current else { return nil }
        let result = current.finish(timeout: 5)
        return result.text.isEmpty ? nil : result.text
    }

    static func cancel() {
        lock.lock()
        let current = stream
        stream = nil
        lock.unlock()
        current?.cancel()
    }
}
