"""_remote_call：M4 回空行／非 JSON（常駐伺服器剛重啟）時要重連重送，不能丟 JSONDecodeError。"""
import io
import unittest
from unittest import mock

import transcribe as t


class FakeProc:
    def __init__(self, lines):
        self.stdin = io.StringIO()
        self.stdout = io.StringIO("".join(lines))

    def poll(self):
        return None

    def kill(self):
        pass


class RemoteCallRobust(unittest.TestCase):
    def setUp(self):
        t._REMOTE_PROC = None

    def tearDown(self):
        t._REMOTE_PROC = None

    def _run(self, first):
        procs = [FakeProc([first]), FakeProc(['{"ok": true}\n'])]
        with mock.patch.object(t, "_remote_proc", side_effect=lambda: procs.pop(0)):
            return t._remote_call({"op": "final"})

    def test_blank_line_retries(self):
        self.assertEqual(self._run("\n"), {"ok": True})

    def test_garbage_line_retries(self):
        self.assertEqual(self._run("warning: something\n"), {"ok": True})

    def test_still_bad_raises_runtime_error(self):
        procs = [FakeProc(["\n"]), FakeProc(["x\n"])]
        with mock.patch.object(t, "_remote_proc", side_effect=lambda: procs.pop(0)):
            with self.assertRaises(RuntimeError):
                t._remote_call({"op": "final"})

    def test_ssh_command_has_homebrew_path(self):
        with mock.patch.object(t, "STT_REMOTE_HOST", "m4"), mock.patch("subprocess.Popen") as popen:
            t._remote_proc()
        self.assertIn("PATH=/opt/homebrew/bin", popen.call_args[0][0][-1])


if __name__ == "__main__":
    unittest.main()
