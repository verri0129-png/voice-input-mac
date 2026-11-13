#!/usr/bin/env python3
"""本機語音輸入的健康狀態，以及「壞掉就自己開一張單」。

這支是 issue #1 的試點：先做出**可判定**的訊號，再把 daily-qa 那套
（去重鍵／五段 body／編號回寫）接上去。

    ~/orca/state/voice-input/health.json   機器可讀的健康狀態（每次跑都覆寫）
    ~/orca/state/voice-input/emitted_issues.json  開過的單（key -> 編號）

**檢查掛在「使用」上，不掛在時鐘上。** 這套輸入沒有固定排程，`App` 是「要用才開」，
所以「App 不在執行」是靜止狀態、不是故障。真正的問題是「你按了 ⌘E 卻沒拿到字」，
那個答案在 `.run.log` 裡：App 啟動時跑一次 `healthcheck.py --emit --apply`，每次轉寫
結束（不管成功或失敗）再跑一次。平常一個字都不會印，壞掉那天才會有單。

全程唯讀巡檢：讀 `pgrep`、`App` 的 `.run.log`、
以及**實際在跑的那個 checkout** 的 `local.env`。不重啟、不重裝 launchd agent，不動 `local.env`，
不上傳任何東西。

用法：

    python3 healthcheck.py                      # 產 health.json，印 HEALTH=OK/DEGRADED
    python3 healthcheck.py --emit               # 再跑 emitter（預設 dry-run，只印 WOULD_CREATE）
    python3 healthcheck.py --emit --apply       # 真的開單，編號寫回 emitted_issues.json；
                                                # 這台已恢復的 src:health 單會被關掉並留言
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
import re
import socket
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import issue_emitter
from issue_emitter import Finding

APP_PATTERN = "VoiceInputMac.app/Contents/MacOS/VoiceInputMac"
HOME = Path.home()
STATE_DIR = HOME / "orca" / "state" / "voice-input"
# app/Sources/Transcriber.swift 的預設；`VOICE_INPUT_TRANSCRIBE_PY` 可以覆寫它。
DEFAULT_TRANSCRIBE = HOME / "orca" / "voice-input-mac" / "transcribe.py"
STT_BACKENDS = ("breeze", "soniox")
# 「按了卻沒拿到字」幾次才值得開單：一次空白可能是誤按、或根本沒講話，
# 連續三次就代表不是運氣問題（麥克風被佔住、裝置換掉、serve 起不來…）。
NO_TEXT_STREAK = 3
# 一定算壞掉的結果（不是「沒講話」那種）。
HARD_FAIL_OUTCOMES = ("failed", "no_audio")
FORBIDDEN = """\
不動 `~/.config/agent-secrets`（金鑰）。
不重啟或重裝 launchd agent（`install.sh`、`com.hunghanchu.*.plist`）——修好之後由使用者決定要不要重啟。
不改模型來源與 Vertex 專案設定；不動 `local.env`（含憑證）。
不把音檔內容、轉錄全文、個人用語表寫進 repo 或 issue。"""



# ── 唯讀探測（每個都能單獨餵字串測，不必真的跑 pgrep）─────────────────────
def parse_pgrep(output: str) -> list[int]:
    """`pgrep -fl` 的輸出：`PID command…`。只留 PID。"""

    pids: list[int] = []
    for line in output.splitlines():
        parts = line.strip().split(None, 1)
        if parts and parts[0].isdigit():
            pids.append(int(parts[0]))
    return pids


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


# ── .run.log：最近一次「真的用到它」的結果 ────────────────────────────────────
# 每一次錄音在 `.run.log` 裡是一段：`[app] recording start` 開頭，接著一個終端標記。
# 只有終端標記決定這一輪的結果；其餘（context、insert、learn…）都是過程。
_RESULT_RE = re.compile(r"\[app\] 結果（停止→文字 ([\d.]+)s）：(.*)$")
_TERMINALS = (
    (re.compile(r"\[app\] transcribe failed: ?(.*)$"), "failed"),
    (re.compile(r"\[app\] recording stop：沒有音訊，放棄"), "no_audio"),
    (re.compile(r"\[app\] exit 3："), "no_speech"),
    (re.compile(r"\[app\] exit 4："), "suspect"),
    (re.compile(r"\[app\] 結果為空，不貼上"), "empty"),
)
_TS_RE = re.compile(r"^\[([0-9T:.+\-Z]+)\]")
DETAIL_CHARS = 120


def _line_time(line: str) -> str:
    m = _TS_RE.match(line.strip())
    return m.group(1) if m else ""


def classify_line(line: str) -> dict | None:
    """一行 log → 這一輪的結果（不是終端標記就回 None）。

    注意：`detail` 只放錯誤訊息，**不放轉錄出來的文字**（那是不能進 issue 的東西）。
    """

    m = _RESULT_RE.search(line)
    if m:
        text = m.group(2).strip()
        if not text:
            return {"outcome": "empty"}
        return {"outcome": "ok", "duration_s": float(m.group(1)), "chars": len(text)}
    for pattern, outcome in _TERMINALS:
        m = pattern.search(line)
        if m:
            info: dict = {"outcome": outcome}
            detail = (m.group(1).strip() if m.groups() else "")[:DETAIL_CHARS]
            if detail:
                info["detail"] = detail
            return info
    return None


def parse_attempts(lines) -> list[dict]:
    """把 log 切成一次一次的錄音，只留有結果的那些。"""

    attempts: list[dict] = []
    current: dict | None = None
    for line in lines:
        if "[app] recording start" in line:
            current = {"at": _line_time(line), "outcome": None}
            attempts.append(current)
            continue
        if current is None:
            continue
        info = classify_line(line)
        if info:
            current.update(info)
            if not current.get("at"):
                current["at"] = _line_time(line)
    return [a for a in attempts if a.get("outcome")]


def read_last_attempt(run_log: Path, tail_bytes: int = 200_000) -> tuple[dict | None, int]:
    """回傳 (最近一次的結果, 從最近一次成功之後連續沒拿到字的次數)。

    只讀 log 尾巴（這支 log 會長到 MB 級），抓最近那幾輪就夠判斷。
    """

    try:
        size = run_log.stat().st_size
        with run_log.open("rb") as fh:
            if size > tail_bytes:
                fh.seek(size - tail_bytes)
            chunk = fh.read()
    except OSError:
        return None, 0
    attempts = parse_attempts(chunk.decode("utf-8", errors="replace").splitlines())
    if not attempts:
        return None, 0
    streak = 0
    for attempt in reversed(attempts):
        if attempt["outcome"] == "ok":
            break
        streak += 1
    return attempts[-1], streak


# ── 觀測與判定 ────────────────────────────────────────────────────────────────
def machine_line(machine: str) -> str:
    """issue body 證據段裡的那一行。開單寫它、關單認它（只關同一台開的單）。"""

    return f"- machine=`{machine}`"


def machine_name() -> str:
    return socket.gethostname().split(".")[0]


def build_record(
    *,
    machine: str,
    checked_at: str,
    app_running: bool,
    app_pids: list[int],
    stt_backend: str,
    stt_source: str,
    runtime: Path,
    sources: dict[str, str],
    last_attempt: dict | None = None,
    consecutive_no_text: int = 0,
    errors: list[str] | None = None,
) -> dict:
    """純函式：把探測結果組成 health.json 的內容（含 ok／reasons）。

    `app_running` 只當觀察值：這套輸入是「要用才開」，App 關著是靜止狀態，不是故障。
    """

    record = {
        "machine": machine,
        "checked_at": checked_at,
        "app_running": bool(app_running),
        "app_pids": list(app_pids),
        "last_attempt": last_attempt,
        "consecutive_no_text": int(consecutive_no_text),
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
    """哪些欄位不合格（人類可讀，順序固定）。空 list＝健康。

    判準全是「用起來會不會出事」，不是「App 有沒有開著」：這套輸入沒有固定排程，
    App 關著是靜止狀態。
    """

    reasons: list[str] = []
    attempt = record.get("last_attempt") or {}
    outcome = attempt.get("outcome")
    streak = int(record.get("consecutive_no_text") or 0)
    if outcome in HARD_FAIL_OUTCOMES:
        reasons.append(f"last_attempt={outcome}（上一次真的沒轉出東西）")
    elif streak >= NO_TEXT_STREAK:
        reasons.append(f"consecutive_no_text={streak}（連續 {streak} 次沒拿到字）")
    if record.get("stt_backend") == "unknown":
        reasons.append("stt_backend=unknown（VOICE_STT 不是 breeze／soniox）")
    return reasons


def collect(
    *,
    runtime: Path,
    state_dir: Path,
    now: datetime | None = None,
    pgrep_app=None,
    machine: str | None = None,
) -> dict:
    """真的去讀系統，回傳 record。

    `pgrep_app` 可以注入，測試就不必依賴這台機器剛好有什麼在跑。
    """

    pgrep_app = pgrep_app or _pgrep_app
    errors: list[str] = []

    try:
        app_pids = parse_pgrep(pgrep_app())
    except Exception as exc:
        app_pids = []
        errors.append(f"pgrep 失敗：{exc}")

    backend, backend_source = stt_backend(runtime)
    run_log = runtime / ".run.log"
    attempt, streak = read_last_attempt(run_log)
    health_path = state_dir / "health.json"
    stamp = (now or datetime.now().astimezone()).isoformat(timespec="seconds")

    record = build_record(
        machine=machine or machine_name(),
        checked_at=stamp,
        app_running=bool(app_pids),
        app_pids=app_pids,
        stt_backend=backend,
        stt_source=backend_source,
        runtime=runtime,
        last_attempt=attempt,
        consecutive_no_text=streak,
        sources={
            "health": str(health_path),
            "run_log": str(run_log),
        },
        errors=errors,
    )
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
    """純函式：health record → 該開的單。健康時回空 list（emitter 就會印 NONE）。

    **不檢查 `app_running`。** 這套輸入是「要用才開」，使用者關掉 App 是正常靜止狀態；
    把它當故障會在你每次關掉之後開一張單。訊號改看「上一次用它有沒有拿到字」。
    """

    machine = str(record.get("machine") or "unknown")
    checked_at = str(record.get("checked_at") or "")
    sources = record.get("sources") or {}
    health_path = sources.get("health") or str(STATE_DIR / "health.json")
    run_log = sources.get("run_log") or str(Path(str(record.get("runtime_dir") or DEFAULT_TRANSCRIBE.parent)) / ".run.log")
    backend = str(record.get("stt_backend") or "unknown")
    observations = (
        machine_line(machine),
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

    attempt = record.get("last_attempt") or {}
    outcome = str(attempt.get("outcome") or "")
    streak = int(record.get("consecutive_no_text") or 0)
    attempt_line = (
        f"- `.run.log` 最後一輪：outcome=`{outcome}`"
        + (f"、detail=`{attempt.get('detail')}`" if attempt.get("detail") else "")
        + (f"、chars={attempt.get('chars')}" if attempt.get("chars") else "")
        + (f"、at=`{attempt.get('at')}`" if attempt.get("at") else "")
    )

    if outcome in HARD_FAIL_OUTCOMES:
        detail = str(attempt.get("detail") or "（沒有錯誤訊息）")
        findings.append(
            base(
                "last_attempt",
                outcome,
                title=(
                    "上一次按 ⌘E 完全沒轉出東西（"
                    + ("沒有音訊" if outcome == "no_audio" else "轉寫失敗")
                    + "）"
                ),
                problem=(
                    f"{machine} 上最近一次實際使用沒有拿到文字，結果是 `{outcome}`：{detail}。"
                    "這不是「沒講話」那種空白，是流程真的斷掉（音訊沒進來，或轉寫程式直接失敗）。"
                    f"使用者看到的只有「按了沒反應」，而 `.run.log` 裡的理由埋在幾百行後面。"
                ),
                evidence=(
                    f"health.json：`{health_path}`（`last_attempt.outcome={outcome}`）",
                    attempt_line,
                    f"App log：`{run_log}`",
                )
                + observations,
                reproduce=(
                    f"grep -n 'transcribe failed\\|沒有音訊，放棄' \"{run_log}\" | tail -5\n"
                    f'tail -40 "{run_log}"\n'
                    f"cat \"{health_path}\""
                ),
                done=(
                    "下一次真的用一次（按 ⌘E 講一句話）後，`.run.log` 有 "
                    "`[app] 結果（停止→文字 …）`，且 `python3 healthcheck.py` 印出 `HEALTH=OK`、"
                    "`last_attempt.outcome=ok`。"
                ),
                observation="下一次實機使用後重跑 `healthcheck.py`（App 每次轉寫結束也會自己跑一次）。",
                labels=("src:health", "kind:bug"),
            )
        )
    elif streak >= NO_TEXT_STREAK:
        findings.append(
            base(
                "consecutive_no_text",
                str(streak),
                title=f"連續 {streak} 次按 ⌘E 都沒拿到文字",
                problem=(
                    f"{machine} 上連續 {streak} 次使用都沒有拿到文字（最近一次是 `{outcome}`）。"
                    "單次空白可能只是沒講話或誤按，連續這麼多次通常是真的收不到聲音："
                    "麥克風被別的 App 佔住、預設輸入裝置被換掉、或暖機的 `serve` 起不來。"
                    f"每一輪在 `.run.log` 裡都有各自的理由，但沒人會去翻。"
                ),
                evidence=(
                    f"health.json：`{health_path}`（`consecutive_no_text={streak}`）",
                    attempt_line,
                    f"App log：`{run_log}`",
                )
                + observations,
                reproduce=(
                    f"grep -n '\\[app\\] exit 3\\|結果為空\\|transcribe failed' \"{run_log}\" | tail -5\n"
                    f'launchctl list | grep -i voice\n'
                    f"system_profiler SPAudioDataType | sed -n '/Input/,+6p' | head -30"
                ),
                done=(
                    "按 ⌘E 連續兩次都拿到文字（`.run.log` 有 `[app] 結果（停止→文字 …）`），"
                    "且 `healthcheck.py` 印出 `HEALTH=OK`、`consecutive_no_text=0`。"
                ),
                observation="下一次實機使用後重跑 `healthcheck.py`（不必等下一次自然故障）。",
                labels=("src:health", "kind:bug"),
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


# ── 恢復就關 ────────────────────────────────────────────────────────────────
def recovered_evidence(record: dict) -> dict[str, str]:
    """純函式：哪些 finding 欄位有「已經恢復」的**正面**證據（欄位 → 一句白話）。

    「這次沒觸發」不等於恢復：例如 `.run.log` 還沒有任何一輪，就沒有證據說
    ⌘E 又能拿到字了，`last_attempt` 的單不能關。
    """

    evidence: dict[str, str] = {}
    attempt = record.get("last_attempt") or {}
    if attempt.get("outcome") == "ok":
        line = f"上一次按 ⌘E（{attempt.get('at') or '時間不明'}）轉出 {attempt.get('chars', '?')} 字"
        evidence["last_attempt"] = line
        evidence["consecutive_no_text"] = line
    if record.get("stt_backend") in STT_BACKENDS:
        evidence["stt_backend"] = f"VOICE_STT 解析為 `{record.get('stt_backend')}`"
    return evidence


def resolve_issues(
    record: dict,
    *,
    repo: Path,
    runner: str = "gh",
    apply: bool = False,
    record_path: Path | None = None,
    out=print,
) -> int:
    """把這台機器開過、而且已經恢復的 src:health 單關掉（別台的單不碰：這台證明不了別台好了）。"""

    machine = str(record.get("machine") or "unknown")
    checked_at = str(record.get("checked_at") or "")

    def comment(field: str, evidence: str) -> str:
        return (
            f"已恢復，自動關閉。{machine} 在 {checked_at} 的健康檢查確認：{evidence}"
            f"（`{field}` 不再是問題）。\n\n"
            "這張單由 `healthcheck.py` 自動關閉；如果又壞了，下一次檢查會重開這張或開新單。"
        )

    return issue_emitter.resolve_open(
        recovered_evidence(record),
        key_prefix="health",
        owner_line=machine_line(machine),  # 只關這台開的單
        label="src:health",
        repo=repo,
        comment=comment,
        runner=runner,
        apply=apply,
        record_path=record_path,
        checked_at=checked_at,
        out=out,
    )


# ── CLI ──────────────────────────────────────────────────────────────────────
def _print_summary(record: dict, health_path: Path, out=print) -> None:
    state = "OK" if record.get("ok") else "DEGRADED"
    out(f"HEALTH={state} machine={record.get('machine')} path={health_path}")
    attempt = record.get("last_attempt") or {}
    out(
        "  last_attempt={} chars={} 連續沒拿到字={}".format(
            attempt.get("outcome") or "（log 裡還沒有任何一輪）",
            attempt.get("chars", "-"),
            record.get("consecutive_no_text"),
        )
    )
    out(
        "  app_running={}（僅供參考：要用才開，關著不是故障） pids={}".format(
            str(record.get("app_running")).lower(), ",".join(str(p) for p in record.get("app_pids") or [])
        )
    )
    out("  stt_backend={}（{}）".format(record.get("stt_backend"), record.get("stt_source")))
    for reason in record.get("reasons") or []:
        out(f"  reason: {reason}")
    for err in record.get("errors") or []:
        out(f"  probe-error: {err}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="語音輸入健康狀態（+ 壞掉時自己開單）")
    parser.add_argument("--state-dir", default=str(STATE_DIR), help="health.json 放哪（預設 ~/orca/state/voice-input）")
    parser.add_argument("--runtime", default=None, help="實際在跑的 checkout（預設 ~/orca/voice-input-mac）")
    parser.add_argument("--no-write", action="store_true", help="只印，不寫 health.json（除錯用）")
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
    emit_rc = issue_emitter.emit_all(
        collect_findings(record),
        scope=scope,
        repo=Path(args.repo),
        runner=args.runner,
        apply=args.apply,
        record_path=record_path,
        checked_at=str(record.get("checked_at") or ""),
    )
    resolve_rc = resolve_issues(
        record, repo=Path(args.repo), runner=args.runner, apply=args.apply, record_path=record_path
    )
    return max(emit_rc, resolve_rc)


if __name__ == "__main__":
    sys.exit(main())
