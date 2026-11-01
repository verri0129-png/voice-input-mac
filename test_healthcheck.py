"""健康檢查與工單 emitter 的回歸測試（issue #1）。

釘住四件事：
1. 探測是**純函式**可測：launchctl／pgrep／local.env 的字串進來，判定出去。
2. `err_lines_since_last_check` 有定義：靠 checkpoint 的 offset，第一次跑只建立基準。
3. 「什麼算 finding」只在 `healthcheck.collect_findings` 一處；健康的 record 必須回空 list
   （emitter 才會印 `EMIT_ISSUES=NONE`）。
4. emitter 的 key／marker／五段 body／去重／編號回寫，用替身 `gh` 跑真的流程，
   不碰 GitHub。
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
from issue_emitter import Finding

SCOPE = "2026-11-01"

FAKE_GH = r'''#!/usr/bin/env python3
"""替身 gh：只支援 issue list / issue create，狀態存在 FAKE_GH_STORE。"""
import json, os, sys

store_path = os.environ["FAKE_GH_STORE"]
try:
    store = json.loads(open(store_path, encoding="utf-8").read() or "{}")
except FileNotFoundError:
    store = {}
issues = store.setdefault("issues", [])
argv = sys.argv[1:]

if argv[:2] == ["issue", "list"]:
    print(json.dumps([{"number": i["number"], "body": i["body"]} for i in issues]))
elif argv[:2] == ["issue", "create"]:
    title = body = ""
    labels = ""
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
        sources={
            "health": "/Users/x/orca/state/voice-input/health.json",
            "err_log": "/Users/x/orca/state/voice-toggle-hotkey.err.log",
            "agent_label": healthcheck.AGENT_LABEL,
        },
    )
    base.update(overrides)
    return healthcheck.build_record(**base)


class LaunchctlParseTests(unittest.TestCase):
    def test_running_agent(self):
        out = "PID\tStatus\tLabel\n2769\t0\tlocal.hunghanchu.voice-toggle-hotkey\n"
        self.assertEqual(
            healthcheck.parse_launchctl(out), (True, 2769, 0)
        )

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
            runtime = self._runtime(Path(tmp), None)
            backend, source = healthcheck.stt_backend(runtime)
            self.assertEqual(backend, "breeze")
            self.assertIn("default", source)

    def test_reads_soniox(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self._runtime(Path(tmp), "VOICE_STT=soniox\n")
            self.assertEqual(healthcheck.stt_backend(runtime)[0], "soniox")

    def test_comment_and_quotes_and_case(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self._runtime(Path(tmp), '# 註解\nVOICE_STT = "Breeze"  # 本地\n')
            backend, source = healthcheck.stt_backend(runtime)
            self.assertEqual(backend, "breeze")
            self.assertIn("local.env:", source)

    def test_unknown_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self._runtime(Path(tmp), "VOICE_STT=whisper\n")
            backend, source = healthcheck.stt_backend(runtime)
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


class RecordTests(unittest.TestCase):
    def test_healthy_is_ok(self):
        record = healthy_record()
        self.assertTrue(record["ok"])
        self.assertEqual(record["reasons"], [])
        for field in (
            "agent_loaded",
            "last_exit",
            "app_running",
            "err_lines_since_last_check",
            "stt_backend",
            "checked_at",
        ):
            self.assertIn(field, record)

    def test_reasons_cover_every_field(self):
        record = healthy_record(
            agent_loaded=False, last_exit=9, app_running=False, err_lines=3, stt_backend="unknown"
        )
        self.assertFalse(record["ok"])
        joined = " | ".join(record["reasons"])
        for token in ("agent_loaded", "last_exit", "app_running", "err_lines", "stt_backend"):
            self.assertIn(token, joined)


class FindingsTests(unittest.TestCase):
    def test_healthy_record_owes_no_ticket(self):
        self.assertEqual(healthcheck.collect_findings(healthy_record()), [])

    def test_app_running_false_is_one_finding(self):
        findings = healthcheck.collect_findings(healthy_record(app_running=False, app_pids=[]))
        self.assertEqual(len(findings), 1)
        finding = findings[0]
        self.assertEqual(finding.field, "app_running")
        self.assertRegex(finding.key(SCOPE), r"^health/2026-11-01/app_running/[0-9a-f]{8}$")
        self.assertIn("src:health", finding.labels)
        self.assertIn("kind:bug", finding.labels)

    def test_key_is_stable_and_machine_specific(self):
        a = healthcheck.collect_findings(healthy_record(app_running=False))[0]
        b = healthcheck.collect_findings(healthy_record(app_running=False))[0]
        other = healthcheck.collect_findings(
            healthy_record(machine="hungmac-mini-2", app_running=False)
        )[0]
        self.assertEqual(a.key(SCOPE), b.key(SCOPE))  # 重跑不會開第二張
        self.assertNotEqual(a.key(SCOPE), other.key(SCOPE))  # 另一台機器是另一張單
        expected = hashlib.sha256(b"chumac-studio-1|false").hexdigest()[:8]
        self.assertTrue(a.key(SCOPE).endswith(expected))

    def test_marker_shape_matches_gate(self):
        finding = healthcheck.collect_findings(healthy_record(app_running=False))[0]
        marker = finding.marker(SCOPE)
        self.assertEqual(marker, f"<!-- key: {finding.key(SCOPE)} -->")
        self.assertEqual(issue_emitter.build_body(finding, SCOPE).splitlines()[0], marker)

    def test_body_has_five_sections(self):
        body = issue_emitter.build_body(
            healthcheck.collect_findings(healthy_record(app_running=False))[0], SCOPE
        )
        for section in ("## 背景", "## 證據", "## 重現命令", "## 完成定義（gate token）", "## 不可動範圍"):
            self.assertIn(section, body)
        self.assertIn("healthcheck.py", body)
        # 不可動範圍是共用的護欄，不能因為換一個 finding 就不見
        self.assertIn("launchd agent", body)

    def test_every_unhealthy_field_gets_a_ticket(self):
        findings = healthcheck.collect_findings(
            healthy_record(agent_loaded=False, last_exit=9, app_running=False, err_lines=2)
        )
        self.assertEqual(
            [f.field for f in findings],
            ["app_running", "agent_loaded", "last_exit", "err_lines_since_last_check"],
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
        findings = healthcheck.collect_findings(healthy_record(app_running=False))
        rc, out = self._emit(findings)
        self.assertEqual(rc, 0)
        self.assertIn("WOULD_CREATE", out)
        self.assertIn("EMIT_ISSUES=DRY_RUN", out)
        self.assertFalse(self.store.exists())

    def test_apply_creates_once_then_skips_existing(self):
        findings = healthcheck.collect_findings(healthy_record(app_running=False))
        key = findings[0].key(SCOPE)

        rc, out = self._emit(findings, apply=True)
        self.assertEqual(rc, 0)
        self.assertIn("CREATED https://github.com/example/repo/issues/1", out)
        self.assertIn("EMIT_ISSUES=CREATED", out)

        rc, out = self._emit(findings, apply=True)
        self.assertEqual(rc, 0)
        self.assertIn(f"SKIPPED_EXISTING key={key}", out)
        store = json.loads(self.store.read_text(encoding="utf-8"))
        self.assertEqual(len(store["issues"]), 1)
        self.assertTrue(store["issues"][0]["body"].startswith(f"<!-- key: {key} -->"))
        self.assertEqual(store["issues"][0]["title"], "[health] ⌘E 按下去沒有 App 可以接（voice-input App 不在執行）")
        self.assertIn("src:health", store["issues"][0]["labels"])

    def test_record_back_writes_number_and_skips_without_gh(self):
        findings = healthcheck.collect_findings(healthy_record(app_running=False))
        record = self.tmp / "state" / "emitted_issues.json"
        rc, out = self._emit(findings, apply=True, record=record)
        self.assertEqual(rc, 0)
        self.assertIn("RECORDED_BACK", out)
        data = json.loads(record.read_text(encoding="utf-8"))
        key = findings[0].key(SCOPE)
        self.assertEqual(data["issues"][key]["number"], "1")
        self.assertEqual(record.stat().st_mode & 0o777, 0o600)

        # gh 完全掛掉也還記得住：record 會先擋掉同一張單
        rc = issue_emitter.emit_all(
            findings,
            scope=SCOPE,
            repo=self.repo,
            runner=str(self.tmp / "no-such-gh"),
            apply=False,
            record_path=record,
            out=lambda line: None,
        )
        self.assertEqual(rc, 0)

    def test_gh_failure_is_exit_2(self):
        findings = healthcheck.collect_findings(healthy_record(app_running=False))
        lines: list[str] = []
        rc = issue_emitter.emit_all(
            findings,
            scope=SCOPE,
            repo=self.repo,
            runner=str(self.tmp / "no-such-gh"),
            apply=True,
            out=lines.append,
        )
        self.assertEqual(rc, 2)
        self.assertIn("EMIT_ISSUES=FAIL", "\n".join(lines))

    def test_parse_marker_keys(self):
        items = [
            {"number": 1, "body": "<!-- key: health/2026-11-01/app_running/deadbeef -->\n\n## 背景"},
            {"number": 2, "body": "沒有 marker 的手寫單"},
            {"number": 3, "body": None},
        ]
        self.assertEqual(
            issue_emitter.parse_marker_keys(items), {"health/2026-11-01/app_running/deadbeef"}
        )


class CliReplayTests(unittest.TestCase):
    """gate：拿 health.json 的副本把 app_running 改 false → 一張單；重跑 → SKIPPED_EXISTING。"""

    def test_from_health_replay_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            state = tmp / "state"
            state.mkdir()
            copy = state / "mutated.json"
            record = healthy_record(app_running=False, app_pids=[],
                                    sources={"health": str(copy), "err_log": "/tmp/x.err.log"})
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
                self.assertIn("CREATED", proc.stdout)
                self.assertIn("EMIT_ISSUES=CREATED", proc.stdout)

                proc = subprocess.run(
                    [os.environ.get("PYTHON", "python3"), str(Path(healthcheck.__file__)), *args],
                    capture_output=True, text=True, check=False,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("SKIPPED_EXISTING", proc.stdout)
                self.assertEqual(len(json.loads(store.read_text(encoding="utf-8"))["issues"]), 1)
            finally:
                os.environ.pop("FAKE_GH_STORE", None)


if __name__ == "__main__":
    unittest.main()
