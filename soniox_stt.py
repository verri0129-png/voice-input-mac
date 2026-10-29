"""Soniox 即時 STT（stt-rt-v5）——語音輸入的另一條 ASR 路線。

用途：`VOICE_STT=soniox` 時取代本地 Breeze。Soniox 自己會吐標點，所以不需要
「先貼規則版再等雲端定版」那套；文字回來就是最終版。

兩種用法：
  1. `SonioxStream`：App 邊錄邊送（真正的低延遲路徑，停止後 ~0.3s 取回文字）。
  2. `transcribe_wav(path)`：整段音檔一次灌進去（CLI 驗證／Swift 端失敗時的後備）。
     注意：停止後才灌整段時，Soniox 的處理速度大約就是實時 1x，長句會慢
     （實測 12.7s 音檔 → 10.7s），所以這條只適合短句與驗證。

金鑰：環境變數 `VOICE_SONIOX_API_KEY`，或 `~/.hermes/secrets/soniox.env`
（沿用字幕專案那一把）。金鑰不會被印出來。
"""
from __future__ import annotations

import json
import os
import time
import wave
from pathlib import Path

WS_URL = "wss://stt-rt.soniox.com/transcribe-websocket"
DEFAULT_MODEL = "stt-rt-v5"
KEY_FILE = Path(os.path.expanduser("~/.hermes/secrets/soniox.env"))
SAMPLE_RATE = 16000
CHUNK_BYTES = SAMPLE_RATE * 2 // 10  # 100ms


def api_key() -> str:
    for name in ("VOICE_SONIOX_API_KEY", "SONIOX_API_KEY"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    try:
        for line in KEY_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            if name.strip() in ("SONIOX_API_KEY", "VOICE_SONIOX_API_KEY"):
                value = value.strip().strip('"').strip("'")
                if value:
                    return value
    except OSError:
        pass
    raise RuntimeError(
        "找不到 Soniox API key：設 VOICE_SONIOX_API_KEY 或寫進 ~/.hermes/secrets/soniox.env"
    )


def build_config(terms=None, *, language="zh", model=None) -> dict:
    """連線後要送的第一個 JSON。語言固定在中文，避免英文字被轉成中文音譯。"""
    config = {
        "model": model or os.environ.get("VOICE_SONIOX_MODEL") or DEFAULT_MODEL,
        "audio_format": "pcm_s16le",
        "sample_rate": SAMPLE_RATE,
        "num_channels": 1,
        "language_hints": [language or "zh"],
        "language_hints_strict": True,
        # 句尾自動定案：使用者停頓就吐 final，不必等 finalize
        "enable_endpoint_detection": True,
    }
    clean = [t for t in (terms or []) if isinstance(t, str) and t.strip()]
    if clean:
        config["context"] = {"terms": clean}
    return config


def pcm_bytes(path: str | Path) -> bytes:
    """16-bit mono wav → 原始 PCM（跳過 44 byte header）。"""
    with wave.open(str(path), "rb") as handle:
        if handle.getsampwidth() != 2:
            raise ValueError("只支援 16-bit wav")
        return handle.readframes(handle.getnframes())


class SonioxStream:
    """一條 WS 連線 = 一次錄音。

    用法（背景執行緒即可）：
        s = SonioxStream(terms=[...])
        s.start()                 # 按下 ⌘E
        s.send(pcm_chunk)         # 錄音中，每個 tap 一塊
        text, conf = s.finish()   # 按停止：送 finalize 並收完 final tokens
    """

    def __init__(self, terms=None, *, language="zh", model=None, timeout=20.0):
        self.terms = list(terms or [])
        self.language = language
        self.model = model
        self.timeout = timeout
        self._ws = None
        self._final: list[str] = []
        self._confidences: list[float] = []
        self._started_at = 0.0
        self._sent_bytes = 0

    # -- 連線 ---------------------------------------------------------------
    def start(self) -> None:
        from websockets.sync.client import connect

        self._started_at = time.perf_counter()
        self._ws = connect(
            WS_URL,
            additional_headers={"Authorization": f"Bearer {api_key()}"},
            max_size=None,
            open_timeout=self.timeout,
        )
        self._ws.send(json.dumps(build_config(self.terms, language=self.language, model=self.model)))

    @property
    def connected(self) -> bool:
        return self._ws is not None

    @property
    def audio_seconds(self) -> float:
        return self._sent_bytes / 2.0 / SAMPLE_RATE

    def send(self, pcm: bytes) -> None:
        if not pcm or self._ws is None:
            return
        # 半個 sample 的尾端會讓串流對不齊，先切齊再送
        if len(pcm) % 2:
            pcm = pcm[:-1]
        self._ws.send(pcm)
        self._sent_bytes += len(pcm)

    # -- 收斂 ---------------------------------------------------------------
    def _drain(self, until_fin: bool) -> None:
        ws = self._ws
        if ws is None:
            raise RuntimeError("Soniox 連線已關閉")
        deadline = time.perf_counter() + self.timeout
        while True:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                raise TimeoutError(f"Soniox 沒有在 {self.timeout:.0f}s 內收完")
            message = ws.recv(timeout=remaining)
            if isinstance(message, bytes):
                continue
            payload = json.loads(message)
            if payload.get("error_code"):
                raise RuntimeError(
                    f"Soniox 錯誤 {payload.get('error_code')}: {payload.get('error_message')}"
                )
            done = False
            for token in payload.get("tokens") or []:
                text = token.get("text") or ""
                if text == "<fin>":
                    done = True
                    continue
                # Soniox 會把句子界線等控制標記當 token 送（例如 <end>）。
                # 這些是模型給客戶端的訊號，不是要貼出去的字。
                if text.startswith("<") and text.endswith(">"):
                    continue
                if token.get("is_final"):
                    self._final.append(text)
                    if token.get("confidence") is not None:
                        self._confidences.append(float(token["confidence"]))
            if done and until_fin:
                return

    def finish(self) -> dict:
        """送 finalize，等模型把剩下的音訊定案，回文字與信心統計。"""
        started = time.perf_counter()
        if self._ws is None:
            return {"text": "", "confidence": None, "elapsed_ms": 0, "audio_s": 0.0}
        try:
            if self._sent_bytes:
                # 文件建議 finalize 前補約 200ms 靜音，準確度最好
                self._ws.send(b"\x00" * (SAMPLE_RATE // 5 * 2))
                self._sent_bytes += SAMPLE_RATE // 5 * 2
            self._ws.send(json.dumps({"type": "finalize"}))
            self._drain(until_fin=True)
        finally:
            self.close()
        text = "".join(self._final).strip()
        confidence = sum(self._confidences) / len(self._confidences) if self._confidences else None
        return {
            "text": text,
            "confidence": round(confidence, 3) if confidence is not None else None,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "audio_s": round(self.audio_seconds, 2),
            "tokens": len(self._final),
        }

    def close(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001 - 收線時的錯誤不影響已收到的文字
                pass


def transcribe_wav(
    path: str | Path,
    *,
    terms=None,
    language="zh",
    model=None,
    timeout=60.0,
    paced=False,
) -> dict:
    """整段 wav 一次送進去（後備/驗證路徑）。paced=True 會用實時速度送，模擬真人講話。"""
    started = time.perf_counter()
    stream = SonioxStream(terms=terms, language=language, model=model, timeout=timeout)
    stream.start()
    try:
        pcm = pcm_bytes(path)
        for offset in range(0, len(pcm), CHUNK_BYTES):
            stream.send(pcm[offset:offset + CHUNK_BYTES])
            if paced:
                time.sleep(0.1)
        result = stream.finish()
    except Exception:
        stream.close()
        raise
    result["total_ms"] = int((time.perf_counter() - started) * 1000)
    return result
