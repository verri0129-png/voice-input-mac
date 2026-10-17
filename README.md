# voice-input-mac — 純本地語音輸入（M4 / M2 皆可跑）

免費、離線、系統級「按住說話放開貼字」，在任意 App 輸入框可用。
STT `mlx-whisper large-v3-turbo`（~1.6GB）+ 潤飾 `Qwen2.5-7B 4bit`（~4.3GB），走 MLX/Metal，不需雲端 key。

## 已驗證（M4 Max 64GB, macOS 26.2）

- `mlx-whisper` 1.6GB + `mlx-lm` 4.3GB 下載落地，`Metal.is_available() == True`
- `say` 合成中文/英文 → `transcribe.py` → Qwen 潤飾，全鏈路通過：
  - `呃 就是 那個 我昨天呃昨天去開會...101大樓` → `昨天開會，地點在101大樓的會議室。`
  - `I want to schedule a meeting um tomorrow at 3pm actually make it 4pm` → `I want to schedule a meeting for 4 pm tomorrow.`（語言保持正確，不翻成中文）
- 靜音幻覺已知：空錄音會產生假字串，已用 RMS 能量門控過濾
- Swift Menu Bar App `swift build` 通過
- 現場 `sounddevice` 錄音：本機無實體麥克風（僅 Teams 虛擬裝置），需外接麥克風或耳機麥克風方可實測；檔案管線不受影響

## M2 16GB 備註

Qwen2.5-7B 4bit 約 4.3GB + Whisper 1.6GB = ~6GB，16GB 機型可直接跑，無需降至 3B。

## 安裝

```bash
brew install portaudio   # 已裝可略
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# 模型首次自動下載到 ~/.cache/huggingface/hub（共 ~6GB）
```

## 命令列

```bash
.venv/bin/python transcribe.py /tmp/test.wav              # 轉寫+潤飾
.venv/bin/python transcribe.py /tmp/test.wav --no-polish  # 只看 STT raw
.venv/bin/python transcribe.py /tmp/test.wav --copy       # 進剪貼簿
.venv/bin/python transcribe.py --record-seconds 5          # 現場錄 5 秒
```

## Menu Bar App

```bash
cd app && swift build -c release
.build/release/VoiceInputMac  # 需麥克風 + 輔助使用權限
```

操作：`左 Cmd+E` 按一次開始錄音、再按一次結束並轉寫貼到當前游標（任意 App）。結果進剪貼簿並自動 `Cmd+V`，中文一律繁體。

## 模型來源

- STT: `mlx-community/whisper-large-v3-turbo`
- 潤飾: `mlx-community/Qwen2.5-7B-Instruct-4bit`（中文能力與語言保持優於 Breeze v0.1 4bit MLX 社群版）

## 設計決策

- Breeze 官方無 MLX 4bit，社群量化 `YouJun3627/Breeze-7B-Instruct-v0_1-4bit-mlx` 實測會把英文翻成中文、潤飾不穩；改用 Qwen2.5-7B-4bit。
- Whisper 靜音幻覺改用 RMS 能量門控過濾（`no_speech_threshold` 對 mlx-whisper 幻覺掩蓋無效）。
