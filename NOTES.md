
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
