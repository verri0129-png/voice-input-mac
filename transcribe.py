"""Pipe: record or file -> mlx-whisper -> qwen polish -> clipboard or stdout."""
import argparse, subprocess, sys, tempfile, os, time
from pathlib import Path

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
#       VOICE_STT_REPO=mlx-community/whisper-large-v3-turbo
#       VOICE_POLISH_REPO=mlx-community/Qwen2.5-7B-Instruct-4bit
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
POLISH_REPO = _setting("VOICE_POLISH_REPO", "mlx-community/Qwen2.5-7B-Instruct-4bit")
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

def wav_samples(path):
    """16-bit PCM wav -> float32 array in [-1,1], or None if unreadable.
    The file path had NO silence gate before, which is how a silent recording turned into
    a Whisper hallucination that got pasted as if it were speech."""
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
    return np.frombuffer(data, dtype="<i2").astype("float32") / 32768.0

def transcribe_audio(audio, language="zh"):
    import mlx_whisper
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
    result = mlx_whisper.transcribe(
        audio,
        path_or_hf_repo=STT_REPO,
        language=language if language else None,
        verbose=False,
    )
    text = (result.get("text") or "").strip()
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

def polish_text(text: str) -> str:
    if not text or not text.strip():
        return ""
    from mlx_lm import load, generate
    # cache model in module
    if not hasattr(polish_text, "_model"):
        print(f"[polish] loading {POLISH_REPO} ...", file=sys.stderr)
        polish_text._model, polish_text._tok = load(POLISH_REPO)
    model, tok = polish_text._model, polish_text._tok
    system = (
        "你是語音轉文字的潤飾助手。只做潤飾，不做翻譯。\n"
        "規則（只輸出最終潤飾結果，不要解釋、不要加引號）：\n"
        "1. 移除填充詞：呃、嗯、那個、然後、就是、um、uh、like、you know 等\n"
        "2. 刪除猶豫重複的字詞句\n"
        "3. 若中途改口，只保留最後意圖\n"
        "4. 依語意自動分段或條列\n"
        "5. 口語贅字改為精煉書面用詞\n"
        "重要：\n"
        "- 保持原文語言，中文就輸出中文、英文就輸出英文、中英夾雜就保持夾雜，絕對不要翻譯。\n"
        "- 任何中文必須用繁體中文輸出，嚴禁簡體（盡量 → 儘量、軟件 → 軟體、數據 → 資料、視頻 → 影片、網絡 → 網路、…等一律轉繁體用詞）。"
    )
    messages = [{"role": "system", "content": system}, {"role": "user", "content": text}]
    prompt = tok.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    out = generate(model, tok, prompt=prompt, max_tokens=512, verbose=False)
    # mlx_lm.generate(prompt=...) 只回傳「新生成的部分」，不含 prompt，所以正常情況
    # 不會 startswith(prompt)。只有在它真的把 prompt 一起吐回來時才需要切掉（那才是異常）。
    if prompt[:40] in out:
        print(f"[polish] 警告：模型輸出夾帶 prompt（out={len(out)}字）", file=sys.stderr)
    polished = out[len(prompt):] if out.startswith(prompt) else out
    polished = polished.strip()
    if not polished:
        print(f"[polish] 警告：潤飾結果為空（原文 {len(text)} 字）——不輸出任何東西，不退回原文", file=sys.stderr)
    # hard fallback: model often still outputs Simplified; s2twp converts phrases (软件/数据/视频/网络 -> 軟體/資料/影片/網路)
    # s2t only converts characters, leaving 数据 as 數據; s2t.json causes s2t.json.json error
    polished = to_taiwan_traditional(polished)
    return polished

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

def process_wav_file(path, language="zh", polish=True, label=""):
    """單檔處理（一次性 CLI 與 --serve 共用）。

    回傳 {"status": ..., "text": ...}：
      ok      = 可以貼上
      suspect = exit 4：有文字但疑似不是語音 → app 不自動貼、保留文字
      silence = exit 3：沒有可用語音
    """
    a = wav_samples(path)
    if a is not None and is_silence(a):
        peak_db, window_db = audio_levels(a)
        print(f"[gate]{label} 沒收到聲音（peak {peak_db:.1f} dB、最大 0.5s 窗 rms {window_db:.1f} dB）"
              f" → 不跑 Whisper（避免幻覺）", file=sys.stderr)
        return {"status": "silence", "text": ""}
    import mlx_whisper
    lang = None if language == "auto" else language
    result = mlx_whisper.transcribe(path, path_or_hf_repo=STT_REPO, language=lang, verbose=False)
    # 音訊面 + Whisper 面的證據一起投票（單一票不攔，避免誤殺真人說話）
    votes = suspect_reason(a, result) if a is not None else suspect_reason(result=result)
    raw = (result.get("text") or "").strip()
    if not raw:
        print("[stt] 沒有辨識到內容（靜音，或麥克風根本沒收到訊號）", file=sys.stderr)
    print(f"[raw] {raw}", file=sys.stderr)
    print(f"[raw-tw] {to_taiwan_traditional(raw)}", file=sys.stderr)
    final = raw if not polish else (polish_text(raw) if raw else "")
    # final hard guarantee: any remaining simplified -> Taiwan traditional
    final = to_taiwan_traditional(final)
    print(f"[final] {final}", file=sys.stderr)
    fabricated = is_fabrication(raw, final)
    if fabricated:
        print(f"[fabrication] {fabricated} → 不自動貼上", file=sys.stderr)
    if final and (len(votes) >= SUSPECT_VOTES_REQUIRED or fabricated):
        print(f"[suspect] 疑似不是語音（{len(votes)} 票：" + "；".join(votes) +
              "）→ 不自動貼上（文字仍在 stdout，可用選單手動貼）", file=sys.stderr)
        return {"status": "suspect", "text": final}
    return {"status": "ok", "text": final}

def serve_forever(language="zh", polish=True):
    """暖機伺服器：模型只載入一次，之後每個請求只做推論。

    動機（實測）：一次性路徑每次都要重載 Whisper(1.6GB)+Qwen(4GB) → 冷 4.26s；
    模型還在記憶體時只要 0.88s。所以在「開始錄音」時就把這個程序拉起來暖機，
    使用者講話的時間剛好拿來載模型；按下停止後只剩推論。

    協定：stdin 每行一個 wav 絕對路徑 → stdout 每行一個 JSON 結果（診斷一律走 stderr）。
    app 端關閉時 stdin 會 EOF，這裡就跟著結束，不會留孤兒程序。
    """
    import json
    print("[serve] 暖機中（OpenCC + Whisper + Qwen）…", file=sys.stderr)
    try:
        to_taiwan_traditional("暖機")
        import numpy as np
        # 必須「不是靜音」才會真的觸發 Whisper 載入（is_silence 會提早 return）
        warm = (np.random.default_rng(0).standard_normal(SAMPLE_RATE).astype("float32") * 0.02)
        transcribe_audio(warm, language=language)
        if polish:
            polish_text("暖機。")
        print("[serve] 暖機完成，等待請求", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"[serve] 暖機失敗（不致命：第一次請求會慢一點）: {e!r}", file=sys.stderr)
    idle_timeout = float(os.environ.get("VOICE_SERVE_IDLE_TIMEOUT", "600"))  # 10 分鐘沒用到就退出，別整天佔 6GB
    import select
    while True:
        try:
            ready, _, _ = select.select([sys.stdin], [], [], idle_timeout)
        except (OSError, ValueError):
            break
        if not ready:
            print(f"[serve] 閒置 {int(idle_timeout)}s → 結束（下次開始錄音會重新暖機）", file=sys.stderr)
            break
        line = sys.stdin.readline()
        if not line:      # app 結束 → stdin EOF → 不留孤兒程序
            break
        path = line.strip()
        if not path:
            continue
        if path == "__quit__":
            break
        try:
            r = process_wav_file(path, language=language, polish=polish, label="[serve]")
        except Exception as e:  # noqa: BLE001
            r = {"status": "error", "text": "", "detail": repr(e)}
            print(f"[serve] 處理失敗: {e!r}", file=sys.stderr)
        print(json.dumps(r, ensure_ascii=False), flush=True)
    print("[serve] 結束（stdin EOF 或收到 __quit__）", file=sys.stderr)

def main():
    ap = argparse.ArgumentParser(description="Transcribe + polish (local, no network)")
    ap.add_argument("wav", nargs="?", help="wav file (16kHz mono). If omitted, records from mic.")
    ap.add_argument("--no-polish", action="store_true")
    ap.add_argument("--language", default="zh", help="zh / en / auto (empty for auto-detect)")
    ap.add_argument("--record-seconds", type=float, default=5)
    ap.add_argument("--device", default=None, help="input device: index or name substring (default: VOICE_INPUT_DEVICE from local.env, else system default). Not hardcoded — M4/M2 devices differ.")
    ap.add_argument("--list-devices", action="store_true", help="list audio input devices and exit")
    ap.add_argument("--copy", action="store_true", help="copy result to clipboard via pbcopy")
    ap.add_argument("--paste", action="store_true", help="paste via clipboard+ AppleScript Cmd+V")
    ap.add_argument("--serve", action="store_true",
                    help="暖機伺服器：stdin 每行一個 wav 路徑，stdout 每行一個 JSON 結果（模型只載入一次）")
    args = ap.parse_args()

    if args.serve:
        serve_forever(language=args.language, polish=not args.no_polish)
        return

    if args.list_devices:
        import sounddevice as sd
        for i, d in enumerate(sd.query_devices()):
            if d.get("max_input_channels", 0) > 0:
                print(f"{i}\t{d['name']}\t({d['max_input_channels']} in)")
        return

    if args.wav:
        r = process_wav_file(args.wav, language=args.language, polish=not args.no_polish)
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
    if args.no_polish:
        final = raw
    else:
        final = polish_text(raw) if raw else ""
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
