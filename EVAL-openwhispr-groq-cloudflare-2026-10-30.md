# 競合評估：OpenWhispr ＋ Groq Whisper ＋ Cloudflare Workers AI（blog 2026-10-29）vs 現有 stack

- 日期：2026-10-30（v2，同日修訂：使用者澄清「已全部改成 Soniox 路線」）
- 評估對象：`company-notes.com`〈自己搭一套免費的 Typeless 替代方案〉＋ `OpenWhispr/openwhispr`（MIT）
- 使用者原問句：「他這種方式的語音轉文字給我的版本比較優劣」
- 範圍宣告（narrow-scope）：只回答「這套 vs 我現有這條線的優劣／要不要換或借」，未重做整個 repo 盤點。
- 本檔＝完整 Blocks 1–4 與 Gate Ledger；聊天只出「先用白話回答」六項＋Verdict。

## v1 → v2 修正（為何重寫）

| v1 說法 | 實況（2026-10-30 查證） |
|---|---|
| 決策主軸是「本地、零帳號」 | **已失效**：`VOICE_STT=soniox`，音訊每句離機；serve 印 `STT=Soniox 雲端，不載本地模型` |
| 主力痛點是 Vertex 定版 timeout（56/121） | **已退役**：Soniox 模式下 App 只送 `{"text"}` 給 serve 做繁化＋詞庫＋收尾，**Vertex 潤稿整條被繞過**；`[refine]` 最後一筆是 10-28 |
| 免費額度是次要價值 | **更次要**：Soniox 實測 35 筆、總音訊 262.9 秒（4.4 分鐘），$0.12/hr ⇒ 目前成本約 **$0.01**；即使每月 5 小時也只有 $0.6 |
| 借「跨供應商備援」保 Vertex | **改指**：真正暴露的是 **STT 單點**（Soniox 掛掉／斷網時沒有自動退路），不是潤稿 |

## 先用白話回答

1. **結論**：**不要換，借兩件事**。而且你改成 Soniox 之後，他這套的**相關性變高了**——
   因為「本地、零帳號」這個你原本的護城河已經沒有了，現在兩邊都是雲端聽寫。
2. **它是什麼**：一支現成桌面聽寫 app（OpenWhispr，Electron、MIT、跨平台），錄音丟 Groq 的
   `whisper-large-v3` 轉文字，再丟 Cloudflare Workers AI 的 Qwen3-30B 潤稿，中間用自己部署的
   Cloudflare Worker 轉接；額度用完自動改丟 Groq 的文字模型。對應你手上的是**整條 STT 管線**
   與**已被你繞過的潤稿層**。
3. **跟你現有的差在哪**：速度你贏（Soniox 串流，停止後中位 0.33 秒；他是整段上傳批次回傳）。
   但**潤稿你現在是空的**：Soniox 自帶標點，serve 只做繁化＋詞庫＋收尾，**沒有 LLM 修口誤那一層**——
   而這一層正是 10-27 把「裁剪端地」修成「裁剪的問題」的東西。他反而有（Qwen3 清贅字／口誤）。
4. **用了會得到什麼**：一個「有 LLM 潤稿＋有備援」的形狀，正好補你現在這條線的兩個洞：
   潤稿層被繞過、STT 只有一家。免費額度對你**沒有價值**（Soniox 一個月 $0.6 等級）。
5. **代價**：換整包要三個帳號＋多一支 Electron app 跟你的 ⌘E 搶熱鍵與麥克風（10-29 才修過
   兩個實例搶麥克風 SIGABRT）；且他沒有你的繁中詞庫、用後學習、四道守門。只借機制幾乎零成本。
6. **依結論二選一（只借一部分）**：①把潤稿層接回來（Vertex 或免費的 CF Qwen3 都可以，
   接在 Soniox 文字之後，走現有守門）；②STT 加一條自動退路（Soniox 失敗／斷網 → Groq whisper
   或本地 Breeze）。兩件都在現有 code 的失敗路徑上，不動主路徑。

### 六項沒涵蓋到的補充

**誰在哪種情況更好**：Windows／沒有 GPU／想要會議記錄與筆記／要「裝好就能用」→ 他那套贏。
要繁中詞庫、用後學習、能改 code、要 0.3 秒延遲 → 你這套贏。**但「隱私」這一欄現在打平**
（兩邊音訊都上雲），這是 v1 講錯、必須更正的一點。

**他的數字可信度**：全文沒有任何延遲或錯誤率數字，只有「我實測非常好用」；你這邊是 log 級證據
（35 筆、中位 0.33s、信心 0.947–0.987）。所以「哪個比較準」兩邊都還沒資格斷言。

## Block 1 — 現有 stack 盤點（分母）

| Capability area | Existing implementation | Maturity | Reconciled |
|---|---|---|---|
| 全域熱鍵 | `app/Sources/App.swift` Carbon hotkey ⌘E | mature | adoption-verified（10-30 log `[key] 看到 E 鍵`） |
| 錄音／注入 | `Recorder.swift` AVAudioEngine＋CGEvent 打字 | mature | adoption-verified（`[app] recording start`） |
| STT（唯一在跑的一條） | Soniox `stt-rt-v5` 串流，`VOICE_STT=soniox` | in-trial（10-29 起，35 筆） | adoption-verified（中位 0.33s、262.9s 音訊） |
| STT 備線（程式在、沒載入） | 本地 Breeze-ASR-25 MLX 4bit | dormant | install-verified（`models/`；serve 不載入） |
| 潤稿層（現況） | `postprocess_transcript`：OpenCC 繁化＋詞庫＋收尾標點 | mature | adoption-verified（`[post] N 字 → ...`） |
| LLM 語意潤稿 | Vertex `gemini-3.8-flash`＋四道守門 | **dormant**（Soniox 模式繞過） | adoption-verified（22 筆 `[refine]`，最後 10-28） |
| 繁中正規化／詞庫 | `s2twp`＋`rewrite_terms.json`（2 條正字） | mature | install-verified（312B，10-28） |
| 用後學習 | `EditLearning.swift` 20 秒快照 diff | mature | adoption-verified（214 筆 `[learn]`） |
| 靜音／幻覺閘 | RMS 能量閘＋`is_fabrication` | mature | adoption-verified（`test_hallucination.py`） |
| 第二意見量測 | Vertex `gemini-3.5-transcribe`，比字＋標點 | in-trial（11 筆，已關閉） | adoption-verified（`[2nd]` 11 筆） |
| 跨供應商備援 | 無（Soniox 失敗只報錯） | aspirational | n/a |
| 講話中即時顯示 partial | 無（Soniox 串流但沒顯示） | aspirational | n/a |
| 真 VAD | 只有能量閘 | in-trial | adoption-verified |
| GUI／設定介面／自動更新 | 無 | aspirational | n/a |
| 離線能力 | 程式保留但預設不載模型 | dormant | install-verified |

**這個領域的既有決策主軸**：由具體痛點驅動（標點錯、停→字 22 秒、M2 記憶體爆），以本機 log
為準；但 10-29 起「本地優先」這條主軸已被 Soniox 取代——**評估時不能再假設零帳號與離線**。

## Block 2 — 工具賣點 ÷ 現有成熟度

| 工具賣點 | 你現有的對應 | 商數 |
|---|---|---|
| 一句話安裝的 GUI app（Mac/Win/Linux） | 自製 Swift 選單列 app，只有 macOS | ≈0（沒有抱怨過介面） |
| 雲端批次 Whisper large-v3 | Soniox 串流 0.33s | ≈0（批次比串流慢） |
| 免費額度（Groq＋CF 每天免費） | Soniox $0.12/hr，月用量 ≈$0.6 | ≈0（錢不是痛點，見關鍵論證） |
| Qwen3-30B LLM 潤稿 | 潤稿層被繞過，只剩規則 postprocess | **non-zero → Block 4** |
| 額度用完自動切另一家 | 無；Soniox 失敗直接報錯 | **non-zero → Block 4** |
| 停用思考輸出 | 已是預設 `thinkingBudget=0`（但路徑 dormant） | ≈0 |
| 內建技術詞 ASR hint | 詞庫＋Soniox 熱詞通道 | ≈0 |
| 會議記錄／筆記／MCP | `meeting-translate` 另一條線 | ≈0（不同用途） |
| Windows 支援 | 無 | ≈0（無需求） |

**關鍵商數論證**：v1 把「免費」列為最大賣點，v2 必須翻掉——你 35 筆實跑的總音訊只有 262.9 秒，
Soniox 成本約 $0.01；就算每月講 5 小時也才 $0.6。**省錢這件事在你的用量下沒有可計算的價值。**
真正 non-zero 的是另外兩列，而且都是「你改成 Soniox 之後才出現的」：①最後一個 LLM 潤稿層被
繞過，現在沒有任何東西會修「裁剪端地→裁剪的問題」這種上下文口誤；②STT 只有一家雲端，
Soniox 掛掉或沒網路時沒有自動退路（Breeze 要改 env 重啟，冷啟要重新載 837MB 權重）。
同形狀的先例是 2026-10-18 的 typeless 生態評估（extract；出處
`EVAL-open-typeless-landscape-2026-10-18.md`）——其「不整包換、只借機制」的判斷 **holds**，
但該條 Block 1 的「本地 STT／本地潤飾」兩列**已 stale**（現為全雲端）。

## Block 3 — 最佳框架搜尋

| Frame | 錨定 Block 1 哪一項 | 痛點還是缺口 | 評估 |
|---|---|---|---|
| 工具自己的：用免費雲端取代付費聽寫 | STT（in-trial） | 缺口（月成本 $0.6） | ≈0 |
| 替代：把他那三層（免費 ASR＋免費 LLM 潤稿＋自動換供應商）當**你少掉的那兩層**的參考 | 潤稿層（dormant）、跨供應商備援（aspirational） | **痛點**（潤稿層消失、單點依賴） | non-zero → Block 4 |

## Block 4 — 可借機制

| 可借機制 | 落點 | 不借的理由 |
|---|---|---|
| LLM 潤稿層接回 Soniox 之後 | `transcribe.py` `postprocess_transcript` 之後，走現有四道守門 | 直候選，缺口明確；供應商未定（Vertex 已付費／CF Qwen3 免費） |
| 失敗自動換供應商（STT 層） | App `SonioxCoordinator` 失敗路徑 → Groq whisper 或本地 Breeze | 直候選；痛點是韌性不是錢 |
| OpenAI 相容的 Worker 代理層 | — | 否決：直連 SDK 即可，多一層要維運 |
| 提示詞工作室（GUI 測 prompt） | — | 否決：prompt 在 code，跑測試更快 |
| Worker 內參數覆寫 | — | 否決：`local.env` 已在做同一件事 |
| 會議記錄／diarization／筆記／MCP | — | 否決：不同用途，`meeting-translate` 已在做 |
| 內建技術詞清單／停用思考 | — | 否決：已有詞庫；thinking=0 已預設 |

## Gate Ledger

```
Step 0: no-qualification — 先例 2026-10-18（openless+Talk, extract）同 frame 且 holds，但有 delta（STT 已全雲端、潤稿層 dormant），不構成 fast-kill；2026-10-17 那條 VOID 不引用
Challenge 1 mechanism layer: checked → OpenWhispr README/package.json（Electron、MIT）、blog 的 Worker 環境變數表、issue tracker 依留言數排序、Soniox 定價頁
Challenge 2 alternative frame: checked → Block 3（替代框架只有一個）
Challenge 3 extraction: checked → Block 4 七列
Challenge 4 testing triage: not-cheap — 阻塞：需 Groq 金鑰＋Cloudflare 帳號與 Worker 部署；OpenWhispr 安裝後會與現有 ⌘E 搶熱鍵與麥克風（10-29 SIGABRT 前例）；would-run: brew install --cask openwhispr
Tool-specific axis: 成本 — Soniox 實測 262.9s/35 筆 ⇒ $0.01；Groq 免費 8h/日與 CF 1 萬 neurons/日對本用量都是雜訊
Test output: n/a（triage = not-cheap；白名單第 2 項）
```

## Verdict

**extract**（修正後仍為 extract，但借的東西換了）。整包採用的期望值低於維護現有這條線：他的價值
建立在「沒有 GPU、不想付費、用 Windows」三個前提，三個在你身上都不成立；而免費額度在你
的實際用量（262.9 秒/兩日）下等於沒有價值。真正可搬的是**兩層結構**：①LLM 潤稿層接回 Soniox
之後（你 10-29 切成 Soniox 時把 Vertex 那一層一併繞過了，現在沒有任何東西修上下文口誤）；
②STT 失敗自動換供應商（現在 Soniox 掛掉就是掛掉）。

- 要接進哪裡：①`postprocess_transcript` 之後；②App `SonioxCoordinator` 失敗分支。
- 下一步：待你決定；若要動，先量一輪「加潤稿層後每句多花多少時間」與「Soniox 失敗率」。
- 最便宜的價值路徑：潤稿層先用已付費的 Vertex（守門都現成），供應商選擇不必現在決定。
- 反轉條件：若你要 Windows／非 Mac 環境，或 Soniox 月費漲到有感的量級 → 重跑本評估改走 trial。

## Precedent write-back

- `~/orca/state/tool-eval-ledger.md`：`## OpenWhispr + Groq/Cloudflare (2026-10-30)` 追加 v2 修正
  （借的項目換成潤稿層＋STT 備援；成本列 ≈0）。
