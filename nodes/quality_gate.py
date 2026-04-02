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


def quality_gate_node(state: dict[str, Any]) -> dict[str, Any]:
    """
    Validate `summary_vn` before it is sent to Telegram.
    """
    # Quality gate nên nhìn cùng tập bài mà brief đang dùng, tránh validate một đằng gửi một nẻo.
    briefing_articles = list(state.get("telegram_candidates", []) or [])
    notion_pages = list(state.get("notion_pages", []))
    summary = str(state.get("summary_vn", "") or "")
    telegram_messages = list(state.get("telegram_messages", []) or [])
    github_topic_messages = list(state.get("github_topic_messages", []) or [])
    facebook_topic_messages = list(state.get("facebook_topic_messages", []) or [])
    is_publish_run = str(state.get("run_mode", "preview") or "preview").strip().lower() == "publish"
    history_articles = get_history(
        days=3 if is_publish_run else 7,
        limit=80 if is_publish_run else 120,
    )
    validation_articles = briefing_articles + history_articles
    summary_mode = str(state.get("summary_mode", "") or "")

    if summary_mode == "no_candidates" and not briefing_articles and not github_topic_messages and not facebook_topic_messages:
        logger.info("📭 Quality gate: không có candidate nào để gửi Telegram.")
        return {
            "summary_vn": "",
            "telegram_messages": [],
            "github_topic_messages": github_topic_messages,
            "facebook_topic_messages": facebook_topic_messages,
            "summary_mode": "no_candidates",
            "summary_warnings": [],
        }

    if summary_mode == "aux_topic_only" and not briefing_articles:
        logger.info("🧪 Quality gate: chỉ có auxiliary topics trong run này.")
        return {
            "summary_vn": "",
            "telegram_messages": [],
            "github_topic_messages": github_topic_messages,
            "facebook_topic_messages": facebook_topic_messages,
            "summary_mode": "aux_topic_only",
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
            per_type=3,
            allow_archive_replay=True,
            include_empty_sections=True,
            allow_high_priority_overflow=True,
        )
        safe_summary = build_safe_digest(
            briefing_articles,
            notion_pages,
            history_articles=history_articles,
            allow_archive_replay=True,
            include_empty_sections=True,
            allow_high_priority_overflow=True,
        )
        logger.warning("⚠️ Quality gate fallback activated: %s", ", ".join(warnings) or "empty_summary")
        return {
            "summary_vn": safe_summary,
            "telegram_messages": safe_messages or [safe_summary],
            "github_topic_messages": github_topic_messages,
            "facebook_topic_messages": facebook_topic_messages,
            "summary_mode": "safe_fallback",
            "summary_warnings": warnings or ["empty_summary"],
        }

    logger.info("✅ Quality gate passed for %d Telegram messages.", len(telegram_messages) or 1)
    return {
        "summary_vn": summary,
        "telegram_messages": telegram_messages or [summary],
        "github_topic_messages": github_topic_messages,
        "facebook_topic_messages": facebook_topic_messages,
        "summary_mode": summary_mode or "deterministic_sections",
        "summary_warnings": [],
    }
