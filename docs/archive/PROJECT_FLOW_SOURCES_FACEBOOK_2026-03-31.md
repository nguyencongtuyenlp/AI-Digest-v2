# Daily Digest Agent: Flow, Sources, APIs, và Độ Khả Thi Facebook

Ngày cập nhật: 2026-03-31

## 1. Mục đích tài liệu

Tài liệu này được viết sau khi đọc lại `context3.txt` và rà toàn bộ codebase hiện tại để trả lời 4 câu hỏi thực tế:

1. Hệ thống đang chạy flow như thế nào, từ lúc lấy tin đến lúc gửi ra ngoài?
2. Hiện tại đang dùng những nguồn nào?
3. Đang dùng những API/service nào thật sự?
4. Facebook hiện khả thi tới đâu trong kiến trúc hiện tại?

Điểm quan trọng nhất: repo này đã được thiết kế theo hướng **không phụ thuộc vào external LLM API** cho phần reasoning chính. Lõi phân tích đang chạy local bằng MLX/Qwen; các kết nối mạng chủ yếu nằm ở lớp **nguồn dữ liệu** và **publish**.

---

## 2. Tổng quan kiến trúc hiện tại

Entry point production:

- `main.py`
- `pipeline_runner.py`
- `graph.py`

Flow LangGraph hiện tại:

```text
gather
→ normalize_source
→ deduplicate
→ collect_feedback
→ classify_and_score
→ (nếu có bài đủ điểm) deep_analysis
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

Các mode vận hành:

- `publish`: chạy production thật, có thể ghi SQLite/Chroma, đẩy Notion, gửi Telegram
- `preview`: chạy reasoning nhưng không pollute dữ liệu thật và không publish
- UI local trong `ui_server.py` cho phép preview, approve rồi publish từ đúng preview state

Scheduler:

- `launchd.plist`: chạy tự động lúc 08:00 mỗi ngày
- `setup_scheduler.sh`: script hỗ trợ cài launchd

---

## 3. Flow chi tiết end-to-end

### Bước 1: Gather đa nguồn

Node: `nodes/gather_news.py`

Hệ thống lấy dữ liệu từ nhiều lớp:

- Curated RSS feeds
- GitHub repo/org/search signals
- Manual social signals
- Watchlist seeds
- Hacker News
- Reddit
- DuckDuckGo search bổ sung
- Telegram channels qua Telethon

Ý đồ kiến trúc ở đây rất rõ:

- **RSS + official sources** là lõi ổn định
- **search/community** chỉ là lớp mở rộng coverage
- **Facebook/social khó scrape** được đưa vào lane riêng dạng manual inbox để tránh phụ thuộc hạ tầng mong manh

### Bước 2: Normalize source

Node: `nodes/normalize_source.py`

Hệ thống enrich metadata cho từng item:

- `source_domain`
- `published_at`
- `age_hours`
- `freshness_bucket`
- `source_verified` theo heuristic
- `source_tier` A/B/C
- `content_available`
- `is_news_candidate`
- `is_ai_relevant`

Lưu ý quan trọng:

- Hệ **không giả `fetched_at` thành `published_at`**
- Nếu không xác định được ngày xuất bản thật, bài sẽ bị giảm độ tin cậy ở downstream

### Bước 3: Deduplicate

Node: `nodes/deduplicate.py`

Dedup theo 2 tầng:

- SQLite URL hash trong `db.py`
- Chroma semantic recall trong `memory.py`

Ngoài bỏ trùng, bước này còn gắn:

- `related_past`: các bài cũ cùng chủ đề để classifier và deep analysis có thêm context

### Bước 4: Feedback loop từ Telegram

Node: `nodes/collect_feedback.py`
Support code: `feedback_loop.py`

Hệ thống đọc feedback từ Telegram Bot API, gắn label heuristic như:

- `stale`
- `weak_source`
- `not_relevant`
- `want_more_depth`
- `promote_delivery`
- `skip_delivery`

Mục tiêu là để batch sau hiểu gu team/sếp tốt hơn, dù hiện tại đây vẫn là lightweight feedback loop chứ chưa phải learning system đầy đủ.

### Bước 5: Classify + score

Node: `nodes/classify_and_score.py`

Mỗi bài được:

- phân loại 1 trong 6 type
- chấm C1/C2/C3
- ra `total_score`
- sinh `summary_vi`
- sinh `editorial_angle`
- chọn `analysis_tier`
- gắn `tags`

LLM chính là local MLX model trong `mlx_runner.py`.

Repo hiện thiên về triage founder-grade:

- trọng số cao cho AI relevance
- phạt mạnh bài stale/off-topic
- boost cho nguồn mạnh, tín hiệu chiến lược, model/API/agent/infra/funding/regulation

### Bước 6: Deep analysis

Node: `nodes/deep_analysis.py`

Chỉ chạy cho `top_articles` đủ ngưỡng. Ngoài nội dung gốc, node này còn kéo thêm:

- community reactions qua DDG
- related history từ memory
- grounding facts/inferences/unknowns

Output là bản phân tích dài để dùng cho Notion page.

### Bước 7: Recommend idea + note summary

Nodes:

- `nodes/recommend_idea.py`
- `nodes/compose_note_summary.py`

Mục tiêu:

- biến bài phân tích thành khuyến nghị hành động
- nén thành `note_summary_vi` ngắn, usable cho Notion property và Telegram brief

### Bước 8: Delivery judge

Node: `nodes/delivery_judge.py`

Đây là lớp lọc trước Telegram:

- xét freshness
- xét groundedness
- xét operator value
- loại bài trùng event
- loại bài stale hoặc nguồn yếu

Không phải bài nào được lưu Notion cũng được lên Telegram.

### Bước 9: Save Notion + local memory

Node: `nodes/save_notion.py`

Nếu ở `publish` mode:

- tạo page Notion theo schema mapping
- lưu SQLite history
- lưu Chroma memory

Repo hiện hỗ trợ mapping động theo tên/type property, nghĩa là không hard-code hoàn toàn một schema duy nhất.

### Bước 10: Summarize + quality gate + Telegram

Nodes:

- `nodes/summarize_vn.py`
- `nodes/quality_gate.py`
- `nodes/send_telegram.py`

Flow cuối:

- dựng các message Telegram từ `telegram_candidates`
- validate summary/message
- nếu fail thì fallback về deterministic safe digest
- gửi qua Telegram Bot API

### Bước 11: Run report

Node: `nodes/generate_run_report.py`

Sau mỗi run, hệ xuất một file markdown report trong `reports/` để review:

- số lượng theo source
- số lượng theo type/tag
- Telegram candidates
- feedback summary
- runtime overrides

---

## 4. Các nguồn dữ liệu hiện đang dùng

## 4.1. Nguồn lõi ổn định

Khai báo trong `source_catalog.py`.

Nhóm này là backbone của hệ:

- OpenAI RSS
- Anthropic RSS
- Google / DeepMind RSS
- Hugging Face blog feed
- Microsoft AI feed
- NVIDIA news
- Databricks AI feed
- Cloudflare AI feed
- TechCrunch AI
- The Verge AI
- Ars Technica
- MIT News AI
- GenK AI

Đánh giá:

- Đây là lane bền nhất trong repo hiện tại
- Phù hợp để làm nguồn production dài hạn

## 4.2. GitHub signals

Nguồn:

- GitHub repo metadata
- GitHub releases
- GitHub org repos
- GitHub repository search

Dữ liệu lấy từ:

- watchlist mặc định trong `source_catalog.py`
- watchlist mở rộng từ `config/watchlist_seeds.txt`
- env overrides

Đánh giá:

- Rất hợp với nhu cầu theo dõi agent/tooling/framework
- Rẻ, sạch, có cấu trúc
- Là nguồn rất giá trị cho use case PM/founder theo dõi sản phẩm AI/dev tools

## 4.3. Watchlist thủ công

File:

- `config/watchlist_seeds.txt`

Hỗ trợ:

- URL trực tiếp
- query thủ công
- GitHub repo
- GitHub org
- GitHub query

Đây là lane rất thực dụng vì bám đúng gu nguồn của team/sếp thay vì chỉ dựa vào search chung chung.

## 4.4. Hacker News

Lấy từ public API:

- top stories
- lọc AI relevance

Đánh giá:

- tốt cho social proof kiểu dân công nghệ
- chất lượng khá ổn
- nên giữ

## 4.5. Reddit

Lấy từ JSON endpoint công khai của các subreddit:

- `LocalLLaMA`
- `MachineLearning`
- `OpenAI`
- `Anthropic`
- `singularity`

Đánh giá:

- tốt cho community pulse
- nhưng cần coi là signal phụ, không phải nguồn fact chính

## 4.6. DuckDuckGo search

Vai trò hiện tại:

- nguồn bổ sung
- không phải core source

Repo đã tự bảo vệ lane này bằng:

- trusted/review/low-quality domain lists
- heuristics chặn landing pages, search pages, dictionary noise
- founder-grade filtering

Đánh giá:

- hữu ích để mở rộng coverage
- nhưng vẫn là lane dễ nhiễu nhất

## 4.7. Telegram channels

Đọc qua Telethon nếu có user credentials.

Vai trò:

- bắt tín hiệu cộng đồng Việt Nam / niche channels

Đánh giá:

- hữu ích nếu team đã có vài channel quen thuộc
- nhưng không nên coi là primary fact source

## 4.8. Manual social signals

File:

- `config/social_signal_inbox.txt`

CLI hỗ trợ append:

- `add_social_signal.py`

Lane này sinh ra chủ yếu cho:

- Facebook group
- Facebook page
- Facebook profile post
- các social post team đã đọc thủ công và muốn đưa vào pipeline

Đây là điểm rất quan trọng:

- Facebook **đang được hỗ trợ theo cách manual/semi-manual**
- không có crawler Facebook tự động trong repo
- không có Facebook Graph API integration trong repo

---

## 5. Các API/service hiện đang dùng thật sự

## 5.1. Dùng trực tiếp

### Notion API

Dùng để:

- đọc schema database/data source
- tạo page mới
- query page theo source URL để reuse nếu đã tồn tại

Code liên quan:

- `nodes/save_notion.py`

Loại kết nối:

- official API qua `notion-client`

### Telegram Bot API

Dùng để:

- gửi digest ra chat/topic
- đồng bộ feedback từ `getUpdates`

Code liên quan:

- `nodes/send_telegram.py`
- `feedback_loop.py`

Loại kết nối:

- official Telegram HTTP API

### GitHub REST API

Dùng để:

- lấy repo metadata
- lấy releases
- lấy org repos
- search repositories

Code liên quan:

- `nodes/gather_news.py`

Loại kết nối:

- official GitHub API

### Hacker News Firebase API

Dùng để:

- lấy `topstories`
- lấy item details

Code liên quan:

- `nodes/gather_news.py`

Loại kết nối:

- public API

### Reddit JSON endpoints

Dùng để:

- lấy post mới từ subreddit

Code liên quan:

- `nodes/gather_news.py`

Loại kết nối:

- public endpoint, không thấy dùng OAuth Reddit chính thức trong repo hiện tại

### Telethon / Telegram MTProto

Dùng để:

- đọc message từ Telegram channels

Code liên quan:

- `nodes/gather_news.py`

Loại kết nối:

- Telegram user session qua Telethon

## 5.2. Search/scrape helpers

### DDGS

Dùng để:

- text search
- news search
- community reaction lookup

Code liên quan:

- `nodes/gather_news.py`
- `nodes/deep_analysis.py`

### RSS + feedparser

Dùng để:

- parse RSS feeds

### requests + BeautifulSoup + trafilatura

Dùng để:

- fetch HTML
- extract full text
- parse title/content

Đây không phải API official của publisher; đây là lớp web fetch/extraction hỗ trợ ingest.

## 5.3. Local inference/storage, không phải external API chính

### MLX / local Qwen

Dùng để:

- classify
- deep analysis
- note summary
- delivery judge

Code liên quan:

- `mlx_runner.py`

Kết luận:

- runtime chính **không phụ thuộc OpenAI/Grok/Anthropic API**

### SQLite

Dùng để:

- dedup URL history
- feedback entries
- app metadata

File:

- `database.db`
- `db.py`

### ChromaDB

Dùng để:

- long-term semantic memory

File:

- `memory.py`

---

## 6. Biến môi trường và cấu hình quan trọng

Xem trong `config/.env.example`.

Các nhóm cấu hình chính:

- Notion
- Telegram bot
- Telethon
- gather/source toggles
- GitHub token
- watchlist paths
- social inbox path
- report directory
- local UI host/port
- MLX model + fallback model

Điểm cần nhớ:

- Không có biến môi trường nào cho Facebook Graph API
- Không có OAuth flow hay token management cho Meta/Facebook trong repo

---

## 7. Đánh giá thực tế từng lane nguồn

| Lane | Mức ổn định | Giá trị | Ghi chú |
|---|---:|---:|---|
| Curated RSS | Cao | Cao | Nguồn production tốt nhất hiện tại |
| GitHub API | Cao | Cao | Rất hợp use case agent/dev tools |
| Watchlist | Cao | Cao | Bám đúng gu sếp/team |
| Hacker News | Trung bình-Cao | Trung bình-Cao | Tốt cho pulse cộng đồng tech |
| Reddit | Trung bình | Trung bình | Signal phụ, không nên dùng làm fact chính |
| DDG | Trung bình-Thấp | Trung bình | Bổ sung coverage, dễ nhiễu |
| Telegram channels | Trung bình | Trung bình | Hợp cho niche VN/community |
| Facebook manual inbox | Cao về vận hành | Cao nếu team curate tốt | Không tự động nhưng thực dụng |
| Facebook auto-ingest | Thấp trong repo hiện tại | Chưa đáng tin | Chưa có implementation |

---

## 8. Độ khả thi của Facebook

## 8.1. Facebook trong code hiện tại đang ở trạng thái nào?

Trạng thái thật của repo hiện tại:

- `facebook.com` nằm trong `BLOCKED_DOMAINS` ở `source_catalog.py`
- các kết quả Facebook từ search/supplemental lane sẽ bị chặn
- nhưng `nodes/gather_news.py` lại có lane riêng `SOCIAL_SIGNAL_ALLOWED_DOMAINS` cho social signals manual
- `config/social_signal_inbox.txt` mô tả rõ use case Facebook
- `add_social_signal.py` mặc định `--platform facebook`

Kết luận ngắn:

- Facebook **không phải nguồn auto-ingest**
- Facebook **được hỗ trợ như nguồn manual curated**

Đây không phải thiếu sót ngẫu nhiên; nhìn kiến trúc thì đây là một quyết định có chủ đích để tránh các vấn đề:

- scrape không ổn định
- login/permission phức tạp
- bài private hoặc group đóng
- comment tree khó lấy sạch
- tín hiệu social dễ nhiễu nếu không có con người curate

## 8.2. Khả thi theo 4 mức

### Mức 1: Manual copy/paste vào inbox

Khả thi: **Rất cao**

Đây là thứ repo đã làm tốt sẵn:

- team đọc post quan trọng
- paste vào `config/social_signal_inbox.txt`
- hoặc dùng `add_social_signal.py`
- pipeline xử lý tiếp như bài thường

Ưu điểm:

- nhanh
- ít rủi ro kỹ thuật
- không phụ thuộc API Facebook
- giữ được các tín hiệu quý mà RSS/search không có

Nhược điểm:

- không tự động
- phụ thuộc người curate

### Mức 2: Semi-manual workflow

Khả thi: **Cao**

Đây là hướng mình đánh giá phù hợp nhất nếu mục tiêu là “giữ chất lượng, ít rủi ro”:

- người trong team thấy post hay
- dùng shortcut/script/browser helper để đẩy vào inbox file
- có thể kèm title, note, comments, posted_at

Đây là extension tự nhiên của repo hiện tại, gần như không phá kiến trúc.

### Mức 3: Facebook official API cho page do mình sở hữu/quản trị

Khả thi: **Trung bình đến thấp**, tùy quyền sở hữu và quyền truy cập thực tế.

Trong codebase hiện tại:

- chưa có implementation
- chưa có token flow
- chưa có mapper dữ liệu
- chưa có error/retry layer riêng

Nếu chỉ theo dõi page do chính mình quản lý thì có thể mở rộng sau, nhưng đây là một project integration mới chứ không phải “bật cờ config là chạy”.

### Mức 4: Tự động lấy post từ group/private profile/comment ecosystem Facebook nói chung

Khả thi: **Thấp**

Theo góc nhìn kỹ thuật của repo hiện tại:

- chưa có ingestion layer phù hợp
- dữ liệu private/semi-private là trở ngại lớn
- comment/social context khó chuẩn hóa
- rất dễ trở thành lane maintenance-heavy

Kể cả khi làm được bằng workaround, nó sẽ là lane dễ vỡ nhất trong toàn bộ hệ thống.

## 8.3. Kết luận thực dụng về Facebook

Nếu câu hỏi là:

“Facebook có đáng làm nguồn chính không?”

Thì câu trả lời theo trạng thái repo hiện tại là:

- **Không nên làm nguồn chính**

Nếu câu hỏi là:

“Facebook có còn giá trị không?”

Thì câu trả lời là:

- **Có, nhưng nên giữ ở dạng curated source**

Nếu câu hỏi là:

“Đường đi nào phù hợp nhất với repo này?”

Thì câu trả lời là:

- **manual/semi-manual social inbox là hướng đúng nhất hiện tại**

---

## 9. Đề xuất chiến lược nguồn cho giai đoạn tiếp theo

## 9.1. Nên giữ làm core

- Curated RSS
- GitHub API
- Watchlist
- Hacker News
- Telegram feedback loop

## 9.2. Nên giữ làm supplemental

- DDG
- Reddit
- Telegram channels

## 9.3. Nên giữ Facebook theo cách nào

- Không kéo Facebook vào lane supplemental search
- Không dựa vào scrape chay làm core
- Giữ Facebook trong lane `manual social signal`
- Nếu muốn tăng throughput, làm thêm lớp semi-manual tooling thay vì làm crawler trước

Ví dụ các nâng cấp hợp lý:

- script import từ clipboard sang `social_signal_inbox.txt`
- form nhỏ trong UI để paste post Facebook
- browser bookmarklet/extension nội bộ để gửi title/url/content/comments vào inbox

---

## 10. Nhận định cuối cùng

Repo hiện tại có một kiến trúc khá rõ ràng và thực dụng:

- reasoning local
- source mix đa tầng
- RSS/GitHub/watchlist làm lõi
- social khó kiểm soát được đưa vào curated lane riêng
- Telegram được dùng vừa làm output, vừa làm feedback input

Đánh giá tổng quát:

- **Flow hiện tại hợp lý và đủ production-minded hơn nhiều so với một crawler đơn thuần**
- **Nguồn/API đang dùng tương đối đúng hướng: ưu tiên nguồn bền, thêm lớp cộng đồng vừa phải**
- **Facebook khả thi nếu coi là curated signal, không khả thi nếu muốn biến thành nguồn tự động chủ lực trong trạng thái repo hiện tại**

Nếu cần một câu chốt rất ngắn:

> RSS + GitHub + watchlist là xương sống. Facebook nên là lane curated thủ công hoặc semi-manual, không nên là core auto source.

