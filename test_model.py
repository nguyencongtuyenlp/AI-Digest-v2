from mlx_lm import load, generate

model_name = "mlx-community/Qwen2.5-32B-Instruct-4bit"
print(f"Bắt đầu tải và load model: {model_name}...")
model, tokenizer = load(model_name)
print("Tải xong! Bắt đầu chạy generate text thử...")

prompt = tokenizer.apply_chat_template(
    [{"role": "user", "content": "Ngắn gọn thôi. 1+1 bằng mấy?"}],
    tokenize=False,
    add_generation_prompt=True,
)
res = generate(model, tokenizer, prompt=prompt, max_tokens=20, verbose=True)
print("\nKết quả trả về:", res)
