"""健康檢查與工單 emitter 的回歸測試（issue #1）。

釘住五件事：
1. 探測是**純函式**可測：launchctl／pgrep／local.env／.run.log 的字串進來，判定出去。
2. `err_lines_since_last_check` 有定義：靠 checkpoint 的 offset，第一次跑只建立基準。
3. **判準掛在「用過有沒有拿到字」上，不掛在「App 有沒有開著」上**——關掉 App 是靜止狀態。
4. 一次空白不開單、連續三次才開；真的失敗（transcribe failed／沒有音訊）一次就開。
5. emitter 的 key／marker／五段 body／去重／編號回寫，用替身 `gh` 跑真的流程，不碰 GitHub。
"""

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import healthcheck
import issue_emitter

SCOPE = "2026-11-01"

FAKE_GH = r'''#!/usr/bin/env python3
"""替身 gh：只支援 issue list / create / close，狀態存在 FAKE_GH_STORE。"""
import json, os, sys

store_path = os.environ["FAKE_GH_STORE"]
try:
    store = json.loads(open(store_path, encoding="utf-8").read() or "{}")
except FileNotFoundError:
    store = {}
issues = store.setdefault("issues", [])
argv = sys.argv[1:]

def opt(name, default=None):
    return argv[argv.index(name) + 1] if name in argv else default

if argv[:2] == ["issue", "list"]:
    state = opt("--state", "open")
    label = opt("--label")
    rows = [
        i for i in issues
        if (state == "all" or i.get("state", "OPEN").lower() == state)
        and (label is None or label in (i.get("labels") or "").split(","))
    ]
    print(json.dumps([{"number": i["number"], "body": i["body"]} for i in rows]))
elif argv[:2] in (["issue", "close"], ["issue", "reopen"]):
    number = int(argv[2])
    for i in issues:
        if i["number"] == number:
            i["state"] = "CLOSED" if argv[1] == "close" else "OPEN"
            i["comment"] = opt("--comment", "")
            i["reason"] = opt("--reason", "")
    open(store_path, "w", encoding="utf-8").write(json.dumps(store))
elif argv[:2] == ["issue", "create"]:
    title = body = labels = ""
    i = 2
    while i < len(argv):
        if argv[i] == "--title":
            title = argv[i + 1]; i += 2
        elif argv[i] == "--body":
            body = argv[i + 1]; i += 2
        elif argv[i] == "--label":
            labels = argv[i + 1]; i += 2
        else:
            i += 1
    number = len(issues) + 1
    issues.append({"number": number, "title": title, "body": body, "labels": labels})
    open(store_path, "w", encoding="utf-8").write(json.dumps(store))
    print(f"https://github.com/example/repo/issues/{number}")
else:
    print(f"fake gh: unsupported {argv}", file=sys.stderr)
    sys.exit(2)
'''


def healthy_record(**overrides) -> dict:
    base: dict = dict(
        machine="chumac-studio-1",
        checked_at="2026-11-01T12:00:00+07:00",
        agent_loaded=True,
        agent_pid=2769,
        last_exit=0,
        app_running=True,
        app_pids=[9943],
        err_lines=0,
        stt_backend="soniox",
        stt_source="local.env:/Users/x/orca/voice-input-mac/local.env",
        runtime=Path("/Users/x/orca/voice-input-mac"),
        last_attempt={"at": "2026-11-01T06:16:27Z", "outcome": "ok", "duration_s": 0.41, "chars": 43},
        consecutive_no_text=0,
        sources={
            "health": "/Users/x/orca/state/voice-input/health.json",
            "err_log": "/Users/x/orca/state/voice-toggle-hotkey.err.log",
            "run_log": "/Users/x/orca/voice-input-mac/.run.log",
            "agent_label": healthcheck.AGENT_LABEL,
        },
    )
    base.update(overrides)
    return healthcheck.build_record(**base)


def failed_record(**overrides) -> dict:
    """最近一次真的失敗（transcribe failed）——這個一次就該開單。"""

    base: dict = dict(
        last_attempt={
            "at": "2026-11-01T06:20:00Z",
            "outcome": "failed",
            "detail": "transcribe.py 失敗（exit 1）",
        }
    )
    base.update(overrides)
    return healthy_record(**base)


class LaunchctlParseTests(unittest.TestCase):
    def test_running_agent(self):
        out = "PID\tStatus\tLabel\n2769\t0\tlocal.hunghanchu.voice-toggle-hotkey\n"
        self.assertEqual(healthcheck.parse_launchctl(out), (True, 2769, 0))

    def test_loaded_but_not_running(self):
        out = "-\t0\tlocal.hunghanchu.voice-toggle-hotkey\n"
        self.assertEqual(healthcheck.parse_launchctl(out), (True, None, 0))

    def test_missing_label(self):
        out = "2769\t0\tcom.example.other\n"
        self.assertEqual(healthcheck.parse_launchctl(out), (False, None, None))

    def test_non_numeric_last_exit_is_none(self):
        out = "-\t-\tlocal.hunghanchu.voice-toggle-hotkey\n"
        self.assertEqual(healthcheck.parse_launchctl(out), (True, None, None))


class PgrepParseTests(unittest.TestCase):
    def test_pids(self):
        out = (
            "9943 /Applications/VoiceInputMac.app/Contents/MacOS/VoiceInputMac\n"
            "10001 /Applications/VoiceInputMac.app/Contents/MacOS/VoiceInputMac --x\n"
        )
        self.assertEqual(healthcheck.parse_pgrep(out), [9943, 10001])

    def test_empty(self):
        self.assertEqual(healthcheck.parse_pgrep(""), [])


class SttBackendTests(unittest.TestCase):
    def _runtime(self, tmp: Path, body: str | None) -> Path:
        runtime = tmp / "checkout"
        runtime.mkdir()
        if body is not None:
            (runtime / "local.env").write_text(body, encoding="utf-8")
        return runtime

    def test_no_local_env_is_breeze_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend, source = healthcheck.stt_backend(self._runtime(Path(tmp), None))
            self.assertEqual(backend, "breeze")
            self.assertIn("default", source)

    def test_reads_soniox(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                healthcheck.stt_backend(self._runtime(Path(tmp), "VOICE_STT=soniox\n"))[0], "soniox"
            )

    def test_comment_and_quotes_and_case(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self._runtime(Path(tmp), '# 註解\nVOICE_STT = "Breeze"  # 本地\n')
            backend, source = healthcheck.stt_backend(runtime)
            self.assertEqual(backend, "breeze")
            self.assertIn("local.env:", source)

    def test_unknown_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend, source = healthcheck.stt_backend(self._runtime(Path(tmp), "VOICE_STT=whisper\n"))
            self.assertEqual(backend, "unknown")
            self.assertIn("whisper", source)

    def test_runtime_dir_defaults_to_transcriber_path(self):
        # App 的預設 transcribe.py 就在這個 repo（轉錄程式與 local.env 同一個資料夾）。
        resolved = healthcheck.runtime_dir()
        self.assertTrue((resolved / "transcribe.py").exists(), resolved)


class ErrLineCountTests(unittest.TestCase):
    def test_missing_file(self):
        self.assertEqual(healthcheck.count_new_lines(Path("/nonexistent/x.err.log"), 0), (0, 0))

    def test_counts_only_appended_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "err.log"
            p.write_text("a\nb\n", encoding="utf-8")
            count, offset = healthcheck.count_new_lines(p, 0)
            self.assertEqual((count, offset), (2, 4))
            with p.open("a", encoding="utf-8") as fh:
                fh.write("c\n")
            self.assertEqual(healthcheck.count_new_lines(p, offset), (1, 6))

    def test_partial_trailing_line_is_left_unread(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "err.log"
            p.write_text("a\nb", encoding="utf-8")
            count, offset = healthcheck.count_new_lines(p, 0)
            self.assertEqual((count, offset), (1, 2))
            with p.open("a", encoding="utf-8") as fh:
                fh.write("\n")
            self.assertEqual(healthcheck.count_new_lines(p, offset), (1, 4))

    def test_truncated_file_restarts(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "err.log"
            p.write_text("x\n", encoding="utf-8")
            self.assertEqual(healthcheck.count_new_lines(p, 999), (1, 2))

    def test_collect_initialises_baseline_then_counts_new_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            err = Path(tmp) / "hotkey.err.log"
            err.write_text("old failure\n", encoding="utf-8")
            kwargs = dict(
                launchctl_list=lambda: "2769\t0\tlocal.hunghanchu.voice-toggle-hotkey\n",
                pgrep_app=lambda: "9943 /Applications/VoiceInputMac.app/Contents/MacOS/VoiceInputMac\n",
                machine="m4",
                now=datetime(2026, 11, 1, 12, 0, 0),
            )
            first = healthcheck.collect(runtime=Path(tmp), state_dir=state, err_log=err, **kwargs)
            self.assertEqual(first["err_lines_since_last_check"], 0)  # 第一次只建立基準
            self.assertTrue((state / "checkpoint.json").exists())

            with err.open("a", encoding="utf-8") as fh:
                fh.write("new failure 1\nnew failure 2\n")
            second = healthcheck.collect(runtime=Path(tmp), state_dir=state, err_log=err, **kwargs)
            self.assertEqual(second["err_lines_since_last_check"], 2)

            third = healthcheck.collect(runtime=Path(tmp), state_dir=state, err_log=err, **kwargs)
            self.assertEqual(third["err_lines_since_last_check"], 0)


class AttemptLogTests(unittest.TestCase):
    """`.run.log` → 「上一次用它拿到字了嗎」。這是整套判準的來源。"""

    def test_success_line(self):
        info = healthcheck.classify_line(
            "[2026-11-01T06:16:27Z] [app] 結果（停止→文字 0.41s）：好，這是轉錄全文"
        )
        self.assertEqual(info["outcome"], "ok")
        self.assertEqual(info["duration_s"], 0.41)
        self.assertEqual(info["chars"], 8)  # 好，這是轉錄全文

    def test_empty_result_line(self):
        self.assertEqual(
            healthcheck.classify_line("[2026-11-01T06:16:27Z] [app] 結果（停止→文字 0.30s）：")["outcome"],
            "empty",
        )

    def test_failure_lines(self):
        cases = {
            "[2026-11-01T06:20:00Z] [app] transcribe failed: transcribe.py 失敗（exit 1）": "failed",
            "[2026-11-01T06:21:00Z] [app] recording stop：沒有音訊，放棄": "no_audio",
            "[2026-11-01T06:22:00Z] [app] exit 3：沒有可用語音（麥克風沒收到聲音…）": "no_speech",
            "[2026-11-01T06:23:00Z] [app] exit 4：疑似非語音 → 未自動貼上": "suspect",
            "[2026-11-01T06:24:00Z] [app] 結果為空，不貼上": "empty",
        }
        for line, outcome in cases.items():
            self.assertEqual(healthcheck.classify_line(line)["outcome"], outcome, line)

    def test_transcript_text_is_never_kept(self):
        """證據只能帶錯誤訊息，不能帶轉錄全文。"""

        info = healthcheck.classify_line("[2026-11-01T06:16:27Z] [app] 結果（停止→文字 0.41s）：私有內容")
        self.assertNotIn("detail", info)
        failed = healthcheck.classify_line("[2026-11-01T06:20:00Z] [app] transcribe failed: boom")
        self.assertEqual(failed["detail"], "boom")

    def test_parse_attempts_groups_by_recording_start(self):
        lines = [
            "[2026-11-01T06:00:00Z] [app] recording start（STT=soniox）",
            "[2026-11-01T06:00:05Z] [app] 結果（停止→文字 0.40s）：第一句",
            "[2026-11-01T06:01:00Z] [app] recording start（STT=soniox）",
            "[2026-11-01T06:01:05Z] [app] recording stop：沒有音訊，放棄",
        ]
        self.assertEqual([a["outcome"] for a in healthcheck.parse_attempts(lines)], ["ok", "no_audio"])

    def test_read_last_attempt_and_streak(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_log = Path(tmp) / ".run.log"
            run_log.write_text(
                "[2026-11-01T06:00:00Z] [app] recording start（STT=soniox）\n"
                "[2026-11-01T06:00:05Z] [app] 結果（停止→文字 0.40s）：成功那次\n"
                "[2026-11-01T06:01:00Z] [app] recording start（STT=soniox）\n"
                "[2026-11-01T06:01:05Z] [app] exit 3：沒有可用語音\n"
                "[2026-11-01T06:02:00Z] [app] recording start（STT=soniox）\n"
                "[2026-11-01T06:02:05Z] [app] 結果為空，不貼上\n"
                "[2026-11-01T06:03:00Z] [app] recording start（STT=soniox）\n"
                "[2026-11-01T06:03:05Z] [app] exit 3：沒有可用語音\n",
                encoding="utf-8",
            )
            attempt, streak = healthcheck.read_last_attempt(run_log)
            self.assertEqual(attempt["outcome"], "no_speech")
            self.assertEqual(streak, 3)

    def test_missing_run_log_is_not_a_finding(self):
        self.assertEqual(healthcheck.read_last_attempt(Path("/nonexistent/.run.log")), (None, 0))


class RecordTests(unittest.TestCase):
    def test_healthy_is_ok(self):
        record = healthy_record()
        self.assertTrue(record["ok"])
        self.assertEqual(record["reasons"], [])
        for field in (
            "agent_loaded",
            "last_exit",
            "app_running",
            "last_attempt",
            "consecutive_no_text",
            "err_lines_since_last_check",
            "stt_backend",
            "checked_at",
        ):
            self.assertIn(field, record)

    def test_app_closed_is_not_a_reason(self):
        """要用才開：App 關著是靜止狀態，不能算 unhealthy。"""

        record = healthy_record(app_running=False, app_pids=[])
        self.assertTrue(record["ok"])
        self.assertEqual(record["reasons"], [])

    def test_reasons_cover_every_field(self):
        record = healthy_record(
            agent_loaded=False,
            last_exit=9,
            err_lines=3,
            stt_backend="unknown",
            last_attempt={"outcome": "failed"},
        )
        self.assertFalse(record["ok"])
        joined = " | ".join(record["reasons"])
        for token in ("agent_loaded", "last_exit", "last_attempt", "err_lines", "stt_backend"):
            self.assertIn(token, joined)


class FindingsTests(unittest.TestCase):
    def test_healthy_record_owes_no_ticket(self):
        self.assertEqual(healthcheck.collect_findings(healthy_record()), [])

    def test_app_running_false_is_not_a_finding(self):
        self.assertEqual(
            healthcheck.collect_findings(healthy_record(app_running=False, app_pids=[])),
            [],
            "使用者關掉 App 不該開單",
        )

    def test_hard_failure_is_one_finding(self):
        findings = healthcheck.collect_findings(failed_record())
        self.assertEqual(len(findings), 1)
        finding = findings[0]
        self.assertEqual(finding.field, "last_attempt")
        self.assertRegex(finding.key(SCOPE), r"^health/2026-11-01/last_attempt/[0-9a-f]{8}$")
        self.assertIn("src:health", finding.labels)
        self.assertIn("kind:bug", finding.labels)

    def test_single_blank_does_not_open_a_ticket(self):
        record = healthy_record(last_attempt={"outcome": "empty"}, consecutive_no_text=1)
        self.assertEqual(healthcheck.collect_findings(record), [])

    def test_three_blanks_in_a_row_open_one_ticket(self):
        record = healthy_record(last_attempt={"outcome": "no_speech"}, consecutive_no_text=3)
        findings = healthcheck.collect_findings(record)
        self.assertEqual([f.field for f in findings], ["consecutive_no_text"])
        self.assertEqual(findings[0].value, "3")

    def test_hard_failure_wins_over_streak(self):
        """一次就壞掉的（transcribe failed）不要同時又開一張「連續三次」。"""

        record = healthy_record(last_attempt={"outcome": "failed"}, consecutive_no_text=5)
        self.assertEqual([f.field for f in healthcheck.collect_findings(record)], ["last_attempt"])

    def test_key_is_stable_and_machine_specific(self):
        a = healthcheck.collect_findings(failed_record())[0]
        b = healthcheck.collect_findings(failed_record())[0]
        other = healthcheck.collect_findings(failed_record(machine="hungmac-mini-2"))[0]
        self.assertEqual(a.key(SCOPE), b.key(SCOPE))  # 重跑不會開第二張
        self.assertNotEqual(a.key(SCOPE), other.key(SCOPE))  # 另一台機器是另一張單
        expected = hashlib.sha256(b"chumac-studio-1|failed").hexdigest()[:8]
        self.assertTrue(a.key(SCOPE).endswith(expected))

    def test_marker_shape_matches_gate(self):
        finding = healthcheck.collect_findings(failed_record())[0]
        marker = finding.marker(SCOPE)
        self.assertEqual(marker, f"<!-- key: {finding.key(SCOPE)} -->")
        self.assertEqual(issue_emitter.build_body(finding, SCOPE).splitlines()[0], marker)

    def test_body_has_five_sections(self):
        body = issue_emitter.build_body(healthcheck.collect_findings(failed_record())[0], SCOPE)
        for section in ("## 背景", "## 證據", "## 重現命令", "## 完成定義（gate token）", "## 不可動範圍"):
            self.assertIn(section, body)
        self.assertIn("healthcheck.py", body)
        self.assertIn("launchd agent", body)  # 共用護欄不能因為換一個 finding 就不見
        self.assertNotIn("- - ", body)  # 證據條列不要疊兩層

    def test_every_unhealthy_field_gets_a_ticket(self):
        findings = healthcheck.collect_findings(
            healthy_record(
                agent_loaded=False,
                last_exit=9,
                err_lines=2,
                stt_backend="unknown",
                last_attempt={"outcome": "failed"},
            )
        )
        self.assertEqual(
            [f.field for f in findings],
            ["last_attempt", "agent_loaded", "last_exit", "err_lines_since_last_check", "stt_backend"],
        )
        for finding in findings:
            self.assertTrue(finding.done.strip(), finding.field)
            self.assertTrue(finding.reproduce.strip(), finding.field)


class EmitterCoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.store = self.tmp / "gh-store.json"
        self.gh = self.tmp / "gh"
        self.gh.write_text(FAKE_GH, encoding="utf-8")
        self.gh.chmod(0o755)
        os.environ["FAKE_GH_STORE"] = str(self.store)

    def tearDown(self):
        os.environ.pop("FAKE_GH_STORE", None)

    def _emit(self, findings, *, apply=False, record=None):
        lines: list[str] = []
        rc = issue_emitter.emit_all(
            findings,
            scope=SCOPE,
            repo=self.repo,
            runner=str(self.gh),
            apply=apply,
            record_path=record,
            checked_at="2026-11-01T12:00:00+07:00",
            out=lines.append,
        )
        return rc, "\n".join(lines)

    def test_no_findings_prints_none(self):
        rc, out = self._emit([])
        self.assertEqual(rc, 0)
        self.assertEqual(out, f"EMIT_ISSUES=NONE scope={SCOPE} (no finding)")

    def test_dry_run_does_not_create(self):
        rc, out = self._emit(healthcheck.collect_findings(failed_record()))
        self.assertEqual(rc, 0)
        self.assertIn("WOULD_CREATE", out)
        self.assertIn("EMIT_ISSUES=DRY_RUN", out)
        self.assertFalse(self.store.exists())

    def test_apply_creates_once_then_skips_existing(self):
        findings = healthcheck.collect_findings(failed_record())
        key = findings[0].key(SCOPE)

        rc, out = self._emit(findings, apply=True)
        self.assertEqual(rc, 0)
        self.assertIn("CREATED https://github.com/example/repo/issues/1", out)

        rc, out = self._emit(findings, apply=True)
        self.assertEqual(rc, 0)
        self.assertIn(f"SKIPPED_EXISTING key={key}", out)
        store = json.loads(self.store.read_text(encoding="utf-8"))
        self.assertEqual(len(store["issues"]), 1)
        self.assertTrue(store["issues"][0]["body"].startswith(f"<!-- key: {key} -->"))
        self.assertIn("src:health", store["issues"][0]["labels"])

    def test_record_back_avoids_gh_entirely(self):
        findings = healthcheck.collect_findings(failed_record())
        record = self.tmp / "state" / "emitted_issues.json"
        rc, out = self._emit(findings, apply=True, record=record)
        self.assertEqual(rc, 0)
        self.assertIn("RECORDED_BACK", out)
        key = findings[0].key(SCOPE)
        data = json.loads(record.read_text(encoding="utf-8"))
        self.assertEqual(data["issues"][key]["number"], "1")
        self.assertEqual(record.stat().st_mode & 0o777, 0o600)

        # 已經回寫過 → 完全不碰 gh（App 每次用完都跑一次，不該次次打網路）
        lines: list[str] = []
        rc = issue_emitter.emit_all(
            findings,
            scope=SCOPE,
            repo=self.repo,
            runner=str(self.tmp / "no-such-gh"),
            apply=True,
            record_path=record,
            out=lines.append,
        )
        self.assertEqual(rc, 0)
        self.assertIn("SKIPPED_RECORDED", "\n".join(lines))

    def test_gh_failure_is_exit_2(self):
        lines: list[str] = []
        rc = issue_emitter.emit_all(
            healthcheck.collect_findings(failed_record()),
            scope=SCOPE,
            repo=self.repo,
            runner=str(self.tmp / "no-such-gh"),
            apply=True,
            out=lines.append,
        )
        self.assertEqual(rc, 2)
        self.assertIn("EMIT_ISSUES=FAIL", "\n".join(lines))

    def test_runner_resolves_outside_path(self):
        # GUI 啟動的行程 PATH 沒有 /opt/homebrew/bin；gh 仍要找到
        self.assertTrue(issue_emitter.resolve_runner("gh").endswith("/gh"))
        self.assertEqual(issue_emitter.resolve_runner("/some/abs/gh"), "/some/abs/gh")

    def test_parse_marker_keys(self):
        items = [
            {"number": 1, "body": "<!-- key: health/2026-11-01/last_attempt/deadbeef -->\n\n## 背景"},
            {"number": 2, "body": "沒有 marker 的手寫單"},
            {"number": 3, "body": None},
        ]
        self.assertEqual(
            issue_emitter.parse_marker_keys(items), {"health/2026-11-01/last_attempt/deadbeef"}
        )


class ResolveTests(unittest.TestCase):
    """恢復就關：同一台、同一個欄位有「已恢復」的證據，才關 src:health 的單。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.store = self.tmp / "gh-store.json"
        self.gh = self.tmp / "gh"
        self.gh.write_text(FAKE_GH, encoding="utf-8")
        self.gh.chmod(0o755)
        os.environ["FAKE_GH_STORE"] = str(self.store)

    def tearDown(self):
        os.environ.pop("FAKE_GH_STORE", None)

    def _seed(self, issues):
        rows = []
        for n, (body, labels, state) in enumerate(issues, start=1):
            rows.append({"number": n, "title": f"t{n}", "body": body, "labels": labels, "state": state})
        self.store.write_text(json.dumps({"issues": rows}), encoding="utf-8")

    def _issues(self):
        return {i["number"]: i for i in json.loads(self.store.read_text(encoding="utf-8"))["issues"]}

    def _body(self, record, scope="2026-11-04"):
        findings = healthcheck.collect_findings(record)
        self.assertEqual(len(findings), 1)
        return issue_emitter.build_body(findings[0], scope)

    def _resolve(self, record, *, apply=True, record_path=None, runner=None):
        lines: list[str] = []
        rc = healthcheck.resolve_issues(
            record,
            repo=self.repo,
            runner=runner or str(self.gh),
            apply=apply,
            record_path=record_path,
            out=lines.append,
        )
        return rc, "\n".join(lines)

    def test_recovered_evidence_needs_positive_signal(self):
        ok = healthcheck.recovered_evidence(healthy_record())
        for field in ("last_attempt", "consecutive_no_text", "agent_loaded", "last_exit", "stt_backend"):
            self.assertIn(field, ok)
        self.assertIn("43 字", ok["last_attempt"])
        # 還沒有任何一輪 → 沒有「上一次有拿到字」的證據，不能關 last_attempt 的單
        none_yet = healthcheck.recovered_evidence(healthy_record(last_attempt=None))
        self.assertNotIn("last_attempt", none_yet)
        self.assertNotIn("consecutive_no_text", none_yet)
        down = healthcheck.recovered_evidence(healthy_record(agent_loaded=False, last_exit=None))
        self.assertNotIn("agent_loaded", down)
        self.assertNotIn("last_exit", down)

    def test_closes_recovered_same_machine_with_comment(self):
        self._seed([(self._body(failed_record()), "src:health,kind:bug", "OPEN")])
        rc, out = self._resolve(healthy_record(checked_at="2026-11-12T21:17:29+07:00"))
        self.assertEqual(rc, 0, out)
        self.assertIn("CLOSED #1", out)
        issue = self._issues()[1]
        self.assertEqual(issue["state"], "CLOSED")
        self.assertEqual(issue["reason"], "completed")
        self.assertIn("2026-11-12T21:17:29+07:00", issue["comment"])
        self.assertIn("43 字", issue["comment"])
        self.assertIn("chumac-studio-1", issue["comment"])

    def test_other_machine_is_not_touched(self):
        other = failed_record(machine="HungdeMac-mini")
        self._seed([(self._body(other), "src:health,kind:bug", "OPEN")])
        rc, out = self._resolve(healthy_record())
        self.assertEqual(rc, 0, out)
        self.assertEqual(self._issues()[1]["state"], "OPEN")
        self.assertNotIn("CLOSED #", out)

    def test_still_failing_field_stays_open(self):
        self._seed([(self._body(healthy_record(agent_loaded=False, last_exit=None)), "src:health,kind:bug", "OPEN")])
        rc, out = self._resolve(healthy_record(agent_loaded=False, last_exit=None))
        self.assertEqual(rc, 0, out)
        self.assertEqual(self._issues()[1]["state"], "OPEN")
        self.assertIn("KEPT_OPEN #1", out)

    def test_only_src_health_issues(self):
        body = self._body(failed_record())
        self._seed([(body, "kind:bug", "OPEN"), ("沒有 marker 的手寫單\n- machine=`chumac-studio-1`", "src:health", "OPEN")])
        rc, out = self._resolve(healthy_record())
        self.assertEqual(rc, 0, out)
        self.assertEqual(self._issues()[1]["state"], "OPEN")
        self.assertEqual(self._issues()[2]["state"], "OPEN")

    def test_dry_run_does_not_close(self):
        self._seed([(self._body(failed_record()), "src:health,kind:bug", "OPEN")])
        rc, out = self._resolve(healthy_record(), apply=False)
        self.assertEqual(rc, 0, out)
        self.assertIn("WOULD_CLOSE #1", out)
        self.assertEqual(self._issues()[1]["state"], "OPEN")

    def test_no_open_issue_known_skips_gh_next_time(self):
        record_path = self.tmp / "state" / "emitted_issues.json"
        self._seed([])
        rc, out = self._resolve(healthy_record(), record_path=record_path)
        self.assertEqual(rc, 0, out)
        self.assertIn("RESOLVE_ISSUES=NONE", out)
        # 上次確認過沒有開著的單、之後也沒開新單 → 不再打 gh
        rc, out = self._resolve(healthy_record(), record_path=record_path, runner=str(self.tmp / "no-such-gh"))
        self.assertEqual(rc, 0, out)
        self.assertIn("RESOLVE_ISSUES=SKIPPED", out)

    def test_still_broken_skips_gh_until_field_recovers(self):
        record_path = self.tmp / "state" / "emitted_issues.json"
        broken = healthy_record(agent_loaded=False, last_exit=None)
        self._seed([(self._body(broken), "src:health,kind:bug", "OPEN")])
        rc, out = self._resolve(broken, record_path=record_path)
        self.assertIn("KEPT_OPEN #1", out)
        # 還壞著 → 不再打 gh（App 每次轉寫完都會跑）
        rc, out = self._resolve(broken, record_path=record_path, runner=str(self.tmp / "no-such-gh"))
        self.assertEqual(rc, 0, out)
        self.assertIn("RESOLVE_ISSUES=SKIPPED", out)
        # 那個欄位恢復了 → 去問 gh 並關單
        rc, out = self._resolve(healthy_record(), record_path=record_path)
        self.assertEqual(rc, 0, out)
        self.assertIn("CLOSED #1", out)

    def test_same_day_relapse_reopens_auto_closed_issue(self):
        record_path = self.tmp / "state" / "emitted_issues.json"
        findings = healthcheck.collect_findings(failed_record())
        emit = lambda: issue_emitter.emit_all(  # noqa: E731
            findings, scope=SCOPE, repo=self.repo, runner=str(self.gh), apply=True,
            record_path=record_path, checked_at="2026-11-01T13:00:00+07:00", out=lines.append,
        )
        lines: list[str] = []
        emit()
        self._resolve(healthy_record(), record_path=record_path)
        self.assertEqual(self._issues()[1]["state"], "CLOSED")
        lines.clear()
        rc = emit()  # 同一天、同一個值又壞了 → 同一個 key
        self.assertEqual(rc, 0, lines)
        self.assertIn("REOPENED #1", "\n".join(lines))
        self.assertEqual(self._issues()[1]["state"], "OPEN")
        self.assertEqual(len(self._issues()), 1)

    def test_err_log_issue_is_not_auto_closed(self):
        # 錯誤 log 的單要人看過才算數；checkpoint 一前進就是 0，不能當恢復證據
        self.assertNotIn("err_lines_since_last_check", healthcheck.recovered_evidence(healthy_record()))

    def test_gh_failure_is_exit_2(self):
        rc, out = self._resolve(healthy_record(), runner=str(self.tmp / "no-such-gh"))
        self.assertEqual(rc, 2)
        self.assertIn("RESOLVE_ISSUES=FAIL", out)


class CliReplayTests(unittest.TestCase):
    """gate：拿 health.json 的副本改成失敗 → 一張單；重跑 → SKIPPED_EXISTING。"""

    def test_from_health_replay_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            state = tmp / "state"
            state.mkdir()
            copy = state / "mutated.json"
            record = failed_record(
                sources={"health": str(copy), "err_log": "/tmp/x.err.log", "run_log": "/tmp/.run.log"}
            )
            copy.write_text(json.dumps(record), encoding="utf-8")

            store = tmp / "gh-store.json"
            gh = tmp / "gh"
            gh.write_text(FAKE_GH, encoding="utf-8")
            gh.chmod(0o755)
            os.environ["FAKE_GH_STORE"] = str(store)
            try:
                args = [
                    "--from-health", str(copy), "--emit", "--apply",
                    "--state-dir", str(state), "--repo", str(tmp), "--runner", str(gh),
                ]
                proc = subprocess.run(
                    [os.environ.get("PYTHON", "python3"), str(Path(healthcheck.__file__)), *args],
                    capture_output=True, text=True, check=False,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("EMIT_ISSUES=CREATED", proc.stdout)

                proc = subprocess.run(
                    [os.environ.get("PYTHON", "python3"), str(Path(healthcheck.__file__)), *args],
                    capture_output=True, text=True, check=False,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                # 第二次一定不開第二張：record 已經回寫（SKIPPED_RECORDED，連 gh 都不問），
                # 或 record 掉了但 GitHub 上已有同一個 key（SKIPPED_EXISTING）。
                self.assertTrue(
                    "SKIPPED_EXISTING" in proc.stdout or "SKIPPED_RECORDED" in proc.stdout,
                    proc.stdout,
                )
                self.assertNotIn("CREATED https://", proc.stdout)
                self.assertEqual(len(json.loads(store.read_text(encoding="utf-8"))["issues"]), 1)
            finally:
                os.environ.pop("FAKE_GH_STORE", None)


if __name__ == "__main__":
    unittest.main()
