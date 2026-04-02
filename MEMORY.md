# MEMORY

Last updated: 2026-03-31

## Project goal
- Daily Digest AI Agent with local Qwen (MLX) doing scoring, deep analysis, and short note summaries.
- Output: Notion page (long-form content) + short note property + Telegram daily brief.
- Split roles: scoring (Claude-like), deep analysis (ChatGPT-like), note summary (compression).
## Company context (from user)
- Project is a small branch within a larger company; if product works and revenue is good it will spin out.
- Boss tracks major tech players frequently (X/Twitter, Facebook groups) and likes Anthropic's thinking/skills.
- Boss bought a Mac mini M4 Pro 48GB to run this local AI project.
- Boss is highly resourceful/product-driven and will keep trying paths until a product works; this project should be treated as a serious foundation, not a toy bot.
- Goal: make local Qwen feel closer to ChatGPT/Claude/Grok in access and reasoning; analysis = ChatGPT-like, scoring = Claude Sonnet-like.
- New direction from boss/user on 2026-03-31: quality is now more important than strict zero-API purity; API budget is available if it materially improves news quality and breadth.
- This changes the optimization target: keep local-first where it helps, but the retrieval/research layer can become hybrid if that is the fastest path to founder-grade quality.
- User wants to fully understand architecture and logic to report to boss.
- User has not fully reviewed the current architecture or scoring rules yet, so explainability and the ability to report the system clearly upward are important product requirements.
- Priority: freshest AI news useful for AI startup, with deeper potential ideas and practical recommendations.
- Expectation: Qwen should propose tools/uses and evaluate ideas; logic should be rigorous (community signals, scoring) and must not feel sloppy.
- User says current Telegram tone/wording is already close to boss feedback and should be preserved/improved carefully, not rewritten wildly.
- User wants explicit source analysis and a clear explanation of how far the system is from real ChatGPT/Claude capability.
- User has now raised the standard again: the project should be treated as a serious product foundation for future agents, not just a news bot.
- Long-term ambition: this system should become a reusable base for many future internal agents, so choices should favor maintainable architecture, source adapters, monitoring, explainability, and low/no-API operating cost.
- Constraints remain important: minimize paid APIs, prioritize free/public sources, local-first infrastructure, and only adopt improvements that are both feasible and likely to improve results materially.
- User wants outside-market benchmarking against similar products/tools, then only copy what clearly fits the current architecture and cost philosophy.
- User explicitly asked whether Nitter is a good path; conclusion for memory: Nitter is not stable enough to be a core source and should only ever be treated as temporary fallback.
- User has Grok API budget/credits only until around the end of July 2026, so any design depending on Grok must be optional and time-bounded rather than foundational.
- Sustainable quality path should prioritize durable sources and retrieval: RSS, official blogs, GitHub releases/changelogs, Hacker News API, newsletters/email ingestion, Reddit API where useful, curated watchlists, Telegram channels, and stronger source whitelists/blocklists.
- User wants the system to feel as "wide-access" and capable as ChatGPT/Claude/Grok from the outside, even if internally it must be achieved through better source adapters, retrieval, clustering, ranking, monitoring, and prompt structure rather than hidden frontier infrastructure.
- Current reporting need is twofold: ship the product forward and also understand the whole architecture well enough to explain scoring, tradeoffs, and roadmap clearly back to the boss.
- UI expectation has changed too: preview should help review the system like a real workspace, not just dump plain text. Telegram-style brief review and Notion-style database review are both valuable for demos and internal iteration.
- Boss specifically wants more items like GitHub repos/tools/agent frameworks/new "ways of thinking" to appear in the brief, not just generic AI news.
- Boss often tracks signals from X/Twitter, Facebook groups, major tech players, and practical product/tool launches; the system should reflect that taste profile.
- User wants a concrete planning discussion for an API-enabled quality upgrade and also wants the whole system to remain understandable enough to explain clearly upward.

## Key pipeline changes
- Added `nodes/normalize_source.py` for source verification + date normalization.
- Graph now: gather → normalize_source → deduplicate → classify_and_score → deep_analysis → recommend_idea → compose_note_summary → delivery_judge → save_notion → summarize_vn → quality_gate → send_telegram.
- `classify_and_score.py` prompt refined for editorial triage; includes analysis_tier and editorial_angle.
- Added strategic boost heuristics + type normalization to reduce misclassification on key cases.
- Added V3 event-aware scoring: same-event articles are clustered, deep analysis only uses event primaries, and multi-source event consensus can boost a representative article.
- Added deterministic freshness penalties so stale / unknown-freshness items are less likely to surface.
- Added `nodes/delivery_judge.py` to decide which articles are worthy of Telegram delivery.
- Strategy direction: improve Qwen primarily through better retrieval/tools/structured prompting, not by trying to replicate hidden Claude/ChatGPT internals.
- Current product goal from user/boss: freshest AI news useful for AI startup, practical recommendations, strong source filtering, and deeper idea generation.

## Prompt tuning assets
- `config/prompt_tuning_cases.md` + `config/prompt_tuning_cases.jsonl` created with 10 real cases from `database.db`.
- `config/output_templates.md` created with product-level Notion + Telegram templates.

## Notion / Telegram formatting
- Notion pages now include a Source Snapshot callout at top (source/domain/published/tier).
- Telegram summary prompt enforces “AI Daily Brief | dd/mm”.

## MLX runner fix
- `mlx_runner.py` updated to use `make_sampler(temp=...)` or `make_sampler(temperature=...)` depending on library signature to avoid runtime crash.

## Current tuning status
- Tiering `skip/basic/deep` matches expected across test set.
- Note summary and Telegram flow now have stronger grounding guardrails: downstream nodes receive `confidence_label`, `grounding_note`, `fact_anchors`, and `unknowns`.
- Added a deterministic `quality_gate` before Telegram with safe HTML fallback if the LLM summary fails sanity checks.
- Notion page now surfaces grounding/evidence status more clearly for each article.
- Telegram delivery now uses 6 deterministic messages by type, removes intro/priority/meta clutter, and can replay recently reported stories with a clear "already in the 8am brief" note when the user runs tests outside the main schedule.
- Future focus: fresh AI news useful for startup, practical recommendations, stronger community signals, and tighter evaluation of scoring/summary logic.
- Real run completed: `60 raw -> 17 dedup -> 10 deep -> 17 Notion -> Telegram sent`.
- Main weakness observed in live output: summary can still over-infer or hallucinate facts if grounding is weak.
- Important fix next: validate the new guardrails on a real end-to-end run and tune fallback thresholds if the safe digest fires too often.
- Checkpoint before V3 upgrade: `.checkpoints/pre_v3_upgrade_20260326_164247.tar.gz`
- Important security note: Telegram bot token was visible in terminal log; rotate it.

## Live run observations
- Many gather sources return `403` or unreliable content from some sites; this is expected from anti-scrape and search engine limitations.
- DDG sometimes throws `Unsupported protocol version 0x304`.
- Telegram channels were skipped because Telethon credentials were not configured.
- Notion page creation worked.
- Telegram sending worked.
- Current source mix is still heavily DDG-dominant; database snapshot shows more DDG than direct official/primary feeds.
- Important strategic conclusion: the main bottleneck is no longer just prompting; it is retrieval breadth/quality/source intelligence.
- Strategic update on 2026-03-31: if API budget exists, the most rational place to spend it is retrieval/source adapters/reranking/extraction first, not blindly replacing all local inference.

## Known runtime observations
- `403` on some sources (Forbes/StackOverflow/etc.) during gather is expected due to anti-scrape.
- DDG sometimes fails with `Unsupported protocol version 0x304`.
- Machine capability check on 2026-03-27:
  - `mlx-community/Qwen2.5-32B-Instruct-4bit` loads and runs successfully on the Mac mini M4 Pro 48GB.
  - A real classify-style JSON test on 32B returned valid structured output in about 48s.
  - `mlx-community/Qwen3.5-35B-A3B-4bit` is currently blocked by local `mlx_lm` support (`qwen3_5_moe not supported`), so the limitation is library/runtime support, not raw machine capacity.

## Strategic direction
- The next quality bar is "founder-grade intelligence product", not merely "daily AI brief".
- Best-fit external models to learn from: Feedly/Inoreader for monitoring feeds, Event Registry/Particle for story-centric grouping, RSSHub/RSS-Bridge/FreshRSS/Huginn for low-cost source adapters, OpenBB for connector/platform architecture.
- Avoid over-investing in private-source scraping or trying to mimic frontier chat products directly; focus on source adapters, monitor definitions, story objects, and workspace-grade explainability first.
- New preferred hybrid target:
  - APIs for retrieval/search/extraction/reranking where they clearly improve quality.
  - Frontier-model analysis only for the small top slice that truly needs it.
  - Keep local Qwen for controllable bulk processing, fallback, and low-cost daily operation.

## Commands
- Run pipeline: `source .venv/bin/activate && python main.py`
- Run with log: `source .venv/bin/activate && python main.py 2>&1 | tee digest_run.log`
- Check memory before resuming: `cat MEMORY.md`
- If switching chats, first read `MEMORY.md` and continue from the live-run observations above.
