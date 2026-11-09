#!/usr/bin/env python3
"""遠端 ASR 伺服器（跑在模型所在的那台 Mac，例如 M4）。

用途：讓另一台 Mac（例如 M2）的 transcribe.py 不必在本機載入 Whisper／Breeze 模型，
改由這裡常駐的模型做辨識。模型只存在這台機器上，不會在 M2 重複下載、也不佔 M2 記憶體。

協定（stdin／stdout 一行一個 JSON，診斷一律走 stderr）：
  請求：{"audio": "<base64 的 PCM，16 kHz 單聲道>", "fmt": "i2"（int16 LE；省略＝float32 LE，舊客戶端）,
         "language": "zh" | null, "prompt": "<initial_prompt>"}
  回應：{"text": "...", "segments": [...]}   （與 mlx_whisper.transcribe 的結果同形）
        或 {"error": "..."}

啟動方式（由 M2 的 transcribe.py 透過 ssh 拉起，也可以手動測試）：
  <repo>/.venv/bin/python <repo>/asr_server.py

模型路徑讀取順序：環境變數 VOICE_STT_REPO → 本機的 local.env → 預設值。
"""
import base64
import json
import os
import sys
from pathlib import Path

import numpy as np


def _load_stt_repo() -> str:
    """與 transcribe.py 同樣的讀法，但不 import transcribe.py（避免拉進不需要的依賴）。"""
    name = "VOICE_STT_REPO"
    default = "mlx-community/whisper-large-v3-turbo"
    value = os.environ.get(name)
    if not value:
        env_file = Path(__file__).with_name("local.env")
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.split("#", 1)[0].strip()
                if "=" in line:
                    k, v = line.split("=", 1)
                    if k.strip() == name:
                        value = v.strip().strip('"').strip("'")
    return value or default


def main() -> int:
    import mlx_whisper

    repo = _load_stt_repo()
    print(f"[asr-server] 暖機中 STT={repo}", file=sys.stderr, flush=True)
    # 暖機：先跑一段非靜音的雜訊，讓模型真的載入（與 transcribe.py 的 serve 相同做法）
    warm = (np.random.default_rng(0).standard_normal(16000).astype("float32") * 0.02)
    try:
        mlx_whisper.transcribe(warm, path_or_hf_repo=repo, language="zh", verbose=False)
    except Exception as e:  # noqa: BLE001
        print(f"[asr-server] 暖機失敗（不致命）: {e!r}", file=sys.stderr, flush=True)
    print("[asr-server] 就緒，等待請求", file=sys.stderr, flush=True)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            raw = base64.b64decode(req["audio"])
            if req.get("fmt") == "i2":  # int16 PCM（新）
                audio = np.frombuffer(raw, dtype="<i2").astype("float32") / 32768.0
            else:  # float32 PCM（舊客戶端）
                audio = np.frombuffer(raw, dtype="<f4").astype("float32")
            language = req.get("language") or None
            prompt = req.get("prompt") or None
            result = mlx_whisper.transcribe(
                audio,
                path_or_hf_repo=repo,
                language=language,
                verbose=False,
                initial_prompt=prompt,
            )
            out = {
                "text": result.get("text") or "",
                "segments": [
                    {k: (float(v) if isinstance(v, (int, float)) else v)
                     for k, v in seg.items() if k != "tokens"}
                    for seg in (result.get("segments") or [])
                ],
            }
        except Exception as e:  # noqa: BLE001
            print(f"[asr-server] 處理失敗: {e!r}", file=sys.stderr, flush=True)
            out = {"error": repr(e)}
        print(json.dumps(out, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
