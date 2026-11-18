"""asr_server：--attach 撞上小妹重啟空檔要重試；載模型時的雜訊不能進 stdout 協定通道（11-18 事故）。"""
import contextlib
import io
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import asr_server as a


class AttachRetry(unittest.TestCase):
    def test_gives_up_after_deadline(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"VOICE_ATTACH_WAIT": "0.6"}):
            t0 = time.time()
            self.assertIsNone(a.attach(Path(d) / "none.sock"))
            self.assertGreaterEqual(time.time() - t0, 0.5)

    def test_connects_when_socket_appears_late(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"VOICE_ATTACH_WAIT": "5"}):
            path = Path(d) / "late.sock"
            srv = socket.socket(socket.AF_UNIX)

            def later():
                time.sleep(1.0)
                srv.bind(str(path)); srv.listen(1)
                c, _ = srv.accept(); c.close()
            threading.Thread(target=later, daemon=True).start()
            # attach 成功連上後會 os._exit(0)，所以只驗證「重試期間沒回 None」：換成假的 os._exit
            with mock.patch.object(a.os, "_exit", side_effect=SystemExit), \
                 mock.patch("sys.stdin", io.TextIOWrapper(io.BytesIO(b""))):
                with self.assertRaises(SystemExit):
                    a.attach(path)


class StdoutGuard(unittest.TestCase):
    def test_noise_during_load_goes_to_stderr(self):
        def noisy():
            print("ffmpeg is not installed")
            return lambda audio, req: {"text": ""}
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with contextlib.redirect_stdout(err):
                a.load_engines(["x"], {"x": noisy}, None)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("ffmpeg", err.getvalue())


if __name__ == "__main__":
    unittest.main()
