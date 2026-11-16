"""11-16：⌘E 與櫃檯小妹共用一份 OctoASR（asr_server --listen／--attach），改寫用本機大腦（local-brain）。不載模型。"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

import asr_server
import rewrite_cloud
import transcribe as t

HERE = Path(__file__).resolve().parent


class SocketShareTest(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(dir="/tmp")  # AF_UNIX 路徑有長度上限，放短路徑
        self.path = Path(self.td.name) / "a.sock"

    def tearDown(self):
        self.td.cleanup()

    def _serve(self):
        seen = []

        def recognise(req, audio):
            seen.append((req.get("backend"), len(audio)))
            return {"text": f"n={len(audio)}", "segments": [], "backend": req.get("backend")}

        srv = asr_server.open_listener(self.path)
        threading.Thread(target=asr_server.accept_loop, args=(srv, recognise), daemon=True).start()
        self.addCleanup(srv.close)
        return seen

    def _client(self):
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.connect(str(self.path))
        self.addCleanup(c.close)
        return c, c.makefile("rwb")

    @staticmethod
    def _ask(f, req):
        f.write((json.dumps(req) + "\n").encode())
        f.flush()
        return json.loads(f.readline())

    def test_socket_is_private_to_the_user(self):
        self._serve()
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_each_connection_has_its_own_stream_buffer(self):
        seen = self._serve()
        _, a = self._client()
        _, b = self._client()
        pcm = (b"\x01\x00" * 100)
        import base64
        enc = base64.b64encode(pcm).decode()
        self.assertEqual(self._ask(a, {"op": "append", "fmt": "i2", "audio": enc, "offset": 0})["len"], 100)
        # b 沒傳過前綴：offset 對不上就要求整段（不能拿到 a 的音訊）
        self.assertEqual(self._ask(b, {"op": "final", "fmt": "i2", "audio": enc, "offset": 100})["error"], "offset")
        out = self._ask(a, {"op": "final", "fmt": "i2", "audio": enc, "offset": 100, "backend": "octo"})
        self.assertEqual((out["text"], seen), ("n=200", [("octo", 200)]))

    def test_does_not_take_over_a_live_socket(self):
        self._serve()
        self.assertIsNone(asr_server.open_listener(self.path))

    def test_stale_socket_file_is_replaced(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(str(self.path))
        s.close()  # 檔案還在、沒人聽
        srv = asr_server.open_listener(self.path)
        self.assertIsNotNone(srv)
        srv.close()

    def test_attach_relays_stdin_to_the_shared_server(self):
        self._serve()
        env = {**os.environ, "VOICE_ASR_SOCKET": str(self.path)}
        p = subprocess.run([sys.executable, str(HERE / "asr_server.py"), "--attach"], env=env, cwd=HERE,
                           input=json.dumps({"fmt": "i2", "audio": "AQABAA==", "backend": "octo"}) + "\n",
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(json.loads(p.stdout)["text"], "n=2", p.stderr)
        self.assertIn("接上常駐伺服器", p.stderr)


class LlmProxyTest(unittest.TestCase):
    def test_forwards_only_the_rewrite_fields(self):
        got = {}

        def post(url, body, timeout):
            got.update(url=url, body=body, timeout=timeout)
            return {"text": "好。", "brain": "e128-ha"}

        out = asr_server.llm_proxy({"op": "llm", "system": "S", "user": "U", "max_tokens": 9, "wait_s": 3,
                                    "timeout_s": 12, "timeout": 17, "audio": "x"}, post=post)
        self.assertEqual(out["text"], "好。")
        self.assertEqual(got["body"], {"system": "S", "user": "U", "max_tokens": 9, "wait_s": 3, "timeout_s": 12})
        self.assertEqual(got["timeout"], 17.0)

    def test_brain_down_is_an_error_reply_not_a_crash(self):
        def post(url, body, timeout):
            raise ConnectionRefusedError(61, "refused")
        self.assertIn("error", asr_server.llm_proxy({"op": "llm", "system": "", "user": "u"}, post=post))

    def test_llm_op_never_reaches_the_asr(self):
        keep = asr_server.llm_proxy
        asr_server.llm_proxy = lambda req: {"text": "ok"}
        try:
            out = asr_server.serve_line(json.dumps({"op": "llm", "user": "u"}), bytearray(),
                                        lambda *a: self.fail("ASR 不該被叫"))
        finally:
            asr_server.llm_proxy = keep
        self.assertEqual(out, {"text": "ok"})


class RemoteCallRetryTest(unittest.TestCase):
    """小妹重啟時共用伺服器換人，舊連線讀到 EOF：重連重送一次，不讓這次聽寫失敗。"""

    class _Proc:
        def __init__(self, reply):
            self.reply = reply
            self.stdin = self
            self.stdout = self

        def write(self, s):
            pass

        def flush(self):
            pass

        def readline(self):
            return self.reply

        def poll(self):
            return None

        def kill(self):
            pass

    def setUp(self):
        self._keep = t._remote_proc

    def tearDown(self):
        t._remote_proc = self._keep
        t._REMOTE_PROC = None

    def test_reconnects_once_after_eof(self):
        procs = iter([self._Proc(""), self._Proc('{"text": "ok"}\n')])
        t._remote_proc = lambda: next(procs)
        self.assertEqual(t._remote_call({"op": "x"}), {"text": "ok"})

    def test_gives_up_after_the_second_eof(self):
        t._remote_proc = lambda: self._Proc("")
        with self.assertRaises(RuntimeError):
            t._remote_call({"op": "x"})


class LocalBrainRewriteTest(unittest.TestCase):
    DRAFT = "好啦那我現在那個語音轉文字標點符號的問題要怎麼解"

    def setUp(self):
        self._keep = (t.local_brain_complete, t.rewrite_draft)
        self.td = tempfile.TemporaryDirectory()
        self.settings = t.rewrite_settings(backend="local-brain", terms_file=str(Path(self.td.name) / "none.json"))
        self.cloud = []
        t.rewrite_draft = lambda text, **k: (self.cloud.append(text), rewrite_cloud.RewriteResult(
            text="雲端版。", backend="cloud:gemini"))[1]

    def tearDown(self):
        t.local_brain_complete, t.rewrite_draft = self._keep
        self.td.cleanup()

    def test_is_the_default_backend(self):
        self.assertEqual(rewrite_cloud.RewriteConfig().backend, "local-brain")

    def test_uses_the_brain_with_the_cloud_prompt(self):
        asked = []
        t.local_brain_complete = lambda system, user: (asked.append((system, user)),
                                                       ("好啦，那我現在那個語音轉文字標點符號的問題要怎麼解？", "e128-ha"))[1]
        out, info = t.apply_rewrite(self.DRAFT, config=self.settings)
        self.assertEqual(info["backend"], "local-brain:e128-ha")
        self.assertIn("，", out)
        self.assertEqual(self.cloud, [])
        self.assertIn("語音辨識逐字稿的修正器", asked[0][0])  # 跟 Gemini 同一份規則

    def test_brain_down_falls_back_to_cloud(self):
        def down(system, user):
            raise RuntimeError("HTTP 503")
        t.local_brain_complete = down
        out, info = t.apply_rewrite(self.DRAFT, config=self.settings)
        self.assertEqual((out, info["backend"], self.cloud), ("雲端版。", "cloud:gemini", [self.DRAFT]))
        self.assertIn("503", info["local_brain_error"])

    def test_rejected_output_keeps_the_words_and_moves_punctuation_only(self):
        t.local_brain_complete = lambda s, u: ("那我現在語音轉文字的問題，要怎麼解？", "e128-ha")  # 刪了「好啦」「那個」
        out, info = t.apply_rewrite(self.DRAFT, config=self.settings)
        self.assertEqual(info["backend"], "local-brain:punct-only")
        self.assertEqual(t._text_only(out), t._text_only(self.DRAFT))
        self.assertEqual(self.cloud, [])

    def test_m2_goes_through_the_ssh_channel(self):
        keep = (t.STT_REMOTE_HOST, t._remote_call)
        sent = []
        t.STT_REMOTE_HOST = "m4"
        t._remote_call = lambda req: (sent.append(req), {"text": "好。", "brain": "e128-ha"})[1]
        try:
            text, brain = self._keep[0]("S", "U")
        finally:
            t.STT_REMOTE_HOST, t._remote_call = keep
        self.assertEqual((text, brain, sent[0]["op"], sent[0]["user"]), ("好。", "e128-ha", "llm", "U"))

    def test_brain_error_reply_raises(self):
        keep = (t.STT_REMOTE_HOST, t._remote_call)
        t.STT_REMOTE_HOST = "m4"
        t._remote_call = lambda req: {"error": "busy"}
        try:
            with self.assertRaises(RuntimeError):
                self._keep[0]("S", "U")
        finally:
            t.STT_REMOTE_HOST, t._remote_call = keep


if __name__ == "__main__":
    unittest.main()
