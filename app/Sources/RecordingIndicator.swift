import AppKit

/// 錄音中的畫面指示圖：螢幕下方浮出一個會跳動的音波（像 Orca／Type Plus 的錄音提示），
/// 讓使用者知道「現在正在錄音」，不必去看選單列的小圖示。
///
/// 設計要點：
/// - `.nonactivatingPanel` + `orderFrontRegardless()`：**不搶焦點**（搶焦點會讓貼上的目標 app 跑掉）。
/// - `ignoresMouseEvents = true`：指示圖不接滑鼠，不會擋到下面的東西。
/// - `.canJoinAllSpaces` + `.fullScreenAuxiliary`：切換桌面／全螢幕 app 都看得到。
final class RecordingIndicator {
    private var panel: NSPanel?
    private var wave: WaveView?
    private let size = NSSize(width: 176, height: 52)

    func show() {
        if panel == nil { build() }
        guard let panel, let screen = NSScreen.main else { return }
        let visible = screen.visibleFrame
        // 螢幕下方、Dock 上方一點的位置
        panel.setFrameOrigin(NSPoint(x: visible.midX - size.width / 2, y: visible.minY + 84))
        wave?.reset()
        panel.orderFrontRegardless()          // 顯示但不啟用 app（不搶焦點）
        Transcriber.log("[ui] 錄音指示圖顯示（不搶焦點）")
    }

    func hide() {
        guard let panel, panel.isVisible else { return }
        panel.orderOut(nil)
        Transcriber.log("[ui] 錄音指示圖關閉")
    }

    /// 即時音量 0…1（Recorder 在主執行緒送進來）
    func push(_ level: Float) { wave?.push(level) }

    private func build() {
        let panel = NSPanel(contentRect: NSRect(origin: .zero, size: size),
                            styleMask: [.borderless, .nonactivatingPanel],
                            backing: .buffered, defer: false)
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.level = .statusBar
        panel.ignoresMouseEvents = true
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
        let wave = WaveView(frame: NSRect(origin: .zero, size: size))
        panel.contentView = wave
        self.panel = panel
        self.wave = wave
    }
}

/// 音波：7 根條子跟著音量上下跳；沒有聲音時自己往下掉，看起來像在呼吸。
final class WaveView: NSView {
    private let barCount = 7
    private var levels: [Float] = []
    private var timer: Timer?
    private let idleDecay: Float = 0.86

    override init(frame: NSRect) {
        super.init(frame: frame)
        reset()
        let t = Timer(timeInterval: 1.0 / 30.0, repeats: true) { [weak self] _ in
            guard let self else { return }
            for i in self.levels.indices { self.levels[i] *= self.idleDecay }
            self.needsDisplay = true
        }
        RunLoop.main.add(t, forMode: .common)     // .common：拖視窗／選單展開時也照跳
        timer = t
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    deinit { timer?.invalidate() }

    func reset() {
        levels = Array(repeating: 0, count: barCount)
        needsDisplay = true
    }

    func push(_ level: Float) {
        levels.removeFirst()
        levels.append(max(0, min(1, level)))
    }

    override func draw(_ dirtyRect: NSRect) {
        let b = bounds
        // 圓角深色膠囊底
        NSColor(calibratedWhite: 0.09, alpha: 0.86).setFill()
        NSBezierPath(roundedRect: b, xRadius: b.height / 2, yRadius: b.height / 2).fill()
        // 左上角紅點（錄音中）
        let dot: CGFloat = 7
        NSColor(calibratedRed: 1.0, green: 0.27, blue: 0.23, alpha: 1).setFill()
        NSBezierPath(ovalIn: NSRect(x: 13, y: b.height - dot - 11, width: dot, height: dot)).fill()
        // 條子
        let left: CGFloat = 28
        let gap: CGFloat = 3
        let area = b.width - left - 14
        let barW = (area - CGFloat(barCount - 1) * gap) / CGFloat(barCount)
        let maxH = b.height - 16
        for (i, v) in levels.enumerated() {
            // 讓小音量也看得出來在動：取平方根壓縮
            let h = max(3, CGFloat(v.squareRoot()) * maxH)
            let x = left + CGFloat(i) * (barW + gap)
            let rect = NSRect(x: x, y: (b.height - h) / 2, width: barW, height: h)
            NSColor(calibratedRed: 1.0, green: 0.62, blue: 0.30, alpha: 0.95).setFill()
            NSBezierPath(roundedRect: rect, xRadius: barW / 2, yRadius: barW / 2).fill()
        }
    }
}
