#!/usr/bin/env python3
"""把 finding 變成去重後的 GitHub issue —— 可共用的 core。

這支刻意不知道任何 repo 的事：**什麼算 finding** 由呼叫端提供（本 repo 是
`healthcheck.py` 的 `collect_findings`），這裡只負責四件事：

1. **key**：`<prefix>/<scope>/<field>/<value-sha8>`，是「觀測值」的函式，
   同一個值重跑不會重開單，值變了才會開第二張。`key_salt` 用來區分
   「同一個值、但發生在不同機器／不同對象」。
2. **marker**：issue body 第一行 `<!-- key: … -->`，去重的唯一依據
   （不靠標題搜尋，所以改標題不會讓同一件事重開單）。
3. **五段 body**：背景／證據／重現命令／完成定義（gate token）／不可動範圍，
   跟 `.github/ISSUE_TEMPLATE/task.yml` 同一套形狀。
4. **去重與編號回寫**：建立前掃既有 issue（含已關閉）的 marker；建立後把
   issue 編號寫回本機 record（JSON），下次連 gh 都不用問就知道開過。
5. **恢復就關**（`resolve_open`）：呼叫端給「哪些欄位有恢復的證據」，這裡把
   同一個 key 前綴、同一個擁有者、欄位已恢復、還開著的單關掉並留言。

daily-qa 的 `emit_issues.py`／`emit_review_issues.py` 是同一個設計的特例；
新 repo 直接 import 這支，不要複製一份再改死。

用法：

    from issue_emitter import Finding, emit_all
    emit_all(findings, scope="2026-11-01", repo=Path("."), apply=False)

判定行（給 shell 與 gate 用的固定台詞）：

    EMIT_ISSUES=NONE           沒有 finding，什麼都不該開
    EMIT_ISSUES=DRY_RUN        只印 WOULD_CREATE，沒有寫入
    EMIT_ISSUES=CREATED        至少開了一張（或全部被跳過）
    EMIT_ISSUES=FAIL           讀 gh／建立失敗（exit 2）
    SKIPPED_EXISTING key=…     GitHub 上已有同一個 key
    SKIPPED_RECORDED key=…     record 裡已有編號（連 gh 都省了）
    RESOLVE_ISSUES=NONE        沒有開著、屬於這個擁有者的單
    RESOLVE_ISSUES=SKIPPED     record 說上次已確認沒有開著的單，連 gh 都不問
    RESOLVE_ISSUES=CLOSED      關了 N 張（或 0 張：都還沒恢復）
    RESOLVE_ISSUES=DRY_RUN     只印 WOULD_CLOSE
    RESOLVE_ISSUES=FAIL        讀 gh／關單失敗（exit 2）

Exit codes：0 = 做完該做的事（含「沒東西要開」）、2 = gh／record 失敗。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

MARKER = "<!-- key: {key} -->"
KEY_LINE_PREFIX = "<!-- key: "
DEFAULT_LIMIT = 300

DEFAULT_FORBIDDEN = """\
不動 `~/.config/agent-secrets`（金鑰）。
不重啟或重裝 launchd agent（`install.sh`、`com.hunghanchu.*.plist`），除非這張單就是要修它。
不改模型來源與 Vertex 專案設定；不動 `local.env`（含憑證）。
不把音檔內容、轉錄全文、個人用語表寫進 repo 或 issue。"""


class EmitterError(RuntimeError):
    """gh 或 record 讀寫失敗——呼叫端應該收成 `EMIT_ISSUES=FAIL`。"""


@dataclass(frozen=True)
class Finding:
    """一件值得開單的事。內容全部由呼叫端提供；這裡只管鍵與排版。"""

    field: str                     # 去重鍵裡的欄位名，例 "app_running"
    value: str                     # 觀測值；sha8 由它（加上 key_salt）決定
    title: str                     # 一句話現象（不含 `[health] ` 這種前綴）
    problem: str                   # 背景段：發生什麼、哪台、為什麼算問題
    done: str                      # 完成定義：哪個可觀測訊號從什麼變成什麼
    reproduce: str = ""            # shell 片段（不含 ``` 圍籬）
    evidence: tuple[str, ...] = ()  # 證據條列（路徑＋行號，不貼全文）
    labels: tuple[str, ...] = ()
    forbidden: str = DEFAULT_FORBIDDEN
    key_prefix: str = "health"
    key_salt: str = ""             # 同一值在不同機器時用來分開鍵
    observation: str = "重跑一次同一條檢查，不是等下一次自然故障。"
    source: str = ""               # 這張單是誰產生的，例 "healthcheck.py"

    def digest(self) -> str:
        raw = f"{self.key_salt}|{self.value}" if self.key_salt else self.value
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]

    def key(self, scope: str) -> str:
        return f"{self.key_prefix}/{scope}/{self.field}/{self.digest()}"

    def marker(self, scope: str) -> str:
        return MARKER.format(key=self.key(scope))


def build_body(finding: Finding, scope: str) -> str:
    """五段 body。第一行一定是 marker——去重只認那一行。"""

    evidence = list(finding.evidence) or ["（呼叫端沒有提供證據條目）"]
    evidence += [
        f"觸發欄位：`{finding.field}` = `{finding.value}`",
        f"scope：`{finding.key_prefix}/{scope}`",
    ]
    # 呼叫端可以自己帶「- 」開頭，不要變成「- - 」。
    bullets = "\n".join(
        line if line.lstrip().startswith("- ") else f"- {line}" for line in evidence
    )
    reproduce = finding.reproduce.strip() or "（呼叫端沒有提供重現命令）"
    source_line = (
        f"（本單由 `{finding.source}` 自動產生，不是人手寫的判斷。）"
        if finding.source
        else ""
    )
    background = finding.problem.strip()
    if source_line:
        background += "\n\n" + source_line
    return f"""{finding.marker(scope)}

## 背景

{background}

## 證據

{bullets}

## 重現命令

```bash
{reproduce}
```

## 完成定義（gate token）

{finding.done.strip()}

觀測來源：{finding.observation}

## 不可動範圍

{finding.forbidden.strip()}
"""


def parse_marker_key(body: str | None) -> str:
    """body 裡第一個 marker 的 key；沒有就回空字串（手寫的單）。"""

    for line in (body or "").splitlines():
        line = line.strip()
        if line.startswith(KEY_LINE_PREFIX) and line.endswith(" -->"):
            return line[len(KEY_LINE_PREFIX): -len(" -->")]
    return ""


def parse_marker_keys(items: Iterable[dict]) -> set[str]:
    """從 `gh issue list --json number,body` 的結果抽出所有 key（含已關閉的單）。"""

    return {key for key in (parse_marker_key(item.get("body")) for item in items) if key}


def resolve_runner(runner: str) -> str:
    """把 `gh` 找出來：GUI／launchd 啟動的行程 PATH 沒有 `/opt/homebrew/bin`。

    找不到就原樣回傳，讓 `_gh` 自己去失敗（錯誤訊息才說明得了是哪一個 binary）。
    """

    if "/" in runner:
        return runner
    for path in (os.environ.get("PATH", ""), "/opt/homebrew/bin:/usr/local/bin"):
        found = shutil.which(runner, path=path)
        if found:
            return found
    return runner


def _gh(runner: str, args: Sequence[str], repo: Path) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            [resolve_runner(runner), *args], cwd=str(repo), capture_output=True, text=True, check=False
        )
    except OSError as exc:  # gh 不存在
        raise EmitterError(f"跑不起 {runner}：{exc}") from exc


def _list_issues(runner: str, repo: Path, extra: Sequence[str], limit: int = DEFAULT_LIMIT) -> list[dict]:
    proc = _gh(
        runner,
        ["issue", "list", *extra, "--limit", str(limit), "--json", "number,body"],
        repo,
    )
    if proc.returncode != 0:
        raise EmitterError(f"gh issue list failed: {proc.stderr.strip() or proc.stdout.strip()}")
    try:
        return json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise EmitterError(f"gh issue list 回傳不是 JSON：{exc}") from exc


def existing_keys(runner: str, repo: Path, limit: int = DEFAULT_LIMIT) -> set[str]:
    """GitHub 上（開著與關著的都算）已有的 key。"""

    return parse_marker_keys(_list_issues(runner, repo, ["--state", "all"], limit))


def create_issue(finding: Finding, scope: str, runner: str, repo: Path) -> str:
    """建一張單，回傳 URL。標題照 `.github/ISSUE_TEMPLATE/task.yml` 的 `[<kind>] …`。"""

    title = f"[{finding.key_prefix}] {finding.title.strip()}"
    args = ["issue", "create", "--title", title, "--body", build_body(finding, scope)]
    if finding.labels:
        args += ["--label", ",".join(finding.labels)]
    proc = _gh(runner, args, repo)
    if proc.returncode != 0:
        raise EmitterError(f"gh issue create failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""


def _issue_number(url: str) -> str:
    tail = url.rstrip("/").rsplit("/", 1)[-1] if url else ""
    return tail if tail.isdigit() else ""


def load_record(path: Path) -> dict:
    """讀編號回寫檔；不存在就回空殼，壞掉就報錯（不要安靜覆蓋掉別人的紀錄）。"""

    if not path.exists():
        return {"issues": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, json.JSONDecodeError) as exc:
        raise EmitterError(f"record 讀不到（{path}）：{exc}") from exc
    if not isinstance(data, dict):
        raise EmitterError(f"record 形狀不對（{path}）：{type(data).__name__}")
    data.setdefault("issues", {})
    return data


def record_back(path: Path, key: str, number: str, url: str = "", when: str = "") -> None:
    """把 issue 編號寫回 record（原子寫、mode 600）。"""

    data = load_record(path)
    data["issues"][key] = {"number": number, "url": url, "at": when}
    _write_record(path, data)


def _write_record(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".record-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


def emit_all(
    findings: Sequence[Finding],
    *,
    scope: str,
    repo: Path,
    runner: str = "gh",
    apply: bool = False,
    record_path: Path | None = None,
    checked_at: str = "",
    out=print,
) -> int:
    """把 findings 開成單（`apply=False` 只印會開什麼）。回傳 exit code。"""

    if not findings:
        out(f"EMIT_ISSUES=NONE scope={scope} (no finding)")
        return 0

    # 先看本機 record：它比 gh 便宜（這支會被 App 每次用完就跑一次，不要每次都打網路），
    # 而且 gh 掛掉時仍能避免重開同一張單。
    recorded: dict[str, dict] = {}
    if record_path is not None:
        try:
            recorded = load_record(record_path)["issues"]
        except EmitterError as exc:
            out(f"EMIT_ISSUES=FAIL {exc}")
            return 2

    candidates: list[Finding] = []
    skipped = 0
    for finding in findings:
        key = finding.key(scope)
        entry = recorded.get(key)
        if entry and entry.get("resolved_at") and entry.get("number"):
            # `resolve_open` 自動關過、同一天同一個值又壞了 → 重開那一張，不能吞掉
            if not apply:
                out(f"WOULD_REOPEN #{entry['number']} key={key}")
                skipped += 1
                continue
            try:
                reopen_issue(int(entry["number"]), f"又壞了：{checked_at} 的檢查再次觀測到同一個問題，重新打開。", runner, repo)
            except EmitterError as exc:
                out(f"EMIT_ISSUES=FAIL {exc}")
                return 2
            record_back(record_path, key, str(entry["number"]), url=entry.get("url", ""), when=checked_at)
            out(f"REOPENED #{entry['number']} key={key}")
            skipped += 1
            continue
        if entry:
            out(f"SKIPPED_RECORDED key={key} -> #{entry.get('number', '?')}")
            skipped += 1
            continue
        candidates.append(finding)

    created = 0
    existing: set[str] = set()
    if candidates:
        try:
            existing = existing_keys(runner, repo)
        except EmitterError as exc:
            if apply:
                out(f"EMIT_ISSUES=FAIL {exc}")
                return 2
            out(f"WARN existing keys unavailable（dry-run 照跑）：{exc}")

    for finding in candidates:
        key = finding.key(scope)
        if key in existing:
            out(f"SKIPPED_EXISTING key={key}")
            skipped += 1
            continue
        if not apply:
            out(f"WOULD_CREATE title={f'[{finding.key_prefix}] {finding.title.strip()}'!r} key={key}")
            created += 1
            existing.add(key)  # 同一批裡重複的 finding 只印一次
            continue
        try:
            url = create_issue(finding, scope, runner, repo)
        except EmitterError as exc:
            out(f"EMIT_ISSUES=FAIL {exc}")
            return 2
        out(f"CREATED {url}")
        created += 1
        existing.add(key)
        number = _issue_number(url)
        if record_path is not None and number:
            record_back(record_path, key, number, url=url, when=checked_at)
            out(f"RECORDED_BACK {record_path} {key}=#{number}")

    out(f"EMIT_ISSUES={'CREATED' if apply else 'DRY_RUN'} scope={scope} new={created} skipped={skipped}")
    return 0


def parse_key(key: str) -> tuple[str, str, str, str]:
    """`Finding.key` 的反函式：`prefix/scope/field/digest`。格式不對就回四個空字串。"""

    parts = key.split("/")
    if len(parts) != 4:
        return ("", "", "", "")
    return parts[0], parts[1], parts[2], parts[3]


def _can_skip_gh(record: dict, recovered: dict[str, str]) -> bool:
    """record 說「上次確認後沒開新單，而且還開著的那幾張，欄位都還沒恢復」→ 不必問 gh。

    App 每次轉寫完都會跑，壞掉期間（單開著、還沒修好）不能次次打網路。
    """

    check = record.get("resolve") or {}
    if not check.get("at") or not isinstance(check.get("open"), dict):
        return False
    if any(str(item.get("at") or "") > check["at"] for item in record["issues"].values()):
        return False  # 上次確認之後又開了新單
    return not set(check["open"].values()) & set(recovered)


def _issue_state(verb: str, number: int, comment: str, runner: str, repo: Path, *extra: str) -> None:
    proc = _gh(runner, ["issue", verb, str(number), *extra, "--comment", comment], repo)
    if proc.returncode != 0:
        raise EmitterError(f"gh issue {verb} #{number} failed: {proc.stderr.strip() or proc.stdout.strip()}")


def close_issue(number: int, comment: str, runner: str, repo: Path) -> None:
    _issue_state("close", number, comment, runner, repo, "--reason", "completed")


def reopen_issue(number: int, comment: str, runner: str, repo: Path) -> None:
    _issue_state("reopen", number, comment, runner, repo)


def resolve_open(
    recovered: dict[str, str],
    *,
    key_prefix: str,
    owner_line: str,
    label: str,
    repo: Path,
    comment,
    runner: str = "gh",
    apply: bool = False,
    record_path: Path | None = None,
    checked_at: str = "",
    out=print,
) -> int:
    """恢復就關。回傳 exit code。

    只關同時符合這幾條的單：開著、帶 `label`、marker 的 key 以 `key_prefix/` 開頭、
    body 裡有 `owner_line`（例：同一台機器）、key 的欄位在 `recovered` 裡。
    `recovered` 是「欄位 → 恢復的證據（一句話）」，由呼叫端判定；沒有正面證據的欄位不關。
    `comment(field, evidence) -> str` 產生關單留言。
    """

    try:
        record = load_record(record_path) if record_path is not None else None
        if record is not None and _can_skip_gh(record, recovered):
            out(f"RESOLVE_ISSUES=SKIPPED (上次 {record['resolve']['at']} 確認後沒開新單，開著的 {len(record['resolve']['open'])} 張都還沒恢復)")
            return 0

        still_open: dict[str, str] = {}  # 編號 → 欄位（下次用來判斷要不要再問 gh）
        closed = 0
        for item in _list_issues(runner, repo, ["--state", "open", "--label", label]):
            number = item.get("number")
            body = item.get("body") or ""
            key = parse_marker_key(body)
            prefix, _scope, field, _digest = parse_key(key)
            if prefix != key_prefix or owner_line not in body:
                continue  # 別人的單（別台機器、手寫的、別的來源）
            if field not in recovered:
                out(f"KEPT_OPEN #{number} key={key}（{field} 還沒有恢復的證據）")
                still_open[str(number)] = field
                continue
            if apply:
                close_issue(number, comment(field, recovered[field]), runner, repo)
                if record is not None:  # 讓同一天又壞時 emit_all 知道要重開，而不是當成已開過
                    record["issues"].setdefault(key, {"number": str(number), "url": "", "at": ""})
                    record["issues"][key]["resolved_at"] = checked_at
            out(f"{'CLOSED' if apply else 'WOULD_CLOSE'} #{number} key={key}")
            closed += 1

        if apply and record is not None:
            record["resolve"] = {"at": checked_at, "open": still_open}
            _write_record(record_path, record)
    except EmitterError as exc:
        out(f"RESOLVE_ISSUES=FAIL {exc}")
        return 2

    if not closed and not still_open:
        out("RESOLVE_ISSUES=NONE (no open issue owned here)")
    else:
        out(f"RESOLVE_ISSUES={'CLOSED' if apply else 'DRY_RUN'} closed={closed} kept_open={len(still_open)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """手動用：把一份 findings JSON 開成單。repo 端通常直接呼叫 emit_all。"""

    parser = argparse.ArgumentParser(description="把 findings JSON 開成去重後的 GitHub issue。")
    parser.add_argument("--findings", required=True, help='JSON list：[{"field":…,"value":…,"title":…}]')
    parser.add_argument("--scope", required=True, help="去重鍵的中段，通常是日期")
    parser.add_argument("--repo", default=".", help="gh 在哪個 repo 跑（預設 cwd）")
    parser.add_argument("--runner", default="gh")
    parser.add_argument("--record", default=None, help="編號回寫檔（JSON）")
    parser.add_argument("--apply", action="store_true", help="真的開單（預設 dry-run）")
    args = parser.parse_args(argv)

    try:
        raw = json.loads(Path(args.findings).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"EMIT_ISSUES=FAIL findings 讀不到：{exc}")
        return 2
    findings = [Finding(**item) for item in raw]
    return emit_all(
        findings,
        scope=args.scope,
        repo=Path(args.repo),
        runner=args.runner,
        apply=args.apply,
        record_path=Path(args.record) if args.record else None,
    )


if __name__ == "__main__":
    sys.exit(main())
