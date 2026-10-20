
## 2026-10-17 全域快捷鍵（⌘E）按了沒反應 → 真正原因與修法

- 症狀：按左 Cmd+E 完全沒反應。`.run.log` 從頭到尾 **0 筆 `source=global`**（只有 app 自己在前景時的 `source=local` 3 筆）。
- 根因：**TCC 的輔助使用授權對不上現在的簽章**。`/Library/Application Support/com.apple.TCC/TCC.db` 裡
  `kTCCServiceAccessibility | local.hunghanchu.voice-input-mac | 2`（表格寫「允許」，last_modified 15:09），
  但 app 執行時 `AXIsProcessTrusted() = false`。今天的穩定簽章是 20:50 才裝上去的，15:09 那筆綁的是舊 ad-hoc 簽章。
  → 全域 NSEvent 監聽器**照樣註冊成功、卻永遠不會被呼叫，而且完全不報錯**。
- 教訓：要判斷權限是否真的生效，**在 app 內印 `AXIsProcessTrusted()`**；只讀 TCC 表格會被騙（表格說允許≠執行時被信任）。
- 教訓：`handleKey` 原本只在通過 guard 後才寫 log，所以「根本沒收到事件」和「收到但被規則擋掉」長得一樣。
  已改成 keyCode==14 時無條件寫一筆 `[key] 看到 E 鍵 flags=... source=...`。
- 修法（已做）：加 **Carbon `RegisterEventHotKey`** 當保底觸發路徑，⌘E 走系統層、**不需要任何 TCC 權限**。
  `InstallEventHandler(GetApplicationEventTarget(), ...)` + `RegisterEventHotKey(UInt32(kVK_ANSI_E), UInt32(cmdKey), ...)`；
  build 要加 `-framework Carbon`。Carbon 的 pressed 會連續送，用 `hotKeyHeld` + released 抑制重複。
- 陷阱：Carbon 熱鍵**分不出左右 Cmd**（都會觸發），而且註冊後該組合會被系統吃掉（其他 app 收不到 ⌘E）。
- 陷阱：**合成按鍵測不出 Carbon 熱鍵**。`osascript -e 'tell application "System Events" to keystroke "e" using command down'`
  實測：`RegisterEventHotKey` 回 0（成功）但 handler 完全沒收到。→ 驗證熱鍵只能用實體按鍵，或請使用者按。
- 這台機器本地時區是 **UTC+7**（本地 21:50 = 14:50Z），對 log 時間要換算。`date` 與 `date -u` 差 7 小時。
- 旁證：21:51:05 那次錄音**沒有任何 `[key]` 紀錄** → 是從選單/Dock 點出來的，不是鍵盤觸發。

## 2026-10-17 「要等很久才輸出文字」→ 每次呼叫都重新載入模型（已修）

實測同一支腳本、8.9 秒音檔：

| 階段 | 冷啟動 | 暖機後 |
|---|---|---|
| Whisper 轉寫 | 1.46 s | 0.27 s |
| OpenCC 轉繁 | 0.01 s | 0.01 s |
| Qwen 潤飾＋補標點 | 2.79 s | 0.61 s |
| **合計** | **4.26 s** | **0.88 s** |

→ 慢的 100% 是「每次呼叫都重載 Whisper(1.6GB) + Qwen 7B 4bit(約4GB)」，推論本身不到 1 秒。

- 修法：`transcribe.py --serve` 常駐暖機程序（模型只載一次）。協定＝stdin 每行一個 wav 路徑、
  stdout 每行一個 JSON（`{"status":"ok|silence|suspect|error","text":...}`），診斷一律走 stderr。
  app 端在 **開始錄音** 時就把它拉起來 → 那 5–6 秒暖機剛好落在使用者講話的時間裡。
- 端到端實測（用選單點擊模擬）：`recording stop` → `[app] 結果（停止→文字 1.94s）`。
- 常駐代價 **RSS 6.1GB**。閒置 `VOICE_SERVE_IDLE_TIMEOUT`（預設 600s）自行退出；app 結束時
  stdin EOF 也會退出（不留孤兒）。
- app 端任何暖機路徑失敗（啟動不了／30s 逾時／回應不是 JSON／status=error）都靜默退回
  一次性路徑，所以最差情況＝改動前的行為。
- 陷阱：`mlx_whisper` **沒有** `load_model`；而且 `transcribe_audio()` 的靜音閘門會提早 return
  **不會載入模型** → 暖機必須餵「不是靜音」的音（用 0.27 振幅以下的雜訊即可）。
- 陷阱：同一套 exit 3/4 邏輯原本在 CLI 檔案模式與 live-record 模式各有一份，已把檔案模式
  抽成 `process_wav_file()` 給 CLI 與 `--serve` 共用；live-record 模式仍留著舊的重複（未動）。

## 2026-10-17 「按了 ⌘\ 但沒反應」的真正原因：輔助使用授權失效（一次修好兩個症狀）

**症狀**：⌘\ 有觸發（log 有 `[hotkey] Carbon ⌘\ 按下`、文字也轉出來了），但畫面上什麼都沒出現。
log 顯示：

```
[paste] ❌ osascript exit=1 stderr=execution error: System Events發生錯誤：不允許「osascript」傳送按鍵。(1002)
```

**根因**：`tccutil` 系統層那一筆 `kTCCServiceAccessibility|local.hunghanchu.voice-input-mac|2`
的 `csreq` 對不上現在的簽章（今天 20:50 之後重新簽章／重裝過），所以：

1. `AXIsProcessTrusted()==false` → 全域 NSEvent 監聽器**靜默失效**（嚴格左 Cmd 路徑死掉）。
2. System Events 的 `keystroke` 也被拒 → **貼不出去**（錯誤 1002「不允許…傳送按鍵」）。

也就是「快捷鍵」與「貼上」是同一個病：**輔助使用授權綁簽章，授權過期兩個一起壞**。

**修法**：`tccutil reset Accessibility local.hunghanchu.voice-input-mac` → 重啟 app →
在「系統設定 → 隱私權與安全性 → 輔助使用」把 `Voice Input` 開關**關掉再打開**
（開關一次會把 csreq 改寫成現在的簽章；只按＋加入或只重開不會改寫舊 csreq）。

**驗證**：`[perm] AXIsProcessTrusted=true`、`[paste] ✅ AppleScript 送出 ⌘V（耗時 0.07s）`。

**踩到的坑**：

- 貼上失敗原本是**完全靜默**的（`try? src.run()` + 不讀 stderr）。診斷一開始就該把
  子行程的 exit code／stderr 寫進 log，否則「文字有進剪貼簿但沒跳到游標」根本看不出來。
- `tccutil reset AppleEvents` 之後，AppleScript 的錯誤訊息會從「不允許「osascript」…」
  變成「不允許「Voice Input」…」——**spawn 出去的 `osascript` 送 AppleEvent 時，
  TCC 會算在 osascript 身上**。改用 `NSAppleScript`（自己行程送）才保證授權綁我們的 bundle id。
- 錯誤碼分工：`-1743` = 沒有自動化權限；`1002`（System Events）＝送按鍵的行程沒有
  **輔助使用**權限。看到 1002 要去查 Accessibility，不是 AppleEvents。
- 輔助使用記在**系統層** TCC（`/Library/.../TCC.db`），自動化／麥克風記在**使用者層**；
  查錯層會看到「清單空的」這種假象。
- TCC 對話框會讓送 AppleEvent 的呼叫**阻塞最久兩分鐘**：貼上務必丟到背景佇列，
  否則整個 app（含熱鍵）會像當掉。
- 啟動時用 `NSAppleScript` 做一次無害查詢（`get version`）當權限 preflight，把「缺權限」
  提前到啟動當下，而不是等貼上時才無聲失敗。

## 2026-10-17 「貼上成功但沒出現」第二層：這台機器把所有帶 ⌘ 的合成按鍵都吃掉

授權修好之後 `[paste] ✅ AppleScript 送出 ⌘V`，但畫面上依然什麼都沒有。用對照實驗
（TextEdit 開一個實體檔，貼完從記憶體＋磁碟讀回）釐清：

| 合成事件 | 結果 |
|---|---|
| `keystroke "XYZ"`（純打字，無修飾鍵） | ✅ 文字進去了 |
| `keystroke "v" using command down` | ❌ 完全沒反應 |
| `keystroke "a"/"x" using command down` | ❌ 完全沒反應 |

→ **不是權限、不是目標 app：這台機器上所有「帶 Command 的合成按鍵」都會被吃掉**
（純按鍵正常）。機器上有一堆事件工具在跑：Karabiner-Elements（Core Service + DriverKit
VirtualHIDDevice）、WindowsShortcuts、各家的 computer-use（orca.computer-use / trycua /
openai.sky / qwenlm.opencomputeruse）… 不需要知道是誰吃掉的，繞過去就好。

**修法：改成 CGEvent unicode 注入（等同使用者打字），不走 ⌘V**：

- `CGEvent(keyboardEventSource:virtualKey:0,keyDown:)` + `keyboardSetUnicodeString(...)`
  → 目標 app 收到的是鍵盤輸入，中文與標點都進得去（實測 TextEdit：`起始＿注入測試：STUB_PROBE_2026 中文與標點都要進得去。`）。
- 字串**只掛在 keyDown**（掛兩邊有些 app 會重複插入）；分塊 20 字、塊間 8ms
  （一次送太長終端機／TUI 會掉字）。
- 換行一律換成空格：TUI 會把 `\n` 當成「送出訊息」，逐字稿不該觸發送出。
- 剪貼簿仍然照放一份，手動 ⌘V 隨時可用；⌘V 的兩條路（AppleScript / CGEvent）降為保底。
- 這條路需要**輔助使用**權限（跟全域快捷鍵同一筆），所以那筆授權仍然是關鍵。

## 2026-10-17 熱鍵規格定案：⌘E（使用者指定）

- 使用者要的是：**按 ⌘E 開始錄音、再按 ⌘E 結束並把文字放進「當下游標所在的位置」**，
  且不限於 Orca。⌘\ 是先前另一條要求（找一個沒被佔用的鍵）下的產物，已改回 ⌘E。
- 熱鍵碼集中在 `AppDelegate.hotKeyCode`（`kVK_ANSI_E` = 14）＋ `hotKeyLabel`，Carbon／NSEvent
  兩條路徑共用；左右 Cmd 都接受（Carbon 熱鍵本來就分不出左右）。
- 「文字進當下游標處」＝ CGEvent unicode 注入到**最前面的 app**（誰有鍵盤焦點就進誰），
  與 app 種類無關；剪貼簿另外留一份。
- 驗證：`[hotkey] Carbon ⌘E InstallEventHandler=0 RegisterEventHotKey=0 ok=true`。
  Carbon 熱鍵**無法用合成按鍵驗證**（合成事件不觸發 Carbon hot key），只能真人按。

## 2026-10-17 潤飾會「順稿改掉原意」：prompt 是元凶，已加程式層驗證

**症狀**：使用者說「文字現在會不會出現？」，貼出來變成「文字現在不會出現。」——語意反了。

**根因**：不是 Whisper 聽錯（`[raw]` 完全正確），是 Qwen 潤飾的 system prompt 自己寫著
「移除填充詞／**刪除猶豫重複的字詞句**／口語贅字改為精煉書面用詞」→ 模型把「會不會」當成
重複的字順稿成「不會」。

**修法**：

1. prompt 反過來寫死：唯一任務是**加標點**與轉繁體；**重複的字必須原樣保留**
   （直接把「會不會／看看／非常非常」當例子寫進去），不可刪口語詞、不可改句型、
   不可把問句改成直述句；標點沒把握用「，」「。」，不要亂加「？」。
2. **程式層驗證（真正的護欄）**：潤飾結果去掉標點、空白並轉繁後，必須與原文**逐字相同**；
   不同就重試一次（加強提示），再不行就放棄潤飾、退回**原文＋半形標點轉全形**。
   → 從此潤飾不可能再改動任何一個字，最差情況只是標點少一點。
3. 副作用：`is_fabrication()`（用潤飾膨脹長度判斷幻覺）以後幾乎不會誤判了，
   因為潤飾再也不能膨脹文字。

**驗證**：5 個對抗案例 0 動字；完整 pipeline（TTS 語音→Whisper→潤飾）輸出
「文字現在會不會出現，你要不要去看看，這個非常非常重要…」字全對、標點全形。

## 2026-10-17 錄音中的畫面音波指示圖（像 Orca／Type Plus）

使用者要求：錄音時畫面上要有會跳動的音波圖示，一眼看出「現在在錄音」。

**做法**：`app/Sources/RecordingIndicator.swift` —— borderless `NSPanel`，
`.nonactivatingPanel` ＋ `orderFrontRegardless()`（**不搶焦點**，搶焦點會讓貼上的目標 app 跑掉）、
`ignoresMouseEvents = true`（不吃點擊）、`level = .statusBar` ＋ `.canJoinAllSpaces`。
位置：主螢幕下方置中（`visibleFrame.minY + 84`）。內容是 7 根跟著音量跳的條子＋紅點，
閒置時條子以 0.86/幀 衰減（看起來像呼吸）。

**音量來源**：`Recorder.onLevel`（主執行緒回呼）。**踩到的坑**：音量一開始只算 `ch[0]`，
永遠是 0——這台機器的輸入裝置 `CADefaultDeviceAggregate` 訊號只出現在 **ch[1]**
（實測 `ch=[0.0000, 0.0096]`）。要和下面混音邏輯一致：**平均所有通道**。
另一坑：對應曲線 `-60…0dB → 0…1` 對這支麥克風太保守（講話峰值約 -40dB），
改用 `-55…-10dB → 0…1` 再加 `sqrt` 壓縮，小音量才看得出來在動。

**驗證**：視窗數 0 →（錄音）1 →（停止）0；log `[ui] 錄音指示圖顯示/關閉`；
用喇叭放 440Hz 測試音時 `screencapture` 截圖可見條子明顯變高，
log 出現 `[rec] …（畫面音波圖峰值 0.29）`；停止後指示圖消失。

## 2026-10-17 標點：只搬標點、字一律用原話（transfer_punctuation）

**症狀**：使用者說「…讓我知道這個時候是在錄音的？」貼出來只有一個句尾問號，中間沒有逗號。

**根因**：Qwen 7B 對 70 字長句幾乎不肯補逗號；而且在「逐字驗證」的第一步就會被擋掉——
它想把句尾「…是在錄音的」修成「…是在錄音？」（實測 71→70 字），
於是整次潤飾失敗、退回原文（＝連標點都沒有）。

**修法**（`transcribe.py`）：
1. `transfer_punctuation()`：用 `difflib.SequenceMatcher` 逐字對齊模型輸出與原文，
   **只採用模型多插的標點**（`replace`/`insert` 區段的標點撿過來），字一律用原文。
   → 從此模型改字完全不影響輸出，語意不可能被改；最差只是標點少。
2. `finalize_punctuation()`：整段 0 個逗號且 ≥24 字時，用連接詞規則補斷句
   （然後／但是／不過／因為／所以／如果／而且／可是／接下來／另外／還有；
   「的時候／的話／之後」後面補）。只插標點、不碰字。

**驗證**：5 個對抗案例 0 動字；長句得到「…在啟動的時候，做一個像是…是在錄音的？」；
完整 pipeline（TTS→Whisper→潤飾）字全對、標點全形。

## 2026-10-18 競合評估：GitHub topic「typeless」36 repos

完整評估（Blocks 1–4＋Gate Ledger＋Verdict）在 `EVAL-open-typeless-landscape-2026-10-18.md`。
判決 **extract**：不整包換，借五個機制（即時部分轉錄、從修正學熱詞、Silero VAD、潤飾強度分級、
中文潤飾回歸測試素材）。可搬碼的來源首選 MIT 的 `platx-ai/Talk`（同架構、有中文測試素材）；
`Open-Less/openless`（雲端 ASR＋雲端潤飾、2.0 起 AGPL-3.0）只當功能對照表，不要搬碼進本公開 repo。

**盤點後修正的舊假設（環境事實，重要）**：

1. **Orca 內建聽寫是關的**（使用者 2026-10-17 21:49 自行關閉）：**現行設定在
   `~/Library/Application Support/orca/profiles/local-default/profile-state.db`（SQLite）的
   `settings` domain**，`voice.enabled = false`（連 `microphoneDeviceLabel: "MixPre-6 II"` 一起）。
   **不要讀同目錄的 `orca-data.json` 判斷現況**：它是 2026-10-10 遷移前的退役快照
   （`profile-state.db.authority` 的內容就是 `sqlite`），mtime 停在 10-10，裡面
   `voice.enabled: true` 是舊值——第一版競合評估就是這樣誤判、被使用者當場糾正。
   pid 42151 那個 `Orca Helper … audio.mojom.AudioService` 也不能當聽寫證據：它是 Chromium
   通用音訊服務，App 播任何聲音都會在，與麥克風無關。
2. **macOS 內建聽寫用過且仍啟用**：`defaults read com.apple.speech.recognition.AppleSpeechRecognition.prefs`
   → `DictationIMLastDictationDate` = 2026-10-11、麥克風 `MixPre-6 II`、語言 zh_TW、自動標點 True。

另外查到：同機還有一條雲端線 `~/orca/meeting-translate`（OpenAI Realtime ASR ＋ `/polish`，
`zh_tw_normalization.json` 10,607 條）；superwhisper／Wispr Flow／MacWhisper／VoiceInk／WhisperKit
在這台機器上**完全沒有痕跡**（grep／brew／pipx／~/Library 全 0 命中）。

## 2026-10-18 錄音中的即時部分轉錄（partial 預覽）

機制：錄音中每 1.2s 把「目前為止」的音訊寫成 `voice-preview.wav`，送到**同一個**暖機程序
（協定多一種請求：`partial <path>`），只跑 Whisper ＋ OpenCC 繁化（**刻意不潤飾**：Qwen 一次
0.6s 追不上講話速度，而且預覽不是最終文字），結果顯示在音波膠囊上方的面板
（`RecordingIndicator.setPreview()`）。停止後插入的最終文字完全不變（仍走 whisper ＋ 繁化 ＋
逐字保真潤飾 ＋ CGEvent 注入）——預覽只是提示，不影響契約。

實測（M4 Max）：預覽 0.24s/次（5.6s 音檔、暖機後；冷啟含載模型 7.3s）；13 秒錄音跑 11 次
預覽（≒1.2s 一次）；stub 端到端 11:11 對帳；注入位置以 TextEdit 文件內容（不是 log 自述）驗證。

踩到的坑（都已修，別再犯）：

1. **`sys.stdin.readline()` 會讓 `select()` 看不到排隊的請求**：TextIOWrapper 會把管子裡後面的
   行先讀進自己的緩衝區，於是「預覽讓位給停止請求」的判斷永遠看不到那行（實測第三個測試因此
   沒讓位）。改成自己用 `os.read` 組行（`_RequestReader`），drain 才看得到。
2. **`.run.log` 有兩個寫者就會掉行**：主執行緒事件與抽乾 serve stderr 的背景執行緒各自
   `seekToEndOfFile` ＋ write → 撞在一起整行消失（實測 Python 印了 2 次 `[partial]`、Swift 只有
   1 行 `[preview]`）。已加 `Transcriber.logLock`。log 是唯一證據通道，掉了就等於沒發生。
3. **NSTextField 標籤高度算錯＝第二行整行消失**：15pt 一行約 18px，兩行要 36px；只給 34px 時
   畫面看起來像「文字被截斷」（其實是根本沒畫出來）。現在面板 78px、標籤 48px。
4. **暖機完成前不要送預覽**：那個請求會卡在「模型還在載」的管子裡，使用者講 3 秒就按停止時，
   正式結果得排在它後面等模型載完（~7s）。已加 `Transcriber.warmReady`（以 serve stderr 的
   「暖機完成」為訊號）。
5. **暖機程序同時間只准一個請求在飛**（`Transcriber.requestLock`）：預覽與正式結果共用同一條
   stdin/stdout，兩個讀者並行會把「誰的回應」讀錯（等於拿到別人的文字）。

已知取捨：

- 第一次錄音（暖機中，約 7s）不會有預覽，暖機完成後才開始。要「第一次就有預覽」得讓暖機程序
  常駐（現在閒置 600s 退出，不整天佔 6.1GB）——這是記憶體 vs 體驗的選擇，要改再改。
- 長錄音（>30s）每次預覽要重解整段音訊 → 更新變慢（60s 音檔約 2s 一次），且停止後可能多等
  一次預覽的時間（≤2s）。短句聽寫不受影響。

驗證方式（重跑用）：

```bash
# Python 協定（決定性、不用麥克風）：預覽 0.24s、排隊的完整請求會讓預覽直接讓位
cd ~/orca/voice-input-mac && .venv/bin/python /tmp/serve_partial_test.py
# app 管線（stub 不依賴麥克風音量）：
launchctl setenv VOICE_INPUT_TRANSCRIBE_PY /tmp/stub_preview.py && open -a /Applications/VoiceInputMac.app
# 用選單觸發開始/停止（合成事件不會觸發 Carbon 熱鍵）
osascript -e 'tell application "System Events" to tell process "VoiceInputMac" to click menu item 1 of menu 1 of menu bar item 1 of menu bar 2'
```

## 2026-10-19 ASR 換成 Breeze-ASR-25（MLX 4bit，使用者決定）

**決定**：ASR 由 `whisper-large-v3-turbo` 換成 `Breeze-ASR-25`（聯發科，Whisper-large-v2 微調，
Apache-2.0）；**潤飾模型不動**（仍是 Qwen2.5-7B-4bit）。理由：台灣中文＋中英混用是使用者實際情境，
官方 benchmark 在該情境勝過 large-v3（CV16-zh-TW 8.95→7.97、CSZS 26.43→13.01），
而 turbo 的優勢只有速度。

**接法**（不進版控）：
- 模型放 `models/breeze-asr-25-mlx-4bit/`（`config.json` ＋ `weights.safetensors` symlink 到 HF cache 的 blob）。
  用社群 MLX 轉檔 `BRlin/Breeze-ASR-25-mlx-4bit`（官方只給 transformers 格式；`weights.safetensors`
  這個檔名是 mlx_whisper 硬需求，BRlin 原本叫 `model.safetensors`）。
- `local.env` 設 `VOICE_STT_REPO=<repo>/models/breeze-asr-25-mlx-4bit`（`.gitignore` 已含 `models/`）。
- 回滾：刪掉 `local.env` 的 `VOICE_STT_REPO` 那一行（或改回 `mlx-community/whisper-large-v3-turbo`）。

**實測（M4 Max，同一批 4 段音、同一個 initial_prompt）**：

| | turbo fp16 | Breeze fp16 | Breeze 4bit |
|---|---|---|---|
| 推論/段 | 0.26–0.28s | 0.44–0.54s | 0.41–0.48s |
| 峰值 RSS（ASR 程序） | 1.86 GB | 3.35 GB | 1.16 GB |
| 暖機程序總量（含 Qwen） | 6.16 GB | — | **5.47 GB** |

**Breeze 的兩個行為差異（已用程式補償）**：
1. **完全不吐標點** → 標點全交給 Qwen，實測 p1 會少結尾句號（其餘正常）。
2. **中文與數字/英文之間會加空格**（「早上 10 點」「這個 spec」）→ 新增 `normalize_spacing()`，
   在 ASR 輸出與最終文字各跑一次（Qwen 也會把空格加回來，只補 ASR 端不夠）。
   只移除緊鄰中日韓字元的空格，英文單字之間的空格保留。

**踩到的坑**：改完 `local.env` 後，app 說「已有暖機程序 pid=…，不重啟」——那是**改設定前**啟動的
舊伺服器，還載著舊模型（RSS 6.16 GB）。換模型後**必須手動 kill 舊的 `transcribe.py --serve`**，
否則下一次錄音還是用舊模型。現在 `serve_forever` 啟動時會印 `[serve] STT=… POLISH=…`，
log 可以直接確認載入的是哪一顆。

## 2026-10-19 規則化清掉口頭禪（cleanup_disfluencies）

**為什麼要有這層**：使用者要「嗯／那個那個／我我」被清掉，但**不能交給 Qwen**——它會順稿
（歷史案例：prompt 叫它「刪除猶豫重複」→ 把「會不會」改成「不會」，語意反轉）。所以改成
**程式規則**，在潤飾之前跑；模型仍然只准加標點。

**規則（只刪兩類，其餘一字不動）**：
1. 整詞重複折疊：`那個那個→那個`、`然後然後→然後`（重複一個虛詞永遠沒有語意）
2. 結巴單字折疊：`我我→我`、`這這→這`（**只有虛詞/指示詞/代名詞名單**；
   看、試、想、聽 等實義動詞不在名單）
3. 獨立語助詞刪除：`嗯/呃/啊/喔/哦/欸/唉`——必須是「句首或標點後」且「後面接標點/空白」；
   句尾語氣詞（`好啊`）不動；全句只剩語助詞時保留原樣。

**刻意不做**：一般「重複字折疊」會誤刪中文正常疊字（`看看`、`試試`、`非常非常`、`會不會`），
這也是為什麼不能寫成「任何字連續重複就折一個」。

**實測**：
- 單元：`嗯，那個那個，我我覺得這個這個方案，呃，應該可以` → `那個，我覺得這個方案，應該可以`
- 不動：`這個非常非常重要`、`你要不要去看看`、`文字現在會不會出現`、`好啊，就這樣`
- 端到端（塞滿口頭禪的 TTS 音）：`[final] 那個，我覺得這個方案應該可以，就是說先這樣。`
- 回歸：原本 4 段音的 `[final]` **一字未變**

**坑**：改完程式一定要 `pkill -f "transcribe.py --serve"`——暖機程序把舊程式載在記憶體裡，
不重啟就還是跑舊邏輯（跟換模型那次同一個坑）。

## 2026-10-19 M2 記憶體爆掉：根因與定案（M2 = 純 Breeze、無 polish）

- 09:00 使用者回報「M2 語音輸入太慢、還碰到記憶體不足」。09:08 從 M4 ssh 進 M2 量到：
  **swap 9.5GB/10GB、15 分鐘 load 15.28、free 20%**，畫面已卡死。
- 根因不是單一模型太大，是 **16GB 上同時有三份 7B 等級模型**：
  1. VoiceInputMac 底下掛了**兩個** `transcribe.py`（各自載 Breeze + Qwen2.5-7B-4bit）；
  2. 另一個 session 啟的 `/tmp/polish_eval.py` 迴圈（7B→3B→4B 依序跑，當時 7B 正在跑）。
  不是三個模型同時載入，是「兩份 7B + 第三份 7B」疊起來。09:14 M2 重開機後 swap 歸零。
- 09:41 **M2 本機實測**（不是推算）：Breeze STT only 尖峰 **2.35GB**（常駐 footprint 2.26GB）；
  Breeze + Qwen3-4B polish 尖峰 **5.42GB**；Breeze 4bit 權重本身 837MB。
- 09:47 使用者選 **no-polish**：`app/Sources/Transcriber.swift` 的暖機與一次性退路都固定加
  `--no-polish`；`local.env` 註解掉 `VOICE_POLISH_REPO`；暖機 log 改印
  `POLISH=disabled (--no-polish)`。**M2 現行尖峰約 2.3GB。**
- 09:58 證實 Breeze 不吐標點（`[raw]` 與 `[final]` 一字不差）→ 10:01 接**零成本規則標點**
  （連接詞前逗號＋句尾句號／問號；`insert_clause_commas()` 等），不載模型、不增延遲。
- 10:18–10:22 依使用者指示清掉 M2 的 Qwen 2.5-3B/7B、Qwen3-4B 與 whisper-large-v3-turbo
  （HF cache 6.1GB → 837MB）；`test_stt.py`／`test_hallucination.py` 不再寫死 whisper；
  `transcribe.py` 預設 STT 改 `BRlin/Breeze-ASR-25-mlx-4bit`、`POLISH_REPO` 預設空＝自動關閉潤飾。
- **兩台刻意不同（使用者明確要求不要混）**：
  | | ASR | 潤飾 | 尖峰 |
  |---|---|---|---|
  | M4 | Breeze-ASR-25 4bit | Qwen2.5-7B-4bit | ~5.5GB |
  | M2 | Breeze-ASR-25 4bit | 無（規則標點） | ~2.3GB |
- 10-20 16:27 M2 的無版控工作副本被 park 成 `origin/wip/m2-voice-input-20261020`
  （commit `7147610`，排除 `local.env`／`models/`／`.venv/`），內容含 `--no-polish`、規則標點、
  熱鍵 toggle、LoginAgent 自動啟動。**尚未併入 main**，要用前先審。
- 證據位置：OpenCode session DB `~/.local/share/opencode/opencode.db`
  → `ses_eaf9b7d3dffeUI4b1n5G0kem0t`（09:00 事故、09:08–09:14 診斷）、
  `ses_eae4fbff3ffeFKlXzekClOXq8L`（08:17 換 Breeze、09:41 量測、09:47 no-polish、10:18+ 刪模型）。
- M2 本機的 `.run.log`／`HANDOFF-M2.md` 隨機器關機無法讀取；以上以 M4 端 session 紀錄為準。
