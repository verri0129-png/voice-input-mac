# VoiceInputMac — Menu Bar App

Swift AppKit, macOS 14+, 無 Xcode 亦可 `swift build`（Command Line Tools 足夠）。

```bash
cd app
swift build                 # debug
swift build -c release      # release
.build/debug/VoiceInputMac  # 或 .build/release/VoiceInputMac
```

- 需要「麥克風」與「輔助使用」權限（系統設定 → 隱私與安全性）
- 操作：按住 Fn 錄音、放開轉寫貼上；或 Option+Space 切換
- Python 端：調用 `../transcribe.py`（走 `.venv/bin/python`，模型自動從 `~/.cache/huggingface/hub` 載入）
