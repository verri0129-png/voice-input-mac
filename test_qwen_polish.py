from mlx_lm import load, generate
import time

model_id = "mlx-community/Qwen2.5-7B-Instruct-4bit"
print(f"loading {model_id} ...")
t0 = time.time()
model, tokenizer = load(model_id)
print(f"loaded in {time.time()-t0:.1f}s")

SYSTEM = """你是語音轉文字的潤飾助手。只做潤飾，不做翻譯。
規則（只輸出最終潤飾結果，不要解釋、不要加引號）：
1. 移除填充詞：呃、嗯、那個、然後、就是、um、uh、like、you know 等
2. 刪除猶豫重複的字詞句
3. 若中途改口，只保留最後意圖
4. 依語意自動分段或條列
5. 口語贅字改為精煉書面用詞
重要：保持原文語言，中文就輸出中文、英文就輸出英文、中英夾雜就保持夾雜，絕對不要翻譯。"""

def polish(raw):
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": raw}]
    prompt = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    out = generate(model, tokenizer, prompt=prompt, max_tokens=512, verbose=False)
    text = out[len(prompt):] if out.startswith(prompt) else out
    return text.strip()

tests = [
    "呃 就是 那個 我想說 這份報告 那個 已經完成了 然後我想請你幫我 幫我看一下 就是看一下有沒有錯字",
    "我們明天下午要開會然後然後那個地點在101大樓呃101大樓的會議室",
    "I want to schedule a meeting um tomorrow at 3pm, actually make it 4pm, yeah 4pm tomorrow",
    "This is a test 然後我想要測試一下中英夾雜 um like 這樣會怎樣",
    "你好,这是语音输入测试,包含填充词2,那个,然后重复重复的句子。",
    "呃我覺得這個專案呃這個專案其實蠻不錯的然後然後我們可以那個可以直接上線了",
]

for raw in tests:
    t0=time.time()
    out=polish(raw)
    print(f"\nraw: {raw}\n -> {out}  ({time.time()-t0:.2f}s)")

# Test edge: clean input should stay clean
print("\n--- clean input ---")
print(polish("今天下午三點在台北101開會"))
print(polish("Hello world, this is a clean sentence."))
