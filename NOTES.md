
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
