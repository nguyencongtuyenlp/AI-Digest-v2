# Daily Digest Agent

Local-first AI Agent chạy trên Apple Silicon để:
- thu thập tin AI/Tech từ nhiều nguồn
- chấm điểm và lọc bài đáng chú ý
- lưu Notion
- tạo brief Telegram chính
- tạo lane GitHub repo/release riêng

Mặc định hệ ưu tiên local inference qua MLX, không cần API inference để chạy hằng ngày.

## Features

| Feature | Mô tả |
|---|---|
| Multi-source gathering | Curated RSS + official blogs + GitHub + watchlist + DDG bổ sung + HN + Reddit + Telegram channels |
| 6-type classification | Research · Product · Business · Policy & Ethics · Society & Culture · Practical |
| Local LLM inference | MLX trên Apple Silicon với Qwen2.5-32B-Instruct-4bit + fallback 14B |
| Optional Grok reranker | Nếu có `XAI_API_KEY`, Grok rerank shortlist để chọn bài nào đáng lên main brief |
| Notion archiving | Tạo hoặc reuse page theo URL nguồn để giảm duplicate |
| Telegram delivery | Brief chính vào `TELEGRAM_THREAD_ID`, GitHub repo digest vào `TELEGRAM_GITHUB_THREAD_ID`, Facebook News vào `TELEGRAM_FACEBOOK_THREAD_ID` |
| Reviewability | Có run report markdown để kiểm tra nguồn, score, candidates và quality gate |
| Run health | Mỗi run có `health_status` + `publish_ready` để biết batch nào nên publish thật |
| Eval harness | Có script regression `eval_digest.py` để đo type/tier/delivery trên bộ case cố định |
| Preview workflow | Chạy preview trong UI rồi `Approve Preview` để publish đúng batch preview đó |

## Architecture

```text
gather
→ normalize_source
→ deduplicate
→ collect_feedback
→ classify_and_score
→ deep_analysis (conditional)
→ recommend_idea
→ compose_note_summary
→ delivery_judge
→ save_notion
→ summarize_vn
→ quality_gate
→ send_telegram
→ generate_run_report
→ END
```

Pipeline dùng LangGraph StateGraph và tách 3 lane Telegram:
- `telegram_messages`: brief công nghệ/AI chính
- `github_topic_messages`: digest GitHub repo/release riêng
- `facebook_topic_messages`: topic riêng cho Facebook News/manual social signals

## Cài đặt

### 1. Tạo virtualenv

```bash
cd /Users/quangdang/Projects/daily-digest-agent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Cấu hình `.env`

```bash
cp config/.env.example config/.env
```

Điền các biến chính:
- `NOTION_TOKEN`
- `NOTION_DATABASE_ID`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `TELEGRAM_THREAD_ID`
- `TELEGRAM_GITHUB_THREAD_ID`
- `TELEGRAM_FACEBOOK_THREAD_ID`

Optional:
- `TELETHON_API_ID`
- `TELETHON_API_HASH`
- `TELETHON_SESSION_NAME`
- `XAI_API_KEY`
- `GROK_DELIVERY_MODEL`
- `ENABLE_SOCIAL_SIGNALS`
- `ENABLE_FACEBOOK_AUTO`
- `FACEBOOK_AUTO_TARGETS_FILE`
- `FACEBOOK_CHROME_PROFILE_DIR`
- `FACEBOOK_STORAGE_STATE_FILE`

## Chạy

### Chạy publish thật

```bash
source .venv/bin/activate
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python main.py
```

### Chạy UI local

```bash
source .venv/bin/activate
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python ui_server.py
```

Mặc định UI ở:

```text
http://127.0.0.1:8787
```

Có thể đổi bằng:

```env
DIGEST_UI_HOST=127.0.0.1
DIGEST_UI_PORT=8787
```

Flow khuyên dùng:
- `Preview`: chạy full reasoning nhưng chưa publish
- review kết quả trong UI
- `Approve Preview`: publish đúng batch preview đó, không regather lại từ đầu

### Chạy eval regression

```bash
source .venv/bin/activate
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python eval_digest.py --write-report
```

Script này sẽ đọc [config/prompt_tuning_cases.jsonl](/Users/quangdang/Projects/daily-digest-agent/config/prompt_tuning_cases.jsonl), chấm deterministic regression cho type/tier/delivery, rồi ghi report markdown vào `reports/`.

### Bật Grok để chọn bài lên brief thông minh hơn

Nếu bạn có xAI API key, chỉ cần thêm vào `config/.env`:

```env
XAI_API_KEY=...
GROK_DELIVERY_JUDGE_ENABLED=1
GROK_DELIVERY_MODEL=grok-4-1-fast-non-reasoning
```

Hệ sẽ giữ gather + scoring local như cũ, nhưng ở bước `delivery_judge` sẽ gửi một shortlist nhỏ lên Grok để:
- rerank bài nào đáng lên main Telegram brief
- siết bớt repo/tool noise
- ưu tiên bài founder-grade, mới, có giá trị vận hành
- tham chiếu feedback gần đây của team khi chọn shortlist cuối

Để giới hạn chi phí, Grok chỉ được gọi cho shortlist cuối, không chạy trên toàn bộ batch.

### Chạy lane GitHub repo riêng

```bash
source .venv/bin/activate
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python github_agent_brief.py
```

## Auto schedule

```bash
cp launchd.plist ~/Library/LaunchAgents/com.quangdang.daily-digest-agent.plist
launchctl load ~/Library/LaunchAgents/com.quangdang.daily-digest-agent.plist
```

Kiểm tra:

```bash
launchctl list | grep digest
```

Gỡ:

```bash
launchctl unload ~/Library/LaunchAgents/com.quangdang.daily-digest-agent.plist
```

## Project Structure

```text
daily-digest-agent/
├── main.py
├── pipeline_runner.py
├── ui_server.py
├── graph.py
├── db.py
├── source_catalog.py
├── runtime_presets.py
├── editorial_guardrails.py
├── digest_formatter.py
├── github_agent_brief.py
├── feedback_loop.py
├── mlx_runner.py
├── launchd.plist
├── requirements.txt
├── config/
│   ├── .env.example
│   ├── prompt_daily_digest.md
│   ├── output_templates.md
│   └── watchlist_seeds.txt
├── nodes/
│   ├── gather_news.py
│   ├── normalize_source.py
│   ├── deduplicate.py
│   ├── collect_feedback.py
│   ├── classify_and_score.py
│   ├── deep_analysis.py
│   ├── recommend_idea.py
│   ├── compose_note_summary.py
│   ├── delivery_judge.py
│   ├── save_notion.py
│   ├── summarize_vn.py
│   ├── quality_gate.py
│   ├── send_telegram.py
│   └── generate_run_report.py
├── reports/
└── README.md
```

## Debug

```bash
# Kiểm tra logs
cat digest.log
cat digest_error.log

# Chạy publish với log ra file
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python main.py 2>&1 | tee debug_output.txt

# Chạy UI local
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python ui_server.py

# Kiểm tra database
python -c "from db import get_history; print(len(get_history()))"

# Xem RAM đang dùng trên Mac
top -l 1 | grep PhysMem

# Chạy eval và ghi report
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python eval_digest.py --write-report
```

## Tùy chỉnh

- Thêm watchlist sếp/team trong `config/watchlist_seeds.txt`
- Chỉnh nguồn và query trong `source_catalog.py`
- Đổi model trong `config/.env`
- Đổi prompt trong `config/prompt_daily_digest.md`
- Đổi giờ chạy trong `launchd.plist`
- Facebook auto dùng `config/facebook_auto_targets.txt` và session Chrome lưu trong `config/facebook_chrome_profile`
- Chạy `./.venv/bin/python facebook_login_setup.py` một lần để login Facebook và lưu `storage_state` cho auto adapter
- Social/Facebook luôn route sang `TELEGRAM_FACEBOOK_THREAD_ID`, không cạnh tranh với main brief
