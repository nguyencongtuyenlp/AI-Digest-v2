# Kế hoạch nâng cấp Daily Digest theo hướng Hybrid AI Agent

## 1. Bối cảnh và mục tiêu

Sau khi rà lại toàn bộ `context3.txt`, có thể chốt khá rõ:

- Hệ hiện tại đã đi được rất xa ở phần pipeline local-first, preview UI, Notion, Telegram, report và editorial guardrails.
- Nút thắt lớn nhất không còn là "có LLM local hay không", mà là:
  - độ rộng và độ sạch của nguồn vào
  - khả năng tìm đúng tín hiệu founder-grade
  - khả năng bắt repo/tool/agent framework/new way of thinking đủ sớm
  - khả năng tổng hợp sâu trên top story mà vẫn giữ chi phí có kiểm soát
- Bối cảnh mới từ sếp đã thay đổi bài toán:
  - ưu tiên chất lượng hơn việc giữ hệ thuần local bằng mọi giá
  - đã có ngân sách API nếu nó thực sự nâng chất lượng
  - cần xuất hiện được những tin kiểu repo/tool/agent trend trên bảng tin
  - vẫn phải giải thích được, kiểm soát được, và báo cáo được

Kết luận chiến lược: nên đi theo hướng `hybrid AI Agent`, trong đó local model tiếp tục giữ vai trò bulk-processing, còn API được đưa vào đúng những chỗ tạo ra chênh lệch chất lượng lớn nhất.

## 2. Chúng ta đã làm được gì đến nay

Từ `context3.txt`, hệ hiện tại đã có các thành quả quan trọng sau:

- Dựng được pipeline end-to-end:
  - `gather -> normalize_source -> deduplicate -> classify_and_score -> deep_analysis -> recommend_idea -> compose_note_summary -> delivery_judge -> save_notion -> summarize_vn -> quality_gate -> send_telegram`
- Hoàn thiện preview UI theo kiểu:
  - shell tối giống Telegram để xem bản tin
  - bảng dữ liệu và detail pane kiểu Notion để review workspace
- Sửa được nhiều vấn đề vận hành:
  - tương thích Notion data source API
  - làm sạch format Telegram
  - freshness và dedup tốt hơn
  - quality gate và delivery judge tốt hơn
  - feedback loop từ Telegram vào pipeline
- Nâng chất lượng nguồn:
  - siết source catalog
  - thêm watchlist chiến lược
  - lọc supplemental source/domain
  - thêm `domain quality tier`
- Nâng ổn định classify:
  - cải thiện JSON fallback ở `mlx_runner.py`
  - giảm retry lãng phí ở `classify_and_score.py`
  - cải thiện thời gian classify đáng kể

Nói ngắn gọn: nền tảng workflow đã có. Điều còn thiếu không phải là "viết lại từ đầu", mà là "bơm thêm API đúng điểm nghẽn".

## 3. Nhận định chiến lược

### 3.1. Điều nên giữ lại

- `Qwen local / MLX local` vẫn rất đáng giữ cho:
  - prefilter số lượng lớn
  - fallback khi API lỗi
  - tóm tắt/rút gọn nội bộ chi phí thấp
  - chạy preview nhanh
  - giữ quyền kiểm soát pipeline

### 3.2. Điều không nên cố local bằng mọi giá

- Search/discovery ngoài web
- Repo/tool discovery từ GitHub
- Extraction các trang JS-heavy hoặc trang khó parse
- Relevance ranking chất lượng cao cho top candidate
- Deep analysis cho top story quan trọng

### 3.3. Kết luận

Kiến trúc đúng không phải là "thay local bằng API", mà là:

- `local cho bulk và fallback`
- `API cho retrieval và extraction`
- `frontier model cho top-K scoring + top-N deep analysis`

## 4. Kiến trúc hybrid đề xuất

```text
Curated feeds / Telegram / Reddit / HN
        +
GitHub API
        +
Web Search API (Exa hoặc Tavily)
        +
Extraction Router (Jina Reader -> Firecrawl fallback)
        v
gather_news
        v
normalize_source
        v
deduplicate + source tiering
        v
Local prefilter (Qwen/MLX)
        v
Frontier scoring/judge (Claude Sonnet-class)
        v
Top-N deep analysis (GPT-5.4 mini / GPT-5.4)
        v
compose_note_summary / quality_gate
        v
Notion workspace + Telegram digest + run report
```

## 5. API nên dùng như thế nào

## 5.1. GitHub API

### Vai trò

- Bắt repo/tool/agent framework mới
- Theo dõi:
  - trending repo
  - release mới
  - topic/tag như `ai-agents`, `mcp`, `openai`, `anthropic`, `browser-use`, `memory`, `tool-use`
  - repo của các org quan trọng
- Giải đúng bài toán sếp đang nhắc: "phải có mấy tin kiểu repo/link tool/new way of thinking"

### Cách dùng trong hệ

- Thêm adapter vào `nodes/gather_news.py`
- Input:
  - watchlist orgs
  - watchlist topics
  - watchlist repos
- Output:
  - article-like objects để đi chung pipeline hiện tại

### Chi phí

- Gần như `0 USD` nếu dùng GitHub API chính thức với token thường
- Theo GitHub Docs, authenticated REST API có rate limit cơ bản `5,000 requests/hour` cho user token, đủ rộng cho use case hiện tại

### Kết luận

- Đây là API nên làm đầu tiên
- ROI rất cao, chi phí gần như bằng 0

## 5.2. Web Search API: ưu tiên Exa, Tavily là phương án thay thế

### Vai trò

- Mở rộng discovery ra ngoài curated RSS
- Bắt tin:
  - startup AI mới
  - tool mới
  - agent workflow mới
  - technical/blog post có giá trị với founder
  - policy / infra / platform movement

### Khuyến nghị

- `Ưu tiên Exa` cho phase 1 vì:
  - modular
  - chi phí rõ
  - rẻ ở lớp search + contents
  - hợp với pipeline đã có scoring riêng
- `Tavily` là phương án thay thế nếu muốn một lớp search/extract/research thống nhất hơn

### Cách dùng trong hệ

- Thêm provider abstraction trong `nodes/gather_news.py`
- Query groups:
  - company watchlist
  - product/model watchlist
  - agent/tool/framework watchlist
  - policy/watchdog watchlist
- Không để query tự động đi thẳng vào digest
- Kết quả phải qua:
  - source tier
  - dedup
  - local prefilter
  - frontier scoring

### Chi phí tham khảo

#### Exa

- Search: khoảng `5 USD / 1k requests` cho nhóm 1-25 results
- Contents: khoảng `1 USD / 1k pages`

#### Tavily

- Free: `1,000 credits/tháng`
- Pay-as-you-go: `0.008 USD / credit`
- Basic search: `1 credit/request`
- Advanced search: `2 credits/request`

### Kết luận

- Nếu muốn khởi động gọn, nên chọn `Exa`
- Nếu muốn hợp nhất nhiều tính năng hơn trong 1 provider, cân nhắc `Tavily`

## 5.3. Extraction API: ưu tiên router `Jina Reader -> Firecrawl fallback`

### Vai trò

- Lấy nội dung sạch để:
  - score tốt hơn
  - deep analysis tốt hơn
  - tránh chết vì trang JS-heavy, redirect, clutter

### Khuyến nghị

- Dùng `router 2 tầng`:
  - tầng 1: extractor rẻ/nhẹ cho phần lớn URL
  - tầng 2: Firecrawl cho trang khó, trang động, hoặc trang cần crawl/extract sâu

### Cách dùng trong hệ

- Tạo `ExtractionRouter` trong `nodes/gather_news.py`
- Rule gợi ý:
  - trang đơn giản: extract nhẹ trước
  - trang khó parse hoặc fail: đẩy sang Firecrawl
  - chỉ extract full text cho candidate đã qua prefilter thô

### Chi phí tham khảo với Firecrawl

- Free: `500 credits`
- Hobby: `3,000 credits/tháng`, khoảng `16 USD/tháng`
- Standard: `100,000 credits/tháng`, khoảng `83 USD/tháng`
- Billing docs cho biết `scrape` cơ bản tiêu tốn `1 credit/page`

### Kết luận

- Không nên dùng Firecrawl cho mọi URL ngay từ đầu
- Nên dùng như `premium fallback extractor`

## 5.4. Frontier scoring/judge: dùng Claude Sonnet-class

### Vai trò

- Chấm relevance cho top candidate sau local prefilter
- Editorial judge:
  - tin nào vào digest
  - tin nào chỉ lưu Notion
  - tin nào bỏ
- Giữ chất "Claude-like" ở phần đánh giá relevance mà trước đây mình đã xác định là mong muốn

### Cách dùng trong hệ

- Gắn vào `nodes/classify_and_score.py`
- Luồng đề xuất:
  - local prefilter giữ 15-30 candidate
  - Claude chấm top candidate
  - lưu lại:
    - reason
    - founder relevance
    - product relevance
    - confidence
    - should_send / should_hold / should_drop

### Chi phí tham khảo

- Claude Sonnet 4.6:
  - input: `3 USD / 1M tokens`
  - output: `15 USD / 1M tokens`

### Kết luận

- Đây là chỗ nên trả tiền vì tác động trực tiếp tới chất lượng digest

## 5.5. Deep analysis: dùng GPT-5.4 mini hàng ngày, GPT-5.4 cho bài thực sự quan trọng

### Vai trò

- Viết deep analysis cho top story
- Tạo executive memo hoặc weekly synthesis
- Cho ra cảm giác "ChatGPT-like" ở phần tổng hợp sâu

### Cách dùng trong hệ

- Gắn vào `nodes/deep_analysis.py`
- Routing đề xuất:
  - daily top story: `GPT-5.4 mini`
  - bài rất quan trọng hoặc weekly memo: `GPT-5.4`
- Chỉ gọi với top `3-5` bài đã qua scoring

### Chi phí tham khảo

- GPT-5.4:
  - input: `2.50 USD / 1M tokens`
  - output: `15 USD / 1M tokens`
- GPT-5.4 mini:
  - input: `0.75 USD / 1M tokens`
  - output: `4.50 USD / 1M tokens`

### Kết luận

- Không cần đẩy toàn pipeline lên model đắt
- Chỉ cần gọi đúng top story là đã đủ kéo chất lượng lên rất rõ

## 5.6. X / social API

### Vai trò

- Theo dõi account chất lượng cao
- Bắt tín hiệu social rất sớm

### Đánh giá

- Có giá trị, nhưng không nên là phase 1
- Rủi ro:
  - chi phí có thể biến động
  - độ bẩn/noise cao
  - phụ thuộc platform policy

### Kết luận

- Để phase 3
- Chỉ bật khi phase 1 và 2 đã chứng minh ROI

## 5.7. Facebook Groups

### Đánh giá

- Có thể hữu ích cho tín hiệu cộng đồng tại Việt Nam
- Nhưng không nên đưa vào giai đoạn đầu vì:
  - access khó bền
  - automation phức tạp
  - noise cao
  - khó maintain

### Kết luận

- Không phải phase 1

## 6. Lộ trình triển khai đề xuất

## 6.1. Phase 1: Retrieval-first hybrid

### Mục tiêu

- Tăng chất lượng đầu vào trước
- Không thay đổi quá mạnh workflow hiện tại

### Việc cần làm

1. Thêm `GitHub API adapter`
2. Thêm `Exa adapter` trong `gather_news`
3. Thêm `ExtractionRouter`
4. Thêm budget/cost logging cơ bản
5. Gắn source trace rõ trong report và UI

### File/module dự kiến đụng tới

- `nodes/gather_news.py`
- `source_catalog.py`
- `nodes/deduplicate.py`
- `nodes/generate_run_report.py`
- `ui_server.py`
- `config/.env.example`

### Kỳ vọng

- Bắt được nhiều tin kiểu repo/tool/agent hơn
- Giảm mạnh phụ thuộc vào RSS broad
- Preview nhìn đúng "workspace intelligence" hơn

## 6.2. Phase 2: Frontier scoring + deep analysis routing

### Mục tiêu

- Tăng chất lượng lựa chọn bài và phần phân tích

### Việc cần làm

1. Gắn Claude Sonnet-class vào `classify_and_score`
2. Gắn GPT-5.4 mini / GPT-5.4 vào `deep_analysis`
3. Thêm cache ở tầng model call
4. Thêm hard cap:
   - số bài được score bởi API
   - số bài được deep analysis
   - budget/ngày và budget/tháng

### File/module dự kiến đụng tới

- `nodes/classify_and_score.py`
- `nodes/deep_analysis.py`
- `pipeline_runner.py`
- `runtime_presets.py`
- `state.py`
- `nodes/generate_run_report.py`

### Kỳ vọng

- Relevance tốt hơn rõ
- Deep analysis đọc "sang" hơn
- Ít bài noise lọt vào digest hơn

## 6.3. Phase 3: Executive intelligence layer

### Mục tiêu

- Nâng hệ từ digest tool thành một AI intelligence workspace thực sự

### Việc cần làm

1. Weekly memo tự động
2. Theo dõi competitor/watchlist theo chủ đề
3. Topic pages trong Notion
4. Optional social connectors
5. Human feedback loop mạnh hơn từ Telegram/Notion

### Kỳ vọng

- Không chỉ gửi "tin"
- Mà gửi được:
  - insight
  - trend
  - action item
  - strategic watchlist

## 7. Ước tính chi phí

## 7.1. Giả định để tính

- `2 run/ngày`
- `60 run/tháng`
- mỗi run có:
  - 10-30 request search/discovery
  - 20-40 URL cần extract
  - 15-30 candidate cần score kỹ
  - 3-5 bài cần deep analysis

Lưu ý: đây là estimate thực dụng để lên kế hoạch, không phải invoice chính thức.

## 7.2. Mức ngân sách đề xuất

| Gói | Mục tiêu | Thành phần chính | Ước tính/tháng |
| --- | --- | --- | --- |
| Pilot tiết kiệm | Chứng minh chất lượng tăng mà vẫn gọn | GitHub API + Exa + Firecrawl Hobby + Claude scoring hạn chế + GPT-5.4 mini cho top story | `40-90 USD` |
| Khuyến nghị | Đủ tốt để demo cho sếp và chạy đều | GitHub API + Exa + Firecrawl Standard hoặc Hobby có auto-recharge + Claude scoring cho top candidate + GPT-5.4 mini hằng ngày + GPT-5.4 cho weekly memo | `120-250 USD` |
| Executive / recall cao | Bắt nhiều tín hiệu hơn, chạy mạnh hơn | Search nhiều hơn, extract nhiều hơn, score sâu hơn, thêm social connector có chọn lọc | `350-700+ USD` |

## 7.3. Cách chi phí thường sẽ phân bố

| Hạng mục | Ghi chú |
| --- | --- |
| GitHub API | Gần như miễn phí ở quy mô hiện tại |
| Search API | Tăng theo số query, nhưng thường vẫn rẻ hơn phần mất thời gian vì noise |
| Extraction API | Dễ thành khoản lớn nếu scrape mọi URL không kiểm soát |
| Claude scoring | Là khoản rất đáng tiền vì ảnh hưởng trực tiếp chất lượng chọn bài |
| GPT deep analysis | Nếu chỉ gọi top 3-5 story thì thường không phải khoản đắt nhất |

## 7.4. Ước tính chi tiết thực dụng

### Kịch bản khuyến nghị

- GitHub API:
  - `0 USD`
- Exa:
  - khoảng `10-25 USD/tháng` cho search + contents ở mức vừa
- Firecrawl:
  - `16 USD/tháng` nếu Hobby đủ
  - `83 USD/tháng` nếu cần Standard
- Claude Sonnet scoring:
  - thường khoảng `15-40 USD/tháng` nếu chỉ score top candidate
- GPT-5.4 mini + GPT-5.4 deep analysis:
  - thường khoảng `5-20 USD/tháng` nếu chỉ gọi top story và weekly memo

### Kết luận tài chính

- Với bài toán hiện tại, chi phí lớn nhất nhiều khả năng sẽ nằm ở `retrieval/extraction`, không phải model
- Đây là tin tốt vì retrieval/extraction cũng chính là nơi tạo ra chênh lệch chất lượng lớn nhất

## 8. Cách kiểm soát chi phí để tránh vượt ngân sách

1. Chỉ gọi search API cho query nhóm có chủ đích, không query tràn lan
2. Chỉ extract full text sau khi qua prefilter thô
3. Dùng `router`:
   - extractor nhẹ trước
   - Firecrawl sau
4. Chỉ gửi top `15-30` candidate sang Claude scoring
5. Chỉ gửi top `3-5` story sang deep analysis
6. Cache theo:
   - URL
   - normalized URL
   - extraction result
   - model result
7. Thêm:
   - budget per run
   - budget per day
   - budget per month
8. Report bắt buộc có:
   - cost per source
   - cost per stage
   - cost per selected article

## 9. Bảng so sánh với các AI hiện hành

Lưu ý: bảng này so sánh theo góc nhìn sản phẩm cho bài toán `AI news intelligence / founder digest`, không phải benchmark model thuần.

| Tiêu chí | Hệ hiện tại (local-first) | Hệ hybrid đề xuất | ChatGPT | Claude | Perplexity | Grok |
| --- | --- | --- | --- | --- | --- | --- |
| Độ rộng truy cập nguồn | Trung bình, còn phụ thuộc RSS/DDG/watchlist | Cao hơn rõ nhờ search + GitHub + extraction | Rất cao | Rất cao | Rất cao trong search/research | Cao ở web/X ecosystem |
| Bắt repo/tool/agent trend | Còn yếu nếu không có nguồn đúng | Mạnh hơn nhiều nhờ GitHub API + search | Khá tốt nhưng không custom theo watchlist riêng của mình bằng | Khá tốt | Rất tốt ở dạng hỏi-đáp research | Tốt nếu gắn với X/social |
| Chất lượng chọn bài cho sếp | Chưa ổn định | Có thể lên mức tốt nếu route đúng | Tốt tổng quát | Rất tốt ở judging/relevance | Tốt ở search-driven answer | Khá tốt ở tín hiệu nóng |
| Deep analysis | Đã có khung nhưng còn phụ thuộc local model | Tăng mạnh với GPT-5.4 mini / GPT-5.4 | Rất mạnh | Rất mạnh | Khá mạnh nhưng thiên research answer | Khá mạnh tùy ngữ cảnh |
| Tùy biến theo nghiệp vụ riêng | Cao | Rất cao | Trung bình | Trung bình | Trung bình | Trung bình |
| Tích hợp Notion/Telegram/workflow riêng | Rất tốt vì mình tự kiểm soát | Rất tốt | Không phải native cho case này | Không phải native cho case này | Không phải native cho case này | Không phải native cho case này |
| Khả năng giải thích vì sao chọn bài | Tốt | Rất tốt nếu log source + scoring reason | Có nhưng không theo schema riêng của mình | Có nhưng không theo pipeline riêng | Tốt ở citation nhưng không theo workflow của mình | Tùy sản phẩm |
| Chi phí dự đoán được | Tốt | Tốt nếu có hard cap | Dễ đội nếu dùng rộng | Dễ đội nếu dùng rộng | Theo plan/product | Tùy plan/API |
| Quyền kiểm soát dữ liệu và logic | Rất cao | Cao | Thấp hơn | Thấp hơn | Thấp hơn | Thấp hơn |
| Phù hợp để làm "hệ riêng cho sếp" | Tốt về nền tảng, chưa đủ rộng | Rất phù hợp | Mạnh nhưng generic | Mạnh nhưng generic | Mạnh nhưng generic | Mạnh nhưng generic |

## 10. Kết luận so với ChatGPT/Claude hiện tại

### Nếu giữ hệ như bây giờ

- Hệ đang tốt ở:
  - workflow
  - kiểm soát
  - local-first
  - khả năng tích hợp
- Nhưng còn thua khá rõ frontier systems ở:
  - breadth of access
  - retrieval quality
  - repo/tool discovery
  - multi-hop web research
  - độ ổn định deep analysis

### Nếu đi theo hướng hybrid đúng cách

- Hệ chưa chắc "mạnh hơn toàn diện" ChatGPT/Claude/Perplexity
- Nhưng có thể `mạnh hơn trong bài toán hẹp của chính mình`, vì:
  - có watchlist riêng
  - có scoring rubric riêng
  - có Notion DB riêng
  - có Telegram delivery riêng
  - có report/audit trail riêng
  - có budget control riêng

Đây là điểm quan trọng nhất để nói với sếp:

> Mục tiêu không phải là copy ChatGPT hay Claude nguyên bản. Mục tiêu là xây một hệ intelligence hẹp nhưng đúng việc hơn cho team mình, bằng cách mượn API frontier ở đúng chỗ tạo ra chênh lệch chất lượng.

## 11. Khuyến nghị chốt để thảo luận với sếp

### Quyết định đề xuất

- Chốt chuyển sang `hybrid AI Agent`
- Không thay local stack
- Không thay toàn bộ model
- Chỉ bơm API vào 4 lớp:
  - `GitHub discovery`
  - `web search`
  - `page extraction`
  - `frontier scoring + deep analysis`

### Thứ tự làm hợp lý nhất

1. `GitHub API`
2. `Exa`
3. `ExtractionRouter` với Firecrawl fallback
4. `Claude scoring`
5. `GPT-5.4 mini / GPT-5.4 deep analysis`
6. `social API` nếu thật sự cần

### Ngân sách nên đề xuất trước

- Giai đoạn pilot: `120-250 USD/tháng`

Lý do:

- đủ để thấy chênh lệch chất lượng
- chưa quá rủi ro
- dễ kiểm soát
- đủ đẹp để demo cho sếp

## 12. Nguồn tham khảo chính thức

- OpenAI API pricing: https://openai.com/api/pricing
- OpenAI platform pricing/tools: https://platform.openai.com/pricing
- ChatGPT pricing/features: https://openai.com/chatgpt/pricing
- Anthropic pricing: https://www.anthropic.com/pricing
- Claude API pricing docs: https://platform.claude.com/docs/about-claude/pricing
- Claude web search: https://www.anthropic.com/news/web-search
- Claude Research: https://support.anthropic.com/en/articles/11088861-using-research-on-claude-ai
- Exa API pricing: https://exa.ai/pricing/api
- Firecrawl pricing: https://www.firecrawl.dev/pricing
- Firecrawl billing/credits: https://docs.firecrawl.dev/billing
- Tavily pricing: https://www.tavily.com/pricing
- Tavily credits docs: https://docs.tavily.com/documentation/api-credits
- GitHub API rate limits: https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api
- xAI models and pricing: https://docs.x.ai/docs/models
