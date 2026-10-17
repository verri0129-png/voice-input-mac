from mlx_lm import load, generate
import time

model_id = "YouJun3627/Breeze-7B-Instruct-v0_1-4bit-mlx"
print(f"loading {model_id} ... (4.5GB, warm ~10-15s)")
t0 = time.time()
model, tokenizer = load(model_id)
print(f"loaded in {time.time()-t0:.1f}s, tokenizer vocab: {len(tokenizer)}")

SYSTEM = """你是專業的語音轉文字潤飾助手。使用者會給你一段 ASR 原始文字（可能含口吃、重複、填充詞、半句修正）。
請一次完成五件事並只輸出潤飾後的最終文字，不要解釋：
1. 去填充詞：移除「呃、嗯、那個、然後、就是」等無意義語助詞
2. 去重複：刪除因口吃或猶豫產生的重複字詞句
3. 取最終意圖：若使用者中途改口，只保留最後一次表達的意圖
4. 結構化：視內容自動分段或條列，讓語意清晰
5. 優化詞彙：修正口語贅字為書面精煉用詞，保留原文語言（中/英文夾雜照舊）

若輸入已乾淨，請原樣輸出。"""

def polish(raw: str):
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": raw},
    ]
    prompt = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    t0 = time.time()
    out = generate(model, tokenizer, prompt=prompt, max_tokens=512, verbose=False)
    dt = time.time() - t0
    # generate returns string after prompt; need to strip prompt
    # mlx_lm generate returns only generation if prompt is string? check
    text = out[len(prompt):] if out.startswith(prompt) else out
    # fallback: if model echoed prompt, try to extract
    return text.strip(), dt

tests = [
    "呃 就是 那個 我想說 呃 這份報告 那個 呃 重複重複 已經完成了 然後 然後我想請你幫我 呃 幫我看一下 就是看一下有沒有錯字",
    "你好,这是语音输入测试,包含填充词2,那个,然后重复重复的句子。",
    "I want to schedule a meeting um tomorrow at 3pm, actually make it 4pm, yeah 4pm tomorrow",
]

for raw in tests:
    polished, dt = polish(raw)
    print(f"\n--- raw: {raw}")
    print(f"polished ({dt:.2f}s): {polished}")

