"""邊錄邊傳（remote_stream_append / _remote_whisper 的增量路徑）。

用 asr_server.handle_request 當「假的 M4」，不載模型；只驗協定與退回整段的條件。
"""
import json
import unittest

import numpy as np

import asr_server
import transcribe as t


class FakeRemote:
    def __init__(self):
        self.buf = bytearray()
        self.calls = []  # (op, 樣本數)
        self.decoded = []  # 最後被拿去辨識的音訊（int16）

    def __call__(self, req):
        self.calls.append((req.get("op") or "full", len(req.get("audio", "")) * 3 // 4 // 2))
        out, audio = asr_server.handle_request(json.loads(json.dumps(req)), self.buf)
        if audio is not None:
            self.decoded.append(np.rint(audio * 32768).astype("int16"))
            return {"text": "ok", "segments": []}
        return out


def audio(n, seed=0):
    return np.random.default_rng(seed).integers(-20000, 20000, n).astype("int16")


def f32(a):
    return a.astype("float32") / 32768.0


class RemoteStreamTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeRemote()
        self._orig = t._remote_call
        t._remote_call = self.fake
        t._STREAM["n"], t._STREAM["digest"] = 0, None

    def tearDown(self):
        t._remote_call = self._orig

    def test_incremental_then_final_sends_only_tail(self):
        a = audio(48000)
        t.remote_stream_append(f32(a[:16000]))
        t.remote_stream_append(f32(a[:32000]))
        t._remote_whisper(f32(a), "zh", None)
        self.assertEqual([c[0] for c in self.fake.calls], ["append", "append", "final"])
        self.assertEqual(self.fake.calls[1][1], 16000)  # 第二次只傳新增的 16000
        self.assertEqual(self.fake.calls[2][1], 16000)  # final 只補尾巴
        self.assertTrue((self.fake.decoded[0] == a).all())  # M4 組出來的就是整段
        self.assertEqual(t._STREAM["n"], 0)

    def test_new_recording_resets(self):
        a, b = audio(32000, 1), audio(32000, 2)
        t.remote_stream_append(f32(a))
        t.remote_stream_append(f32(b))  # 內容不是 a 的延續
        t._remote_whisper(f32(b), "zh", None)
        self.assertTrue((self.fake.decoded[0] == b).all())

    def test_final_with_unrelated_audio_goes_full(self):
        a, b = audio(32000, 1), audio(40000, 2)
        t.remote_stream_append(f32(a))
        t._remote_whisper(f32(b), "zh", None)
        self.assertEqual(self.fake.calls[-1][0], "full")
        self.assertTrue((self.fake.decoded[0] == b).all())

    def test_server_lost_buffer_falls_back_to_full(self):
        a = audio(48000)
        t.remote_stream_append(f32(a[:32000]))
        self.fake.buf.clear()  # 模擬 M4 重啟／ssh 重連，緩衝區沒了
        t._remote_whisper(f32(a), "zh", None)
        self.assertEqual([c[0] for c in self.fake.calls], ["append", "final", "full"])
        self.assertTrue((self.fake.decoded[0] == a).all())

    def test_no_stream_is_plain_full(self):
        a = audio(16000)
        t._remote_whisper(f32(a), "zh", None)
        self.assertEqual(self.fake.calls, [("full", 16000)])

    def test_server_float32_legacy_request(self):
        buf = bytearray(b"xx")
        import base64
        a = f32(audio(100))
        out, dec = asr_server.handle_request({"audio": base64.b64encode(a.astype("<f4").tobytes()).decode()}, buf)
        self.assertIsNone(out)
        self.assertEqual(len(buf), 0)
        self.assertTrue(np.allclose(dec, a))


if __name__ == "__main__":
    unittest.main()
