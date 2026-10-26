# voice-input-mac — 本地辨識＋雲端語意重寫（M4 / M2 皆可跑）

免費、系統級「按住說話放開貼字」，在任意 App 輸入框可用。
STT `Breeze-ASR-25 MLX 4bit` 走本地 MLX/Metal；停止錄音後的語意重寫走 Vertex AI
`gemini-3.8-flash`（只送文字草稿與游標周圍的有限上下文，不送音訊）。

## 已驗證（M4 Max 64GB, macOS 26.2）

- Breeze ASR 走本地 MLX/Metal，並保留繁體正規化、靜音／幻覺門控
- 受控文字測試走 Vertex AI `gemini-3.8-flash`：`會議公式` → `會議共識`，守門允許字典強制替換後才輸出
- Swift Menu Bar App `./build.sh` 通過
- 現場 `sounddevice` 錄音：輸入裝置**不寫死**——預設用系統預設輸入，可用 `--device`（索引或名稱子串）指定。兩台裝置不同（M4: `MixPre-6 II`；M2: `SOUNDPEATS POP Clip` 藍芽 / `BOYA mini 2`），以 `--list-devices` 查。

## M2 16GB 備註

M2 仍然只在本地跑 ASR；重寫使用同一個雲端模型，所以不需要在 M2 上放 20GB 本地 LLM。

## 安裝

```bash
brew install portaudio   # 已裝可略
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# ASR 權重會依 `VOICE_STT_REPO` 在首次轉寫時自動取得
```

## 命令列

```bash
.venv/bin/python transcribe.py /tmp/test.wav              # 轉寫+雲端重寫
.venv/bin/python transcribe.py /tmp/test.wav --rewrite-backend rules  # 只做確定性標點
.venv/bin/python transcribe.py /tmp/test.wav --rewrite-backend off    # 只看 STT raw
.venv/bin/python transcribe.py /tmp/test.wav --context-json /tmp/context.json --terms-file ./rewrite_terms.json
.venv/bin/python transcribe.py /tmp/test.wav --copy       # 進剪貼簿
.venv/bin/python transcribe.py --record-seconds 5          # 現場錄 5 秒（系統預設輸入）
.venv/bin/python -m unittest test_rewrite_cloud -v        # 雲端守門與協定測試
.venv/bin/python transcribe.py --list-devices              # 列出可用輸入裝置
.venv/bin/python transcribe.py --record-seconds 5 --device "BOYA"   # 指定裝置（索引或名稱子串）
```

## Menu Bar App

```bash
cd app && ./build.sh                          # 產出 .build/release/VoiceInputMac.app（繞過壞掉的 SwiftPM）
open .build/release/VoiceInputMac.app
```

首次啟動會跳「輔助使用」授權提示；第一次錄音會跳麥克風授權——兩者都要允許，
全域熱鍵與自動貼上才會生效（TCC 權限無法用腳本授予，需手動開關）。
`build.sh` 會在預設 SDK 被拒時自動改用較舊的 CLT SDK（見檔頭註解）。
若 `swift build` 可用，也可沿用 `swift build -c release`。

操作：`⌘E` 按一次開始錄音、再按一次結束並轉寫貼到當前游標（任意 App；左右 Cmd 都可）。結果進剪貼簿並自動 `Cmd+V`，中文一律繁體。

## 模型來源

- STT: 本地 `models/breeze-asr-25-mlx-4bit`
- 重寫：Vertex AI `gemini-3.8-flash`，失敗時自動退回確定性規則標點
- 個人專有名詞：`rewrite_terms.json`（未進版控；格式見 `rewrite_terms.example.json`）
- 用後學習：貼上後約 20 秒，若你在同一欄位手動改字，會從那次修改推導窄範圍 `誤聽→正確` 映射並寫入本機詞庫。音訊不上傳，學習也不走雲端。

## 設計決策

- 本地 7B 潤飾模型已移除；語意修正改走雲端 `gemini-3.8-flash`，失敗時退回確定性規則標點。
- Whisper 靜音幻覺改用 RMS 能量門控過濾（`no_speech_threshold` 對 mlx-whisper 幻覺掩蓋無效）。
