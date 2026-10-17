"""Pipe: record or file -> mlx-whisper -> qwen polish -> clipboard or stdout."""
import argparse, subprocess, sys, tempfile, os, time
from pathlib import Path

# Ensure Homebrew ffmpeg is found under non-interactive ssh (PATH is /usr/bin:/bin only)
for _p in ("/opt/homebrew/bin", "/usr/local/bin"):
    if _p not in os.environ.get("PATH", "") and Path(_p, "ffmpeg").exists():
        os.environ["PATH"] = _p + ":" + os.environ.get("PATH", "")

STT_REPO = "mlx-community/whisper-large-v3-turbo"
POLISH_REPO = "mlx-community/Qwen2.5-7B-Instruct-4bit"
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
    # hard fallback: if model still outputs Simplified, convert via OpenCC if available
    try:
        from opencc import OpenCC  # s2t
        if any("\u4e00" <= c <= "\u9fff" for c in polished):
            polished = OpenCC("s2t").convert(polished)
    except Exception:
        pass
    return polished

def main():
    ap = argparse.ArgumentParser(description="Transcribe + polish (local, no network)")
    ap.add_argument("wav", nargs="?", help="wav file (16kHz mono). If omitted, records from mic.")
    ap.add_argument("--no-polish", action="store_true")
    ap.add_argument("--language", default="zh", help="zh / en / auto (empty for auto-detect)")
    ap.add_argument("--record-seconds", type=float, default=5)
    ap.add_argument("--copy", action="store_true", help="copy result to clipboard via pbcopy")
    ap.add_argument("--paste", action="store_true", help="paste via clipboard+ AppleScript Cmd+V")
    args = ap.parse_args()

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
    audio = sd.rec(int(secs * sr), samplerate=sr, channels=1, dtype="float32")
    sd.wait()
    audio = audio.flatten()
    print(f"[rec] captured {len(audio)} samples, rms {rms_db(audio):.1f} dB", file=sys.stderr)
    raw = transcribe_audio(audio, language=args.language)
    print(f"[raw] {raw}")
    if args.no_polish:
        final = raw
    else:
        final = polish_text(raw) if raw else ""
    print(final)
    if args.copy or args.paste:
        subprocess.run(["pbcopy"], input=final.encode("utf-8"), check=True)
        if args.paste:
            subprocess.run(["osascript", "-e", 'tell application "System Events" to keystroke "v" using command down'], check=True)

if __name__ == "__main__":
    main()
