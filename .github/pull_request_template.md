<!-- 這一包在解哪一單、用什麼證明。第 1 段與第 3 段沒填就不要合。 -->

## 解哪一單

Closes #

## 改了什麼

<!-- 一到三句。動到辨識／重寫行為的，寫出舊行為與新行為各是什麼，
     並說明這是「先貼規則版」還是「雲端定版」那一層的改變。 -->

## 證據（gate token）

<!-- 貼指令與它的實際輸出，不要貼結論。轉錄範例請把個資與公司名替換掉。 -->

```
./.venv/bin/python -m unittest discover -s . -p 'test_*.py'   # Ran N tests, OK
```

## 動到哪些敏感面

- [ ] 沒動模型來源／Vertex 專案／`local.env`
- [ ] 沒動 launchd agent（`install.sh`、`com.hunghanchu.*.plist`）
- [ ] 音訊上雲的行為沒變（若變了，這裡要寫出來並取得同意）

## 送出前確認

- [ ] 沒有把音檔內容、轉錄全文或個人用語表放進 repo
- [ ] 這包只解一張單（一個 issue 一個 session）
