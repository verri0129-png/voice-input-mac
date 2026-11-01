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

## 健康狀態（health.json）

「現在有沒有壞」有機器可讀的答案，不必回頭翻 log：

```bash
python3 healthcheck.py                 # 產 ~/orca/state/voice-input/health.json，印 HEALTH=OK/DEGRADED
python3 healthcheck.py --emit          # 壞掉時會開哪些單（dry-run，只印 WOULD_CREATE）
python3 healthcheck.py --emit --apply  # 真的開 GitHub issue，編號寫回 emitted_issues.json
```

`health.json` 欄位：`agent_loaded`（熱鍵 agent 在 `launchctl` 清單裡）、`last_exit`、
`app_running`（⌘E 有沒有 App 接）、`err_lines_since_last_check`（上次檢查之後
`~/orca/state/voice-toggle-hotkey.err.log` 多了幾行，靠 `checkpoint.json` 的 offset；
第一次跑只建立基準、不把歷史 log 算成新錯誤）、`stt_backend`（`breeze`／`soniox`／`unknown`）、
`checked_at`。`stt_backend` 讀的是**實際在跑的那個 checkout** 的 `local.env`（預設
`~/orca/voice-input-mac`，可用 `--runtime` 指定），不讀本 clone 的、也不讀 shell 的環境變數
——App 是 GUI 啟動的，看不到你 shell 裡 export 的東西。

開單規則只有一處：`healthcheck.collect_findings`（App 沒跑、agent 沒載入、`last_exit` 非 0、
error log 有新增行、`VOICE_STT` 認不得）。`issue_emitter.py` 是可共用的 core：去重 key
`<!-- key: health/<date>/<field>/<sha8> -->`、五段 body、gh 去重、編號回寫都在那裡，
新 repo 只要提供「什麼算 finding」。健康的機器跑 `--emit` 會印 `EMIT_ISSUES=NONE`。

重放驗證（不必等真的壞）：

```bash
cp ~/orca/state/voice-input/health.json /tmp/h.json
python3 - <<'PY'
import json, pathlib
p = pathlib.Path("/tmp/h.json"); d = json.loads(p.read_text())
d["app_running"] = False; d["app_pids"] = []
p.write_text(json.dumps(d, ensure_ascii=False))
PY
python3 healthcheck.py --from-health /tmp/h.json --emit --apply   # CREATED（一張）
python3 healthcheck.py --from-health /tmp/h.json --emit --apply   # SKIPPED_EXISTING（不是第二張）
```

## 第二意見（背景量測，不影響貼字）

貼完字之後，python 端會把**同一段音訊**再送一次 Vertex `gemini-3.5-transcribe`，比對它跟本地
Breeze 聽到的哪裡不一樣，寫進 `.second-opinion.jsonl`（gitignored）。它**不會改任何已貼上的字**，
也不進定版流程——存在的目的只有一個：累積樣本回答「本地跟雲端誰常對」。

**代價：音訊會離開這台機器**（上傳到自己的 Vertex 專案；原本只有文字會送）。`VOICE_SECOND_OPINION=0` 可整條關掉，serve 啟動時會印出 `[serve] 第二意見=on/off`。

```bash
./.venv/bin/python transcribe.py --second-opinion-report     # 一致率、常見不一致、延遲、token
./.venv/bin/python transcribe.py /tmp/x.wav --second-opinion # 單檔同步跑一次並印出比對
```

比對分兩個維度，**不混在一起**：

- **字**：兩邊都先繁化（雲端回簡體，`s2twp` 後才比）再去標點；`10 → 十` 這種整段數字的差異
  記成「數字格式差異」，不算不一致。
- **標點**：單獨算，半形 `,` 與全形 `，` 先統一（那是編碼不是斷句）。本地 ASR 通常只吐逗號，
  雲端會自己補句尾——這正是目前靠規則版＋`gemini-3.8-flash` 補的那一段，所以值得分開量。
  定版檔（你真的看到的那一版）在 8 秒內出現時，會再多一組「定版 vs 雲端」的標點比對。

## 模型來源

- STT: 本地 `models/breeze-asr-25-mlx-4bit`
- 重寫：Vertex AI `gemini-3.8-flash`，失敗時自動退回確定性規則標點
- 個人專有名詞：`rewrite_terms.json`（未進版控；格式見 `rewrite_terms.example.json`）
- 用後學習：貼上後約 20 秒，若你在同一欄位手動改字，會從那次修改推導窄範圍 `誤聽→正確` 映射並寫入本機詞庫。音訊不上傳，學習也不走雲端。

## 設計決策

- 本地 7B 潤飾模型已移除；語意修正改走雲端 `gemini-3.8-flash`。
- **先貼再定版（Typeless 式）**：停止錄音後先貼「規則標點版」（實測 0.5–1.5s），雲端重寫在背景執行緒跑，完成後由 App 把剛貼的那一段原地換成定版（約 3–5s）。雲端失敗／逾時就保留已貼上的規則版，使用者永遠不用等雲端。
  - 原地替換有守門：欄位去掉那段之後必須仍等於貼上前的內容（代表你還沒手動改字），而且那段在欄位裡只能出現一次；不成立就不動手。
  - 定版完成後才排用後學習，並以定版文字記帳，避免把定版本身學成使用者的手改。
- 思考階段是延遲主因（實測 thoughts 325–980 token、單次 2–13s，長句會吃掉輸出預算而 `finish=MAX_TOKENS` 截斷）。預設送 `thinkingConfig.thinkingBudget=0`，並把 `MAX_TOKENS` 視為失敗。
- Whisper 靜音幻覺改用 RMS 能量門控過濾（`no_speech_threshold` 對 mlx-whisper 幻覺掩蓋無效）。
