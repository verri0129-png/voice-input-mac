import AppKit
import ApplicationServices

@main
final class AppDelegate: NSObject, NSApplicationDelegate {
    private var statusItem: NSStatusItem!
    private var recorder = Recorder()
    private var isRecording = false
    private var leftCmdDown = false

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
        statusItem.button?.toolTip = "Voice Input — 左 Cmd+E 開始/結束錄音並貼上 — Dock 可見，選單有 開啟/重新啟動/結束"
        let menu = NSMenu()
        menu.addItem(NSMenuItem(title: "左 Cmd+E：開始/結束錄音", action: #selector(toggleRec), keyEquivalent: ""))
        menu.addItem(NSMenuItem(title: "狀態：待命 🎙️", action: nil, keyEquivalent: ""))
        menu.addItem(.separator())
        menu.addItem(NSMenuItem(title: "開啟 VoiceInputMac 檔案位置", action: #selector(revealInFinder), keyEquivalent: ""))
        menu.addItem(NSMenuItem(title: "重新啟動", action: #selector(relaunch), keyEquivalent: "r"))
        menu.addItem(NSMenuItem(title: "結束", action: #selector(quit), keyEquivalent: "q"))
        statusItem.menu = menu
        // 檔案 & 輔助：自動建 .app 包（若僅有二進位）並配好 Info.plist
        BundleHelper.ensureAppBundle()
        NSEvent.addGlobalMonitorForEvents(matching: .flagsChanged) { [weak self] e in self?.handleFlags(e) }
        NSEvent.addLocalMonitorForEvents(matching: .flagsChanged) { [weak self] e in self?.handleFlags(e); return e }
        NSEvent.addGlobalMonitorForEvents(matching: .keyDown) { [weak self] e in self?.handleKey(e) }
        NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] e in self?.handleKey(e); return e }
        print("[app] launched. python=\(Transcriber.pythonBin) script=\(Transcriber.scriptPath) hotkey=LeftCmd+E toggle, Dock visible, bundle=\(Bundle.main.bundlePath)")
    }

    private func handleFlags(_ e: NSEvent) {
        // 55 = 左 Cmd, 54 = 右 Cmd
        if e.keyCode == 55 {
            leftCmdDown = e.modifierFlags.contains(.command)
        } else if e.keyCode == 54 {
            // 右 Cmd 不觸發左 Cmd 邏輯
            if e.modifierFlags.contains(.command) { leftCmdDown = false }
        }
        if !e.modifierFlags.contains(.command) { leftCmdDown = false }
    }

    private func handleKey(_ e: NSEvent) {
        // E keyCode = 14, 需左 Cmd 按住
        guard e.keyCode == 14, e.modifierFlags.contains(.command) else { return }
        // 嚴格只認左 Cmd；若 flags 事件漏掉，退化為任何 Cmd+E 也觸發（註解下一行可改為嚴格）
        if leftCmdDown || true {
            // 為避免長按重複觸發，只在 keyDown 第一次
            if e.isARepeat { return }
            toggleRec()
        }
    }

    @objc func toggleRec() {
        if isRecording { stopAndTranscribe() } else { startRec() }
    }

    @objc func startRec() {
        guard !isRecording else { return }
        isRecording = true
        statusItem.button?.title = "🔴"
        recorder.start()
        print("[app] recording...")
    }

    @objc func stopAndTranscribe() {
        guard isRecording else { return }
        isRecording = false
        statusItem.button?.title = "⏳"
        guard let url = recorder.stop() else { statusItem.button?.title = "🎙️"; return }
        print("[app] wav \(url.path)")
        DispatchQueue.global(qos: .userInitiated).async {
            do {
                let text = try Transcriber.transcribe(wavURL: url)
                DispatchQueue.main.async {
                    print("[app] polished: \(text)")
                    self.statusItem.button?.title = "🎙️"
                    guard !text.isEmpty else { return }
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(text, forType: .string)
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.15) { Transcriber.pasteViaClipboard() }
                }
            } catch {
                DispatchQueue.main.async {
                    print("[app] transcribe failed: \(error)")
                    self.statusItem.button?.title = "🎙️"
                    let alert = NSAlert()
                    alert.messageText = "轉寫失敗"
                    alert.informativeText = "\(error)"
                    alert.runModal()
                }
            }
            try? FileManager.default.removeItem(at: url)
        }
    }

    // Dock 圖示右鍵選單。NSApplication 沒有 dockMenu 屬性；那是 delegate 方法。
    func applicationDockMenu(_ sender: NSApplication) -> NSMenu? {
        let dockMenu = NSMenu()
        dockMenu.addItem(NSMenuItem(title: "重新啟動", action: #selector(relaunch), keyEquivalent: ""))
        dockMenu.addItem(NSMenuItem(title: "結束", action: #selector(quit), keyEquivalent: ""))
        return dockMenu
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
