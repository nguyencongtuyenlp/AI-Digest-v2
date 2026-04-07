"""
summarize_vn.py — LangGraph node: Tổng hợp daily digest tiếng Việt cho Telegram.

Node này không tự tóm tắt từng bài nữa. Thay vào đó, nó nhận `note_summary_vi`
đã được nén riêng ở node trước và ghép thành một daily digest tự nhiên hơn.
"""

from __future__ import annotations

import logging
from typing import Any

from digest.storage.db import get_history
from digest.editorial.editorial_guardrails import (
    build_safe_digest,
    build_safe_digest_messages,
    build_telegram_copy_from_structured,
)
from digest.runtime.xai_grok import (
    grok_news_copy_enabled,
    grok_news_copy_max_articles,
    rewrite_news_blurbs,
)

logger = logging.getLogger(__name__)

BAD_PHRASES = [
    "tín hiệu sơ bộ",
    "chưa vượt trội so với",
    "chưa vượt qua được",
    "chỉ ở mức tín hiệu",
    "chỉ cung cấp tín hiệu",
    "chỉ mang tính tín hiệu",
]

BOILERPLATE_SOURCE_MARKERS = [
    "Hacker News API",
    "GitHub API",
    "theo bài viết",
    "được đăng trên",
]

BOILERPLATE_PREFIXES = [
    "GitHub repo của",
]


def _dynamic_per_type_limit(*article_groups: list[dict[str, Any]]) -> int:
    lane_counts: dict[str, int] = {}
    for group in article_groups:
        for article in group:
            lane = str(article.get("primary_type", "") or "").strip()
            if not lane:
                continue
            lane_counts[lane] = lane_counts.get(lane, 0) + 1
    return max(3, max(lane_counts.values(), default=0))


def _prefix_experiment_messages(messages: list[str], header: str) -> list[str]:
    if not messages:
        return []
    prefixed = list(messages)
    prefixed[0] = f"{header}\n\n{prefixed[0]}".strip()
    return prefixed


def _telegram_eligible_articles(articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    eligible: list[dict[str, Any]] = []
    skipped = 0
    for article in articles:
        summary = build_telegram_copy_from_structured(article, max_len=360)
        note_summary_vi = summary.lower()
        if note_summary_vi and any(phrase in note_summary_vi for phrase in BAD_PHRASES):
            skipped += 1
            continue
        if len(summary) < 30:
            skipped += 1
            continue
        if summary.count("(") >= 2 and any(
            marker in summary for marker in ["API", "Hacker News", "GitHub", "theo bài viết"]
        ):
            skipped += 1
            continue
        if any(marker in summary for marker in BOILERPLATE_SOURCE_MARKERS) and len(summary) < 140:
            skipped += 1
            continue
        if any(summary.startswith(prefix) for prefix in BOILERPLATE_PREFIXES) and len(summary) < 240:
            skipped += 1
            continue
        eligible.append(article)

    if skipped:
        logger.info("🧹 Skipped %d articles from Telegram output due to internal scoring language.", skipped)
    return eligible


def _apply_grok_news_copy(
    candidate_groups: list[list[dict[str, Any]]],
    *,
    runtime_config: dict[str, Any],
    feedback_summary_text: str,
) -> None:
    if not grok_news_copy_enabled(runtime_config):
        return

    shortlist: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    max_articles = grok_news_copy_max_articles(runtime_config)

    for group in candidate_groups:
        for article in group:
            key = str(article.get("url", "") or article.get("title", "") or id(article))
            if not key or key in seen_keys:
                continue
            seen_keys.add(key)
            shortlist.append(article)
            if len(shortlist) >= max_articles:
                break
        if len(shortlist) >= max_articles:
            break

    if not shortlist:
        return

    try:
        rewritten = rewrite_news_blurbs(shortlist, feedback_summary_text=feedback_summary_text)
    except Exception:
        logger.exception("⚠️ Grok news copy failed; using deterministic fallback copy.")
        rewritten = {}
    updated = 0
    for article in shortlist:
        key = str(article.get("url", "") or article.get("title", "") or "")
        polished = rewritten.get(key, {})
        blurb = str(polished.get("blurb", "") or "").strip()
        if not blurb:
            continue
        article["telegram_blurb_vi"] = blurb
        updated += 1

    if updated:
        logger.info("✅ Grok news copy polished %d/%d selected articles.", updated, len(shortlist))


def summarize_vn_node(state: dict[str, Any]) -> dict[str, Any]:
    """
    LangGraph node: dựng 6 Telegram messages cố định theo type.
    Nếu không có tin mới đủ dùng, nhắc lại các tin gần nhất đã báo cáo trước đó.
    """
    # Chỉ dùng các bài đã qua delivery judge để dựng brief.
    # Nếu không có candidate nào, hệ sẽ fallback sang history thay vì lôi toàn bộ final_articles lên Telegram.
    briefing_articles = _telegram_eligible_articles(list(state.get("telegram_candidates", []) or []))
    notion_pages = state.get("notion_pages", [])
    is_publish_run = str(state.get("run_mode", "preview") or "preview").strip().lower() == "publish"
    history_articles = get_history(
        days=3 if is_publish_run else 7,
        limit=80 if is_publish_run else 120,
    )
    runtime_config = dict(state.get("runtime_config", {}) or {})
    per_type_limit = _dynamic_per_type_limit(briefing_articles)

    _apply_grok_news_copy(
        [briefing_articles],
        runtime_config=runtime_config,
        feedback_summary_text=str(state.get("feedback_summary_text", "") or ""),
    )

    if (
        is_publish_run
        and not briefing_articles
        and not history_articles
    ):
        logger.info("📭 Không có candidate nào cho publish run; bỏ qua Telegram summary.")
        return {
            "summary_vn": "",
            "telegram_messages": [],
            "summary_mode": "no_candidates",
        }

    type_coverage = len(
        {
            str(article.get("primary_type", "") or "").strip()
            for article in briefing_articles
            if str(article.get("primary_type", "") or "").strip()
        }
    )
    include_empty_sections = False
    if is_publish_run and briefing_articles and type_coverage < 3:
        logger.info(
            "🧭 Publish run hiện chỉ có %d lane có candidate; sẽ chỉ render các lane có bài thật.",
            type_coverage,
        )

    telegram_messages = build_safe_digest_messages(
        briefing_articles,
        notion_pages,
        history_articles=history_articles,
        per_type=per_type_limit,
        allow_archive_replay=True,
        include_empty_sections=include_empty_sections,
        allow_high_priority_overflow=True,
    )
    if not telegram_messages:
        safe_summary = build_safe_digest(
            briefing_articles,
            notion_pages,
            history_articles=history_articles,
            allow_archive_replay=True,
            include_empty_sections=False,
            allow_high_priority_overflow=True,
        )
        logger.info("✅ Không dựng được sections, dùng safe digest deterministic.")
        return {
            "summary_vn": safe_summary,
            "telegram_messages": [safe_summary],
            "summary_mode": "deterministic_fallback",
        }

    summary = "\n\n".join(telegram_messages)
    logger.info("✅ Built %d Telegram type messages.", len(telegram_messages))
    return {
        "summary_vn": summary,
        "telegram_messages": telegram_messages,
        "summary_mode": "deterministic_sections",
    }
