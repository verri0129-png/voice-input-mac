import AppKit
import ApplicationServices

@main
final class AppDelegate: NSObject, NSApplicationDelegate {
    private var statusItem: NSStatusItem!
    private var recorder = Recorder()
    private var isRecording = false
    private var leftCmdDown = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        // 全域熱鍵 + 自動貼上需要「輔助使用」權限；首次啟動跳出系統提示，方便加入清單並開關
        let axOpts = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
        _ = AXIsProcessTrustedWithOptions(axOpts)
        NSApp.setActivationPolicy(.accessory)
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        statusItem.button?.title = "🎙️"
        statusItem.button?.toolTip = "Voice Input — 左 Cmd+E 開始/結束錄音並貼上"
        let menu = NSMenu()
        menu.addItem(NSMenuItem(title: "左 Cmd+E：開始/結束錄音", action: #selector(toggleRec), keyEquivalent: ""))
        menu.addItem(NSMenuItem(title: "狀態：待命 🎙️", action: nil, keyEquivalent: ""))
        menu.addItem(.separator())
        menu.addItem(NSMenuItem(title: "結束", action: #selector(quit), keyEquivalent: "q"))
        statusItem.menu = menu
        // 左 Cmd (keyCode 55) 追蹤 + Cmd+E 切換，global + local 都要
        NSEvent.addGlobalMonitorForEvents(matching: .flagsChanged) { [weak self] e in self?.handleFlags(e) }
        NSEvent.addLocalMonitorForEvents(matching: .flagsChanged) { [weak self] e in self?.handleFlags(e); return e }
        NSEvent.addGlobalMonitorForEvents(matching: .keyDown) { [weak self] e in self?.handleKey(e) }
        NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] e in self?.handleKey(e); return e }
        print("[app] launched. python=\(Transcriber.pythonBin) script=\(Transcriber.scriptPath) hotkey=LeftCmd+E toggle")
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

    @objc func quit() { NSApp.terminate(nil) }
}
