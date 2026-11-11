# voice-input-mac — 工作規則

給在這個 repo 工作的 agent 讀。沒有寫在這裡的流程，不要自己發明。

## 驗收（PR 的 gate token，兩個都要貼實際輸出）

```bash
./.venv/bin/python -m unittest discover -s . -p 'test_*.py'   # 期望 Ran N tests / OK
./.venv/bin/python healthcheck.py                             # 期望 HEALTH=OK
```

貼指令與輸出，不要貼「應該沒問題」。紅燈就不要合。

## 這兩個證明不了什麼（重要）

它們證明「程式邏輯對」，**不證明「按 ⌘E 有字」**。端到端那一層目前沒有自動化：

- 現在：改完自己按一次 ⌘E、講一句、看有沒有字。
- 要自動化：走 `create-verification-skill`，產出一份「怎麼像真人操作、看到什麼才算成功」的 SOP（`cua-driver` 可以開 App 並操作）。**這是這個 repo 目前最大的缺口。**

宣告完成時要講清楚是哪一層過了：單元測試過了 ≠ 端到端過了。

## 大改動前先看爆炸半徑

動到音訊管線（`transcribe.py`、`soniox_stt.py` 的 `installTap`）、熱鍵 agent、雲端重寫之前，先讀：

```
~/.agents/skills-arsenal/blast-radius/SKILL.md
```

這支不在常駐層，要顯式讀。它會逼你回答「這次改動誰會被影響、怎麼證明沒被影響」。

## 修 bug 的規矩

1. **先重現**，把重現的實際輸出貼出來。重現不了就先做能重現的條件，不要靠猜。
2. **二分找原因**，一次砍掉一半可能性，每一步都要有執行期證據。
3. 沒有重現證據的修改不算修好。「可能會好一點」的東西不要送。

（這套是 pstack `bug-fix` playbook 的規矩，2026-10-29 的 SIGABRT 就是這樣修對的。）

## 加功能的規矩

動手前先寫一句 **gate token**：這次改完，跑什麼指令、看到什麼輸出，代表做完了。
寫不出來就代表這件事還不能開工——先想清楚怎麼驗，再寫程式。

## 不要碰

- `local.env`（憑證與本機設定，已在 `.gitignore`）
- launchd plist（`com.hunghanchu.*`）
- 模型來源／Vertex 專案設定
- 音檔內容、轉錄全文、個人用語表不進 repo
