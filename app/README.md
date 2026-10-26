# VoiceInputMac — Menu Bar App

Swift AppKit, macOS 14+, 無 Xcode 亦可 `swift build`（Command Line Tools 足夠）。

```bash
cd app
swift build                 # debug
swift build -c release      # release
.build/debug/VoiceInputMac  # 或 .build/release/VoiceInputMac
```

- 需要「麥克風」與「輔助使用」權限（系統設定 → 隱私與安全性）
- 操作：`⌘E` 按一次開始錄音、再按一次結束並轉寫貼到目前游標
- 停止時會先抓取前景 App、視窗標題與游標周圍文字，再把文字草稿送雲端重寫
- 貼上後約 20 秒，若你在同一欄位手動改字，會呼叫本機學習流程並更新未進版控的個人詞庫
- Python 端：調用 `../transcribe.py`（走 `.venv/bin/python`，ASR 模型自動從 `models/` 載入）
