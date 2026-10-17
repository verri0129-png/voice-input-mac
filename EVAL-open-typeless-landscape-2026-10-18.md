# 競合評估：GitHub topic「typeless」生態（36 repos）vs 現有 stack

- 日期：2026-10-18
- 評估對象：`https://github.com/topics/typeless`（36 repos）＋其所對應的 `typeless.com`（閉源雲端訂閱）
- 使用者原問句：「這連結給你，一堆現成的，你評估一下」（前一輪：「你確定不用參考 typeless？」）
- 範圍宣告（narrow-scope）：本輪只回答「值不值得借／要不要改用現成的」，未重做整個 repo 的
  全面盤點。Block 1 由一支 fan-out subagent 實查本機（產出 5 段事實），其回報結果已併入下段。
- 本檔＝完整 Blocks 1–4 與 Gate Ledger；聊天只出「先用白話回答」六項＋Verdict。

## 先用白話回答

1. **結論**：**不要整包換，只借機制**（extract）。這些現成專案值得參考，但用它們替換你手上這支
   在本機的期望值低於繼續維護；真正該借的是五個我們還沒做的機制。
2. **它是什麼**：GitHub 上有一整個「開源 Typeless／Wispr Flow 替代品」生態（36 個 repo），就跟你
   這幾天做的東西同一件事——按鍵、說話、潤飾後的文字出現在游標處。桌機可用的只有 8 個，
   其中 `Open-Less/openless`（跨平台、雲端 ASR＋雲端潤飾）與 `platx-ai/Talk`（macOS、全本地 MLX）
   是最完整的兩個；`typeless.com` 本身是閉源雲端訂閱產品，不在可採用範圍。
3. **跟你現有的差在哪**：我們這條「⌘E→錄音→本地 Whisper→逐字保真的潤飾→CGEvent 打字注入」
   已經能跑，而且解掉兩個只有這台機器才有的病（帶 Command 的合成按鍵會被系統吞掉、Accessibility
   授權會因重新簽章失效）。現成專案贏在**功能面**：講話中即時顯示部分轉錄、浮動音量指示、
   自動從你的手動修正學聽錯的詞、per-app 潤飾風格、真正的 VAD、設定介面與自動更新。
4. **用了會得到什麼**：若你想要的痛點是「講完才發現聽錯」與「不知道到底有沒有在錄」，
   借這幾塊能直接解掉；整包換過去只會多拿到 UI 與更新機制，而我們缺的那幾塊它也是各自實作，
   不會自動流進來。另外盤點時挖到一件跟你痛點直接相關的事：**Orca 內建聽寫現在的設定是
   開啟的（`settings.voice.enabled: true`），它會搶麥克風**——你之前用 `voice-watch` 追的
   「到底是誰在錄音」還沒結案。
5. **代價**：`openless` 預設要 **Volcengine ASR＋Ark 潤飾的雲端憑證**（本地只有 ASR），而且
   2.0.0-Beta.1 起改為 **AGPL-3.0-only**（1.x 才是 MIT）——把它的碼搬進我們這個公開 repo 會被
   授權傳染；`Talk` 是 MIT，但要再下載 ~3GB 模型、重新授權 TCC，且會與現有 ⌘E 流程互搶。
   兩者都要付「重學一套系統」的成本。
6. **依結論二選一（extract）**：可借的五個機制與落點見 Block 4；最便宜的第一步是把
   **即時部分轉錄**做進現有的錄音指示圖（純本地、無新依賴、同時解掉「有沒有在錄」與
   「講完才發現聽錯」）。

## Block 1 — 現有 stack 盤點（分母）

| Capability area | Existing implementation | Maturity | Reconciled |
|---|---|---|---|
| 全域熱鍵 | `app/Sources/App.swift` Carbon RegisterEventHotKey ⌘E | mature | adoption-verified（`.run.log` `[hotkey] … ok=true`） |
| 錄音 | `app/Sources/Recorder.swift` AVAudioEngine，逐緩衝 RMS | mature | adoption-verified（`[rec] … 畫面音波圖峰值 0.29`） |
| 錄音中指示 | `app/Sources/RecordingIndicator.swift` NSPanel 膠囊＋7 條音波 | in-trial（今日上線） | adoption-verified（視窗數 0→1→0＋截圖） |
| 本地 STT | `transcribe.py` mlx_whisper `whisper-large-v3-turbo`＋initial_prompt | mature | adoption-verified（`[raw]`） |
| 本地潤飾 | `transcribe.py` `polish_text()` Qwen2.5-7B-4bit＋逐字不變 invariant | mature（今日收緊） | adoption-verified（5 對抗案例 0 動字） |
| 延遲 | `transcribe.py --serve` 暖機常駐（RSS ~6.1GB） | mature（停→字 0.7–1.9s） | adoption-verified（`[app] 結果（停止→文字 X.XXs）`） |
| 文字注入 | `Transcriber.insertText` CGEvent unicode 打字（20 字/塊） | mature | adoption-verified（實注入＋log） |
| 權限／TCC | `app/build.sh` 穩定自簽＋`preflightAutomation`＋`AXIsProcessTrusted` | mature | adoption-verified（`[perm]`） |
| 幻覺／靜音閘 | `is_silence`＋exit 3/4＋`is_fabrication` | mature | adoption-verified（`status=silence`） |
| 診斷 | `.run.log`＋`[raw]/[raw-tw]/[final]` | mature | adoption-verified |
| 潤飾回歸測試 | 只有 `/tmp/polish_test*.py` 臨時腳本 | aspirational | adoption-verified（repo 內無測試） |
| 講話中即時部分轉錄 | 無 | aspirational（absence） | n/a |
| 從修正自動學熱詞 | 無（只有手寫 initial_prompt） | aspirational（absence） | n/a |
| VAD | 只有能量閘 | in-trial | adoption-verified |
| 發佈工程 | 本機 `build.sh` 裝 `/Applications` | in-trial | adoption-verified |
| 第二條線（同機、非同用途） | `~/orca/meeting-translate` 雲端 ASR（OpenAI Realtime）＋`/polish`＋`zh_tw_normalization.json`（10,607 條） | mature | adoption-verified（subagent 實查） |
| Orca 內建聽寫 | sherpa-onnx（zipformer-bilingual-zh-en），RPC `DictationStart/Chunk` | active（`settings.voice.enabled: true`） | adoption-verified（`orca-data.json`＋`voice-watch/run1/mic.log` pid 42151 為 Orca audio service） |
| macOS 內建聽寫 | `com.apple.speech.recognition…prefs`，zh_TW、麥克風 MixPre-6 II | previously used（2026-10-11 最後一次） | adoption-verified（`defaults read`） |
| 第三方商業聽寫工具 | superwhisper／Wispr Flow／MacWhisper／VoiceInk／WhisperKit | not found（無任何痕跡） | adoption-verified（grep／brew／pipx／Library 全 0 命中） |

**這個領域的既有決策主軸**：本地、零帳號、可改；而且**以本機實測為準**——這台機器會吞掉任何
帶 Command 的合成按鍵，且 Accessibility 授權會被重新簽章打掉，所以「別人的 README 怎麼寫」
不構成可行證據。盤點另修正了兩個我先前的錯誤假設：Orca 內建聽寫**沒有被關掉**（現為 enabled），
macOS 內建聽寫在本自製 app 可用之前仍在使用中。

## Block 2 — 候選 ÷ 現有成熟度（逐項）

| 候選 | 形式／授權 | 它的主打能力 | 對應我們現有的 | 商數 |
|---|---|---|---|---|
| `Open-Less/openless` | Tauri2+Rust；AGPL-3.0（2.0 beta 起） | 跨平台聽寫、雲端 ASR＋雲端潤飾、串流注入、style pack、詞典學習 | 熱鍵／注入／潤飾（mature）、指示圖（in-trial） | non-zero → Block 4；整包採用被雲端依賴與 AGPL 擋住 |
| `platx-ai/Talk` | Swift；MIT | 本地 MLX 聽寫、浮動指示＋即時預覽、熱詞學習、per-app prompt | 同上，且我們已是同架構 | non-zero → Block 4（最近的雙胞胎、最佳取材源） |
| `hehehai/voxt` | Swift；macOS | 會議記錄、說話者分離 | 無（不同用途） | ≈0 |
| `joe223/AriaType` | Rust | 跨 app 語音寫作、SenseVoice | 同上核心 | ≈0 |
| `Wentong-Liu/SayIt` | Swift；macOS | 「Typeless 免費版、無字數限制」 | 自製 app 已是同級且更貼本機 | ≈0 |
| `0xlau/openbroca` | Electron／TS | 可插拔本地或雲端 ASR＋LLM | 同上 | ≈0（Electron 肥） |
| `ethannortharc/fonos` | Rust | 語音 recipe／TTS／桌面動作 | 無（超出聽寫） | ≈0（不同用途） |
| 其餘 12 個 | 無關或寄生 | — | — | ≈0 |

剔除的 12 個：與聽寫無關（`create-react-app-starter` 等）、硬體與遙控（`dji-mic-command`、`RemotePilot-Releases`）、
寄生／逆向（`Typeless-AD-Skipper`、`fufu1209/Typeless` 繞訂閱額度、`typeless-switch`、`typeless-plusplus` 已停維護）、
非 macOS 或用途不符（`typoless` 已封存、`typeless-ibus`、`vokiki`、`typex`、`Hoshiro`）。

> 表內刻意不列星數：星數是「採用跡象」而非能力證據，不能拿來當成熟度或採用理由。

**關鍵商數論證**：最有份量的那一列是 openless 的「串流注入＋多雲端供應商」。它是唯一成熟到
一句指令就能取代整個 app 的東西（最近仍有更新、有正式釋出通道與套件管理安裝），但預設路徑要
**雲端 ASR 與雲端潤飾憑證**，本地只有 ASR；而且 2.0 起為 AGPL-3.0。我們這邊零帳號、全本地，
且已解掉兩個本機特有病理。此列與上一次（2026-10-17，`tool-eval-ledger.md` 內一條 **VOID** 條目，
標的誤讀為 skill 而作廢）無關，不是同一條先例的重複。

## Block 3 — 最佳框架搜尋

| Frame | 錨定 Block 1 哪一項 | 痛點還是缺口 | 評估 |
|---|---|---|---|
| 工具自己的框架：用開源本地聽寫取代 Typeless | 文字注入管線（mature） | 缺口（已經有了） | ≈0 |
| 替代框架（僅一個）：把這些 repo 當「我們還沒做的那幾塊」的參考實作 | 潤飾品質＋錄音體驗（in-trial／aspirational） | **痛點**（使用者連兩次抱怨標點與看不到錄音中） | non-zero → Block 4 |

## Block 4 — 可借機制（extraction）

| 可借機制 | 接到我們哪個檔案 | 來源 | 不借的理由（若否決） |
|---|---|---|---|
| 講話中即時顯示部分轉錄 | `transcribe.py serve_forever`＋`RecordingIndicator` 面板顯示 partial | Talk／openless | — 直的候選 |
| 從手動修正學聽錯的詞（熱詞表） | 新 `vocab.json` → 餵 `STT_INITIAL_PROMPT` 與 `POLISH_SYSTEM_PROMPT` | Talk（EditObserver）／openless（需確認卡） | — 直的候選，但要使用者確認，不可靜默學習 |
| Silero VAD 取代能量閘 | `transcribe.py is_silence` | Talk（`silero_vad.mlmodelc`） | — 直的候選 |
| 潤飾強度分級（輕／中／強） | `POLISH_SYSTEM_PROMPT` 變體＋選單 | Talk（PolishIntensity）／openless（4 模式） | — 直的候選 |
| 中文潤飾回歸測試素材 | 新增 `tests/`（音檔＋期望輸出） | Talk（`zh_short/medium/long`、`polish_cases.json`） | — 直的候選 |
| thinking block 剝除 | `polish_text` 輸出清理 | Talk（`stripThinkingBlock`） | 否決：Qwen2.5 不吐 think，換模型才需要 |
| Style pack／市集 | — | openless | 否決：等於拆掉「不准改字」護欄，且超出範圍 |
| 讀取游標前後文 | `Transcriber` 走 AX | openless／Talk | 否決：隱私＋目前沒有這個痛 |
| 雲端 ASR／潤飾供應商 | — | openless | 否決：與本地、零帳號前提衝突 |

## Gate Ledger

```
Step 0: no-qualification（tool-eval-ledger 內唯一 typeless 條目為 2026-10-17 VOID：標的誤讀，非先例；閉源 typeless.com 有結構障礙但本輪問的是「借不借鏡」，不構成 fast-kill）
Challenge 1 mechanism layer: checked → 讀 Talk 的 Talk/LLM/LLMService.swift 全文＋GitHub API 檔案樹；openless README 的 Status/Architecture/License 段
Challenge 2 alternative frame: checked → Block 3（替代框架只有一個）
Challenge 3 extraction: checked → Block 4 九列機制清單
Challenge 4 testing triage: not-cheap → 阻塞因素：openless 預設需 Volcengine＋Ark 雲端憑證、Talk 需 ~3GB 模型下載，兩者都要重新授權 TCC 並與現有 ⌘E 流程互搶；would-run: brew tap Open-Less/openless && brew install --cask openless（或 Talk 的 DMG）
Tool-specific axis: license — openless 2.0.0-Beta.1 起 AGPL-3.0-only（1.x MIT），搬碼會傳染本公開 repo；Talk 為 MIT
Test output: n/a（triage = not-cheap，未安裝；白名單第 2 項）
```

## Verdict

**extract**。整包採用（openless／Talk／其餘）在本機的期望值低於維護現有 app：我們已經在
「熱鍵→錄音→本地 STT→逐字保真的潤飾→CGEvent 注入」這條路成熟，且解掉兩個只有這台機器才有的
病理；換過去是用雲端憑證與 AGPL 換功能，不是換能力。價值集中在四個還沒做的機制：
**即時部分轉錄、從修正學習熱詞、真 VAD、潤飾強度分級**（外加中文潤飾回歸測試素材），
取材首選 MIT 的 `platx-ai/Talk`（同架構、有中文測試素材）；openless 只當功能對照表。

- 要接進哪裡：Block 4 的五個直候選。
- 下一步：待使用者選定；已記入 `~/orca/voice-input-mac/NOTES.md` 後續工作。
- 最便宜的價值路徑：把「即時部分轉錄」做進現有錄音指示圖（純本地、無新依賴）。
- 反轉條件：openless 或 Talk 出現**全本地潤飾＋能通過本機 Command 按鍵病理**的版本 → 重跑，改走 trial。

## Precedent write-back

- `~/orca/state/tool-eval-ledger.md`：新增 `## openless + Talk（typeless 生態）(2026-10-18)`，verdict=extract。
