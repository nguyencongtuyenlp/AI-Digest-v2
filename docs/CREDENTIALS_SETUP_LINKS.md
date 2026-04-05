# Credential Setup Links

Checked on: 2026-04-05

Mục tiêu của file này là giúp lấy nhanh các credential còn thiếu để bật toàn bộ capability của `AI-Digest-v2`.

## 1. Core bắt buộc

### Notion

Env:
- `NOTION_TOKEN`
- `NOTION_DATABASE_ID`

Link trực tiếp:
- Tạo / quản lý integration: <https://www.notion.so/my-integrations>
- Auth guide chính thức: <https://developers.notion.com/docs/authorization>

Cách lấy:
- Vào `my-integrations` -> `New integration`
- Chọn workspace
- Tạo integration nội bộ
- Copy `Internal Integration Token` -> dán vào `NOTION_TOKEN`
- Mở database chính trong Notion -> copy URL -> lấy chuỗi ID 32 ký tự -> dán vào `NOTION_DATABASE_ID`
- Share database đó cho integration bằng `...` -> `Add connections`

Verify nhanh:
- Chạy preview/publish, nếu Notion đã share đúng thì repo sẽ tạo/reuse page thay vì báo thiếu quyền.

### Telegram Bot

Env:
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_BOT_USERNAME`
- `TELEGRAM_CHAT_ID`
- `TELEGRAM_THREAD_ID`

Link trực tiếp:
- BotFather: <https://t.me/BotFather>
- Telegram Bot tutorial chính thức: <https://core.telegram.org/bots/tutorial>

Cách lấy:
- Mở `@BotFather`
- Chạy `/newbot`
- Copy token bot -> dán vào `TELEGRAM_BOT_TOKEN`
- Username bot -> dán vào `TELEGRAM_BOT_USERNAME`

Lấy `TELEGRAM_CHAT_ID`:
- Thêm bot vào group/channel đích
- Gửi một tin nhắn thử trong chat đó
- Mở:
  - `https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/getUpdates`
- Tìm `chat.id` trong JSON -> dán vào `TELEGRAM_CHAT_ID`

Lấy `TELEGRAM_THREAD_ID`:
- Nếu group dùng forum topics, gửi 1 tin vào từng topic
- Mở lại `getUpdates`
- Tìm `message_thread_id` của từng topic
- Topic brief chính -> `TELEGRAM_THREAD_ID`

Ghi chú:
- `TELEGRAM_FACEBOOK_THREAD_ID` là legacy, repo hiện tại không còn dùng.

### Telethon / Telegram channel reader

Env:
- `TELETHON_API_ID`
- `TELETHON_API_HASH`
- `TELETHON_SESSION_NAME=digest_session`

Link trực tiếp:
- Telegram API tools: <https://my.telegram.org/apps>

Cách lấy:
- Đăng nhập Telegram ở `my.telegram.org`
- Tạo app mới
- Copy `api_id` -> `TELETHON_API_ID`
- Copy `api_hash` -> `TELETHON_API_HASH`
- Giữ `TELETHON_SESSION_NAME=digest_session`
- Sau đó chạy app/repo một lần để hoàn tất login và sinh file `digest_session.session`

Verify nhanh:
- `source_health_check.py` sẽ không còn báo `telethon_session` là missing/dead.

## 2. Optional nhưng rất nên có

### Notion topic pages

Env:
- `NOTION_TOPIC_DATABASE_ID`

Link trực tiếp:
- Mở database topic trong Notion workspace của bạn

Cách lấy:
- Tạo thêm một database riêng cho topic pages / watchlist intelligence
- Copy ID từ URL database đó -> dán vào `NOTION_TOPIC_DATABASE_ID`
- Share database này cho cùng integration ở trên

Nếu chưa có:
- Repo vẫn tạo artifact markdown trong `reports/topics_YYYY-MM-DD/`

### GitHub token

Env:
- `GITHUB_TOKEN`

Link trực tiếp:
- Tạo fine-grained PAT: <https://github.com/settings/personal-access-tokens/new>
- GitHub docs chính thức: <https://docs.github.com/github/authenticating-to-github/creating-a-personal-access-token>

Khuyên dùng:
- Fine-grained token
- Quyền read-only là đủ cho đa số use case repo/release/search hiện tại

### xAI / Grok

Env:
- `XAI_API_KEY`

Link trực tiếp:
- xAI Console: <https://console.x.ai/>
- Getting started chính thức: <https://docs.x.ai/docs/tutorial>

Cách lấy:
- Tạo account xAI
- Vào console
- Tạo API key ở trang API Keys
- Dán vào `XAI_API_KEY`

Ghi chú:
- Key này bật thêm Grok rerank, source-gap suggestions, scout.

## 3. Optional community signals

### Reddit API

Env:
- `REDDIT_CLIENT_ID`
- `REDDIT_CLIENT_SECRET`
- `REDDIT_USER_AGENT`

Link trực tiếp:
- Reddit app registration: <https://developers.reddit.com/app-registration>
- Reddit for Developers: <https://developers.reddit.com/docs/capabilities/server/reddit-api>

Ghi chú quan trọng:
- Reddit đã siết app registration hơn trước.
- Nếu flow cũ kiểu `/prefs/apps` không hoạt động, hãy ưu tiên đường chính thức ở `developers.reddit.com`.
- Nếu bạn chưa lấy được credential này, repo vẫn fallback sang Reddit public JSON nhưng kém ổn định hơn.

## 4. Optional Facebook source phụ

Không còn là delivery lane chính thức, nhưng vẫn có thể dùng như nguồn phụ.

Env / file:
- `FACEBOOK_STORAGE_STATE_FILE`

Link trực tiếp:
- Script local trong repo: [facebook_login_setup.py](/Users/quangdang/Projects/AI-digest-v2/facebook_login_setup.py)

Cách lấy:
- Chạy script login local để Playwright tạo `facebook_storage_state.json`
- Repo sẽ coi file này là stale nếu quá 7 ngày

Ghi chú:
- Không cần lấy token cloud nào riêng cho Facebook
- Chỉ cần session file local

## 5. Watchlist strategic env mới

Những biến này không phải credential, nhưng nếu bạn muốn bật lớp strategic watchlist qua env thì nên chuẩn bị:

Env:
- `WATCHLIST_COMPANIES`
- `WATCHLIST_PRODUCTS`
- `WATCHLIST_TOOLS`
- `WATCHLIST_POLICIES`
- `WATCHLIST_TOPICS`

Format:
- Dùng `||` để ngăn cách nhiều item

Ví dụ:

```env
WATCHLIST_COMPANIES=OpenAI||Anthropic||Google DeepMind
WATCHLIST_PRODUCTS=GPT-4.1||Claude Code||Gemini 2.5
WATCHLIST_TOOLS=LangGraph||AutoGen||OpenAI Agents SDK
WATCHLIST_POLICIES=EU AI Act||US AI policy
WATCHLIST_TOPICS=MCP||AI coding agent||browser use
```

## 6. Biến legacy có thể bỏ

Bạn không cần đi lấy các biến này nữa:
- `TELEGRAM_FACEBOOK_THREAD_ID`

Biến này có thể để trống hoặc xóa hẳn:
- `GROK_FACEBOOK_SCORE_ENABLED`

## 7. Thứ tự nên lấy nhanh nhất

1. `NOTION_TOKEN`
2. `NOTION_DATABASE_ID`
3. `TELEGRAM_BOT_TOKEN`
4. `TELEGRAM_CHAT_ID`
5. `TELEGRAM_THREAD_ID`
7. `TELETHON_API_ID`
8. `TELETHON_API_HASH`
9. `NOTION_TOPIC_DATABASE_ID`
10. `GITHUB_TOKEN`
11. `XAI_API_KEY`
12. `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET`

## 8. Sau khi lấy xong, verify ngay

Lệnh nên chạy:

```bash
python3 source_health_check.py
python3 weekly_memo.py --days 7 --write
python3 watchlist_intelligence.py --write
python3 -m unittest test_editorial_guardrails
```

Nếu muốn mình tiếp tục, bước sau cùng hợp lý là:
- bạn dán giá trị thật vào `config/.env`
- mình sẽ kiểm tra từng capability đã lên đủ chưa và báo phần nào còn thiếu cấu hình
