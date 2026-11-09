"""Pipe: record or file -> local Breeze ASR -> cloud rewrite -> clipboard or stdout."""
import argparse, base64, difflib, hashlib, subprocess, sys, tempfile, os, time, re, threading, unicodedata, json
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from rewrite_cloud import (
    RewriteConfig,
    RewriteRejected,
    RewriteResult,
    infer_term_candidates,
    learn_insertion_edits,
    load_term_rules,
    merge_term_candidates,
    rewrite_draft,
    sanitize_context,
)

# Ensure Homebrew ffmpeg is found under non-interactive ssh (PATH is /usr/bin:/bin only)
for _p in ("/opt/homebrew/bin", "/usr/local/bin"):
    if _p not in os.environ.get("PATH", "") and Path(_p, "ffmpeg").exists():
        os.environ["PATH"] = _p + ":" + os.environ.get("PATH", "")

# --- per-machine settings -----------------------------------------------------
# The two machines deliberately run DIFFERENT models (M4 is a 64GB studio that
# can afford bigger ones; M2 is a 16GB mini). Model choice therefore must NOT
# travel with the code: the values below are only defaults, and each machine
# overrides them in an untracked `local.env` next to this file (or via env vars
# of the same names). Never copy local.env between machines.
#
#   local.env example:
#       VOICE_STT_REPO=/Users/hunghanchu/orca/voice-input-mac/models/breeze-asr-25-mlx-4bit
#       VOICE_REWRITE_BACKEND=cloud
#       VOICE_CLOUD_REWRITE_MODEL=gemini-3.8-flash
#       VOICE_CLOUD_REWRITE_LOCATION=global
#       VOICE_INPUT_DEVICE=SOUNDPEATS        # optional; default = system default
#
def _local_env():
    cfg = {}
    p = Path(__file__).with_name("local.env")
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg

_LOCAL = _local_env()

def _setting(name, default):
    return os.environ.get(name) or _LOCAL.get(name) or default

STT_REPO = _setting("VOICE_STT_REPO", "mlx-community/whisper-large-v3-turbo")

# ── 遠端 ASR：模型只放在另一台機器（例如 M4），本機（例如 M2）不載入、不佔記憶體 ──
# 設定 VOICE_STT_REMOTE=m4（ssh 別名）即啟用；留空就維持原本的本地模型。
# 遠端那台要有同一份 repo（含 asr_server.py）與 .venv（含 mlx_whisper、mlx_qwen3_asr）。
# VOICE_STT_REMOTE_BACKEND=breeze|qwen 選遠端用哪個模型（留空＝由遠端的 VOICE_ASR_BACKEND 決定，預設 breeze）。
# 改完要重開 App（或等暖機程序閒置結束）才會生效；兩個模型都常駐在遠端，切換不必重啟遠端。
STT_REMOTE_HOST = (_setting("VOICE_STT_REMOTE", "") or "").strip()
STT_REMOTE_PY = _setting("VOICE_STT_REMOTE_PY", "~/orca/voice-input-mac/.venv/bin/python")
STT_REMOTE_SCRIPT = _setting("VOICE_STT_REMOTE_SCRIPT", "~/orca/voice-input-mac/asr_server.py")
STT_REMOTE_BACKEND = (_setting("VOICE_STT_REMOTE_BACKEND", "") or "").strip().lower()
# 選單列「ASR 模型」寫的小檔（內容 breeze 或 qwen）。每次辨識都重讀，所以切換立即生效、不用重啟；
# 檔案不存在或內容不認得就退回上面的設定。
ASR_BACKEND_FILE = Path(_setting("VOICE_ASR_BACKEND_FILE", "~/orca/state/voice-input/asr-backend")).expanduser()
ASR_BACKENDS = ("breeze", "qwen")

def current_remote_backend() -> str:
    try:
        chosen = ASR_BACKEND_FILE.read_text(encoding="utf-8").strip().lower()
    except OSError:
        chosen = ""
    return chosen if chosen in ASR_BACKENDS else STT_REMOTE_BACKEND
# Qwen 的熱詞太多會整段回吐（字幕專案實測），所以設上限
STT_QWEN_MAX_TERMS = int(_setting("VOICE_STT_QWEN_MAX_TERMS", "40"))
if STT_REMOTE_BACKEND and not STT_REMOTE_HOST:
    print(f"[remote-stt] ⚠ 設了 VOICE_STT_REMOTE_BACKEND={STT_REMOTE_BACKEND} 但沒設 VOICE_STT_REMOTE："
          "只有遠端模式才會選模型，現在用的是本機模型", file=sys.stderr)
_REMOTE_PROC = None
_REMOTE_LOCK = threading.Lock()

def _remote_proc():
    """長駐一條 ssh 連線（遠端的模型因此只載入一次）；斷線時自動重連。"""
    global _REMOTE_PROC
    if _REMOTE_PROC is None or _REMOTE_PROC.poll() is not None:
        cmd = f"{STT_REMOTE_PY} {STT_REMOTE_SCRIPT}"
        print(f"[remote-stt] 連線 {STT_REMOTE_HOST}（模型在遠端，本機不載入）", file=sys.stderr)
        _REMOTE_PROC = subprocess.Popen(
            ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=30", STT_REMOTE_HOST, cmd],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1,
        )
    return _REMOTE_PROC

def _pcm16(samples):
    """wav_samples 給的是 int16/32768；乘回去再四捨五入，無損。"""
    import numpy as np
    return np.clip(np.rint(np.asarray(samples, dtype="float32") * 32768.0), -32768, 32767).astype("<i2")

def _remote_call(req):
    """送一行 JSON、讀一行回應（呼叫端要先拿著 _REMOTE_LOCK）。斷線就重連並丟 RuntimeError。"""
    global _REMOTE_PROC
    line = ""
    try:
        proc = _remote_proc()
        proc.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
    except (OSError, ValueError) as e:
        print(f"[remote-stt] 通訊失敗: {e!r}", file=sys.stderr)
    if not line:
        if _REMOTE_PROC is not None:
            try:
                _REMOTE_PROC.kill()
            except OSError:
                pass
        _REMOTE_PROC = None
        _stream_reset()  # 遠端緩衝區跟著沒了
        raise RuntimeError(f"遠端 ASR 無回應（確認 ssh {STT_REMOTE_HOST} 可用，且遠端有 asr_server.py）")
    return json.loads(line)

def _b64(pcm):
    return base64.b64encode(pcm.tobytes()).decode("ascii")

# 邊錄邊傳：上行頻寬（M2→M4 約 130–180 KB/s）是瓶頸，停止後才一次傳整段，錄越久等越久。
# 錄音中每次預覽 tick 只傳「上次之後新增」的樣本，M4 先存著；停止時只補尾巴。
# 正確性靠內容比對：n＝已送出的樣本數、digest＝那段前綴的 sha1；
# 最終音訊的前 n 個樣本 sha1 對得上才走增量，否則（新錄音、重連、別的呼叫）整段重傳。
_STREAM = {"n": 0, "digest": None}

def _sha(pcm):
    return hashlib.sha1(memoryview(pcm)).digest()

def _stream_reset():
    _STREAM["n"], _STREAM["digest"] = 0, None

def _streamed_prefix(pcm):
    """已經傳到 M4 的前綴有幾個樣本（對不上＝0）。呼叫端要先拿著 _REMOTE_LOCK。"""
    n = _STREAM["n"]
    return n if n and n <= len(pcm) and _sha(pcm[:n]) == _STREAM["digest"] else 0

def remote_stream_append(pcm):
    """錄音中呼叫：pcm 是「到目前為止」的整段 int16 音訊；只把新增的部分傳到 M4。"""
    with _REMOTE_LOCK:
        n = _streamed_prefix(pcm)  # 0＝新錄音或對不上，從頭來（offset=0 會讓 M4 清掉舊的）
        if n and len(pcm) == n:
            return
        resp = _remote_call({"op": "append", "fmt": "i2", "audio": _b64(pcm[n:]), "offset": n})
        if resp.get("ok"):
            _STREAM["n"], _STREAM["digest"] = len(pcm), _sha(pcm)
        else:
            _stream_reset()

def _remote_whisper(samples, language, prompt):
    """把 16 kHz 音訊送到遠端 asr_server.py，回傳與 mlx_whisper 同形的結果。
    錄音中已經預先傳過的前綴不重傳（見 _STREAM）。"""
    pcm = _pcm16(samples)
    base = {"fmt": "i2", "language": language, "prompt": prompt}
    backend = current_remote_backend()
    if backend:
        base["backend"] = backend
        base["terms"] = stt_terms()
    with _REMOTE_LOCK:
        n = _streamed_prefix(pcm)
        _stream_reset()  # 無論成敗，M4 端的緩衝區這次都會被清掉
        resp = None
        print(f"[remote-stt] 送出 {'增量：已先傳 %d 樣本，只補 %d' % (n, len(pcm) - n) if n else '整段 %d' % len(pcm)} 樣本",
              file=sys.stderr)
        if n:
            resp = _remote_call(dict(base, op="final", audio=_b64(pcm[n:]), offset=n))
            if resp.get("error") == "offset":
                print("[remote-stt] 增量對不上 → 改送整段", file=sys.stderr)
                resp = None
        if resp is None:
            resp = _remote_call(dict(base, audio=_b64(pcm)))
    if "error" in resp:
        raise RuntimeError(f"遠端 ASR 失敗：{resp['error']}")
    got = resp.get("backend")  # 舊版伺服器不回報
    print(f"[remote-stt] 遠端用的模型：{got or '未回報（遠端還是舊版）'}", file=sys.stderr)
    if backend and got != backend:
        # 不偷偷用別的模型：試用期間若悄悄跑 Breeze，使用者會把結果當成 Qwen 的
        raise RuntimeError(f"要求 {backend} 但遠端是 {got or '舊版（忽略了 backend）'}："
                           f"M4 的 asr_server.py 需要更新並重啟（git pull ＋ 結束舊的暖機程序）")
    # Qwen 以 zh 輸出簡體；詞庫替換在 rewrite 階段（早於最後的繁化），詞庫變體是繁體寫法，
    # 所以在遠端結果的邊界就繁化，所有後端走同一條路（Breeze 本來就是繁體，等於 no-op）。
    if resp.get("text"):
        resp["text"] = to_taiwan_traditional(resp["text"])
    return resp

def _whisper(language, audio=None, path=None):
    """統一的 Whisper 入口：遠端啟用時送到 M4；否則用本機模型（行為與原本相同）。"""
    prompt = stt_initial_prompt()
    if STT_REMOTE_HOST:
        if audio is None and path:
            audio = wav_samples(path)
        if audio is None:
            raise RuntimeError("遠端 ASR 需要可讀取的音訊")
        return _remote_whisper(audio, language, prompt)
    import mlx_whisper
    src = path if path is not None else audio
    return mlx_whisper.transcribe(src, path_or_hf_repo=STT_REPO, language=language,
                                  verbose=False, initial_prompt=prompt)

# ── STT provider ──────────────────────────────────────────────────────────────
# breeze（預設）：本地 Breeze-ASR-25，完全離線。
# soniox        ：Soniox 即時 API（雲端），自帶標點，停止後幾乎不用等；
#                 代價是每句音訊離機、斷網不能用。切回只要把 local.env 的
#                 VOICE_STT 改回 breeze（兩條路線的程式都保留）。
STT_PROVIDER = (_setting("VOICE_STT", "breeze") or "breeze").strip().lower()
# Soniox 認得幾個詞就吃幾個（context 上限 8000 token，比 Whisper 的 224 token 寬鬆）
SONIOX_MAX_TERMS = int(_setting("VOICE_SONIOX_MAX_TERMS", "120"))
# Whisper 的 initial_prompt：實測能修掉同音字誤判（「逗號」被聽成「痘号」），
# 而且因為寫了「台灣中文」，輸出傾向繁體（OpenCC 那道仍然保留當保險）。
STT_INITIAL_PROMPT = _setting(
    "VOICE_STT_INITIAL_PROMPT",
    "以下是台灣中文的語音轉文字。標點符號：逗號、句號、問號、驚嘆號、頓號、引號、分號。"
    "常見詞：錄音、音波震動、語音轉文字、麥克風、快捷鍵、標點、潤飾。",
)
# Whisper 的 initial_prompt 就是它的熱詞通道（同一個音「瓷庫／詞庫」，prompt 裡出現過
# 正確寫法就傾向寫對）。但它的上下文只有 ~224 token，塞太多會擠掉音訊本身，
# 所以這裡硬性截在 200 字以內，只放詞庫的「正字」（不放變體）。
WHISPER_PROMPT_CHARS = 200


def stt_initial_prompt(terms_file: str | None = None) -> str:
    """固定提示 + 個人詞庫正字。詞庫為空或讀不到時回傳原本的固定提示。"""
    base = STT_INITIAL_PROMPT.strip()
    try:
        rules = load_term_rules(terms_file or REWRITE_TERMS_FILE)
    except Exception:  # noqa: BLE001 - 提示字串壞掉不該擋住辨識
        return base
    words = list(dict.fromkeys(rule.text.strip() for rule in rules if rule.text.strip()))
    if not words:
        return base
    head = "專有名詞："
    room = WHISPER_PROMPT_CHARS - len(base) - len(head)
    if room <= 2:
        return base
    picked: list[str] = []
    used = 0
    for word in words:
        if used + len(word) + 1 > room:
            break
        picked.append(word)
        used += len(word) + 1
    return base + head + "、".join(picked) if picked else base


def stt_terms(terms_file: str | None = None) -> list[str]:
    """給 Qwen 的熱詞（純詞，不含 Whisper 那種提示句）。取詞庫的正字，
    跳過明顯是句子片段的條目（含標點或太長）——詞庫被「手動修正學習」塞進片段時，
    Qwen 會把它們當詞去聽，比 Whisper 更吃虧。"""
    try:
        rules = load_term_rules(terms_file or REWRITE_TERMS_FILE)
    except Exception:  # noqa: BLE001
        return []
    bad = set("，。,.？?！!、；;：:（）()「」\"'")
    words = [r.text.strip() for r in rules if r.text.strip()]
    words = [w for w in words if len(w) <= 16 and not (set(w) & bad)]
    return list(dict.fromkeys(words))[:STT_QWEN_MAX_TERMS]


REWRITE_BACKEND = _setting("VOICE_REWRITE_BACKEND", "cloud")
CLOUD_REWRITE_MODEL = _setting("VOICE_CLOUD_REWRITE_MODEL", "gemini-3.8-flash")
CLOUD_REWRITE_LOCATION = _setting("VOICE_CLOUD_REWRITE_LOCATION", "global")
CLOUD_REWRITE_PROJECT = _setting("VOICE_CLOUD_REWRITE_PROJECT", "")
CLOUD_REWRITE_TIMEOUT = float(_setting("VOICE_CLOUD_REWRITE_TIMEOUT", "8"))
CLOUD_REWRITE_MAX_OUTPUT_TOKENS = int(_setting("VOICE_CLOUD_REWRITE_MAX_OUTPUT_TOKENS", "1024"))
CLOUD_REWRITE_MAX_TERMS = int(_setting("VOICE_CLOUD_REWRITE_MAX_TERMS", "60"))
# 思考階段是延遲的主要來源（實測 thoughts 325–980 token）。0 = 盡量關掉；
# 設成 none 則完全不送 thinkingConfig。這顆模型不一定聽話，finishReason 仍會檢查。
_RAW_THINKING = _setting("VOICE_CLOUD_THINKING_BUDGET", "0").strip().lower()
CLOUD_REWRITE_THINKING = (
    None if _RAW_THINKING in ("none", "off", "") else int(_RAW_THINKING)
)
# Priority PayGo：預設開（使用者 2026-10-27 決定），設 0 就回標準池。
CLOUD_REWRITE_PRIORITY = _setting("VOICE_CLOUD_PRIORITY", "1").strip().lower() not in (
    "0",
    "false",
    "off",
    "no",
    "",
)
# Typeless 式定版：先貼規則版，雲端在背景重寫，完成後由 App 原地替換。
# 這是「貼上之後最多等多久才定版」的上限，不再擋住貼字。
REFINE_WAIT_SECONDS = float(_setting("VOICE_REFINE_WAIT", "8"))
REWRITE_TERMS_FILE = _setting(
    "VOICE_REWRITE_TERMS_FILE", str(Path(__file__).with_name("rewrite_terms.json"))
)
# --- 背景第二意見（second opinion）------------------------------------------
# 停止後貼上的路徑完全不變；這裡只是把同一段音訊**再**送一次 Vertex 的專用轉錄模型
# （gemini-3.5-transcribe），比對它跟本地 Breeze 聽到的哪裡不一樣，寫進 JSONL。
# 只記錄、不覆蓋任何已貼上的字（第一版只量不一致率，還不是決策依據）。
# 代價：**音訊會離開這台機器**（上傳到使用者的 Vertex 專案）；文字原本就會送。
# VOICE_SECOND_OPINION=0 可整條關掉。
SECOND_OPINION = _setting("VOICE_SECOND_OPINION", "1").strip().lower() not in (
    "0", "off", "no", "false", "",
)
SECOND_OPINION_MODEL = _setting("VOICE_SECOND_OPINION_MODEL", "gemini-3.5-transcribe")
SECOND_OPINION_TIMEOUT = float(_setting("VOICE_SECOND_OPINION_TIMEOUT", "20"))
# 太長的不送：轉錄模型音檔上限 1 小時，但「口述」沒必要上傳十分鐘的音訊。
SECOND_OPINION_MAX_SECONDS = float(_setting("VOICE_SECOND_OPINION_MAX_SECONDS", "180"))
SECOND_OPINION_LOG = _setting(
    "VOICE_SECOND_OPINION_LOG", str(Path(__file__).with_name(".second-opinion.jsonl"))
)
# 等定版檔多久（只為了「標點比對」有第三個對象；等不到就只比本地與雲端）。
SECOND_OPINION_REFINE_WAIT = float(_setting("VOICE_SECOND_OPINION_REFINE_WAIT", "8"))
DEFAULT_INPUT_DEVICE = _setting("VOICE_INPUT_DEVICE", None)
SAMPLE_RATE = 16000

def rms_db(samples):
    import numpy as np
    rms = (samples**2).mean() ** 0.5 if len(samples) else 0
    return 20 * __import__("math").log10(rms + 1e-9)

def is_silence(samples, min_duration_s=0.3):
    """沒收到聲音嗎？（Whisper 前先擋掉，免得幻覺被當成逐字稿）

    門檻全部是量出來的（M4 + MixPre-6 II，訊號只出現在第 2 聲道，16k 混音後）：
      全 0（修好之前的 bug）             peak -180 dB
      房間穩定底噪（實錄 5.9 秒）        peak -29 dB、窗 rms -45 dB、窗起伏只有 6 dB
      真人說話                          peak 明顯更高、且有抑揚（窗起伏 20 dB 以上）

    三條規則（任一成立就算沒收到聲音）：
      1. 幾乎沒有能量          peak < -35 且 窗 rms < -48
      2. 有音量但完全平的包絡  peak < -25 且 窗 RMS 起伏 < 8 dB  ← 穩定底噪/風扇聲
    第 2 條是關鍵：房間底噪（-45 dB）比「死掉的輸入」大得多，會穿過第 1 條，
    而 Whisper 對這種底噪的幻覺是「高信心」的（實測 no_speech_prob=0.000、
    avg_logprob=-0.10），信心分數救不了。人聲有抑揚、底噪沒有，用這個分。
    真人說話只要夠大聲（peak > -25）就完全不受規則 2 影響。
    """
    if len(samples) < SAMPLE_RATE * min_duration_s:
        return True
    peak_db, window_db = audio_levels(samples)
    if peak_db < SILENCE_PEAK_DB and window_db < SILENCE_WINDOW_DB:
        return True
    if peak_db < FLAT_NOISE_PEAK_DB and envelope_range_db(samples) < FLAT_NOISE_RANGE_DB:
        return True
    return False

# 見 is_silence() 的實測說明
SILENCE_PEAK_DB = -35.0
SILENCE_WINDOW_DB = -48.0
FLAT_NOISE_PEAK_DB = -25.0
FLAT_NOISE_RANGE_DB = 8.0

def envelope_range_db(samples, sr=SAMPLE_RATE, window_s=0.5):
    """逐窗 RMS 的（最大 - 最小）dB。人聲抑揚大，穩定底噪幾乎是平的。"""
    import numpy as np
    a = np.asarray(samples, dtype="float32")
    w = max(1, int(sr * window_s))
    n = a.size // w
    if n < 3:
        return 999.0
    frames = a[: n * w].astype("float64").reshape(n, w)
    db = 10 * np.log10((frames ** 2).mean(axis=1) + 1e-18)
    return float(db.max() - db.min())

def audio_levels(samples, sr=SAMPLE_RATE, window_s=0.5):
    """回傳 (peak_db, 最大 window_s 秒窗 rms_db)。"""
    import numpy as np
    a = np.asarray(samples, dtype="float32")
    if a.size == 0:
        return (-180.0, -180.0)
    peak_db = float(20 * np.log10(float(np.abs(a).max()) + 1e-9))
    return (peak_db, speech_level_db(a, sr, window_s))

def speech_level_db(samples, sr=SAMPLE_RATE, window_s=0.5):
    """最大 window_s 秒窗的 RMS（dBFS）；空陣列回 -180。"""
    import numpy as np
    a = np.asarray(samples, dtype="float32")
    if a.size == 0:
        return -180.0
    w = max(1, int(sr * window_s))
    if a.size <= w:
        return rms_db(a)
    n = a.size // w
    frames = a[: n * w].astype("float64").reshape(n, w)
    powers = (frames ** 2).mean(axis=1)
    return float(10 * np.log10(float(powers.max()) + 1e-18))

def frame_db(samples, sr=SAMPLE_RATE, frame_s=0.02):
    """逐框 RMS（dBFS）陣列，預設 20ms 框。"""
    import numpy as np
    a = np.asarray(samples, dtype="float32")
    w = max(1, int(sr * frame_s))
    n = a.size // w
    if n == 0:
        return np.zeros(0, dtype="float64")
    frames = a[: n * w].astype("float64").reshape(n, w)
    return 10 * np.log10((frames ** 2).mean(axis=1) + 1e-18)

def speech_activity(samples, sr=SAMPLE_RATE, frame_s=0.02,
                    floor_db=-55.0, rel_db=8.0):
    """粗糙 VAD（不需要外部套件）：回傳 (active_ratio, longest_run_s, floor_db)。

    噪音底 = 逐 20ms 框 RMS 的第 10 百分位；框超過 max(底 + rel_db, floor_db)
    算 active。真人說話會在時間軸留下連續的 active 段；穩定底噪/風扇聲則是
    要嘛幾乎沒框超過（因為「底+8dB」自己就在噪音的波動範圍之上），要嘛完全平。
    """
    import numpy as np
    db = frame_db(samples, sr, frame_s)
    if db.size == 0:
        return (0.0, 0.0, -180.0)
    floor = float(np.percentile(db, 10))
    thresh = max(floor + rel_db, floor_db)
    active = db > thresh
    ratio = float(active.mean())
    best = cur = 0
    for v in active:
        cur = cur + 1 if v else 0
        best = max(best, cur)
    return (ratio, best * frame_s, floor)

# 「疑似不是語音」的票數門檻：單一訊號不夠可靠（可能誤殺真人），兩票才攔。
SUSPECT_VOTES_REQUIRED = 2
SUSPECT_ACTIVE_RATIO = 0.08
SUSPECT_RUN_S = 0.25

def is_fabrication(raw, final):
    """潤飾模型自己編內容嗎？

    實測（2026-10-17）：對 8 秒粉紅噪音，Whisper 只吐出 5 個字「暗示及燈」，
    Qwen 潤飾卻把它擴寫成 95 字的完整段落（燈光設計、舞臺劇…）。潤飾只該補標點
    與刪贅字，長度不該暴增——暴增就是模型在編。這種情況直接算「不只一票」。
    """
    if not raw or not final:
        return None
    limit = max(3 * len(raw), len(raw) + 40)
    if len(final) > limit:
        return f"潤飾後膨脹 {len(raw)}→{len(final)} 字（超過 {limit}，模型自己在編內容）"
    return None

def suspect_reason(samples, result=None):
    """回傳「疑似不是語音」的票數清單；空清單 = 看起來像語音。

    為什麼要做這個：Whisper 對「房間噪音」的幻覺是**高信心**的（實測全 0 音檔
    no_speech_prob=0.000、avg_logprob=-0.10），所以「文字看起來怪」擋不住；
    能擋的只有「這段音訊根本沒有人說話的形狀」。這裡把音訊面的證據做成投票，
    兩票以上才判可疑 —— 判可疑時 app 不自動貼上，但文字會被保留（可手動貼），
    所以誤殺的代價只有一次點選，不會讓真人說話變成亂貼。
    """
    votes = []
    ratio, run_s, floor_db = speech_activity(samples)
    if ratio < SUSPECT_ACTIVE_RATIO:
        votes.append(f"active_ratio={ratio:.3f}<{SUSPECT_ACTIVE_RATIO}（幾乎沒有超過底噪的框）")
    if run_s < SUSPECT_RUN_S:
        votes.append(f"最長連續語音段={run_s:.2f}s<{SUSPECT_RUN_S}s")
    env = envelope_range_db(samples)
    if env < FLAT_NOISE_RANGE_DB:
        votes.append(f"包絡起伏={env:.1f}dB<{FLAT_NOISE_RANGE_DB}dB（沒有抑揚）")
    if result is not None:
        segs = result.get("segments") or []
        if segs:
            nsp = max(float(s.get("no_speech_prob", 0.0)) for s in segs)
            cr = max(float(s.get("compression_ratio", 0.0)) for s in segs)
            if nsp > 0.6:
                votes.append(f"no_speech_prob={nsp:.2f}>0.6")
            if cr > 2.4:
                votes.append(f"compression_ratio={cr:.2f}>2.4（重複迴圈）")
        why = hallucination_reason(result)
        if why:
            votes.append(f"低信心短句（{why}）")
    return votes

def hallucination_reason(result):
    """Whisper 自己回報的低信心結果（短句又 avg_logprob 很差）。

    注意這只是輔助：Whisper 對幻覺常常「很有信心」（實測全 0 音檔的幻覺
    no_speech_prob=0.000、avg_logprob=-0.10），信心分數抓不到它；
    主要防線是 is_silence() 的能量門檻。"""
    segs = result.get("segments") or []
    if not segs:
        return None
    avg_lp = sum(float(s.get("avg_logprob", 0.0)) for s in segs) / len(segs)
    text = (result.get("text") or "").strip()
    if len(text) < 5 and avg_lp < -0.8:
        return f"avg_logprob={avg_lp:.2f} text='{text}'"
    return None

def wav_pcm16(path):
    """16-bit PCM wav -> int16 array，讀不了回 None。"""
    import numpy as np
    import wave
    try:
        with wave.open(path, "rb") as w:
            if w.getsampwidth() != 2:
                return None
            data = w.readframes(w.getnframes())
    except Exception as e:  # noqa: BLE001
        print(f"[gate] 無法讀取 {path}: {e!r}", file=sys.stderr)
        return None
    return np.frombuffer(data, dtype="<i2")

def wav_samples(path):
    """16-bit PCM wav -> float32 array in [-1,1], or None if unreadable.
    The file path had NO silence gate before, which is how a silent recording turned into
    a Whisper hallucination that got pasted as if it were speech."""
    pcm = wav_pcm16(path)
    return None if pcm is None else pcm.astype("float32") / 32768.0

def transcribe_audio(audio, language="zh"):
    import numpy as np
    # normalize to float32 in [-1,1] if int16
    if audio.dtype == np.int16:
        audio = audio.astype("float32") / 32768.0
    # energy gate
    if is_silence(audio):
        print(f"[gate] silence (rms {rms_db(audio):.1f} dB) -> skip Whisper", file=sys.stderr)
        return ""
    # Use word_timestamps=False for speed; rely on gate for hallucination
    # Use suppress_blank / no_speech handling via temperature fallback
    result = _whisper(language if language else None, audio=audio)
    text = cleanup_disfluencies(normalize_spacing((result.get("text") or "").strip()))
    # extra guard: 低信心短句視為幻覺（注意：Whisper 對幻覺也可能很有信心，
    # 實測全 0 音檔的幻覺 no_speech_prob=0.000，所以主要防線仍是能量門檻）
    segs = result.get("segments") or []
    if segs:
        avg_lp = sum(s.get("avg_logprob", 0) for s in segs) / len(segs)
        if len(text) < 5 and avg_lp < -0.8:
            print(f"[guard] low confidence hallucination filtered: lp={avg_lp:.2f} text='{text}'", file=sys.stderr)
            return ""
    return text

_OPENCC = None
_OPENCC_ERROR = None

def to_taiwan_traditional(text: str) -> str:
    """Unconditional Simplified->Traditional Taiwan (with phrases). No-op if no CJK.

    以前 opencc 載入失敗會靜默回傳原文（= 簡體直接外流）。現在改為大聲失敗：
    寧可讓 app 顯示「轉寫失敗」，也不要貼出簡體。
    """
    global _OPENCC, _OPENCC_ERROR
    if not text or not any("\u4e00" <= c <= "\u9fff" for c in text):
        return text
    if _OPENCC is None and _OPENCC_ERROR is None:
        try:
            from opencc import OpenCC
            _OPENCC = OpenCC("s2twp")
        except Exception as e:  # noqa: BLE001
            _OPENCC_ERROR = repr(e)
            print(f"[opencc] 無法載入 opencc: {e!r}", file=sys.stderr)
    if _OPENCC is None:
        raise SystemExit(f"[opencc] opencc 不可用（{_OPENCC_ERROR}）——為避免輸出簡體，中止")
    return _OPENCC.convert(text)

def _text_only(s: str) -> str:
    """只留文字（去掉標點、空白、符號）。用來驗證潤飾「一個字都沒動」。"""
    return re.sub(r"[\W_]+", "", s, flags=re.UNICODE)

_ASCII_TO_FULLWIDTH = {",": "，", ".": "。", "?": "？", "!": "！", ":": "：", ";": "；"}

def _is_cjk(ch: str) -> bool:
    return bool(ch) and ("\u3400" <= ch <= "\u9fff" or "\uf900" <= ch <= "\ufaff")

def normalize_punct_only(s: str) -> str:
    """保底路徑：不動任何字，只把緊鄰中文的半形標點換成全形。"""
    out = []
    for i, ch in enumerate(s):
        if ch in _ASCII_TO_FULLWIDTH:
            prev = out[-1] if out else ""
            nxt = s[i + 1] if i + 1 < len(s) else ""
            if _is_cjk(prev) or _is_cjk(nxt):
                out.append(_ASCII_TO_FULLWIDTH[ch])
                continue
        out.append(ch)
    return "".join(out)

def _puncts_only(s: str) -> str:
    """只留真正的標點（Unicode 類別 P）。

    不能寫成「非文字就當標點」：模型偶爾會吐壞字元（實測輸出夾帶 U+FFFD，
    結果「log發現」變成「log�發現」）。類別 P 只含標點，壞字元（So/Cf/Cc）自然被排除。
    """
    return "".join(ch for ch in s if unicodedata.category(ch).startswith("P"))

def _strip_invisible(s: str) -> str:
    """丟掉控制字元與替換字元（保留換行）。避免看不見的垃圾被貼到游標處。"""
    return "".join(ch for ch in s
                   if ch == "\n" or (not unicodedata.category(ch).startswith("C") and ch != "\ufffd"))

_CJK = "\u3400-\u4dbf\u4e00-\u9fff\u3000-\u303f\uff00-\uffef"

# --- 口頭禪／結巴的規則化清理（潤飾之前跑）-----------------------------------
# 為什麼不讓模型清：它會「順稿」——實測把「會不會」改成「不會」（語意反轉）。
# 為什麼不能「所有重複字都折疊」：中文正常疊字很多（看看、試試、非常非常、會不會），
# 全部折疊會誤刪語意。所以只折疊兩類：
#   1) 整詞重複：那個那個 → 那個（重複一個虛詞永遠沒有語意）
#   2) 高風險結巴單字：我我 / 你你 / 這這 / 就就（看、試、想、聽 等實義動詞不在名單）
_REPEAT_WORDS = ("那個", "這個", "就是", "因為", "然後", "所以", "可是", "但是",
                 "其實", "我們", "你們", "他們", "現在", "然後")
_STUTTER_CHARS = "我你他她它這那個就是的對好"
# 只刪「獨立」的語助詞：前面是句首/標點/空白、後面是標點/空白/句尾。
# 句尾的語氣詞（好啊、對啊）不動，因為那是語氣不是結巴。
_FILLERS = ("嗯哼", "嗯", "呃", "啊", "喔", "哦", "欸", "唉")

def _collapse_repeats(s):
    """折疊整詞重複與結巴單字，回傳 (新字串, 折疊次數)。"""
    removed = 0
    for _ in range(3):                     # 我我我 → 我（多跑幾輪到收斂）
        changed = False
        for w in _REPEAT_WORDS:
            s, n = re.subn(rf"({re.escape(w)})[、,\s]*\1", r"\1", s)
            removed += n; changed = changed or n > 0
        for ch in _STUTTER_CHARS:
            s, n = re.subn(rf"({re.escape(ch)})[、,\s]*\1", r"\1", s)
            removed += n; changed = changed or n > 0
        if not changed:
            break
    return s, removed

def _strip_fillers(s):
    """刪掉獨立的語助詞，回傳 (新字串, 刪除次數)。全句只有語助詞時保留原樣。"""
    removed = 0
    for f in _FILLERS:
        pat = re.compile(rf"(?:^|(?<=[，。、！？,.!?\s])){re.escape(f)}[，、,]?\s*")
        new, n = pat.subn("", s)
        if n and _text_only(new):           # 不能清到整句變空
            s = new; removed += n
    return s, removed

def cleanup_disfluencies(s: str) -> str:
    """規則化清掉結巴與語助詞（只刪這兩類，其餘一字不動）。"""
    if not s:
        return s
    before = s
    s, n_rep = _collapse_repeats(s)
    s, n_fill = _strip_fillers(s)
    s = re.sub(r"\s{2,}", " ", s).strip()
    if n_rep or n_fill:
        print(f"[cleanup] 折疊 {n_rep} 處、刪語助詞 {n_fill} 處："
              f"{before[:60]} → {s[:60]}", file=sys.stderr)
    return s

def normalize_spacing(s: str) -> str:
    """去掉「緊鄰中日韓字元」的空格，保留英文單字之間的空格。

    Breeze-ASR-25 會在中文與數字/英文之間塞空格（「早上 10 點」「這個 spec」），
    whisper-large-v3-turbo 不會。這種空格會原樣貼到游標處，看起來像亂碼。
    只動空格、不動字：英文片語（"API doc"）中間的空格保留。
    """
    if not s:
        return s
    t = re.sub(rf"(?<=[{_CJK}])[ \t]+", "", s)
    t = re.sub(rf"[ \t]+(?=[{_CJK}])", "", t)
    return t

def transfer_punctuation(raw: str, polished: str) -> str:
    """把潤飾結果的標點搬到原文上：原文的字一個都不動，只採用模型多插的標點。

    這是對付「模型手癢改字」的根本解：Qwen 會想把句尾「…是在錄音的」修成「…是在錄音？」，
    整句就因為這一字之差被逐字驗證擋掉、連逗號都沒了（實測 71→70 字）。
    逐字對齊後只搬標點，就能同時拿到「它標的逗號」與「你的原話」。
    """
    import difflib
    sm = difflib.SequenceMatcher(None, raw, polished, autojunk=False)
    out: list[str] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            out.append(raw[i1:i2])
            continue
        # delete / replace：字一律保留原文，只把模型那側的標點撿過來
        out.append(raw[i1:i2])
        if tag == "replace":
            out.append(_puncts_only(polished[j1:j2]))
        elif tag == "insert":
            out.append(_puncts_only(polished[j1:j2]))
    return "".join(out)

_CLAUSE_PREFIX = ("然後", "但是", "不過", "因為", "所以", "如果", "而且", "可是", "接下來", "另外", "還有")
_CLAUSE_SUFFIX = ("的時候", "的話", "之後")

def insert_clause_commas(s: str) -> str:
    """LLM 不肯斷句時的確定性補救：只在常見語氣停頓的連接詞處插「，」。

    只插入標點、不碰任何字，所以不可能改到原話；位置就算不理想，也只是讀起來怪，
    不會改變語意（實測 Qwen 7B 對 70 字的長句只擠得出一個句尾問號）。
    """
    out = s
    for m in _CLAUSE_PREFIX:
        i = out.find(m)
        while i > 0:
            prev = out[i - 1]
            if prev not in "，。？！、：；「」（）：…" and (prev.isalnum() or _is_cjk(prev)):
                out = out[:i] + "，" + out[i:]
                i += 1
            i = out.find(m, i + len(m) + 1)
    for m in _CLAUSE_SUFFIX:
        i = out.find(m)
        while i >= 0:
            j = i + len(m)
            if j < len(out) and out[j] not in "，。？！、：；「」（）：…":
                out = out[:j] + "，" + out[j:]
            i = out.find(m, j + 1)
    return out

_TERMINAL_PUNCT = "。？！"
_CLAUSE_PUNCT = "，、：；"
_ALL_PUNCT = _TERMINAL_PUNCT + _CLAUSE_PUNCT + "「」『』（）【】…—～"

# 疑問語氣判斷：10/19 的 M2 純規則版就有（`origin/wip/m2-voice-input-20261020`
# 的 `_QUESTION_TAIL`／`punctuate_deterministic`），改成「先貼再定版」時被雲端路徑
# 取代、整條規則跟著消失，結果規則版一律收「。」，問句也是句號。
# 只認明確的正反問與句末語氣詞，避免把「我不知道他為什麼沒來」這種直述句判成問句。
_QUESTION_TAIL = re.compile(
    r"(嗎|呢|好不好|對不對|是不是|要不要|可不可以|有沒有|行不行|能不能|會不會|對吧|是吧)$"
)
# 句尾語氣詞：先剝掉再判斷（「…要怎麼解啊」）
_TAIL_PARTICLES = "啊啦嘛呀喔哦耶"
# 疑問詞（wh-）：只認「同一子句裡、疑問詞前面沒有否定」的情況，
# 避免把「我不知道他為什麼沒來」「我沒有說誰要來」判成問句。
_WH_WORDS = ("什麼時候", "什麼", "怎麼", "為什麼", "哪裡", "哪一個", "哪個", "哪邊",
             "誰", "多少", "幾個", "幾點", "多久")
_NEGATIONS = ("不", "沒", "別", "無")

def _terminal_mark(base: str) -> str:
    """句尾該用「？」還是「。」：只有明確疑問語氣才用問號。只決定標點，不動字。"""
    core = base.rstrip(_CLAUSE_PUNCT + " 　").rstrip(_TAIL_PARTICLES)
    if _QUESTION_TAIL.search(core):
        return "？"
    clause = core.split("，")[-1]
    for word in _WH_WORDS:
        i = clause.rfind(word)
        if i < 0:
            continue
        if any(neg in clause[:i] for neg in _NEGATIONS):
            return "。"
        return "？"
    return "。"

def sanitize_punctuation(s: str) -> str:
    """把疊在一起的標點收乾淨（「吧，？」→「吧？」、「，，」→「，」）。

    模型偶爾會把逗號和問號一起吐出來（實測：「…或是頓號吧，？」），照搬到輸出會看起來像壞掉。
    只動標點，不碰任何文字。
    """
    out: list[str] = []
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if ch not in _ALL_PUNCT:
            out.append(ch)
            i += 1
            continue
        j = i
        while j < n and s[j] in _ALL_PUNCT:
            j += 1
        run = s[i:j]
        # 這一段裡如果有句末標點（。？！），前面吊著的逗號類就是多餘的
        if any(c in _TERMINAL_PUNCT for c in run):
            keep = [c for c in run if c not in _CLAUSE_PUNCT]
        else:
            keep = list(run)
        out.append("".join(c for k, c in enumerate(keep) if k == 0 or c != keep[k - 1]))
        i = j
    result = "".join(out)
    # 結尾吊著逗號類很難看（「…是在錄音的，」）→ 收成句號或問號
    if result and result[-1] in _CLAUSE_PUNCT:
        result = result[:-1] + _terminal_mark(result[:-1])
    return result

def _ensure_terminal_punct(s: str) -> str:
    """整段沒有句末標點時補一個結尾標點：問句「？」、其餘「。」。只加標點，一個字都不動。

    Breeze 的 raw 完全沒有標點，規則路徑遇到沒有連接詞的句子（「…也都正確」）
    會一個標點都沒有，貼上去像一坨字——雲端 timeout 時使用者看到的就是這版。
    """
    text = s.rstrip()
    if len(_text_only(text)) < 8:
        return s
    if text and text[-1] in _ALL_PUNCT:
        return s
    mark = _terminal_mark(text)
    print(f"[polish] 整段沒有結尾標點 → 補「{mark}」", file=sys.stderr)
    return text + mark

def finalize_punctuation(s: str) -> str:
    """輸出前最後一道：疊字標點收乾淨；整段沒有逗號又是長句時，才動用連接詞規則補斷句。

    最後一律確認有結尾標點（見 `_ensure_terminal_punct`），規則版與雲端版都走這道。
    """
    if not s:
        return s
    cleaned = sanitize_punctuation(_strip_invisible(normalize_punct_only(s)))
    if "，" in cleaned or len(_text_only(cleaned)) < 24:
        return _ensure_terminal_punct(cleaned)
    patched = insert_clause_commas(cleaned)
    if patched != cleaned:
        print("[polish] 模型沒斷句（0 個逗號）→ 用連接詞規則補上", file=sys.stderr)
    return _ensure_terminal_punct(patched)

def rewrite_settings(
    backend: str | None = None,
    terms_file: str | None = None,
    cloud_model: str | None = None,
    cloud_location: str | None = None,
    cloud_project: str | None = None,
    cloud_timeout: float | None = None,
    cloud_max_output_tokens: int | None = None,
    cloud_max_terms: int | None = None,
    cloud_thinking_budget: int | str | None = None,
    cloud_priority: bool | None = None,
) -> RewriteConfig:
    return RewriteConfig(
        backend=(backend or REWRITE_BACKEND or "cloud").strip().lower(),
        model=(cloud_model or CLOUD_REWRITE_MODEL).strip(),
        location=(cloud_location or CLOUD_REWRITE_LOCATION).strip() or "global",
        project=(cloud_project if cloud_project is not None else CLOUD_REWRITE_PROJECT).strip(),
        timeout_seconds=float(cloud_timeout if cloud_timeout is not None else CLOUD_REWRITE_TIMEOUT),
        max_output_tokens=int(
            cloud_max_output_tokens
            if cloud_max_output_tokens is not None
            else CLOUD_REWRITE_MAX_OUTPUT_TOKENS
        ),
        terms_file=(terms_file or REWRITE_TERMS_FILE).strip(),
        max_terms=int(cloud_max_terms if cloud_max_terms is not None else CLOUD_REWRITE_MAX_TERMS),
        thinking_budget=(
            CLOUD_REWRITE_THINKING
            if cloud_thinking_budget is None
            else (None if cloud_thinking_budget == "none" else int(cloud_thinking_budget))
        ),
        priority=CLOUD_REWRITE_PRIORITY if cloud_priority is None else bool(cloud_priority),
    )


def _allowed_rewrite_deletions() -> set[str]:
    return set(_REPEAT_WORDS) | set(_STUTTER_CHARS) | set(_FILLERS)


def apply_terms(text: str, rules=()) -> str:
    """在本地套用使用者詞庫（變體 → 正字）。零模型、零延遲、零網路。

    詞庫原本只進雲端 prompt（`select_terms`），雲端一 timeout 就等於沒有；
    而使用者實際先看到、也常常只看到的是規則版。所以規則版也要套：
    只做整段字串替換，不判斷上下文（要看上下文的那條路是雲端）。
    """
    if not text or not rules:
        return text
    pairs: list[tuple[str, str]] = []
    for rule in rules:
        for variant in getattr(rule, "variants", ()) or ():
            if variant and variant != rule.text and variant in text:
                pairs.append((variant, rule.text))
    if not pairs:
        return text
    # 長的先換：避免短變體先吃掉長變體的一部分（例如「磁庫房」→「詞庫房」）
    pairs.sort(key=lambda pair: len(pair[0]), reverse=True)
    out = text
    applied: list[str] = []
    for variant, target in pairs:
        hits = out.count(variant)
        if not hits:
            continue
        out = out.replace(variant, target)
        applied.append(f"{variant}→{target}×{hits}")
    if applied:
        print(f"[terms] 詞庫套用：{'、'.join(applied)}", file=sys.stderr)
    return out


def rules_rewrite_text(text: str, rules=()) -> str:
    """Deterministic fallback: 詞庫替換 + punctuation only, never touch other wording."""
    return finalize_punctuation(normalize_punct_only(apply_terms(text, rules)))


def apply_rewrite(
    text: str,
    *,
    enabled: bool = True,
    backend: str | None = None,
    context: dict | None = None,
    config: RewriteConfig | None = None,
) -> tuple[str, dict]:
    """Run Typeless-style rewriting, with deterministic rules as the floor."""
    settings = config or rewrite_settings(backend=backend)
    clean_context = sanitize_context(context)
    rules = load_term_rules(settings.terms_file)
    info: dict = {
        "backend": settings.backend,
        "model": settings.model,
        "terms": len(rules),
        "context_fields": sorted(clean_context.keys()),
    }
    if not enabled:
        info["backend"] = "off"
        return text, info
    if not text or not text.strip():
        return "", info
    if settings.backend in ("off", "none", "raw"):
        info["backend"] = "off"
        return text, info
    if settings.backend in ("rules", "local"):
        if settings.backend == "local":
            print("[rewrite] 本地 LLM 已移除（改走雲端或規則）→ 用規則標點", file=sys.stderr)
        info["backend"] = "rules"
        return rules_rewrite_text(text, rules), info

    try:
        result = rewrite_draft(
            text,
            context=clean_context,
            terms=rules,
            config=settings,
            allowed_deletions=_allowed_rewrite_deletions(),
        )
    except RewriteRejected as exc:
        # 守門拒絕：模型改了字／刪了不該刪的。以前整段放棄 → 使用者只看到沒有標點的
        # 規則版。現在退成「只搬標點」：字一律用原話，只採用模型標的標點。
        model_text = str(getattr(exc, "model_text", "") or "")
        if model_text.strip():
            moved = transfer_punctuation(text, model_text)
            if moved and moved != text:
                print(
                    f"[rewrite] 雲端被守門拒絕（{exc.reason}）→ 只搬標點，字不動",
                    file=sys.stderr,
                )
                info["backend"] = "cloud:punct-only"
                info["reason"] = str(exc.reason)[:300]
                info["traffic_type"] = str(getattr(exc, "traffic_type", "") or "")
                return finalize_punctuation(
                    to_taiwan_traditional(normalize_spacing(moved))
                ), info
        print(f"[rewrite] 雲端重寫退回規則（{exc.reason}）", file=sys.stderr)
        info["backend"] = "rules-fallback"
        info["reason"] = str(exc.reason)[:300]
        return rules_rewrite_text(text, rules), info
    except Exception as exc:  # noqa: BLE001 - transcription must survive rewrite failures
        print(f"[rewrite] 雲端重寫退回規則（{exc}）", file=sys.stderr)
        info["backend"] = "rules-fallback"
        info["reason"] = str(exc)[:300]
        return rules_rewrite_text(text, rules), info

    print(
        "[rewrite] 雲端通過："
        f"{result.backend} {result.milliseconds}ms {result.reason} "
        f"token in/out={result.input_tokens}/{result.output_tokens}"
        + (f" traffic={result.traffic_type}" if result.traffic_type else ""),
        file=sys.stderr,
    )
    if settings.priority and result.traffic_type and result.traffic_type != "ON_DEMAND_PRIORITY":
        print(
            f"[rewrite] priority 被降級（traffic={result.traffic_type}）→ 這次算標準價",
            file=sys.stderr,
        )
    final = finalize_punctuation(to_taiwan_traditional(normalize_spacing(result.text)))
    info.update(
        {
            "backend": result.backend,
            "milliseconds": result.milliseconds,
            "changed_chars": result.changed_chars,
            "checked_chars": result.checked_chars,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "reason": result.reason,
        }
    )
    return final, info



def resolve_input_device(spec):
    """Map a device spec (index or name substring) to a sounddevice input index.
    Returns None for system default. Not hardcoded — M4 and M2 have different devices."""
    import sounddevice as sd
    if spec is None or str(spec).strip() == "":
        return None
    s = str(spec).strip()
    if s.lstrip("-").isdigit():
        return int(s)
    hits = [(i, d) for i, d in enumerate(sd.query_devices())
            if d.get("max_input_channels", 0) > 0 and s.lower() in d["name"].lower()]
    if not hits:
        raise SystemExit(f"[rec] no input device matching '{spec}'")
    return hits[0][0]

def parse_serve_request(line: str) -> tuple[str, dict, str | None]:
    """Parse one warm-server request.

    Backward-compatible inputs:
      /path/to.wav
      partial /path/to.wav
      __quit__
    Typeless-style inputs:
      {"wav": "/path/to.wav", "context": {...}, "refine": "/tmp/refine.json"}
    """
    text = line.strip()
    if text.startswith("{"):
        try:
            import json

            payload = json.loads(text)
        except ValueError as exc:
            raise ValueError(f"請求不是合法 JSON：{exc}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("wav"), str):
            raise ValueError("JSON 請求必須包含 wav 路徑")
        context = payload.get("context")
        if context is not None and not isinstance(context, dict):
            raise ValueError("context 必須是物件")
        refine = payload.get("refine")
        if refine is not None and not isinstance(refine, str):
            raise ValueError("refine 必須是檔案路徑字串")
        return payload["wav"], context or {}, refine or None
    return text, {}, None


# ── Soniox 路線（VOICE_STT=soniox）────────────────────────────────────────────
#
# 和 Breeze 路線的差別：Soniox 自己吐標點、自己定案，所以沒有「先貼規則版、
# 雲端在背景定版」那一段；停止後拿到的文字就是最終版。Breeze 路線的程式一行都
# 沒動，切回只要把 local.env 的 VOICE_STT 改回 breeze。


def soniox_terms(terms_file: str | None = None) -> list[str]:
    """Soniox 的 context 熱詞：只放「正字」，長詞先放。

    不放誤聽變體：Whisper 的 initial_prompt 曾把變體一起塞進去，結果模型更常寫錯；
    Soniox 的 context 是「你要的寫法」清單，混進變體等於教它寫錯字。
    """
    rules = load_term_rules(terms_file or REWRITE_TERMS_FILE)
    terms: list[str] = []
    for rule in rules:
        text = rule.get("text") if isinstance(rule, dict) else getattr(rule, "text", None)
        if isinstance(text, str) and text and text not in terms:
            terms.append(text)
    extra = _setting("VOICE_SONIOX_TERMS", "") or ""
    for word in re.split(r"[,，、]", extra):
        word = word.strip()
        if word and word not in terms:
            terms.append(word)
    terms.sort(key=len, reverse=True)   # 長詞優先，短詞不會先吃掉
    return terms[:SONIOX_MAX_TERMS]


def postprocess_transcript(text: str, *, rules=None) -> dict:
    """把 Soniox 的文字收成可以貼的樣子。

    Soniox 給的是「簡體＋標點」，所以順序和 Breeze 路線不同：
      1. 先繁化（詞庫是繁體，先套詞庫永遠不命中）
      2. 再套詞庫字串替換
      3. 最後才補結尾標點（Soniox 通常已經有，這步只是保險）
    """
    raw = (text or "").strip()
    if not raw:
        return {"status": "silence", "text": ""}
    # 保險：Soniox 的控制標記（<end>、<fin> 之類）不該出現在貼出去的文字裡
    raw = re.sub(r"<[a-zA-Z_]+>", "", raw).strip()
    if not raw:
        return {"status": "silence", "text": ""}
    text = cleanup_disfluencies(normalize_spacing(raw))
    text = to_taiwan_traditional(text)
    if rules:
        text = apply_terms(text, rules)
    text = normalize_spacing(text)
    text = _ensure_terminal_punct(text)
    return {"status": "ok", "text": text}


def process_wav_file_soniox(path, *, language="zh", label="", config=None) -> dict:
    """單檔走 Soniox（CLI 驗證、Swift 端失敗時的後備）。"""
    a = wav_samples(path)
    if a is not None and is_silence(a):
        peak_db, window_db = audio_levels(a)
        print(f"[gate]{label} 沒收到聲音（peak {peak_db:.1f} dB、最大 0.5s 窗 rms {window_db:.1f} dB）"
              f" → 不送 Soniox（避免花錢換幻覺）", file=sys.stderr)
        return {"status": "silence", "text": ""}
    settings = config or rewrite_settings()
    rules = load_term_rules(settings.terms_file)
    terms = soniox_terms(settings.terms_file)
    from soniox_stt import transcribe_wav

    try:
        result = transcribe_wav(path, terms=terms, language=language)
    except Exception as exc:  # noqa: BLE001 - 對外回錯誤，App 會退回 Breeze 路徑
        print(f"[soniox] 失敗：{exc!r}", file=sys.stderr)
        return {"status": "error", "text": "", "detail": repr(exc)}
    raw = (result.get("text") or "").strip()
    print(f"[soniox] 轉錄 {result.get('elapsed_ms')}ms 音訊 {result.get('audio_s')}s "
          f"信心 {result.get('confidence')} 熱詞 {len(terms)}", file=sys.stderr)
    print(f"[raw] {raw}", file=sys.stderr)
    out = postprocess_transcript(raw, rules=rules)
    if out["status"] == "silence":
        print("[stt] Soniox 沒有辨識到內容（靜音或麥克風沒收到訊號）", file=sys.stderr)
    else:
        print(f"[final] {out['text']}", file=sys.stderr)
    out["rewrite"] = {
        "backend": "soniox",
        "stt": "soniox",
        "terms": len(terms),
        "confidence": result.get("confidence"),
        "milliseconds": result.get("elapsed_ms"),
    }
    return out


def process_wav_file(
    path,
    language="zh",
    polish=True,
    label="",
    context=None,
    config=None,
    refine_path=None,
    second_opinion_sync=False,
):
    """單檔處理（一次性 CLI 與 --serve 共用）。

    回傳 {"status": ..., "text": ...}：
      ok      = 可以貼上
      suspect = exit 4：有文字但疑似不是語音 → app 不自動貼、保留文字
      silence = exit 3：沒有可用語音

    refine_path 給定時走 Typeless 式「先貼再定版」：這裡只做規則標點就回傳，
    雲端重寫丟到背景執行緒，完成後把定版文字寫進 refine_path，由 App 原地替換。
    這樣單次雲端呼叫的 2–20 秒不會擋在「停止錄音→看到文字」之間。
    """
    if STT_PROVIDER == "soniox":
        # Soniox 模式：完全不碰本地模型。refine_path 也用不到——回來的文字
        # 已經是最終版，App 不會進入「等定版」那一段。
        return process_wav_file_soniox(path, language=language, label=label, config=config)
    a = wav_samples(path)
    if a is not None and is_silence(a):
        peak_db, window_db = audio_levels(a)
        print(f"[gate]{label} 沒收到聲音（peak {peak_db:.1f} dB、最大 0.5s 窗 rms {window_db:.1f} dB）"
              f" → 不跑 Whisper（避免幻覺）", file=sys.stderr)
        return {"status": "silence", "text": ""}
    lang = None if language == "auto" else language
    result = _whisper(lang, audio=a, path=path)
    # 音訊面 + Whisper 面的證據一起投票（單一票不攔，避免誤殺真人說話）
    votes = suspect_reason(a, result) if a is not None else suspect_reason(result=result)
    raw = cleanup_disfluencies(normalize_spacing((result.get("text") or "").strip()))
    if not raw:
        print("[stt] 沒有辨識到內容（靜音，或麥克風根本沒收到訊號）", file=sys.stderr)
    print(f"[raw] {raw}", file=sys.stderr)
    print(f"[raw-tw] {to_taiwan_traditional(raw)}", file=sys.stderr)
    settings = config or rewrite_settings()
    defer_cloud = bool(
        refine_path
        and polish
        and raw
        and settings.backend not in ("off", "none", "raw", "rules", "local")
    )
    if defer_cloud:
        final, rewrite = apply_rewrite(raw, enabled=True, backend="rules", context=context)
        rewrite["backend"] = "rules-first"
        rewrite["refine_pending"] = True
        print(f"[rewrite] 先貼規則版，雲端在背景重寫（完成後原地定版，最多等 "
              f"{REFINE_WAIT_SECONDS:.0f}s）", file=sys.stderr)
    else:
        final, rewrite = apply_rewrite(raw, enabled=polish, context=context, config=config)
    # 模型或規則可能把英文/數字兩側的空格加回來 → 最終文字再正規化一次
    final = normalize_spacing(final)
    # final hard guarantee: any remaining simplified -> Taiwan traditional
    final = to_taiwan_traditional(final)
    print(f"[final] {final}", file=sys.stderr)
    fabricated = is_fabrication(raw, final)
    if fabricated:
        print(f"[fabrication] {fabricated} → 不自動貼上", file=sys.stderr)
    if final and (len(votes) >= SUSPECT_VOTES_REQUIRED or fabricated):
        print(f"[suspect] 疑似不是語音（{len(votes)} 票：" + "；".join(votes) +
              "）→ 不自動貼上（文字仍在 stdout，可用選單手動貼）", file=sys.stderr)
        return {"status": "suspect", "text": final, "rewrite": rewrite}
    # 第二意見：同一段音訊再送一次 Vertex 轉錄模型，只記錄、不改字。
    # 放在「確定要貼」之後、回傳之前；背景執行緒，貼字不等它。
    extra = {}
    if raw and (SECOND_OPINION or second_opinion_sync):
        seconds = (len(a) / float(SAMPLE_RATE)) if a is not None else None
        if second_opinion_sync:
            try:
                audio = Path(path).read_bytes()
            except OSError as exc:
                print(f"[2nd] 跳過：讀不到音檔（{exc}）", file=sys.stderr)
                audio = None
            if audio is not None:
                record = second_opinion_record(
                    audio, raw, settings, seconds=seconds,
                    refine_path=refine_path, refine_wait=0,
                )
                print(f"[2nd] {describe_second_opinion(record)}", file=sys.stderr)
                try:
                    _append_jsonl(SECOND_OPINION_LOG, record)
                except OSError as exc:
                    print(f"[2nd] 寫入記錄失敗：{exc!r}", file=sys.stderr)
                extra["second_opinion"] = record
        else:
            start_second_opinion(
                path, raw, settings, seconds=seconds, refine_path=refine_path
            )
    if defer_cloud:
        start_refinement(raw, context, settings, refine_path, final)
        return {"status": "ok", "text": final, "rewrite": rewrite,
                "refine_pending": True, **extra}
    return {"status": "ok", "text": final, "rewrite": rewrite, **extra}


def _write_json_atomic(path: str, payload: dict) -> None:
    """原子寫檔：App 端可能正在輪詢，不能讓它讀到半個 JSON。"""
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _settle_text(value: str, rules=()) -> str:
    """雲端結果的收尾：與 apply_rewrite 成功路徑、process_wav_file 完全相同的鏈。

    `rules` 有值時先套詞庫——雲端被 prompt 告知正字卻仍寫錯時，這是最後一道
    零成本的攔截；沒有規則時行為與舊版完全相同。
    """
    text = finalize_punctuation(to_taiwan_traditional(normalize_spacing(apply_terms(value, rules))))
    return to_taiwan_traditional(normalize_spacing(text))


def refine_in_background(raw, context, settings, refine_path, pasted_text):
    """跑雲端重寫並把定版文字寫進 refine_path（在背景執行緒裡跑）。"""
    started = time.perf_counter()
    try:
        clean_context = sanitize_context(context)
        rules = load_term_rules(settings.terms_file)
        result = rewrite_draft(
            raw,
            context=clean_context,
            terms=rules,
            config=settings,
            allowed_deletions=_allowed_rewrite_deletions(),
        )
        text = _settle_text(result.text, rules)
        if not text:
            payload = {"ok": False, "reason": "雲端回空字串"}
        elif text == pasted_text:
            payload = {"ok": True, "text": text, "reason": result.reason,
                       "milliseconds": result.milliseconds, "same_as_pasted": True}
        elif is_fabrication(raw, text):
            payload = {"ok": False, "reason": f"定版文字疑似幻覺（{is_fabrication(raw, text)}）"}
        else:
            payload = {
                "ok": True,
                "text": text,
                "reason": result.reason,
                "milliseconds": result.milliseconds,
                "changed_chars": result.changed_chars,
                "same_as_pasted": False,
            }
    except Exception as exc:  # noqa: BLE001 - 定版失敗就保留規則版，不能影響已貼上的字
        payload = {"ok": False, "reason": str(exc)[:300]}
    payload["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
    try:
        _write_json_atomic(refine_path, payload)
    except Exception as exc:  # noqa: BLE001
        print(f"[refine] 寫入精修檔失敗：{exc!r}", file=sys.stderr)
        return
    print(
        f"[refine] 定版 {'成功' if payload.get('ok') else '放棄'} {payload['elapsed_ms']}ms "
        f"{payload.get('reason', '')[:120]}",
        file=sys.stderr,
    )


def start_refinement(raw, context, settings, refine_path, pasted_text):
    """把雲端重寫丟到背景；不 join，貼字不等它。"""
    worker = threading.Thread(
        target=refine_in_background,
        args=(raw, context, settings, refine_path, pasted_text),
        name="voice-refine",
        daemon=True,
    )
    worker.start()
    return worker

# --- 第二意見（second opinion）：同一段音訊再送一次 Vertex 專用轉錄模型 -----
# 目的只有一個：量「本地 Breeze 跟雲端 gemini-3.5-transcribe 誰常對、錯在哪」。
# 它**不碰**已貼上的字，也不進定版流程；寫 JSONL 供 `--second-opinion-report` 統計。
_SECOND_OPINION_SLOTS = threading.Semaphore(
    max(1, int(_setting("VOICE_SECOND_OPINION_PARALLEL", "2") or 2))
)


def build_cloud_transcribe_request(audio: bytes, settings, model: str | None = None):
    """組出 Vertex generateContent 請求（純函式：測試不碰網路、不碰 ADC）。"""
    from rewrite_cloud import _project_id

    active_model = (model or SECOND_OPINION_MODEL).strip()
    url = (
        f"https://aiplatform.googleapis.com/v1/projects/{_project_id(settings)}"
        f"/locations/{settings.location}/publishers/google/models/"
        f"{urllib.parse.quote(active_model, safe='')}:generateContent"
    )
    payload = {
        "contents": [{"role": "user", "parts": [{"inline_data": {
            "mime_type": "audio/wav",
            "data": base64.b64encode(audio).decode("ascii"),
        }}]}],
        # 只要「它聽到什麼字」：不加 smart 模式、不要求標點。
        # 修正與標點仍是原本那條 gemini-3.8-flash 的事。
        "generationConfig": {"temperature": 0},
    }
    return url, {"Content-Type": "application/json"}, payload


def parse_cloud_transcript(response: dict) -> tuple[str, int, int]:
    """從 Vertex 回應取出逐字稿與 token 數。

    轉錄模型把文字放在 `parts[].audioTranscription.text`（實測 v1beta
    generateContent 同時也放了 `parts[].text`），兩邊都讀、去重後串起來。
    """
    chunks: list[str] = []
    for candidate in response.get("candidates") or []:
        for part in (candidate.get("content") or {}).get("parts") or []:
            chunk = ((part.get("audioTranscription") or {}).get("text") or part.get("text") or "")
            chunk = chunk.strip()
            if chunk and chunk not in chunks:
                chunks.append(chunk)
    usage = response.get("usageMetadata") or {}
    return (
        "".join(chunks),
        int(usage.get("promptTokenCount", 0) or 0),
        int(usage.get("candidatesTokenCount", 0) or 0),
    )


def cloud_transcribe_audio(
    audio: bytes,
    settings,
    *,
    model: str | None = None,
    timeout: float | None = None,
    transport=None,
    token: str | None = None,
) -> dict:
    """送整段音訊到 Vertex 轉錄模型，回 {"text","input_tokens","output_tokens","elapsed_ms"}。"""
    from rewrite_cloud import _post_json, mint_access_token

    url, headers, payload = build_cloud_transcribe_request(audio, settings, model)
    headers = dict(headers)
    headers["Authorization"] = f"Bearer {token or mint_access_token(settings)}"
    poster = transport or _post_json
    started = time.perf_counter()
    response = poster(url, headers, payload, float(timeout or SECOND_OPINION_TIMEOUT))
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    text, input_tokens, output_tokens = parse_cloud_transcript(response)
    return {
        "text": text,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "elapsed_ms": elapsed_ms,
    }


_ARABIC_DIGITS = set("0123456789.")
_CN_NUMERALS = set("〇零一二三四五六七八九十百千萬萬億兩两")


def _is_numeral_only(text: str) -> bool:
    """整段都是數字（阿拉伯或中文數字）。用來把「10 → 十」歸成格式差異。"""
    compact = "".join(ch for ch in text if not ch.isspace())
    if not compact:
        return False
    if all(ch in _ARABIC_DIGITS for ch in compact):
        return True
    return all(ch in _CN_NUMERALS or ch in _ARABIC_DIGITS for ch in compact)


def compare_transcripts(local: str, cloud: str, max_spans: int = 6) -> dict:
    """比「字」不比標點：兩邊都先繁化再只留文字，才不會把「，」算成錯。

    「10 → 十」這種整段都是數字的差異算**格式差異**，不算聽錯：實測同一句就會出現，
    混進不一致率會讓「誰比較準」的統計失真。
    """
    local_chars = _text_only(to_taiwan_traditional(local))
    cloud_chars = _text_only(to_taiwan_traditional(cloud))
    if local_chars == cloud_chars:
        return {
            "agree": True,
            "spans": [],
            "formatting": [],
            "local_chars": len(local_chars),
            "cloud_chars": len(cloud_chars),
            "diff_chars": 0,
        }
    matcher = difflib.SequenceMatcher(None, local_chars, cloud_chars, autojunk=False)
    spans = []
    formatting = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        local_part, cloud_part = local_chars[i1:i2], cloud_chars[j1:j2]
        if _is_numeral_only(local_part) and _is_numeral_only(cloud_part):
            formatting.append({"local": local_part, "cloud": cloud_part, "at": i1})
            continue
        if len(spans) < max_spans:
            spans.append({"local": local_part, "cloud": cloud_part, "at": i1})
    return {
        "agree": not spans,
        "spans": spans,
        "formatting": formatting,
        "local_chars": len(local_chars),
        "cloud_chars": len(cloud_chars),
        "diff_chars": sum(max(len(s["local"]), len(s["cloud"])) for s in spans),
    }


def punctuation_profile(text: str) -> dict:
    """數一段文字裡的標點（只看 Unicode 類別 P，壞字元不算）。"""
    marks = _puncts_only(text)
    counts: dict[str, int] = {}
    for ch in marks:
        counts[ch] = counts.get(ch, 0) + 1
    return {"marks": marks, "counts": counts, "total": len(marks)}


def compare_punctuation(local: str, cloud: str) -> dict:
    """標點**另外**算一個維度：不能併進「一致率」，否則每句都算不一致。

    本地 ASR 幾乎不給標點（Breeze raw 沒有），雲端轉錄模型會自己補逗號與句尾——
    這正是我們現在靠規則版＋gemini-3.8-flash 補的那一段，所以值得單獨量。
    """
    # 先統一全形：本地/Whisper 常給半形「,」，雲端給「，」——那是編碼差異，不是斷句差異。
    left = punctuation_profile(normalize_punct_only(local))
    right = punctuation_profile(normalize_punct_only(cloud))
    return {
        "local_total": left["total"],
        "cloud_total": right["total"],
        "local_counts": left["counts"],
        "cloud_counts": right["counts"],
        "same": left["marks"] == right["marks"],
    }


def read_refined_text(refine_path: str | None, wait_seconds: float = 0.0) -> str | None:
    """等並讀定版檔（`{"ok":true,"text":…}`）。等不到或不是 ok 就回 None。

    refine 檔是每次錄音一個 UUID 路徑（App 端產生），所以不必擔心讀到上一句。
    """
    if not refine_path:
        return None
    path = Path(refine_path)
    deadline = time.perf_counter() + max(0.0, wait_seconds)
    while True:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = None
        if isinstance(payload, dict) and payload.get("ok") and payload.get("text"):
            return str(payload["text"])
        if time.perf_counter() >= deadline:
            return None
        time.sleep(0.25)


def second_opinion_record(
    audio: bytes,
    local_text: str,
    settings,
    *,
    seconds: float | None = None,
    transport=None,
    token: str | None = None,
    refine_path: str | None = None,
    refine_wait: float | None = None,
) -> dict:
    record = {
        "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "model": SECOND_OPINION_MODEL,
        "seconds": round(float(seconds), 2) if seconds else None,
        "local": local_text,
    }
    try:
        result = cloud_transcribe_audio(
            audio, settings, transport=transport, token=token
        )
    except Exception as exc:  # noqa: BLE001 - 第二意見失敗不能影響任何看得到的東西
        record.update({"status": "error", "error": str(exc)[:200]})
        return record
    record.update(
        {
            "status": "ok",
            "cloud": to_taiwan_traditional(result["text"]),
            "cloud_raw": result["text"],
            "elapsed_ms": result["elapsed_ms"],
            "input_tokens": result["input_tokens"],
            "output_tokens": result["output_tokens"],
            **compare_transcripts(local_text, result["text"]),
            "punctuation": compare_punctuation(local_text, result["text"]),
        }
    )
    # 定版（你真正看到的那一版）晚幾秒才寫檔；等得到就多一個「定版 vs 雲端標點」。
    refined = read_refined_text(
        refine_path,
        SECOND_OPINION_REFINE_WAIT if refine_wait is None else refine_wait,
    )
    record["refined"] = refined
    if refined:
        record["punctuation_refined_vs_cloud"] = compare_punctuation(refined, result["text"])
    return record


def _append_jsonl(path: str, record: dict) -> None:
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def describe_second_opinion(record: dict) -> str:
    if record.get("status") != "ok":
        return f"失敗（{str(record.get('error', ''))[:80]}）"
    if record.get("agree"):
        formatting = record.get("formatting") or []
        extra = ""
        if formatting:
            extra = "、數字格式 " + "；".join(
                f"{s.get('local') or '∅'}→{s.get('cloud') or '∅'}" for s in formatting
            )
        punctuation = record.get("punctuation") or {}
        if punctuation and punctuation.get("local_total") != punctuation.get("cloud_total"):
            extra += f"、標點 {punctuation.get('local_total', 0)}→{punctuation.get('cloud_total', 0)}"
        return f"一致{extra}（{record.get('elapsed_ms', 0)}ms）"
    spans = "；".join(
        f"{s.get('local') or '∅'}→{s.get('cloud') or '∅'}" for s in record.get("spans") or []
    )
    return f"不一致 {len(record.get('spans') or [])} 處：{spans}（{record.get('elapsed_ms', 0)}ms）"


def second_opinion_in_background(
    audio: bytes, local_text: str, settings, seconds=None, refine_path=None
):
    if not _SECOND_OPINION_SLOTS.acquire(blocking=False):
        print("[2nd] 跳過：已經有兩個第二意見在跑", file=sys.stderr)
        return None
    record = None
    try:
        # 等定版放在放掉併發名額之後：等檔可能 8 秒，不該擋住下一句的第二意見。
        record = second_opinion_record(
            audio, local_text, settings, seconds=seconds, refine_path=refine_path,
            refine_wait=0,
        )
    finally:
        _SECOND_OPINION_SLOTS.release()
    if record is None:
        return None
    if record.get("status") == "ok" and refine_path and not record.get("refined"):
        refined = read_refined_text(refine_path, SECOND_OPINION_REFINE_WAIT)
        if refined:
            record["refined"] = refined
            record["punctuation_refined_vs_cloud"] = compare_punctuation(
                refined, record.get("cloud_raw") or ""
            )
    try:
        _append_jsonl(SECOND_OPINION_LOG, record)
    except OSError as exc:
        print(f"[2nd] 寫入記錄失敗：{exc!r}", file=sys.stderr)
    print(f"[2nd] {describe_second_opinion(record)}", file=sys.stderr)
    return record


def start_second_opinion(path, local_text, settings, seconds=None, refine_path=None):
    """把第二意見丟到背景。音訊在這裡（同步）讀進記憶體，之後 App 刪檔也不影響。"""
    if not (SECOND_OPINION and local_text):
        return None
    if seconds is not None and seconds > SECOND_OPINION_MAX_SECONDS:
        print(
            f"[2nd] 跳過：{seconds:.0f}s 超過上限 {SECOND_OPINION_MAX_SECONDS:.0f}s",
            file=sys.stderr,
        )
        return None
    try:
        audio = Path(path).read_bytes()
    except OSError as exc:
        print(f"[2nd] 跳過：讀不到音檔（{exc}）", file=sys.stderr)
        return None
    worker = threading.Thread(
        target=second_opinion_in_background,
        args=(audio, local_text, settings, seconds, refine_path),
        name="voice-2nd",
        daemon=True,
    )
    worker.start()
    return worker


def second_opinion_report(path: str | None = None) -> str:
    """統計 JSONL：一致率、常見不一致、延遲與 token。"""
    target = Path(path or SECOND_OPINION_LOG)
    if not target.exists():
        return f"[2nd] 還沒有任何記錄（{target}）"
    records = []
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    ok = [r for r in records if r.get("status") == "ok"]
    errors = [r for r in records if r.get("status") == "error"]
    agree = [r for r in ok if r.get("agree")]
    pairs: dict[tuple[str, str], int] = {}
    for record in ok:
        for span in record.get("spans") or []:
            key = (span.get("local", ""), span.get("cloud", ""))
            pairs[key] = pairs.get(key, 0) + 1
    lines = [
        f"樣本 {len(records)}（成功 {len(ok)}、失敗 {len(errors)}），"
        f"一致 {len(agree)}、不一致 {len(ok) - len(agree)}"
    ]
    formatting_total = sum(len(r.get("formatting") or []) for r in ok)
    if formatting_total:
        lines.append(f"數字格式差異 {formatting_total} 處（10→十 這類，不算不一致）")
    if ok:
        elapsed = sorted(int(r.get("elapsed_ms", 0) or 0) for r in ok)
        mid = len(elapsed) // 2
        median = (
            elapsed[mid]
            if len(elapsed) % 2
            else int((elapsed[mid - 1] + elapsed[mid]) / 2)
        )
        lines.append(f"一致率 {len(agree) / len(ok):.0%}")
        lines.append(f"延遲 中位 {median}ms、最大 {elapsed[-1]}ms")
        lines.append(
            f"token 輸入 {sum(int(r.get('input_tokens', 0) or 0) for r in ok)}、"
            f"輸出 {sum(int(r.get('output_tokens', 0) or 0) for r in ok)}"
        )
    punctuation = [r.get("punctuation") or {} for r in ok]
    if punctuation:
        local_totals = [int(p.get("local_total", 0) or 0) for p in punctuation]
        cloud_totals = [int(p.get("cloud_total", 0) or 0) for p in punctuation]
        with_local = sum(1 for n in local_totals if n)
        lines.append(
            f"標點：本地(ASR raw) 有標點 {with_local}/{len(punctuation)}、"
            f"平均 {sum(local_totals) / len(punctuation):.1f} 個；"
            f"雲端平均 {sum(cloud_totals) / len(punctuation):.1f} 個"
        )
        cloud_counts: dict[str, int] = {}
        for profile in punctuation:
            for mark, count in (profile.get("cloud_counts") or {}).items():
                cloud_counts[mark] = cloud_counts.get(mark, 0) + count
        if cloud_counts:
            lines.append(
                "雲端標點分布："
                + "、".join(
                    f"{mark}×{count}"
                    for mark, count in sorted(cloud_counts.items(), key=lambda kv: -kv[1])
                )
            )
        word_agree = [r for r in ok if r.get("agree")]
        punct_differs = [
            r for r in word_agree if (r.get("punctuation") or {}).get("same") is False
        ]
        if word_agree:
            lines.append(f"字一致但標點不同：{len(punct_differs)}/{len(word_agree)}")
        refined = [r for r in ok if r.get("refined")]
        if refined:
            same_punct = sum(
                1
                for r in refined
                if (r.get("punctuation_refined_vs_cloud") or {}).get("same")
            )
            lines.append(
                f"定版可用 {len(refined)} 筆，其中定版標點與雲端相同 {same_punct} 筆"
            )
    if pairs:
        lines.append("最常不一致（本地→雲端）：")
        for (local, cloud), count in sorted(pairs.items(), key=lambda kv: -kv[1])[:10]:
            lines.append(f"  {local or '∅'} → {cloud or '∅'}  ×{count}")
    for record in errors[:5]:
        lines.append(f"  失敗樣本 {record.get('ts')}：{str(record.get('error', ''))[:80]}")
    return "\n".join(lines)


def partial_text(path, language="zh"):
    """錄音中「即時預覽」用：Whisper ＋ 繁化，**不潤飾**。

    遠端 STT（STT_REMOTE_HOST）模式下**沒有預覽文字**：這個 tick 只用來把新增的音訊先傳到 M4
    （邊錄邊傳，見 remote_stream_append），永遠回空字串。不要在遠端模式略過它，否則停止後要整段重傳。

    刻意與 process_wav_file 不同：
      - 不重寫：最終文字才走雲端校正（而且預覽不是最終文字）
      - 不做 suspect／fabrication 投票：那是「要不要自動貼」的判斷，與預覽無關
      - 靜音直接回空字串：不跑 Whisper（每次停頓都燒一次推論沒有意義）

    實測（M4 Max，暖機）：whisper-large-v3-turbo 約 33x 即時速度
    → 10s 的音訊一次預覽約 0.3s，1.2s 一次完全跟得上。
    """
    if STT_REMOTE_HOST:
        # 遠端 STT：不做預覽辨識（沒有文字回來）；借這個 tick 把新增的音訊先傳到 M4（邊錄邊傳）
        pcm = wav_pcm16(path)
        if pcm is not None:
            remote_stream_append(pcm)
        return ""
    a = wav_samples(path)
    if a is None or is_silence(a):
        return ""
    lang = None if language == "auto" else language
    raw = transcribe_audio(a, language=lang)
    return to_taiwan_traditional(raw) if raw else ""


class _RequestReader:
    """stdin「一行一請求」的讀取器（自己組行，不用 sys.stdin.readline）。

    為什麼不用 sys.stdin.readline：TextIOWrapper 會把管子裡**後面的行**先讀進自己的緩衝區，
    之後 select() 看到的是空的 fd —— 就沒辦法知道「已經有下一個請求在排隊」，
    而預覽請求必須能立刻讓位給使用者的停止請求（實測過：第三個測試因此沒讓位）。
    """

    def __init__(self, fd=0):
        self.fd = fd
        self.buf = b""
        self.pending = []      # 已經讀進來、還沒處理的整行（FIFO）
        self.eof = False

    def _split_ready(self):
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            s = line.decode("utf-8", "replace").strip()
            if s:
                self.pending.append(s)

    def next_request(self, idle_timeout):
        """回傳 ('line', 請求) 或 ('idle', None) 或 ('eof', None)。"""
        import os
        import select
        while True:
            self._split_ready()
            if self.pending:
                return ("line", self.pending.pop(0))
            if self.eof:
                return ("eof", None)
            try:
                ready, _, _ = select.select([self.fd], [], [], idle_timeout)
            except (OSError, ValueError):
                return ("eof", None)
            if not ready:
                return ("idle", None)
            try:
                chunk = os.read(self.fd, 1 << 16)
            except OSError:
                return ("eof", None)
            if not chunk:
                return ("eof", None)
            self.buf += chunk

    def drain(self):
        """回傳「已經在排隊、還沒處理」的所有請求，並把它們從佇列移除（不阻塞）。"""
        import os
        import select
        while not self.eof:
            try:
                ready, _, _ = select.select([self.fd], [], [], 0)
            except (OSError, ValueError):
                break
            if not ready:
                break
            try:
                chunk = os.read(self.fd, 1 << 16)
            except OSError:
                break
            if not chunk:
                self.eof = True
                break
            self.buf += chunk
        self._split_ready()
        out = self.pending
        self.pending = []
        return out


def serve_forever(language="zh", polish=True, config=None):
    """暖機伺服器：本地 ASR 模型只載入一次，之後每個請求只做推論。

    動機（實測）：一次性路徑每次都要重載 Breeze ASR → 冷啟動數秒；
    模型還在記憶體時只要不到 1s。所以在「開始錄音」時就把這個程序拉起來暖機，
    使用者講話的時間剛好拿來載模型；按下停止後只剩推論加雲端重寫。

    協定：stdin 每行一個 wav 絕對路徑，或 {"wav": ..., "context": {...}} →
    stdout 每行一個 JSON 結果（診斷一律走 stderr）。
    app 端關閉時 stdin 會 EOF，這裡就跟著結束，不會留孤兒程序。
    """
    import json
    active = config or rewrite_settings()
    soniox_mode = STT_PROVIDER == "soniox"
    print(f"[serve] 暖機中（OpenCC{' + 本地 ASR' if not soniox_mode else '；STT=Soniox 雲端，不載本地模型'}；"
          f"重寫={active.backend}）…", file=sys.stderr)
    # 把實際載入的模型寫進 log：改 local.env 之後要一眼看出「現在是哪一顆」
    # （踩過：改完設定，舊的暖機程序還在跑，log 只看得到「已有暖機程序，不重啟」）
    print(f"[serve] STT={'soniox' if soniox_mode else STT_REPO} REWRITE={active.backend}:{active.model}",
          file=sys.stderr)
    # 第二意見會把音訊送出機器：這行是每次啟動的「你正在上傳音訊」提示，
    # 不像 rewrite 那樣只送文字。要看狀態就從這裡看，不要猜。
    print(
        f"[serve] 第二意見={'on' if SECOND_OPINION else 'off'} "
        f"model={SECOND_OPINION_MODEL} log={SECOND_OPINION_LOG}"
        + ("" if SECOND_OPINION else "（VOICE_SECOND_OPINION=0）"),
        file=sys.stderr,
    )
    try:
        to_taiwan_traditional("暖機")
        if not soniox_mode:
            import numpy as np
            # 必須「不是靜音」才會真的觸發 Whisper 載入（is_silence 會提早 return）
            warm = (np.random.default_rng(0).standard_normal(SAMPLE_RATE).astype("float32") * 0.02)
            transcribe_audio(warm, language=language)
        print("[serve] 暖機完成，等待請求", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"[serve] 暖機失敗（不致命：第一次請求會慢一點）: {e!r}", file=sys.stderr)
    idle_timeout = float(os.environ.get("VOICE_SERVE_IDLE_TIMEOUT", "600"))  # 10 分鐘沒用到就退出，別整天佔 6GB
    reader = _RequestReader()
    while True:
        kind, req = reader.next_request(idle_timeout)
        if kind == "idle":
            print(f"[serve] 閒置 {int(idle_timeout)}s → 結束（下次開始錄音會重新暖機）", file=sys.stderr)
            break
        if kind == "eof":
            break
        if req == "__quit__":
            break
        if not isinstance(req, str):
            print(f"[serve] 忽略非文字請求：{type(req).__name__}", file=sys.stderr)
            continue
        # Soniox 模式：App 已經有一份帶標點的文字，只要繁化＋詞庫＋收尾。
        # 走同一條 stdin/stdout 協定，一次請求一行回應，不動既有排隊邏輯。
        if req.startswith("{"):
            try:
                peek = json.loads(req)
            except ValueError:
                peek = None
            if isinstance(peek, dict) and isinstance(peek.get("text"), str):
                rules = load_term_rules(active.terms_file)
                out = postprocess_transcript(peek["text"], rules=rules)
                print(f"[post] {len(peek['text'])} 字 → {out['status']} {len(out.get('text') or '')} 字",
                      file=sys.stderr)
                print(json.dumps(out, ensure_ascii=False), flush=True)
                continue
        if req.startswith("partial "):
            if soniox_mode:
                # Soniox 是即時串流，錄音中不需要再跑一次本地預覽
                print(json.dumps({"status": "partial", "text": ""}, ensure_ascii=False), flush=True)
                continue
            # 錄音中的即時預覽。優先權最低：先看不阻塞地是不是已經有「完整請求」在排隊，
            # 有就直接放棄這次預覽去處理它 —— 使用者按下停止後不該再等一個過期的預覽跑完。
            queued = reader.drain()
            fulls = [q for q in queued if not q.startswith("partial ")]
            if not fulls:
                try:
                    t = partial_text(req[len("partial "):], language=language)
                except Exception as e:  # noqa: BLE001
                    t = ""
                    print(f"[partial] 預覽失敗: {e!r}", file=sys.stderr)
                print(f"[partial] 目前為止 {len(t)} 字", file=sys.stderr)
                print(json.dumps({"status": "partial", "text": t}, ensure_ascii=False), flush=True)
                continue
            # 排隊中的預覽全部丢掉；完整請求只留最後一個（前面的音訊是它的前綴）
            req = fulls[-1]
            print(f"[partial] 偵測到完整請求在排隊 → 丢掉 {len(queued) - len(fulls)} 個過期預覽、"
                  f"跳過 {len(fulls) - 1} 個過期完整請求，直接處理最後一個", file=sys.stderr)
        try:
            request_path, request_context, request_refine = parse_serve_request(req)
            r = process_wav_file(
                request_path,
                language=language,
                polish=polish,
                label="[serve]",
                context=request_context,
                config=active,
                refine_path=request_refine,
            )
        except ValueError as e:
            r = {"status": "error", "text": "", "detail": str(e)}
            print(f"[serve] 請求格式錯誤: {e!r}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            r = {"status": "error", "text": "", "detail": repr(e)}
            print(f"[serve] 處理失敗: {e!r}", file=sys.stderr)
        print(json.dumps(r, ensure_ascii=False), flush=True)
    print("[serve] 結束（stdin EOF 或收到 __quit__）", file=sys.stderr)

def load_request_context(path: str | None) -> dict:
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError) as exc:
        print(f"[context] 無法讀取 {path}（{exc}），本次不用上下文", file=sys.stderr)
        return {}
    if not isinstance(payload, dict):
        print(f"[context] {path} 不是物件，本次不用上下文", file=sys.stderr)
        return {}
    return payload


def handle_terms_cli(path: str, *, list_: bool = False, add: str | None = None,
                     variants: str = "", remove: str | None = None) -> None:
    """本機版「個人詞庫」管理，對等 Typeless 的 /user/dictionary/{add,list,delete}。

    他們把詞庫放伺服器（add/list/update/delete/bulk-import）；我們放
    `rewrite_terms.json`（不進版控），形狀相容於 `load_term_rules()`。
    """
    doc_path = Path(path)
    document: dict = {"version": 1, "terms": []}
    if doc_path.exists():
        try:
            loaded = json.loads(doc_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise SystemExit(f"[terms] {path} 不是合法 JSON，未變更：{exc}") from exc
        if isinstance(loaded, list):
            document = {"version": 1, "terms": loaded}
        elif isinstance(loaded, dict):
            document = loaded
            document.setdefault("terms", [])
        else:
            raise SystemExit(f"[terms] {path} 形狀不認得（既不是物件也不是陣列）")
    rows = document.setdefault("terms", [])

    if list_ or (add is None and remove is None):
        if not rows:
            print("(詞庫是空的)")
            return
        for row in rows:
            if isinstance(row, str):
                print(f"{row}\t-")
            elif isinstance(row, dict):
                variants_list = row.get("match") or row.get("variants") or []
                print(f"{row.get('text', '')}\t{'、'.join(variants_list) if variants_list else '-'}")
        return

    if add:
        new_variants = [v.strip() for v in variants.replace("，", ",").split(",") if v.strip()]
        for row in rows:
            if isinstance(row, dict) and row.get("text") == add:
                merged = list(dict.fromkeys(list(row.get("match") or []) + new_variants))
                row["match"] = merged
                break
        else:
            rows.append({"text": add, "match": new_variants})
        doc_path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")
        print(f"[terms] 已寫入「{add}」（變體：{'、'.join(new_variants) or '無'}）→ {path}")
        return

    if remove:
        before = len(rows)
        rows[:] = [
            row for row in rows
            if not (isinstance(row, dict) and row.get("text") == remove) and row != remove
        ]
        doc_path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")
        print(f"[terms] 已刪除「{remove}」（{before - len(rows)} 筆）→ {path}")


def learn_edit_file(path: str, terms_file: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        summary = learn_insertion_edits(payload, terms_file)
    except (OSError, ValueError, RuntimeError) as exc:
        raise SystemExit(f"[learn] 無法從手動修正學到詞庫：{exc}") from exc
    print(
        "learned={learned} candidates={candidates} "
        "existing={existing} conflicts={conflicts}".format(**summary)
    )
    return summary


def main():
    ap = argparse.ArgumentParser(description="Transcribe locally, rewrite with cloud context")
    ap.add_argument("wav", nargs="?", help="wav file (16kHz mono). If omitted, records from mic.")
    ap.add_argument("--no-polish", action="store_true", help="只做 ASR，不做任何重寫")
    ap.add_argument(
        "--rewrite-backend",
        choices=["cloud", "rules", "off"],
        default=None,
        help="預設走 VOICE_REWRITE_BACKEND，一般不用加",
    )
    ap.add_argument("--context-json", default=None, help="Typeless-style context JSON 檔案")
    ap.add_argument("--terms-file", default=None, help="個人專有名詞 JSON 檔案")
    ap.add_argument("--learn-edit-json", default=None, help="從一次手動修正學到個人詞庫")
    ap.add_argument("--terms-list", action="store_true", help="列出個人詞庫（對等 Typeless 的 dictionary list）")
    ap.add_argument("--terms-add", default=None, help="新增/更新詞庫條目：--terms-add 詞庫 --terms-variant 瓷庫,磁庫")
    ap.add_argument("--terms-variant", default="", help="搭配 --terms-add：可能被聽錯的寫法，逗號分隔")
    ap.add_argument("--terms-remove", default=None, help="從詞庫刪除一個詞")
    ap.add_argument("--cloud-model", default=None)
    ap.add_argument("--cloud-location", default=None)
    ap.add_argument("--cloud-project", default=None)
    ap.add_argument("--cloud-timeout", type=float, default=None)
    ap.add_argument("--cloud-max-output-tokens", type=int, default=None)
    ap.add_argument("--cloud-max-terms", type=int, default=None)
    ap.add_argument("--cloud-thinking-budget", type=int, default=None,
                    help="思考 token 上限（0 = 盡量關掉，預設）；-1 = 不送 thinkingConfig")
    ap.add_argument("--language", default="zh", help="zh / en / auto (empty for auto-detect)")
    ap.add_argument("--record-seconds", type=float, default=5)
    ap.add_argument("--device", default=None, help="input device: index or name substring (default: VOICE_INPUT_DEVICE from local.env, else system default). Not hardcoded — M4/M2 devices differ.")
    ap.add_argument("--list-devices", action="store_true", help="list audio input devices and exit")
    ap.add_argument("--copy", action="store_true", help="copy result to clipboard via pbcopy")
    ap.add_argument("--paste", action="store_true", help="paste via clipboard+ AppleScript Cmd+V")
    ap.add_argument("--serve", action="store_true",
                    help="暖機伺服器：stdin 每行一個 wav 路徑，stdout 每行一個 JSON 結果（模型只載入一次）")
    ap.add_argument("--second-opinion", action="store_true",
                    help="同一段音訊再送一次 Vertex 轉錄模型並印出比對（同步，供驗證用）")
    ap.add_argument("--second-opinion-report", action="store_true",
                    help="統計第二意見記錄：一致率、常見不一致、延遲與 token")
    ap.add_argument("--stt", choices=["breeze", "soniox"], default=None,
                    help="覆寫 VOICE_STT：breeze（本地，預設）或 soniox（雲端即時）")
    ap.add_argument("--postprocess-json", default=None,
                    help="只做後處理：吃 {\"text\": ...} 或純文字檔，印出繁化＋詞庫後的最終文字")
    args = ap.parse_args()
    global STT_PROVIDER
    if args.stt:
        STT_PROVIDER = args.stt
    if args.postprocess_json:
        payload = Path(args.postprocess_json).read_text(encoding="utf-8")
        try:
            doc = json.loads(payload)
            text = (doc.get("text") or "") if isinstance(doc, dict) else payload
        except ValueError:
            text = payload
        rules = load_term_rules(args.terms_file or REWRITE_TERMS_FILE)
        out = postprocess_transcript(text, rules=rules)
        print(f"[post] {out['status']} {len(out.get('text') or '')} 字", file=sys.stderr)
        if out["text"]:
            print(out["text"])
        if out["status"] == "silence":
            sys.exit(3)
        return
    if args.learn_edit_json:
        terms_file = args.terms_file or REWRITE_TERMS_FILE
        learn_edit_file(args.learn_edit_json, terms_file)
        return
    if args.terms_list or args.terms_add or args.terms_remove:
        handle_terms_cli(
            args.terms_file or REWRITE_TERMS_FILE,
            list_=args.terms_list,
            add=args.terms_add,
            variants=args.terms_variant,
            remove=args.terms_remove,
        )
        return
    if args.second_opinion_report:
        print(second_opinion_report())
        return
    settings = rewrite_settings(
        backend=args.rewrite_backend,
        terms_file=args.terms_file,
        cloud_model=args.cloud_model,
        cloud_location=args.cloud_location,
        cloud_project=args.cloud_project,
        cloud_timeout=args.cloud_timeout,
        cloud_max_output_tokens=args.cloud_max_output_tokens,
        cloud_max_terms=args.cloud_max_terms,
        cloud_thinking_budget=(
            "none" if args.cloud_thinking_budget == -1 else args.cloud_thinking_budget
        ),
    )
    context = load_request_context(args.context_json)

    if args.serve:
        serve_forever(language=args.language, polish=not args.no_polish, config=settings)
        return

    if args.list_devices:
        import sounddevice as sd
        for i, d in enumerate(sd.query_devices()):
            if d.get("max_input_channels", 0) > 0:
                print(f"{i}\t{d['name']}\t({d['max_input_channels']} in)")
        return

    if args.wav:
        r = process_wav_file(
            args.wav,
            language=args.language,
            polish=not args.no_polish,
            context=context,
            config=settings,
            second_opinion_sync=args.second_opinion,
        )
        if r.get("second_opinion"):
            print(json.dumps(r["second_opinion"], ensure_ascii=False), file=sys.stderr)
        # stdout 只放最終文字：app 整份取用，不可能再誤取診斷行（以前的 "[raw] …" 洩漏）
        if r["text"]:
            print(r["text"])
        if r["status"] == "silence":
            sys.exit(3)
        if r["status"] == "suspect":
            sys.exit(4)
        if args.copy or args.paste:
            subprocess.run(["pbcopy"], input=r["text"].encode("utf-8"), check=True)
            if args.paste:
                subprocess.run(["osascript", "-e", 'tell application "System Events" to keystroke "v" using command down'], check=True)
        return

    # live record
    import sounddevice as sd
    import numpy as np
    sr = SAMPLE_RATE
    secs = args.record_seconds
    print(f"[rec] recording {secs}s @ {sr}Hz ... speak now", file=sys.stderr)
    dev = resolve_input_device(args.device or DEFAULT_INPUT_DEVICE)
    if dev is not None:
        print(f"[rec] using device {dev}: {sd.query_devices(dev)['name']}", file=sys.stderr)
    audio = sd.rec(int(secs * sr), samplerate=sr, channels=1, dtype="float32", device=dev)
    sd.wait()
    audio = audio.flatten()
    print(f"[rec] captured {len(audio)} samples, rms {rms_db(audio):.1f} dB", file=sys.stderr)
    if is_silence(audio):
        print("[gate] 靜音 → 不跑 Whisper（避免幻覺）", file=sys.stderr)
        sys.exit(3)
    votes = suspect_reason(audio)
    raw = transcribe_audio(audio, language=args.language)
    if not raw:
        print("[stt] 沒有辨識到內容（靜音，或麥克風根本沒收到訊號）", file=sys.stderr)
    print(f"[raw] {raw}", file=sys.stderr)
    print(f"[raw-tw] {to_taiwan_traditional(raw)}", file=sys.stderr)
    final, _ = apply_rewrite(
        raw,
        enabled=not args.no_polish,
        context=context,
        config=settings,
    )
    final = normalize_spacing(final)
    final = to_taiwan_traditional(final)
    print(f"[final] {final}", file=sys.stderr)
    if final:
        print(final)
    fabricated = is_fabrication(raw, final)
    if fabricated:
        print(f"[fabrication] {fabricated} → 不自動貼上", file=sys.stderr)
    if final and (len(votes) >= SUSPECT_VOTES_REQUIRED or fabricated):
        print(f"[suspect] 疑似不是語音（{len(votes)} 票：" + "；".join(votes) +
              "）→ 不自動貼上（文字仍在 stdout，可用選單手動貼）", file=sys.stderr)
        sys.exit(4)
    if args.copy or args.paste:
        subprocess.run(["pbcopy"], input=final.encode("utf-8"), check=True)
        if args.paste:
            subprocess.run(["osascript", "-e", 'tell application "System Events" to keystroke "v" using command down'], check=True)

if __name__ == "__main__":
    main()
