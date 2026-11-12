#!/bin/bash
# 由 M4 的 launchd 於每天 09:00 觸發（見 ~/Library/LaunchAgents/orca.voice-input.m2-sync.plist）。
# 做法：等到 M2 連得上（使用者在 M2 的時段），讓 M2 自己 fast-forward 到 origin/main、
# 跑單元測試、重裝 App、把 ASR 預設設成 qwen06、預先下載 0.6B 模型。
# rewrite_terms.json 被 gitignore，所以從 M4 複製過去；local.env 不碰（各機器自己的設定）。
# 10:00 還連不上就放棄，結果寫在 ~/orca/state/voice-input/m2-sync.log。
set -u
REPO="$HOME/orca/voice-input-mac"
STATE="$HOME/orca/state/voice-input"
LOG="$STATE/m2-sync.log"
mkdir -p "$STATE"
log() { echo "$(date '+%F %T') $*" >> "$LOG"; }

DEADLINE=$(date -j -f '%F %T' "$(date +%F) 10:00:00" +%s)
until ssh -o ConnectTimeout=8 -o BatchMode=yes m2 true 2>/dev/null; do
  if [ "$(date +%s)" -ge "$DEADLINE" ]; then log "放棄：10:00 前 M2 連不上"; exit 1; fi
  log "M2 連不上，10 分鐘後重試"
  sleep 600
done
log "M2 連得上，開始同步"

if ! scp -q "$REPO/rewrite_terms.json" m2:orca/voice-input-mac/rewrite_terms.json; then
  log "失敗：複製 rewrite_terms.json"; exit 1
fi

ssh m2 'bash -s' <<'REMOTE' >> "$LOG" 2>&1
set -eo pipefail
cd "$HOME/orca/voice-input-mac"
git fetch -q origin main
git merge --ff-only -q origin/main
echo "[m2] HEAD $(git log --oneline -1)"
VOICE_ASR_BACKEND_FILE=/tmp/no-such-asr-backend ./.venv/bin/python -m unittest discover -s . -p 'test_*.py' > /tmp/m2-test.out 2>&1
grep -E '^(Ran |OK|FAILED)' /tmp/m2-test.out
mkdir -p "$HOME/orca/state/voice-input"
echo qwen06 > "$HOME/orca/state/voice-input/asr-backend"
./.venv/bin/python -c "from huggingface_hub import snapshot_download; snapshot_download('Qwen/Qwen3-ASR-0.6B')"
(cd app && ./install.sh --no-open)
echo "[m2] done"
REMOTE
rc=$?
if [ $rc -eq 0 ]; then log "完成：M2 已更新到 origin/main、預設 qwen06"; else log "失敗：M2 遠端步驟 exit=$rc（見上方輸出）"; fi
exit $rc
