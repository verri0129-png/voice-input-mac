import AppKit
import ApplicationServices
import Carbon

@main
final class AppDelegate: NSObject, NSApplicationDelegate {
    private var statusItem: NSStatusItem!
    private var recorder = Recorder()
    // 錄音中的畫面音波指示圖（不搶焦點，讓使用者知道現在在錄音）
    private let indicator = RecordingIndicator()
    // 錄音中的即時預覽：每 ~1.2s 把「目前為止」的音訊丟給暖機程序跑一次 Whisper（不潤飾），
    // 顯示在指示圖上方。純提示，「停止後插入的最終文字」仍走原本的完整管線。
    private var previewTimer: DispatchSourceTimer?
    private var previewPath: URL?
    private var previewBusy = false
    private var previewReadySkipLogged = false
    private var isRecording = false
    private var leftCmdDown = false
    private var lastToggle: TimeInterval = 0
    // exit 4（疑似非語音）時保留的文字與對應選單項：不自動貼，但讓使用者能手動貼。
    private var pendingText: String?
    private var pendingItem: NSMenuItem?
    // 有沒有收到過真正的 Cmd 修飾鍵事件。某些環境（合成事件、遠端桌面）完全收不到
    // flagsChanged；那種情況下才降級放行，否則嚴格只認左 Cmd。
    private var sawCmdFlags = false
    // 系統層快捷鍵（Carbon RegisterEventHotKey）。不用 NSEvent 全域監聽器當唯一路徑，
    // 因為那種監聽器依賴 TCC（輔助使用/輸入監控），簽章一換授權就失效、而且失敗時完全
    // 不會報錯（監聽器照樣註冊，只是永遠不呼叫）。Carbon 熱鍵走系統層，不吃 TCC。
    private var hotKeyRef: EventHotKeyRef?
    private var hotKeyHeld = false

    // 進場點：不要依賴 AppKit 的預設 main。它會走 NSApplicationMain，靠 Info.plist 的
    // NSMainNibFile 指派 delegate；我們沒有 nib，delegate 就永遠不會被設，
    // applicationDidFinishLaunching 也就不會被呼叫（= 沒有選單列圖示、不會要權限）。
    // 所以自己建 NSApplication、指派 delegate、進 run loop。
    // NSApplication.delegate 是 assign/unsafe_unretained 語意，不保證 ARC 留著 delegate，
    // 所以用 static 強參考持有它，不要用區域變數。
    private static let appDelegate = AppDelegate()

    static func main() {
        let app = NSApplication.shared
        app.delegate = appDelegate
        app.run()
    }

    // Dock 可見、可用 Dock 選單關閉/重啟
    func applicationDidFinishLaunching(_ notification: Notification) {
        let axOpts = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
        _ = AXIsProcessTrustedWithOptions(axOpts)
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        statusItem.button?.title = "🎙️"
        statusItem.button?.toolTip = "Voice Input — ⌘E 開始/結束錄音，文字插入當下游標處 — Dock 可見，選單有 開啟/重新啟動/結束"
        let menu = NSMenu()
        menu.addItem(NSMenuItem(title: "⌘E：開始/結束錄音", action: #selector(toggleRec), keyEquivalent: ""))
        menu.addItem(NSMenuItem(title: "狀態：待命 🎙️", action: nil, keyEquivalent: ""))
        let pending = NSMenuItem(title: "貼上上次保留的結果（疑似非語音時）", action: #selector(pastePending), keyEquivalent: "")
        pending.isEnabled = false
        pendingItem = pending
        menu.addItem(pending)
        menu.addItem(.separator())
        menu.addItem(NSMenuItem(title: "開啟 VoiceInputMac 檔案位置", action: #selector(revealInFinder), keyEquivalent: ""))
        menu.addItem(NSMenuItem(title: "重新啟動", action: #selector(relaunch), keyEquivalent: "r"))
        menu.addItem(NSMenuItem(title: "結束", action: #selector(quit), keyEquivalent: "q"))
        statusItem.menu = menu
        // 檔案 & 輔助：自動建 .app 包（若僅有二進位）並配好 Info.plist
        BundleHelper.ensureAppBundle()
        NSEvent.addGlobalMonitorForEvents(matching: .flagsChanged) { [weak self] e in self?.handleFlags(e, source: "global") }
        NSEvent.addLocalMonitorForEvents(matching: .flagsChanged) { [weak self] e in self?.handleFlags(e, source: "local"); return e }
        NSEvent.addGlobalMonitorForEvents(matching: .keyDown) { [weak self] e in self?.handleKey(e, source: "global") }
        NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] e in self?.handleKey(e, source: "local"); return e }
        // 執行時真的被信任嗎？TCC 的表格說「允許」不代表簽章換過之後還算數，
        // 所以把實際結果寫進 log（`false` = 全域監聽器一定收不到跨 app 按鍵）。
        Transcriber.log("[perm] AXIsProcessTrusted=\(AXIsProcessTrusted())")
        // 貼上要用的「自動化」權限也在啟動時先確認（缺的話現在跳對話框，
        // 而不是等按下停止貼上時才靜默失敗 → 使用者只會看到「沒反應」）
        Transcriber.preflightAutomation()
        // 錄音音量 → 畫面音波指示圖（音波要跟著講話跳動，才知道麥克風有在收）
        recorder.onLevel = { [weak self] level in self?.indicator.push(level) }
        setupCarbonHotKey()
        Transcriber.log("[app] launched. bundle=\(Bundle.main.bundlePath) python=\(Transcriber.pythonBin) script=\(Transcriber.scriptPath) log=\(Transcriber.logPath)")
    }

    // 熱鍵：⌘E（使用者指定）。按一下開始錄音，再按一下結束並把文字插入「當下游標所在的位置」。
    // Carbon 熱鍵無法分辨左右 Cmd，所以左右都算（使用者要的就是 ⌘E）。
    private let hotKeyCode: UInt16 = UInt16(kVK_ANSI_E)
    private let hotKeyLabel = "⌘E"

    // MARK: - 系統層快捷鍵（Carbon ⌘\，不需任何 TCC 權限）

    private func setupCarbonHotKey() {
        var specs = [
            EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed)),
            EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyReleased)),
        ]
        let handler: EventHandlerUPP = { _, event, _ in
            let kind = event.map { GetEventKind($0) } ?? 0
            DispatchQueue.main.async { AppDelegate.appDelegate.carbonHotKey(kind: kind) }
            return noErr
        }
        var handlerRef: EventHandlerRef?
        let hs = InstallEventHandler(GetApplicationEventTarget(), handler, specs.count, &specs, nil, &handlerRef)
        let id = EventHotKeyID(signature: OSType(0x56494E50) /* 'VINP' */, id: 1)
        let rs = RegisterEventHotKey(UInt32(hotKeyCode), UInt32(cmdKey), id, GetApplicationEventTarget(), 0, &hotKeyRef)
        Transcriber.log("[hotkey] Carbon ⌘E InstallEventHandler=\(hs) RegisterEventHotKey=\(rs) ok=\(hotKeyRef != nil)")
    }

    func carbonHotKey(kind: UInt32) {
        switch kind {
        case UInt32(kEventHotKeyPressed):
            guard !hotKeyHeld else { return }   // 按住會連續送 pressed，只認第一次
            hotKeyHeld = true
            Transcriber.log("[hotkey] Carbon ⌘E 按下")
            trigger(source: "carbon")
        case UInt32(kEventHotKeyReleased):
            hotKeyHeld = false
        default:
            break
        }
    }

    // 所有觸發路徑（Carbon 熱鍵、NSEvent 全域/本地監聽器）共用，去抖動避免同一次
    // 按鍵被多條路徑各送一次而 start/stop 連發。
    private func trigger(source: String) {
        let now = Date().timeIntervalSince1970
        guard now - lastToggle > 0.4 else {
            Transcriber.log("[key] 去抖動略過重複的觸發（source=\(source)）")
            return
        }
        lastToggle = now
        Transcriber.log("[key] 觸發（source=\(source)）")
        toggleRec()
    }

    private func handleFlags(_ e: NSEvent, source: String) {
        // 55 = 左 Cmd, 54 = 右 Cmd
        if e.keyCode == 55 || e.keyCode == 54 { sawCmdFlags = true }
        if e.keyCode == 55 {
            leftCmdDown = e.modifierFlags.contains(.command)
        } else if e.keyCode == 54 {
            // 右 Cmd 不觸發左 Cmd 邏輯
            if e.modifierFlags.contains(.command) { leftCmdDown = false }
        }
        if !e.modifierFlags.contains(.command) { leftCmdDown = false }
    }

    private func handleKey(_ e: NSEvent, source: String) {
        // E 鍵 keyCode = 14（kVK_ANSI_E），需 Cmd 按住
        // 診斷用：只要看到 E 鍵（不管修飾鍵）就記一筆，這樣「收不到」和「收到但被規則擋掉」
        // 才分得出來（先前只有通過檢查才寫 log，收不到時完全沒痕跡）。
        if e.keyCode == hotKeyCode {
            Transcriber.log("[key] 看到 E 鍵 flags=\(e.modifierFlags.rawValue) repeat=\(e.isARepeat) source=\(source)")
        }
        guard e.keyCode == hotKeyCode, e.modifierFlags.contains(.command) else { return }
        // 左右 Cmd 都接受（使用者要的就是 ⌘E；Carbon 熱鍵本來也分辨不出左右）
        guard !e.isARepeat else { return }
        trigger(source: source)
    }

    @objc func toggleRec() {
        if isRecording { stopAndTranscribe() } else { startRec() }
    }

    @objc func startRec() {
        guard !isRecording else { return }
        isRecording = true
        statusItem.button?.title = "🔴"
        // 趁使用者講話時先把模型載好（暖機 ~6s），按下停止後只剩推論（~0.9s）
        Transcriber.startWarmServer()
        recorder.start()
        indicator.show()          // 畫面上的音波指示圖（不搶焦點）
        startPreviewLoop()        // 即時預覽（暖機完成後才真的開始送）
        Transcriber.log("[app] recording start")
    }

    @objc func stopAndTranscribe() {
        guard isRecording else { return }
        isRecording = false
        stopPreviewLoop()
        indicator.hide()          // 停止錄音就收掉音波圖（含即時預覽）
        let tStop = Date()
        statusItem.button?.title = "⏳"
        guard let url = recorder.stop() else {
            statusItem.button?.title = "🎙️"
            Transcriber.log("[app] recording stop：沒有音訊，放棄")
            return
        }
        Transcriber.log("[app] recording stop -> \(url.path)")
        DispatchQueue.global(qos: .userInitiated).async {
            do {
                let context = ContextCapture.capture()
                Transcriber.log("[context] \(ContextCapture.describe(context))")
                let (text, suspect) = try Transcriber.transcribe(
                    wavURL: url,
                    contextJSON: ContextCapture.encode(context)
                )
                DispatchQueue.main.async {
                    Transcriber.log("[app] 結果（停止→文字 \(String(format: "%.2f", Date().timeIntervalSince(tStop)))s）：\(text)")
                    self.statusItem.button?.title = "🎙️"
                    guard !text.isEmpty else {
                        Transcriber.log("[app] 結果為空，不貼上")
                        return
                    }
                    if suspect {
                        // exit 4：疑似非語音（噪音幻覺）。不自動貼，但保留文字，讓使用者
                        // 能從選單手動貼——這樣「真的是我講的」不會掉，噪音卻不會污染文件。
                        self.pendingText = text
                        self.pendingItem?.isEnabled = true
                        Transcriber.log("[app] exit 4：疑似非語音 → 未自動貼上；文字已保留（選單『貼上上次保留的結果』可用）")
                        self.statusItem.button?.title = "⚠️"
                        self.statusItem.button?.toolTip = "疑似非語音，未自動貼上。文字已保留：\(text.prefix(40))（選單可手動貼）"
                        DispatchQueue.main.asyncAfter(deadline: .now() + 5) {
                            self.statusItem.button?.title = "🎙️"
                            self.statusItem.button?.toolTip = "Voice Input — ⌘E 開始/結束錄音，文字插入當下游標處 — Dock 可見，選單有 開啟/重新啟動/結束"
                        }
                        return
                    }
                    // 等 0.15s 讓焦點穩定，再於背景注入文字（TCC 對話框可能阻塞，不能卡主執行緒）
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.15) {
                        DispatchQueue.global(qos: .userInitiated).async {
                            let originalField = ContextCapture.learningSnapshot()?["field_text"]
                            let inserted = Transcriber.insertText(text)
                            EditLearning.schedule(
                                originalField: originalField,
                                insertedText: inserted,
                                context: context,
                                termsFile: EditLearning.termsFileURL()
                            )
                        }
                    }
                }
            } catch {
                let code = (error as NSError).code
                DispatchQueue.main.async {
                    self.statusItem.button?.title = "🎙️"
                    if code == 3 {
                        // transcribe.py 的能量閘門：整段幾乎沒收到聲音 → 不貼上（避免幻覺文字被當成逐字稿）
                        Transcriber.log("[app] exit 3：沒有可用語音（麥克風沒收到聲音，或結果被判為幻覺）→ 未貼上任何東西")
                        self.statusItem.button?.title = "🔇"
                        self.statusItem.button?.toolTip = "沒有可用語音（沒收到聲音或判為幻覺）— 詳見 ~/orca/voice-input-mac/.run.log"
                        DispatchQueue.main.asyncAfter(deadline: .now() + 5) {
                            self.statusItem.button?.title = "🎙️"
                            self.statusItem.button?.toolTip = "Voice Input — ⌘E 開始/結束錄音，文字插入當下游標處 — Dock 可見，選單有 開啟/重新啟動/結束"
                        }
                        return
                    }
                    Transcriber.log("[app] transcribe failed: \(error.localizedDescription)")
                    let alert = NSAlert()
                    alert.messageText = "轉寫失敗"
                    alert.informativeText = "\(error.localizedDescription)"
                    alert.runModal()
                }
            }
            try? FileManager.default.removeItem(at: url)
        }
    }

    // MARK: - 錄音中的即時預覽（partial）

    private func startPreviewLoop() {
        previewPath = FileManager.default.temporaryDirectory.appendingPathComponent("voice-preview.wav")
        previewBusy = false
        previewReadySkipLogged = false
        let t = DispatchSource.makeTimerSource(queue: DispatchQueue.global(qos: .utility))
        t.schedule(deadline: .now() + 1.0, repeating: 1.2)
        t.setEventHandler { [weak self] in self?.previewTick() }
        t.resume()
        previewTimer = t
        Transcriber.log("[preview] 即時預覽迴圈啟動（每 1.2s 一次）")
    }

    private func stopPreviewLoop() {
        guard previewTimer != nil else { return }
        previewTimer?.cancel()
        previewTimer = nil
        Transcriber.log("[preview] 即時預覽迴圈停止")
    }

    /// 計時器在背景執行緒；狀態（isRecording／previewBusy）只在主執行緒讀寫。
    private func previewTick() {
        DispatchQueue.main.async { [weak self] in
            guard let self, self.isRecording, !self.previewBusy else { return }
            self.previewBusy = true
            DispatchQueue.global(qos: .utility).async { [weak self] in
                guard let self else { return }
                self.runPreview()
                DispatchQueue.main.async { self.previewBusy = false }
            }
        }
    }

    /// 把「目前為止」的音訊寫成 wav，丟給暖機程序做一次 Whisper（partial = 不潤飾、不做貼上判斷）。
    private func runPreview() {
        guard let url = previewPath else { return }
        // 暖機完成前不要送：那個請求會卡在「模型還在載」的管子裡，正式結果得排在它後面等
        guard Transcriber.warmReady else {
            if !previewReadySkipLogged {
                previewReadySkipLogged = true
                Transcriber.log("[preview] 暖機還沒完成，先不送預覽（模型載完才開始預覽）")
            }
            return
        }
        guard recorder.writePartialWav(to: url) else { return }
        let t0 = Date()
        guard let text = Transcriber.partialText(wavURL: url) else { return }
        Transcriber.log("[preview] \(String(format: "%.2f", Date().timeIntervalSince(t0)))s → \(text)")
        DispatchQueue.main.async { [weak self] in
            guard let self, self.isRecording else { return }
            self.indicator.setPreview(text)
        }
    }

    // Dock 圖示右鍵選單。NSApplication 沒有 dockMenu 屬性；那是 delegate 方法。
    func applicationDockMenu(_ sender: NSApplication) -> NSMenu? {
        let dockMenu = NSMenu()
        dockMenu.addItem(NSMenuItem(title: "重新啟動", action: #selector(relaunch), keyEquivalent: ""))
        dockMenu.addItem(NSMenuItem(title: "結束", action: #selector(quit), keyEquivalent: ""))
        return dockMenu
    }

    // exit 4 保留的文字：使用者確認真的是自己說的話時，從選單手動貼上。
    @objc func pastePending() {
        guard let text = pendingText, !text.isEmpty else { return }
        Transcriber.log("[app] 手動貼上保留的文字（\(text.count) 字）")
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.15) {
            DispatchQueue.global(qos: .userInitiated).async { Transcriber.insertText(text) }
        }
    }

    @objc func revealInFinder() { NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: Bundle.main.bundlePath)]) }
    @objc func relaunch() {
        let url = URL(fileURLWithPath: Bundle.main.bundlePath)
        let cfg = NSWorkspace.OpenConfiguration(); cfg.createsNewApplicationInstance = true
        NSWorkspace.shared.openApplication(at: url, configuration: cfg, completionHandler: nil)
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) { NSApp.terminate(nil) }
    }
    @objc func quit() { NSApp.terminate(nil) }
}

private enum BundleHelper {
    static func ensureAppBundle() {
        // 若以裸二進位啟動（如 .build/release/VoiceInputMac），補一個 Release .app 殼讓 Dock/Finder 看得到
        let bp = Bundle.main.bundlePath
        if bp.hasSuffix(".app") { return }
        let exe = URL(fileURLWithPath: bp)
        let build = exe.deletingLastPathComponent() // .../release
        let appURL = build.appendingPathComponent("VoiceInputMac.app")
        let macOS = appURL.appendingPathComponent("Contents/MacOS")
        let plist = appURL.appendingPathComponent("Contents/Info.plist")
        try? FileManager.default.createDirectory(at: macOS, withIntermediateDirectories: true)
        let dest = macOS.appendingPathComponent("VoiceInputMac")
        if !FileManager.default.fileExists(atPath: dest.path) {
            try? FileManager.default.copyItem(at: exe, to: dest)
        }
        if !FileManager.default.fileExists(atPath: plist.path) {
            let info = """
            <?xml version=\"1.0\" encoding=\"UTF-8\"?>
            <!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">
            <plist version=\"1.0\"><dict>
            <key>CFBundleName</key><string>VoiceInputMac</string>
            <key>CFBundleDisplayName</key><string>VoiceInputMac</string>
            <key>CFBundleIdentifier</key><string>com.voiceinput.mac</string>
            <key>CFBundleVersion</key><string>1</string>
            <key>CFBundlePackageType</key><string>APPL</string>
            <key>CFBundleExecutable</key><string>VoiceInputMac</string>
            <key>LSUIElement</key><false/>
            </dict></plist>
            """
            try? info.write(to: plist, atomically: true, encoding: .utf8)
        }
    }
}
