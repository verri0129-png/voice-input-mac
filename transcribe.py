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

def is_silence(samples, threshold_db=-45, min_duration_s=0.3):
    """Quick energy gate before running Whisper to avoid hallucination on silence."""
    import numpy as np
    if len(samples) < SAMPLE_RATE * min_duration_s:
        return True
    # check overall RMS
    db = rms_db(samples.astype("float32"))
    return db < threshold_db

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
    # extra guard: if avg_logprob very low and compression low, likely hallucination
    segs = result.get("segments") or []
    if segs:
        avg_lp = sum(s.get("avg_logprob", 0) for s in segs) / len(segs)
        if len(text) < 5 and avg_lp < -0.8:
            print(f"[guard] low confidence hallucination filtered: lp={avg_lp:.2f} text='{text}'", file=sys.stderr)
            return ""
    return text

def to_taiwan_traditional(text: str) -> str:
    """Unconditional Simplified->Traditional Taiwan (with phrases). No-op if no CJK."""
    if not text or not any("\u4e00" <= c <= "\u9fff" for c in text):
        return text
    try:
        from opencc import OpenCC
        return OpenCC("s2twp").convert(text)
    except Exception:
        return text

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
    polished = out[len(prompt):] if out.startswith(prompt) else out
    polished = polished.strip()
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
    args = ap.parse_args()

    if args.list_devices:
        import sounddevice as sd
        for i, d in enumerate(sd.query_devices()):
            if d.get("max_input_channels", 0) > 0:
                print(f"{i}\t{d['name']}\t({d['max_input_channels']} in)")
        return

    if args.wav:
        import mlx_whisper
        lang = None if args.language == "auto" else args.language
        result = mlx_whisper.transcribe(args.wav, path_or_hf_repo=STT_REPO, language=lang, verbose=False)
        raw = (result.get("text") or "").strip()
        print(f"[raw] {raw}")
        if args.no_polish:
            final = raw
        else:
            final = polish_text(raw) if raw else ""
        # final hard guarantee: any remaining simplified -> Taiwan traditional (covers --no-polish and polish fallback failures)
        final = to_taiwan_traditional(final)
        print(final)
        if args.copy or args.paste:
            subprocess.run(["pbcopy"], input=final.encode("utf-8"), check=True)
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
    raw = transcribe_audio(audio, language=args.language)
    print(f"[raw] {raw}")
    if args.no_polish:
        final = raw
    else:
        final = polish_text(raw) if raw else ""
    final = to_taiwan_traditional(final)
    print(final)
    if args.copy or args.paste:
        subprocess.run(["pbcopy"], input=final.encode("utf-8"), check=True)
        if args.paste:
            subprocess.run(["osascript", "-e", 'tell application "System Events" to keystroke "v" using command down'], check=True)

if __name__ == "__main__":
    main()
