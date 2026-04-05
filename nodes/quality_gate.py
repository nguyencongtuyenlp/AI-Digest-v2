"""
quality_gate.py - Final summary sanity gate before Telegram.

If the LLM summary fails deterministic checks, fall back to a safe digest built
from already-grounded article metadata.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import get_history
from editorial_guardrails import (
    build_safe_digest,
    build_safe_digest_messages,
    validate_telegram_messages,
    validate_telegram_summary,
)

logger = logging.getLogger(__name__)


def _dynamic_per_type_limit(*article_groups: list[dict[str, Any]]) -> int:
    lane_counts: dict[str, int] = {}
    for group in article_groups:
        for article in group:
            lane = str(article.get("primary_type", "") or "").strip()
            if not lane:
                continue
            lane_counts[lane] = lane_counts.get(lane, 0) + 1
    return max(3, max(lane_counts.values(), default=0))


def quality_gate_node(state: dict[str, Any]) -> dict[str, Any]:
    """
    Validate `summary_vn` before it is sent to Telegram.
    """
    # Quality gate nên nhìn cùng tập bài mà brief đang dùng, tránh validate một đằng gửi một nẻo.
    briefing_articles = list(state.get("telegram_candidates", []) or [])
    notion_pages = list(state.get("notion_pages", []))
    summary = str(state.get("summary_vn", "") or "")
    telegram_messages = list(state.get("telegram_messages", []) or [])
    is_publish_run = str(state.get("run_mode", "preview") or "preview").strip().lower() == "publish"
    history_articles = get_history(
        days=3 if is_publish_run else 7,
        limit=80 if is_publish_run else 120,
    )
    validation_articles = briefing_articles + history_articles
    summary_mode = str(state.get("summary_mode", "") or "")
    per_type_limit = _dynamic_per_type_limit(briefing_articles, history_articles)

    if summary_mode == "no_candidates" and not briefing_articles:
        logger.info("📭 Quality gate: không có candidate nào để gửi Telegram.")
        return {
            "summary_vn": "",
            "telegram_messages": [],
            "summary_mode": "no_candidates",
            "summary_warnings": [],
        }

    if telegram_messages:
        warnings = validate_telegram_messages(telegram_messages, validation_articles, notion_pages)
    else:
        warnings = validate_telegram_summary(summary, validation_articles, notion_pages)

    if warnings or not summary.strip():
        safe_messages = build_safe_digest_messages(
            briefing_articles,
            notion_pages,
            history_articles=history_articles,
            per_type=per_type_limit,
            allow_archive_replay=True,
            include_empty_sections=True,
            allow_high_priority_overflow=True,
        )
        safe_summary = build_safe_digest(
            briefing_articles,
            notion_pages,
            history_articles=history_articles,
            max_articles=per_type_limit,
            allow_archive_replay=True,
            include_empty_sections=True,
            allow_high_priority_overflow=True,
        )
        logger.warning("⚠️ Quality gate fallback activated: %s", ", ".join(warnings) or "empty_summary")
        return {
            "summary_vn": safe_summary,
            "telegram_messages": safe_messages or [safe_summary],
            "summary_mode": "safe_fallback",
            "summary_warnings": warnings or ["empty_summary"],
        }

    logger.info("✅ Quality gate passed for %d Telegram messages.", len(telegram_messages) or 1)
    return {
        "summary_vn": summary,
        "telegram_messages": telegram_messages or [summary],
        "summary_mode": summary_mode or "deterministic_sections",
        "summary_warnings": [],
    }
