import mlx_whisper
import numpy as np
import tempfile, os, subprocess

# 生成一段測試音訊：用 ffmpeg 產生中文語音測試（或直接測試模型載入）
# 先驗證模型能載入並做一次空音訊 transcribe
print("loading whisper-large-v3-turbo ...")
# 產生 1 秒靜音 16kHz
sr = 16000
audio = np.zeros(sr * 1, dtype=np.float32)
print(f"audio shape: {audio.shape}, dtype: {audio.dtype}")

# 用 numpy array 模式測試（避免檔案 IO）
import time
t0 = time.time()
result = mlx_whisper.transcribe(audio, path_or_hf_repo="mlx-community/whisper-large-v3-turbo", language="zh", verbose=False)
elapsed = time.time() - t0
print(f"transcribe silence took {elapsed:.2f}s")
print(f"result: {result}")

# 測試一段 ffmpeg 合成的中文 TTS（若有 say）
# 使用 macOS say 產生測試音檔
wav_path = "/tmp/test_stt_say.wav"
try:
    subprocess.run(["say", "-v", "Meijia", "-r", "180", "--file-format=WAVE", f"--data-format=LEI16@16000", "-o", wav_path, "你好，這是語音輸入測試，包含填充詞呃、那個、然後重複重複的句子。"], check=True, timeout=10)
    print(f"say generated: {os.path.getsize(wav_path)} bytes")
    t0 = time.time()
    result2 = mlx_whisper.transcribe(wav_path, path_or_hf_repo="mlx-community/whisper-large-v3-turbo", language="zh", verbose=False)
    print(f"say transcribe took {time.time()-t0:.2f}s")
    print(f"say result: {result2}")
except Exception as e:
    print(f"say test skipped: {e}")
    # fallback: ffmpeg sine tone
    try:
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-ar", "16000", wav_path], capture_output=True, timeout=10)
        t0 = time.time()
        result2 = mlx_whisper.transcribe(wav_path, path_or_hf_repo="mlx-community/whisper-large-v3-turbo", verbose=False)
        print(f"sine transcribe: {result2} in {time.time()-t0:.2f}s")
    except Exception as e2:
        print(f"ffmpeg fallback also failed: {e2}")
