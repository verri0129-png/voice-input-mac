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
    /// 錄音中的即時轉錄預覽（Whisper raw, 未潤飾）。放在音波膠囊上方。
    private var previewPanel: NSPanel?
    private var preview: PreviewView?
    private let size = NSSize(width: 176, height: 52)
    private let previewSize = NSSize(width: 560, height: 78)

    func show() {
        if panel == nil { build() }
        if previewPanel == nil { buildPreview() }
        guard let panel, let screen = NSScreen.main else { return }
        let visible = screen.visibleFrame
        // 螢幕下方、Dock 上方一點的位置
        panel.setFrameOrigin(NSPoint(x: visible.midX - size.width / 2, y: visible.minY + 84))
        // 預覽擺在音波正上方
        previewPanel?.setFrameOrigin(NSPoint(x: visible.midX - previewSize.width / 2,
                                            y: visible.minY + 84 + size.height + 10))
        wave?.reset()
        preview?.text = ""
        previewPanel?.orderOut(nil)
        panel.orderFrontRegardless()          // 顯示但不啟用 app（不搶焦點）
        Transcriber.log("[ui] 錄音指示圖顯示（不搶焦點）")
    }

    func hide() {
        if let pp = previewPanel, pp.isVisible { pp.orderOut(nil) }
        guard let panel, panel.isVisible else { return }
        panel.orderOut(nil)
        Transcriber.log("[ui] 錄音指示圖關閉")
    }

    /// 即時轉錄預覽（空字串 = 還沒聽到東西，把預覽收起來）
    func setPreview(_ text: String) {
        if previewPanel == nil { buildPreview() }
        guard let pp = previewPanel, let view = preview else { return }
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.isEmpty {
            if pp.isVisible { pp.orderOut(nil) }
            return
        }
        if view.text == trimmed { return }
        view.text = trimmed
        if !pp.isVisible {
            pp.orderFrontRegardless()         // 同樣不啟用 app，不搶焦點
            Transcriber.log("[ui] 即時預覽顯示")
        }
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

    private func buildPreview() {
        let panel = NSPanel(contentRect: NSRect(origin: .zero, size: previewSize),
                            styleMask: [.borderless, .nonactivatingPanel],
                            backing: .buffered, defer: false)
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.level = .statusBar
        panel.ignoresMouseEvents = true
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
        let view = PreviewView(frame: NSRect(origin: .zero, size: previewSize))
        panel.contentView = view
        previewPanel = panel
        preview = view
    }
}

/// 即時轉錄的文字面板：深色圓角底 + 灰字（最後一行優先顯示，太長從前面截掉）。
/// 標題列寫明「未潤飾」，避免跟停止後插入的最終文字混淆。
final class PreviewView: NSView {
    private let label = NSTextField(labelWithString: "")
    private let hint = NSTextField(labelWithString: "即時預覽（Whisper 原文，未潤飾；停止錄音後才插入最終文字）")

    var text: String {
        get { label.stringValue }
        set {
            guard label.stringValue != newValue else { return }
            label.stringValue = newValue
            needsDisplay = true
        }
    }

    override init(frame: NSRect) {
        super.init(frame: frame)
        hint.font = NSFont.systemFont(ofSize: 10)
        hint.textColor = NSColor(calibratedWhite: 0.95, alpha: 0.45)
        hint.frame = NSRect(x: 14, y: frame.height - 19, width: frame.width - 28, height: 13)
        hint.autoresizingMask = [.width, .minYMargin]
        addSubview(hint)

        label.font = NSFont.systemFont(ofSize: 15)
        label.textColor = NSColor(calibratedWhite: 0.96, alpha: 1)
        label.maximumNumberOfLines = 2
        label.lineBreakMode = .byTruncatingHead      // 講太長時保留尾巴（最新聽到的）
        label.usesSingleLineMode = false
        label.cell?.wraps = true
        label.cell?.isScrollable = false
        // 15pt 的一行約 18px；兩行要 36px，留到 48px 免得第二行被裁掉
        // （實測踩過：寫 34px 時第二行整行消失，畫面上看起來像被截斷）
        label.frame = NSRect(x: 14, y: 6, width: frame.width - 28, height: 48)
        label.autoresizingMask = [.width, .height]
        addSubview(label)
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    override func draw(_ dirtyRect: NSRect) {
        NSColor(calibratedWhite: 0.09, alpha: 0.86).setFill()
        NSBezierPath(roundedRect: bounds, xRadius: 14, yRadius: 14).fill()
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
