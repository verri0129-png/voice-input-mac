import mlx_whisper
import numpy as np
import time

# 測試：白噪音、短靜音是否會幻覺
for desc, audio in [
    ("silence 1s", np.zeros(16000, dtype=np.float32)),
    ("silence 3s", np.zeros(48000, dtype=np.float32)),
    ("low noise", np.random.randn(16000).astype(np.float32)*0.001),
]:
    t0=time.time()
    r=mlx_whisper.transcribe(audio, path_or_hf_repo="mlx-community/whisper-large-v3-turbo", language="zh", verbose=False, no_speech_threshold=0.6)
    print(f"{desc}: text='{r['text']}' no_speech={r['segments'][0]['no_speech_prob']:.4f} avg_logprob={r['segments'][0]['avg_logprob']:.3f} ({time.time()-t0:.2f}s)")
    # 試更高的 threshold
    r2=mlx_whisper.transcribe(audio, path_or_hf_repo="mlx-community/whisper-large-v3-turbo", language="zh", verbose=False, no_speech_threshold=0.9)
    print(f"  -> thr 0.9: text='{r2['text']}' no_speech={r2['segments'][0]['no_speech_prob']:.4f}")

# 測試 word_timestamps 與 hallucination_silence_threshold
print("\n--- word_timestamps test ---")
import tempfile, subprocess, os
wav_path="/tmp/test_say2.wav"
subprocess.run(["say","-v","Meijia","-r","180","--file-format=WAVE",f"--data-format=LEI16@16000","-o",wav_path,"測試短句。"], check=True, timeout=10)
for wt in [False, True]:
    t0=time.time()
    r=mlx_whisper.transcribe(wav_path, path_or_hf_repo="mlx-community/whisper-large-v3-turbo", language="zh", verbose=False, word_timestamps=wt, hallucination_silence_threshold=0.5 if wt else None)
    print(f"word_timestamps={wt}: {r['text']} ({time.time()-t0:.2f}s)")
