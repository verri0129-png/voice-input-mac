#!/usr/bin/env python3
"""遠端 ASR 伺服器（跑在模型所在的那台 Mac，例如 M4）。

用途：讓另一台 Mac（例如 M2）的 transcribe.py 不必在本機載入 Whisper／Breeze 模型，
改由這裡常駐的模型做辨識。模型只存在這台機器上，不會在 M2 重複下載、也不佔 M2 記憶體。

協定（stdin／stdout 一行一個 JSON，診斷一律走 stderr）：
  請求：{"audio": "<base64 的 PCM，16 kHz 單聲道>", "fmt": "i2"（int16 LE；省略＝float32 LE，舊客戶端）,
         "language": "zh" | null, "prompt": "<initial_prompt>",
         "backend": "breeze" | "qwen" | "qwen06" | "octo"（省略＝本機 VOICE_ASR_BACKEND，預設 breeze）,
         "terms": ["熱詞", ...]（qwen 用：空白串成 context；octo 用：hotwords；breeze 用 prompt，不看 terms）}
  回應：{"text": "...", "segments": [...]}   （與 mlx_whisper.transcribe 的結果同形）
        或 {"error": "..."}

啟動方式（由 M2 的 transcribe.py 透過 ssh 拉起，也可以手動測試）：
  <repo>/.venv/bin/python <repo>/asr_server.py            自己載模型
  ... asr_server.py --listen   另開 Unix socket（VOICE_ASR_SOCKET），讓別人共用這份模型（櫃檯小妹的那一份）
  ... asr_server.py --attach   接到 --listen 的那一份；沒有就自己載（⌘E 本機與 M2 經 ssh 都用這個）
  op=llm 請求 {"op": "llm", "system", "user", "max_tokens"}：轉給本機大腦（VOICE_LOCAL_BRAIN_URL）。

設定讀取順序：環境變數 → 本機的 local.env → 預設值。
  VOICE_STT_REPO      Breeze（mlx_whisper）模型路徑
  VOICE_ASR_BACKEND   請求沒指定 backend 時用哪個，預設 octo
  VOICE_ASR_BACKENDS  啟動時要載入哪些，預設 DEFAULT_LOADED（只有 octo；其他用到才載）
  VOICE_QWEN_REPO     Qwen3-ASR 1.7B 模型，預設 Qwen/Qwen3-ASR-1.7B
  VOICE_QWEN06_REPO   Qwen3-ASR 0.6B 模型（後端名 qwen06），預設 Qwen/Qwen3-ASR-0.6B
  VOICE_OCTO_REPO     OctoASR 模型（後端名 octo，預設載入），預設 Mininglamp-2718/OctoASR-1.7B-Instruct-1.0-MLX-8bit
  VOICE_OCTO_VAD_REPO octo 用的 VAD，預設 Mininglamp-2718/fsmn-vad-mlx（兩個都只讀本機 HF cache，不下載）
  VOICE_OCTO_SRC      OctoASR 引擎原始碼（審過、pin 住的 clone，不在這個 repo），預設 ~/orca/vendor/OctoASR
"""
import base64
import json
import os
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np


def _setting(name: str, default: str) -> str:
    """與 transcribe.py 同樣的讀法，但不 import transcribe.py（避免拉進不需要的依賴）。"""
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


def _load_stt_repo() -> str:
    return _setting("VOICE_STT_REPO", "mlx-community/whisper-large-v3-turbo")


def run_backend(engines: dict, default: str, req: dict, audio, builders: dict | None = None):
    """依 req["backend"] 挑引擎辨識。engines: 名稱 → fn(audio, req) -> {"text", "segments"}。
    要求的後端沒載入：有 builders 就當場載入（之後常駐，選單切到沒預載的模型也能用）；
    載不起來或沒給 builders 就回 error（不偷偷換成別的模型，免得使用者以為在比較）。"""
    name = req.get("backend") or default
    engine = engines.get(name)
    if engine is None and builders and name in builders:
        engines.update(load_engines([name], builders, None))
        engine = engines.get(name)
    if engine is None:
        return {"error": f"backend {name!r} 沒載入（有：{sorted(engines)}）"}
    out = engine(audio, req)
    out["backend"] = name
    return out


def _breeze_engine(repo: str):
    import mlx_whisper

    def run(audio, req):
        result = mlx_whisper.transcribe(
            audio, path_or_hf_repo=repo, language=req.get("language") or None,
            verbose=False, initial_prompt=req.get("prompt") or None,
        )
        return {
            "text": result.get("text") or "",
            "segments": [
                {k: (float(v) if isinstance(v, (int, float)) else v)
                 for k, v in seg.items() if k != "tokens"}
                for seg in (result.get("segments") or [])
            ],
        }
    return run


def _qwen_engine(model: str):
    import mlx.core as mx
    from mlx_qwen3_asr import Session

    session = Session(model, dtype=mx.float16)

    def run(audio, req):
        # context 吃空白分隔的熱詞；Qwen 沒有 no_speech_prob／compression_ratio，segments 留空
        ctx = " ".join(t for t in (req.get("terms") or []) if t)
        r = session.transcribe(audio, context=ctx, language=req.get("language") or None)
        return {"text": r.text or "", "segments": []}
    return run


OCTO_SRC_DEFAULT = str(Path.home() / "orca/vendor/OctoASR")


def _local_snapshot(repo: str) -> str:
    """本機資料夾直接用；否則只從 HF cache 找，不上網下載（缺模型就大聲失敗）。"""
    if Path(repo).expanduser().is_dir():
        return str(Path(repo).expanduser())
    from huggingface_hub import snapshot_download
    return snapshot_download(repo, local_files_only=True)


def _octo_engine(repo: str):
    """OctoASR（Qwen3-ASR 架構微調，MLX 8bit）＋ fsmn-vad。引擎原始碼在 VOICE_OCTO_SRC，用到才 import，
    其他後端不受影響。引擎只吃檔案路徑，所以每次寫成暫存 wav、辨識完就刪。
    回傳原文（可能是簡體）：跟 qwen 一樣，繁體化與修字表由客戶端（transcribe.py／duplex fix_text）做。"""
    src = _setting("VOICE_OCTO_SRC", OCTO_SRC_DEFAULT)
    if src not in sys.path:
        sys.path.insert(0, src)  # 引擎的頂層套件叫 core／utils，放最前面才不會被同名套件搶走
    from core.auto_model import AutoModel

    model = AutoModel(model=_local_snapshot(repo),
                      vad_model=_local_snapshot(_setting("VOICE_OCTO_VAD_REPO", "Mininglamp-2718/fsmn-vad-mlx")))
    return _octo_runner(model)


def _octo_runner(model):
    def run(audio, req):
        # language 不傳：引擎要的是 "Chinese" 這種名稱，傳 "zh" 會被原樣塞進 prompt；自動偵測已實測可用
        terms = [t for t in (req.get("terms") or []) if t]
        pcm = (np.clip(np.asarray(audio, dtype="float32"), -1.0, 1.0) * 32767.0).astype("<i2")
        fd, path = tempfile.mkstemp(prefix="octo-", suffix=".wav")
        os.close(fd)
        try:
            with wave.open(path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes(pcm.tobytes())
            r = model.generate(path, hotwords=terms or None, merge_vad=True)
        finally:
            os.unlink(path)
        text = r.get("text", "") if isinstance(r, dict) else r
        return {"text": text or "", "segments": []}
    return run


def handle_request(req: dict, buf: bytearray):
    """解一個請求。回傳 (回應, 要辨識的音訊)；音訊為 None 代表不用跑模型，直接回「回應」。

    op=append：邊錄邊傳的增量；offset（樣本數）必須等於已收到的長度，否則回 error=offset，
               客戶端會改送整段。offset=0 代表新的一段錄音，先清空。
    op=final ：停止後只補尾巴；offset 對得上才把 buf + 尾巴 當整段辨識。
    其他     ：整段音訊（舊行為）；順便清空 buf，避免殘留。
    """
    op = req.get("op")
    raw = base64.b64decode(req["audio"]) if req.get("audio") else b""
    if req.get("fmt") == "i2":  # int16 PCM
        pcm = raw
    else:  # float32 PCM（舊客戶端）→ 只能是整段
        buf.clear()
        return None, np.frombuffer(raw, dtype="<f4").astype("float32")
    if op == "append" and req.get("offset") == 0:
        buf.clear()
    have = len(buf) // 2
    if op in ("append", "final") and req.get("offset") != have:
        return {"error": "offset", "have": have}, None  # transcribe.py 以這個字串判斷要不要改送整段
    if op == "append":
        buf += pcm
        return {"ok": True, "len": have + len(pcm) // 2}, None
    if op == "final":
        pcm = bytes(buf) + pcm
    buf.clear()
    return None, np.frombuffer(pcm, dtype="<i2").astype("float32") / 32768.0


def load_engines(names, builders, warm):
    """依序建立後端並暖機。建構成功就登記；暖機失敗只記 log（與舊版「暖機失敗不致命」一致，
    mlx_whisper 會在第一個請求時再載入）。建構失敗（例如 Qwen 權重載不進來）才不登記，
    之後要求該後端會得到明確的 error。"""
    engines = {}
    for name in names:
        print(f"[asr-server] 暖機中 backend={name}", file=sys.stderr, flush=True)
        try:
            if name not in builders:
                raise ValueError(f"不認得的 backend {name!r}（只有 {sorted(builders)}）")
            engines[name] = builders[name]()
        except Exception as e:  # noqa: BLE001
            print(f"[asr-server] backend={name} 載入失敗: {e!r}", file=sys.stderr, flush=True)
            continue
        if warm is None:  # 用到才載入（run_backend）：不暖機，這次請求本身就是第一次推論
            continue
        try:
            # 先跑一段非靜音的雜訊，讓權重真的進記憶體（與 transcribe.py 的 serve 相同做法）
            engines[name](warm, {"language": "zh"})
        except Exception as e:  # noqa: BLE001
            print(f"[asr-server] backend={name} 暖機失敗（不致命）: {e!r}", file=sys.stderr, flush=True)
    return engines


# 預設只載 octo（11-16 決定：⌘E 與櫃檯小妹共用 OctoASR）。選單的其他值（qwen06、qwen、breeze）
# 檔案留著、第一次被要求時才載入（run_backend 的 builders），平常不佔記憶體。
DEFAULT_LOADED = "octo"


def _builders() -> dict:
    return {
        "breeze": lambda: _breeze_engine(_load_stt_repo()),
        "qwen": lambda: _qwen_engine(_setting("VOICE_QWEN_REPO", "Qwen/Qwen3-ASR-1.7B")),
        "qwen06": lambda: _qwen_engine(_setting("VOICE_QWEN06_REPO", "Qwen/Qwen3-ASR-0.6B")),
        "octo": lambda: _octo_engine(_setting("VOICE_OCTO_REPO", "Mininglamp-2718/OctoASR-1.7B-Instruct-1.0-MLX-8bit")),
    }


LLM_URL_DEFAULT = "http://127.0.0.1:7871/complete"


def llm_proxy(req: dict, post=None) -> dict:
    """op=llm：把改寫請求轉給本機大腦（localllm_server 的 /complete）。給 M2 用：M2 經這條 ssh 通道
    借 M4 的大腦，不另開連線。回應 {"text"} 或 {"error"}；大腦忙、沒開、逾時都回 error，客戶端退回雲端。"""
    import urllib.request
    body = {k: req[k] for k in ("system", "user", "max_tokens", "wait_s", "timeout_s") if k in req}
    timeout = float(req.get("timeout") or 30)
    url = _setting("VOICE_LOCAL_BRAIN_URL", LLM_URL_DEFAULT)
    try:
        if post is not None:
            return post(url, body, timeout)
        r = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception as e:  # noqa: BLE001
        return {"error": f"local brain: {e!r}"[:300]}


def serve_line(line: str, buf: bytearray, recognise) -> dict:
    """一行請求 → 一個回應。recognise(req, audio) 跑模型（listen 模式下排進主執行緒）。"""
    try:
        req = json.loads(line)
        if req.get("op") == "llm":
            return llm_proxy(req)
        out, audio = handle_request(req, buf)
        return recognise(req, audio) if audio is not None else out
    except Exception as e:  # noqa: BLE001
        print(f"[asr-server] 處理失敗: {e!r}", file=sys.stderr, flush=True)
        return {"error": repr(e)}


SOCKET_DEFAULT = "~/orca/state/voice-input/asr.sock"


def socket_path() -> Path:
    return Path(_setting("VOICE_ASR_SOCKET", SOCKET_DEFAULT)).expanduser()


def _socket_alive(path: Path) -> bool:
    import socket
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(str(path))
        return True
    except OSError:
        return False
    finally:
        s.close()


def open_listener(path: Path):
    """綁 Unix socket（0600，只有本人能連）。已經有活的伺服器在聽就不搶（例如私有埠的測試小妹），回 None。"""
    import socket
    if path.exists():
        if _socket_alive(path):
            print(f"[asr-server] {path} 已有別的伺服器在聽，這個程序不共用", file=sys.stderr, flush=True)
            return None
        path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o177)
    try:
        srv.bind(str(path))
    finally:
        os.umask(old)
    srv.listen(4)
    return srv


def accept_loop(srv, recognise) -> None:
    """每條連線一個執行緒、各自的邊錄邊傳緩衝區；辨識一律交給 recognise（主執行緒）。"""
    import threading

    def client(conn):
        try:
            with conn, conn.makefile("rwb") as f:
                buf = bytearray()
                for raw in f:
                    line = raw.decode("utf-8", "replace").strip()
                    if line:
                        f.write((json.dumps(serve_line(line, buf, recognise), ensure_ascii=False) + "\n").encode())
                        f.flush()
        except OSError:  # 客戶端中途斷線（App 結束、ssh 斷）：只結束這條連線
            pass

    while True:
        try:
            conn, _ = srv.accept()
        except OSError:
            return
        threading.Thread(target=client, args=(conn,), daemon=True).start()


def attach(path: Path) -> int | None:
    """--attach：把 stdin／stdout 接到常駐伺服器的 socket（M4 只留一份模型）。連不上回 None，呼叫端自己載模型。"""
    import socket
    import threading
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(str(path))
    except OSError:
        s.close()
        return None
    print(f"[asr-server] 接上常駐伺服器 {path}（不另外載模型）", file=sys.stderr, flush=True)

    def up():
        try:
            for line in sys.stdin.buffer:
                s.sendall(line)
        except OSError:
            pass
        try:
            s.shutdown(socket.SHUT_WR)
        except OSError:
            pass

    threading.Thread(target=up, daemon=True).start()
    out = sys.stdout.buffer
    with s.makefile("rb") as f:
        for line in f:
            out.write(line)
            out.flush()
    # 伺服器先斷時 up 還卡在讀 stdin、握著它的鎖；正常收尾會 abort（macOS 跳「Python 未預期的結束」），所以直接離開
    sys.stderr.flush()
    os._exit(0)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--attach" in argv:
        rc = attach(socket_path())
        if rc is not None:
            return rc
        print("[asr-server] 沒有常駐伺服器，自己載模型", file=sys.stderr, flush=True)
    default = _setting("VOICE_ASR_BACKEND", "octo")
    wanted = [n.strip() for n in _setting("VOICE_ASR_BACKENDS", DEFAULT_LOADED).split(",") if n.strip()]
    if default not in wanted:
        wanted.append(default)
    warm = (np.random.default_rng(0).standard_normal(16000).astype("float32") * 0.02)
    builders = _builders()
    engines = load_engines(wanted, builders, warm)

    def run(req, audio):
        return run_backend(engines, default, req, audio, builders)

    if "--listen" not in argv:
        print(f"[asr-server] 就緒，等待請求（預設={default}，已載入={sorted(engines)}）", file=sys.stderr, flush=True)
        buf = bytearray()  # 邊錄邊傳：已收到的 int16 PCM（跨請求保留，final 或整段請求時清空）
        for line in sys.stdin:
            if line.strip():
                print(json.dumps(serve_line(line, buf, run), ensure_ascii=False), flush=True)
        return 0
    return serve_shared(engines, default, run)


def serve_shared(engines, default, run) -> int:
    """--listen：stdin（擁有者，例如櫃檯小妹）之外再開 Unix socket 給 ⌘E 與 M2 共用同一份模型。
    MLX 的模型只在載入它的執行緒用：所有辨識排進佇列由主執行緒跑；stdin 關掉（擁有者結束）就整個結束。"""
    import queue
    import threading
    jobs: queue.Queue = queue.Queue()

    def recognise(req, audio):
        done: queue.Queue = queue.Queue(1)
        jobs.put((req, audio, done))
        return done.get()

    path = socket_path()
    srv = open_listener(path)
    mine = path.stat().st_ino if srv is not None else None
    if srv is not None:
        threading.Thread(target=accept_loop, args=(srv, recognise), daemon=True).start()

    def owner():
        buf = bytearray()
        for line in sys.stdin:
            if line.strip():
                print(json.dumps(serve_line(line, buf, recognise), ensure_ascii=False), flush=True)
        jobs.put(None)

    threading.Thread(target=owner, daemon=True).start()
    print(f"[asr-server] 就緒，等待請求（預設={default}，已載入={sorted(engines)}，"
          f"共用 socket={path if srv is not None else '無'}）", file=sys.stderr, flush=True)
    try:
        while (job := jobs.get()) is not None:
            req, audio, done = job
            try:
                done.put(run(req, audio))
            except Exception as e:  # noqa: BLE001
                print(f"[asr-server] 處理失敗: {e!r}", file=sys.stderr, flush=True)
                done.put({"error": repr(e)})
    finally:
        if srv is not None:
            srv.close()
            try:
                if path.stat().st_ino == mine:  # 只刪自己綁的那個
                    path.unlink()
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
