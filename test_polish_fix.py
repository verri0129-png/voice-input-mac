from mlx_lm import load, generate
model_id = "YouJun3627/Breeze-7B-Instruct-v0_1-4bit-mlx"
model, tokenizer = load(model_id)

# 試兩個版本的 prompt
SYSTEM_V2 = """你是語音轉文字的潤飾助手。只做潤飾，不做翻譯。
規則（只輸出最終潤飾結果，不要解釋）：
1. 移除口吃填充詞：呃、嗯、那個、然後、就是、um、uh、like、you know
2. 刪除猶豫重複的字詞
3. 若中途改口，只保留最後一次的意圖
4. 依語意自動分段或條列
5. 把口語贅字改為精煉書面用詞
重要：保持原文語言，中文輸入就輸出中文，英文輸入就輸出英文，中英夾雜就保持夾雜，絕對不要把英文翻成中文或反之。"""

def polish(raw, system):
    messages = [{"role": "system", "content": system}, {"role": "user", "content": raw}]
    prompt = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    out = generate(model, tokenizer, prompt=prompt, max_tokens=512, verbose=False)
    text = out[len(prompt):] if out.startswith(prompt) else out
    return text.strip()

tests = [
    "I want to schedule a meeting um tomorrow at 3pm, actually make it 4pm, yeah 4pm tomorrow",
    "呃 就是 那個 我想說 這份報告 那個 已經完成了 然後我想請你幫我 幫我看一下 就是看一下有沒有錯字",
    "我們明天下午要開會然後然後那個地點在101大樓呃101大樓的會議室",
    "This is a test 然後我想要測試一下中英夾雜 um like 這樣會怎樣",
]

for raw in tests:
    out = polish(raw, SYSTEM_V2)
    print(f"\nraw: {raw}\n -> {out}")
