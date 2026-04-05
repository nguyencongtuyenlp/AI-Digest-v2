# Changelog — Daily Digest Agent



---

## Tóm tắt

Agent **local-first** (MLX / Qwen trên Apple Silicon): thu tin AI/Tech đa nguồn → chuẩn hóa & dedup → phân loại + chấm điểm → phân tích sâu (một phần bài) → note ngắn → **delivery judge** (chọn bài lên Telegram) → lưu Notion → dựng bản tin Telegram (có **quality gate**) → gửi 3 luồng: **brief chính**, **GitHub repo digest**, **Facebook News** (khi có bài). Có **UI preview** (`ui_server.py`), **run report** markdown, **run health**, **eval regression**, **temporal snapshot**, **source history** (học nguồn theo thời gian). Tùy chọn tích hợp **xAI / Grok** (rerank, scout, copy…) qua biến môi trường — không bắt buộc để pipeline chạy.

---

## 2. Luồng pipeline (đã ổn định trong các phiên bản gần đây)

Thứ tự nút trong graph (có thể có thêm `collect_feedback` tùy cấu hình):

`gather_news` → `normalize_source` → `deduplicate` → `collect_feedback` (nếu bật) → `classify_and_score` → `deep_analysis` → `recommend_idea` → `compose_note_summary` → **`delivery_judge`** → `save_notion` → `summarize_vn` → `quality_gate` → `send_telegram` → `generate_run_report` (và các bước phụ như artifact cleanup tùy cấu hình).

**Trước V3** delivery judge chưa có; sau V3 được chèn giữa note và Notion để bài lưu DB vẫn có thể khác bộ gửi Telegram.

---

## 3. Thu thập & nguồn

- **RSS / feed curated** (`source_catalog`), khung giờ lấy tin cấu hình được (`gather_rss_hours`).
- **DuckDuckGo / tìm kiếm bổ sung** (EN/VN), dễ gặp lỗi mạng kiểu `Unsupported protocol version 0x304`, hoặc kết quả kém — trong context có ghi nhận.
- **Hacker News API**, **Reddit** (JSON public), **GitHub** (repo watchlist, org, search query), **watchlist seed** file.
- **Telegram channels** qua Telethon — chỉ chạy khi cấu hình credential; log từng có “skipping” khi chưa set.
- **Facebook**: Playwright + Chrome, session `facebook_storage_state.json`, danh sách target `facebook_auto_targets.txt`; discovery group/page tùy chọn; lane riêng `facebook_topic`. Trong quá trình làm có: chờ hết skeleton “Đang tải”, sửa thứ tự dòng (tác giả / thời gian / nội dung), ưu tiên permalink `.../posts/...`, test routing GitHub/main/Facebook tách biệt.
- Chiến lược dài hạn trong thảo luận: **không lấy Nitter làm core** (không ổn định); ưu RSS, official blog, GitHub, HN, Reddit API; Grok/X chỉ nên là lớp bổ sung có kiểm soát.

---

## 4. Chuẩn hóa & dedup

- `normalize_source`: domain, **published_at** (tránh dùng `fetched_at` giả làm ngày đăng), cờ stale / freshness unknown.
- Dedup trong batch và gợi ý **event clustering** (V3): bài cùng sự kiện gom nhóm; deep analysis ưu tiên bài đại diện; tránh lãng phí suy luận trùng.

---

## 5. Phân loại & chấm điểm (`classify_and_score`)

- 6 type: Research, Product, Business, Policy & Ethics, Society & Culture, Practical.
- Điểm C1/C2/C3, `tags`, `analysis_tier` (deep/basic/skip), `editorial_angle`.
- Prompt lớn (taxonomy tag); có **Grok prefilter** tùy chọn để “cứu” bài khỏi bị lọc sớm.
- **Strategic keyword / type normalization** để giảm phân loại sai các case quan trọng.

---

## 6. Phân tích sâu & gợi ý

- `deep_analysis`: tìm thêm tín hiệu cộng đồng (DDG), bản phân tích dài cho Notion.
- `recommend_idea`: gói ý từng bài top.
- `compose_note_summary`: nén `note_summary_vi` cho Notion + downstream Telegram.

---

## 7. Delivery judge & Telegram

- **Rule cứng** + tùy chọn **LLM judge** (MLX JSON) + tùy chọn **Grok rerank** shortlist (`xai_grok.py`, `GROK_DELIVERY_*`).
- Tách lane: **main** vs **github.com** vs **facebook**; không để bài GitHub/Facebook cạnh tranh slot main nếu đã gán lane.
- `summarize_vn`: dựng **6 message theo type** (deterministic), có **replay** archive khi thiếu tin; có **Grok news copy** polish blurb tùy cấu hình.
- `quality_gate`: kiểm HTML/format; fallback an toàn nếu model lỗi.
- `send_telegram`: thread riêng cho main / GitHub / Facebook (`TELEGRAM_*_THREAD_ID`).

---

## 8. Notion

- Tạo/reuse page theo URL; map property tùy schema (multi data source); có snapshot nguồn / grounding trong page.
- Log từng cảnh báo cột thiếu hoặc lệch tên so với schema.

---

## 9. UI & feedback

- `ui_server.py`: preview 3 lane, nút Run Preview (production / Grok Smart preset), Approve / Publish Notion only, run report.
- **Feedback loop** (`collect_feedback`): đồng bộ phản hồi từ Telegram vào pipeline (nhãn, gợi ý) — chi tiết cấu hình trong code và `.env`.

---

## 10. Quan sát, eval, health

- **`eval_digest.py`**: regression trên `config/prompt_tuning_cases.jsonl`; trong context có báo cáo kiểu pass rate 85.7% → sau mở rộng case đạt 100% (số liệu cụ thể nằm trong `reports/eval_digest_*.md`).
- **`run_health.py`**: `health_status`, `publish_ready`, metric (tỉ lệ GitHub, đa dạng nguồn, …).
- **`generate_run_report.md`**: báo cáo từng run (candidate, skip reason, …).
- **Temporal snapshots** (`temporal_snapshots`): JSON sau gather / sau score để debug.
- **Source history**: điều chỉnh điểm nguồn theo lịch sử (noise, chọn bài).

---

## 11. MLX & model

- Mặc định **Qwen2.5-32B-Instruct-4bit** (MLX); có fallback 14B khi thiếu RAM / OOM khi nhiều process.
- Sửa **`mlx_runner`**: tương thích `make_sampler(temp=...)` vs `temperature=...` theo phiên bản thư viện.

---

## 12. V3 (khoảng 26/03/2026 — theo checkpoint & memory)

- Checkpoint trước V3: `pre_v3_upgrade_20260326_164247.tar.gz`; backup DB `database_reset_backup_20260326_165247.db`.
- Event-aware clustering, **delivery_judge** chèn vào graph, summarize/quality gate dùng **`telegram_candidates`** thật — không fallback lén lấy lại toàn bộ `final_articles` khi judge chặn hết (đã vá edge case trong thảo luận).
- Test unit cho stale, duplicate event, weak source.

---

## 13. Roadmap & hướng hybrid (khoảng 27–31/03/2026)

- `ROADMAP_UPGRADE_2026-03-27.md`: 4 giai đoạn (nguồn → đo lường → chất lượng → độ dài).
- `SOURCE_ANALYSIS_REPORT_2026-03-27.md`: so sánh kỳ vọng vs thực tế hệ thống.
- Checkpoint direction B: `pre_direction_b_20260327_151755.tar.gz`.
- Chốt **ưu tiên chất lượng** hơn “zero API”; API dùng đúng chỗ (retrieval/rerank), Grok có giới hạn thời gian/ngân sách trong thảo luận.
- `PROJECT_FLOW_SOURCES_FACEBOOK_2026-03-31.md`: luồng Facebook chi tiết.

---

## 14. Repo & vận hành (đầu 04/2026)

- Đồng bộ mã lên git; loại file session trình duyệt khỏi repo (tránh lộ session).
- Thư mục `reports/` chứa nhiều `daily_digest_run_*.md`, `eval_digest_*.md`, snapshot.

---

## 15. Rủi ro & lưu ý (đã ghi trong dump)

- Nhiều site trả **403** khi crawl; trích bài phụ thuộc trang và chống bot.
- **Nitter** không nên làm trục chính.

---

## 16. Chưa gắn version — chỉnh sửa gần nhất (mã hiện tại)

- Delivery: nới ưu tiên bài official / báo lớn, tăng số slot candidate Telegram chính; overflow theo type nới điều kiện.
- Copy Telegram: hướng tóm tin, bớt khuyên bảo; lọc cụm khuyên trên đường ra; prompt note + Grok news copy siết theo wire-style.
- Facebook: cải thiện scroll/selector feed; nới gate `founder_grade` với group AI đã cấu hình; thêm từ khóa VN; preset `grok_smart` bật Facebook auto + social signal; Grok Facebook rerank khi ≥1 bài.

---

