#!/usr/bin/env python3
"""本機語音輸入的健康狀態，以及「壞掉就自己開一張單」。

這支是 issue #1 的試點：先做出**可判定**的訊號，再把 daily-qa 那套
（去重鍵／五段 body／編號回寫）接上去。

    ~/orca/state/voice-input/health.json   機器可讀的健康狀態（每次跑都覆寫）
    ~/orca/state/voice-input/checkpoint.json  log 讀到哪裡（讓「上次檢查之後」有定義）
    ~/orca/state/voice-input/emitted_issues.json  開過的單（key -> 編號）

全程唯讀巡檢：讀 `launchctl list`、`pgrep`、熱鍵 agent 的 `.err.log`、以及**實際在跑的那個
checkout** 的 `local.env`。不重啟、不重裝 agent，不動 `local.env`，不上傳任何東西。

用法：

    python3 healthcheck.py                      # 產 health.json，印 HEALTH=OK/DEGRADED
    python3 healthcheck.py --emit               # 再跑 emitter（預設 dry-run，只印 WOULD_CREATE）
    python3 healthcheck.py --emit --apply       # 真的開單，編號寫回 emitted_issues.json
    python3 healthcheck.py --emit --from-health /tmp/health.json --apply
                                                # 重放：用一份改過的副本判定（不碰系統）

為什麼 `stt_backend` 要讀「實際在跑的那個 checkout」而不是本檔旁邊：App 是 GUI 啟動的，
`transcribe.py` 路徑寫在 `app/Sources/Transcriber.swift`（預設 `~/orca/voice-input-mac`），
`VOICE_STT` 在**那份** `local.env` 裡。本檔可能在另一個 clone（例如 nested checkout）裡跑，
讀自己旁邊會得到預設值 breeze，跟使用者實際在用的後端不同。同理，這裡**不讀 shell 的
環境變數**：launchd／Finder 啟動的 App 看不到你 shell 裡 export 的東西，讀了只會誤導。
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import issue_emitter
from issue_emitter import Finding

AGENT_LABEL = "local.hunghanchu.voice-toggle-hotkey"
APP_PATTERN = "VoiceInputMac.app/Contents/MacOS/VoiceInputMac"
HOME = Path.home()
STATE_DIR = HOME / "orca" / "state" / "voice-input"
HOTKEY_ERR_LOG = HOME / "orca" / "state" / "voice-toggle-hotkey.err.log"
# app/Sources/Transcriber.swift 的預設；`VOICE_INPUT_TRANSCRIBE_PY` 可以覆寫它。
DEFAULT_TRANSCRIBE = HOME / "orca" / "voice-input-mac" / "transcribe.py"
STT_BACKENDS = ("breeze", "soniox")
FORBIDDEN = """\
不改 Hermes 的 config／skills、不動 `~/.hermes`。
不重啟或重裝 launchd agent（`install.sh`、`com.hunghanchu.*.plist`）——修好之後由使用者決定要不要重啟。
不改模型來源與 Vertex 專案設定；不動 `local.env`（含憑證）。
不把音檔內容、轉錄全文、個人用語表寫進 repo 或 issue。"""


# ── 唯讀探測（每個都能單獨餵字串測，不必真的跑 launchctl）─────────────────────
def parse_launchctl(output: str, label: str = AGENT_LABEL) -> tuple[bool, int | None, int | None]:
    """`launchctl list` 的三欄格式：PID、last exit、label。回傳 (loaded, pid, last_exit)。

    PID 欄是 `-`＝agent 已載入但現在沒在跑（on-demand job 正常）；last exit 欄是 `-`＝從沒跑過。
    """

    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2] == label:
            pid = int(parts[0]) if parts[0].isdigit() else None
            last_exit = int(parts[1]) if parts[1].isdigit() else None
            return True, pid, last_exit
    return False, None, None


def parse_pgrep(output: str) -> list[int]:
    """`pgrep -fl` 的輸出：`PID command…`。只留 PID。"""

    pids: list[int] = []
    for line in output.splitlines():
        parts = line.strip().split(None, 1)
        if parts and parts[0].isdigit():
            pids.append(int(parts[0]))
    return pids


def _launchctl_list() -> str:
    proc = subprocess.run(["launchctl", "list"], capture_output=True, text=True, check=False)
    return proc.stdout or ""


def _pgrep_app() -> str:
    proc = subprocess.run(["pgrep", "-fl", APP_PATTERN], capture_output=True, text=True, check=False)
    return proc.stdout or ""  # 沒有 process 時 rc=1、stdout 空，不算錯誤


# ── 設定與路徑 ────────────────────────────────────────────────────────────────
def read_local_env(path: Path) -> dict[str, str]:
    """跟 transcribe.py 的 `_local_env()` 同一套解析（`#` 後是註解、去掉引號）。"""

    cfg: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return cfg
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if "=" in line:
            key, value = line.split("=", 1)
            cfg[key.strip()] = value.strip().strip('"').strip("'")
    return cfg


def runtime_dir(explicit: Path | None = None) -> Path:
    """使用者實際在跑的那個 checkout（`local.env` 與 `transcribe.py` 的家）。

    順序：--runtime → `VOICE_INPUT_TRANSCRIBE_PY` 的資料夾 → `~/orca/voice-input-mac`
    → 本檔所在目錄（最後手段，e.g. 全新 clone）。
    """

    if explicit:
        return Path(explicit).expanduser()
    override = os.environ.get("VOICE_INPUT_TRANSCRIBE_PY", "").strip()
    if override:
        return Path(override).expanduser().parent
    if DEFAULT_TRANSCRIBE.exists():
        return DEFAULT_TRANSCRIBE.parent
    return Path(__file__).resolve().parent


def stt_backend(runtime: Path) -> tuple[str, str]:
    """回傳 (backend, 來源說明)。值不是 breeze／soniox 就是 `unknown`。"""

    env_path = runtime / "local.env"
    raw = read_local_env(env_path).get("VOICE_STT", "").strip().lower()
    if not raw:
        return "breeze", "default（local.env 沒有 VOICE_STT）"
    if raw in STT_BACKENDS:
        return raw, f"local.env:{env_path}"
    return "unknown", f"local.env:{env_path}（VOICE_STT={raw!r}）"


# ── log checkpoint：讓「上次檢查之後」有定義 ───────────────────────────────────
def load_checkpoint(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, json.JSONDecodeError):
        return {"logs": {}}
    if not isinstance(data, dict):
        return {"logs": {}}
    data.setdefault("logs", {})
    return data


def save_checkpoint(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".checkpoint-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


def count_new_lines(path: Path, offset: int) -> tuple[int, int]:
    """從 `offset` 讀到檔尾，回傳 (新增的完整行數, 新的 offset)。

    - 檔案不存在：`(0, 0)`。
    - 檔案變小（被 truncate／輪替）：從 0 重讀，不要假設前面的位元組還在。
    - 尾巴沒有換行的殘行不計數、也不前進 offset：那行還沒寫完，下一次再算，
      免得同一行被算兩次。
    """

    try:
        size = path.stat().st_size
    except OSError:
        return 0, 0
    if size < offset:
        offset = 0
    try:
        with path.open("rb") as fh:
            fh.seek(offset)
            chunk = fh.read()
    except OSError:
        return 0, offset
    cut = chunk.rfind(b"\n")
    if cut < 0:
        return 0, offset
    complete = chunk[: cut + 1]
    return complete.count(b"\n"), offset + len(complete)


# ── 觀測與判定 ────────────────────────────────────────────────────────────────
def machine_name() -> str:
    return socket.gethostname().split(".")[0]


def build_record(
    *,
    machine: str,
    checked_at: str,
    agent_loaded: bool,
    agent_pid: int | None,
    last_exit: int | None,
    app_running: bool,
    app_pids: list[int],
    err_lines: int,
    stt_backend: str,
    stt_source: str,
    runtime: Path,
    sources: dict[str, str],
    errors: list[str] | None = None,
) -> dict:
    """純函式：把探測結果組成 health.json 的內容（含 ok／reasons）。"""

    record = {
        "machine": machine,
        "checked_at": checked_at,
        "agent_loaded": bool(agent_loaded),
        "agent_pid": agent_pid,
        "last_exit": last_exit,
        "app_running": bool(app_running),
        "app_pids": list(app_pids),
        "err_lines_since_last_check": int(err_lines),
        "stt_backend": stt_backend,
        "stt_source": stt_source,
        "runtime_dir": str(runtime),
        "sources": sources,
        "errors": list(errors or []),
    }
    reasons = unhealthy_reasons(record)
    record["ok"] = not reasons
    record["reasons"] = reasons
    return record


def unhealthy_reasons(record: dict) -> list[str]:
    """哪些欄位不合格（人類可讀，順序固定）。空 list＝健康。"""

    reasons: list[str] = []
    if not record.get("agent_loaded"):
        reasons.append("agent_loaded=false（熱鍵 agent 不在 launchctl 清單裡）")
    last_exit = record.get("last_exit")
    if last_exit not in (0, None):
        reasons.append(f"last_exit={last_exit}（上次不是正常結束）")
    if not record.get("app_running"):
        reasons.append("app_running=false（⌘E 沒有 App 可接）")
    err_lines = int(record.get("err_lines_since_last_check") or 0)
    if err_lines > 0:
        reasons.append(f"err_lines_since_last_check={err_lines}（錯誤 log 有新增行）")
    if record.get("stt_backend") == "unknown":
        reasons.append("stt_backend=unknown（VOICE_STT 不是 breeze／soniox）")
    return reasons


def collect(
    *,
    runtime: Path,
    state_dir: Path,
    err_log: Path = HOTKEY_ERR_LOG,
    now: datetime | None = None,
    launchctl_list=None,
    pgrep_app=None,
    machine: str | None = None,
) -> dict:
    """真的去讀系統，回傳 record（同時把 log checkpoint 往前推）。

    `launchctl_list`／`pgrep_app` 可以注入，測試就不必依賴這台機器剛好有什麼在跑。
    """

    launchctl_list = launchctl_list or _launchctl_list
    pgrep_app = pgrep_app or _pgrep_app
    errors: list[str] = []

    try:
        agent_loaded, agent_pid, last_exit = parse_launchctl(launchctl_list())
    except Exception as exc:  # 探測失敗也要產出檔案，不能讓健康檢查自己消失
        agent_loaded, agent_pid, last_exit = False, None, None
        errors.append(f"launchctl list 失敗：{exc}")

    try:
        app_pids = parse_pgrep(pgrep_app())
    except Exception as exc:
        app_pids = []
        errors.append(f"pgrep 失敗：{exc}")

    checkpoint_path = state_dir / "checkpoint.json"
    checkpoint = load_checkpoint(checkpoint_path)
    entry = checkpoint["logs"].get(err_log.name) or {}
    first_run = "offset" not in entry
    err_lines, new_offset = count_new_lines(err_log, int(entry.get("offset") or 0))
    if first_run:
        # 第一次跑只建立基準，不要把歷史 log 當成「這次新產生的錯誤」。
        err_lines = 0
    checkpoint["logs"][err_log.name] = {"offset": new_offset, "path": str(err_log)}

    backend, backend_source = stt_backend(runtime)
    health_path = state_dir / "health.json"
    stamp = (now or datetime.now().astimezone()).isoformat(timespec="seconds")
    checkpoint["updated_at"] = stamp

    record = build_record(
        machine=machine or machine_name(),
        checked_at=stamp,
        agent_loaded=agent_loaded,
        agent_pid=agent_pid,
        last_exit=last_exit,
        app_running=bool(app_pids),
        app_pids=app_pids,
        err_lines=err_lines,
        stt_backend=backend,
        stt_source=backend_source,
        runtime=runtime,
        sources={"health": str(health_path), "err_log": str(err_log), "agent_label": AGENT_LABEL},
        errors=errors,
    )
    save_checkpoint(checkpoint_path, checkpoint)
    return record


def write_health(record: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".health-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(record, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


# ── 什麼算 finding（這個 repo 唯一的供應點）─────────────────────────────────────
def collect_findings(record: dict) -> list[Finding]:
    """純函式：health record → 該開的單。健康時回空 list（emitter 就會印 NONE）。"""

    machine = str(record.get("machine") or "unknown")
    checked_at = str(record.get("checked_at") or "")
    sources = record.get("sources") or {}
    health_path = sources.get("health") or str(STATE_DIR / "health.json")
    err_log = sources.get("err_log") or str(HOTKEY_ERR_LOG)
    backend = str(record.get("stt_backend") or "unknown")
    observations = (
        f"- machine=`{machine}`",
        f"- checked_at=`{checked_at}`",
        f"- stt_backend=`{backend}`",
    )
    findings: list[Finding] = []

    def base(field: str, value: str, **kwargs) -> Finding:
        return Finding(
            field=field,
            value=value,
            key_salt=machine,  # M4 和 M2 同一天壞同一件事，是兩張單
            forbidden=FORBIDDEN,
            source="healthcheck.py",
            **kwargs,
        )

    if not record.get("app_running"):
        findings.append(
            base(
                "app_running",
                "false",
                title="⌘E 按下去沒有 App 可以接（voice-input App 不在執行）",
                problem=(
                    f"{machine} 上找不到 `{APP_PATTERN}` 的 process，"
                    "但熱鍵 agent 是載入著的"
                    f"（agent_loaded={str(record.get('agent_loaded')).lower()}）。"
                    "也就是說：熱鍵出現、使用者按下去，卻沒有東西接——表現完全像「壞了」，"
                    "而且不會有任何 log 留下來。"
                ),
                evidence=(
                    f"health.json：`{health_path}`（`app_running=false`）",
                    f"runtime checkout：`{record.get('runtime_dir')}`",
                )
                + observations,
                reproduce=(
                    f'pgrep -fl "{APP_PATTERN}"; echo "rc=$?（1 = 沒有 process）"\n'
                    f'launchctl list | grep -i voice\n'
                    f'cat "{health_path}"'
                ),
                done=(
                    "同一條 `pgrep -fl` 有 PID，且 `python3 healthcheck.py` 印出 `HEALTH=OK`、"
                    "health.json 的 `app_running=true` 且 `ok=true`。"
                ),
                observation="實機開一次 App 之後重跑 `healthcheck.py`（不必等下一次自然故障）。",
                labels=("src:health", "kind:bug"),
            )
        )

    if not record.get("agent_loaded"):
        findings.append(
            base(
                "agent_loaded",
                "false",
                title="⌘E 的熱鍵 agent 沒有載入",
                problem=(
                    f"`launchctl list` 裡沒有 `{AGENT_LABEL}`。沒有這個 agent，⌘E 根本不會被送出，"
                    "使用者按了只會以為是自己按錯。"
                ),
                evidence=(f"health.json：`{health_path}`（`agent_loaded=false`）",) + observations,
                reproduce=(
                    f"launchctl list | grep -i voice-toggle-hotkey; echo \"rc=$?\"\n"
                    f'plutil -p ~/Library/LaunchAgents/{AGENT_LABEL}.plist'
                ),
                done=(
                    f"`launchctl list | grep -i voice-toggle-hotkey` 有 `{AGENT_LABEL}` 且 last exit 為 `0`；"
                    "`python3 healthcheck.py` 印出 `HEALTH=OK`（或只剩與本單無關的 reason）。"
                ),
                observation="由使用者決定何時重新載入 agent，載入後重跑 `healthcheck.py`。",
                labels=("src:health", "kind:bug"),
            )
        )

    last_exit = record.get("last_exit")
    if last_exit not in (0, None):
        findings.append(
            base(
                "last_exit",
                str(last_exit),
                title=f"熱鍵 agent 上一次以 exit {last_exit} 結束",
                problem=(
                    f"`{AGENT_LABEL}` 的 last exit 是 `{last_exit}`。plist 是 `KeepAlive=true`，"
                    "所以 launchd 會一直重拉——使用者感受到的是「熱鍵有時候沒反應」，"
                    "而每次崩在 `.err.log` 裡的理由可能都不一樣。"
                ),
                evidence=(
                    f"health.json：`{health_path}`（`last_exit={last_exit}`）",
                    f"錯誤 log：`{err_log}`",
                )
                + observations,
                reproduce=(
                    f"launchctl list | grep -i voice-toggle-hotkey\n"
                    f'tail -20 "{err_log}"'
                ),
                done=(
                    "agent 連續跑一段時間後 `launchctl list` 的第二欄是 `0`，"
                    "且 `healthcheck.py` 印出 `HEALTH=OK`（`last_exit` 不再是非零值）。"
                ),
                observation="重跑 `healthcheck.py` 看 `last_exit`（不必等下一次自然故障）。",
                labels=("src:health", "kind:bug"),
            )
        )

    err_lines = int(record.get("err_lines_since_last_check") or 0)
    if err_lines > 0:
        findings.append(
            base(
                "err_lines_since_last_check",
                str(err_lines),
                title=f"熱鍵 agent 的錯誤 log 新增了 {err_lines} 行",
                problem=(
                    f"上次檢查之後 `{err_log}` 多了 {err_lines} 行。錯誤 log 平常是空的"
                    "（agent 正常跑時不會寫東西），所以任何新增行都值得看一眼；"
                    "這條的 key 帶行數，行數再長會再開一張。"
                ),
                evidence=(
                    f"health.json：`{health_path}`（`err_lines_since_last_check={err_lines}`）",
                    f"錯誤 log：`{err_log}`",
                )
                + observations,
                reproduce=f'tail -20 "{err_log}"',
                done=(
                    "log 的新增行被判定成因並修掉，`tail` 沒有新的 error；"
                    "下次檢查 `healthcheck.py` 印 `HEALTH=OK`（`err_lines_since_last_check=0`）。"
                ),
                observation="跑一次 `healthcheck.py`（checkpoint 已前進，只會算新行）。",
                labels=("src:health", "kind:eng"),
            )
        )

    if backend == "unknown":
        raw = str(record.get("stt_source") or "")
        findings.append(
            base(
                "stt_backend",
                raw or "unknown",
                title="VOICE_STT 的值不是 breeze／soniox",
                problem=(
                    f"`{record.get('runtime_dir')}/local.env` 的 `VOICE_STT` 解析成 `unknown`"
                    f"（來源：{raw}）。這個值不符合任何一條 STT 路線，客戶端行為無法預期："
                    "可能是默默退回預設、也可能是每次錄音都失敗。"
                ),
                evidence=(
                    f"health.json：`{health_path}`（`stt_backend=unknown`、`stt_source={raw}`）",
                )
                + observations,
                reproduce=(
                    f'grep -n VOICE_STT "{record.get("runtime_dir")}/local.env"\n'
                    f"python3 healthcheck.py"
                ),
                done=(
                    "`VOICE_STT` 是 `breeze` 或 `soniox`，`healthcheck.py` 印出 "
                    "`HEALTH=OK`（`stt_backend` 不再是 `unknown`）。"
                ),
                observation="改完 `local.env` 後重跑 `healthcheck.py`（不需要重啟）。",
                labels=("src:health", "kind:eng", "needs-user"),
            )
        )

    return findings


# ── CLI ──────────────────────────────────────────────────────────────────────
def _print_summary(record: dict, health_path: Path, out=print) -> None:
    state = "OK" if record.get("ok") else "DEGRADED"
    out(f"HEALTH={state} machine={record.get('machine')} path={health_path}")
    out(
        "  agent_loaded={} pid={} last_exit={}".format(
            str(record.get("agent_loaded")).lower(), record.get("agent_pid"), record.get("last_exit")
        )
    )
    out(
        "  app_running={} pids={}".format(
            str(record.get("app_running")).lower(), ",".join(str(p) for p in record.get("app_pids") or [])
        )
    )
    out(
        "  err_lines_since_last_check={} stt_backend={}（{}）".format(
            record.get("err_lines_since_last_check"),
            record.get("stt_backend"),
            record.get("stt_source"),
        )
    )
    for reason in record.get("reasons") or []:
        out(f"  reason: {reason}")
    for err in record.get("errors") or []:
        out(f"  probe-error: {err}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="語音輸入健康狀態（+ 壞掉時自己開單）")
    parser.add_argument("--state-dir", default=str(STATE_DIR), help="health.json 放哪（預設 ~/orca/state/voice-input）")
    parser.add_argument("--runtime", default=None, help="實際在跑的 checkout（預設 ~/orca/voice-input-mac）")
    parser.add_argument("--no-write", action="store_true", help="只印，不寫 health.json／checkpoint（除錯用）")
    parser.add_argument("--emit", action="store_true", help="跑 emitter（預設 dry-run）")
    parser.add_argument("--apply", action="store_true", help="真的開 GitHub issue")
    parser.add_argument("--from-health", default=None, help="用這份 health.json 判定（重放用；不碰系統）")
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parent), help="gh 在哪個 repo 跑")
    parser.add_argument("--runner", default="gh", help="gh binary（測試可指向替身）")
    parser.add_argument("--record", default=None, help="編號回寫檔（預設 <state-dir>/emitted_issues.json）")
    parser.add_argument("--check-only", action="store_true", help="只做健康檢查，忽略 --emit")
    args = parser.parse_args(argv)

    state_dir = Path(args.state_dir).expanduser()
    health_path = state_dir / "health.json"

    if args.from_health:
        try:
            record = json.loads(Path(args.from_health).expanduser().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"HEALTH=FAIL 讀不到 {args.from_health}：{exc}")
            return 2
    else:
        process_runtime = runtime_dir(Path(args.runtime).expanduser() if args.runtime else None)
        record = collect(runtime=process_runtime, state_dir=state_dir)
        if not args.no_write:
            write_health(record, health_path)
        _print_summary(record, health_path)

    if not args.emit or args.check_only:
        return 0 if record.get("ok") else 1

    scope = str(record.get("checked_at") or "")[:10] or datetime.now().date().isoformat()
    record_path = Path(args.record).expanduser() if args.record else state_dir / "emitted_issues.json"
    return issue_emitter.emit_all(
        collect_findings(record),
        scope=scope,
        repo=Path(args.repo),
        runner=args.runner,
        apply=args.apply,
        record_path=record_path,
        checked_at=str(record.get("checked_at") or ""),
    )


if __name__ == "__main__":
    sys.exit(main())
