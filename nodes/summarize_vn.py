"""
summarize_vn.py — LangGraph node: Tổng hợp daily digest tiếng Việt cho Telegram.

Node này không tự tóm tắt từng bài nữa. Thay vào đó, nó nhận `note_summary_vi`
đã được nén riêng ở node trước và ghép thành một daily digest tự nhiên hơn.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import get_history
from editorial_guardrails import build_safe_digest, build_safe_digest_messages
from xai_grok import (
    grok_news_copy_enabled,
    grok_news_copy_max_articles,
    rewrite_news_blurbs,
)

logger = logging.getLogger(__name__)


def _prefix_experiment_messages(messages: list[str], header: str) -> list[str]:
    if not messages:
        return []
    prefixed = list(messages)
    prefixed[0] = f"{header}\n\n{prefixed[0]}".strip()
    return prefixed


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

    rewritten = rewrite_news_blurbs(shortlist, feedback_summary_text=feedback_summary_text)
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
    briefing_articles = list(state.get("telegram_candidates", []) or [])
    github_briefing_articles = list(state.get("github_topic_candidates", []) or [])
    facebook_briefing_articles = list(state.get("facebook_topic_candidates", []) or [])
    notion_pages = state.get("notion_pages", [])
    is_publish_run = str(state.get("run_mode", "preview") or "preview").strip().lower() == "publish"
    history_articles = get_history(
        days=3 if is_publish_run else 7,
        limit=80 if is_publish_run else 120,
    )
    runtime_config = dict(state.get("runtime_config", {}) or {})

    _apply_grok_news_copy(
        [briefing_articles, github_briefing_articles, facebook_briefing_articles],
        runtime_config=runtime_config,
        feedback_summary_text=str(state.get("feedback_summary_text", "") or ""),
    )

    if (
        is_publish_run
        and not briefing_articles
        and not github_briefing_articles
        and not facebook_briefing_articles
        and not history_articles
    ):
        logger.info("📭 Không có candidate nào cho publish run; bỏ qua Telegram summary.")
        return {
            "summary_vn": "",
            "telegram_messages": [],
            "github_topic_messages": [],
            "facebook_topic_messages": [],
            "summary_mode": "no_candidates",
        }

    type_coverage = len(
        {
            str(article.get("primary_type", "") or "").strip()
            for article in briefing_articles
            if str(article.get("primary_type", "") or "").strip()
        }
    )
    include_empty_sections = True
    if is_publish_run and briefing_articles and type_coverage < 6:
        logger.info(
            "🧭 Publish run chỉ có %d/6 type có main candidates; sẽ giữ đủ 6 topic và đánh dấu nhóm chưa có tin nổi bật.",
            type_coverage,
        )

    telegram_messages = build_safe_digest_messages(
        briefing_articles,
        notion_pages,
        history_articles=history_articles,
        per_type=3,
        allow_archive_replay=True,
        include_empty_sections=include_empty_sections,
        allow_high_priority_overflow=True,
    )
    github_topic_messages = _prefix_experiment_messages(
        build_safe_digest_messages(
            github_briefing_articles,
            notion_pages,
            history_articles=[],
            per_type=2,
            allow_archive_replay=False,
            include_empty_sections=False,
        ),
        "<b>GitHub Repo Digest</b>",
    )
    facebook_topic_messages = _prefix_experiment_messages(
        build_safe_digest_messages(
            facebook_briefing_articles,
            notion_pages,
            history_articles=[],
            per_type=2,
            allow_archive_replay=False,
            include_empty_sections=False,
        ),
        "<b>Facebook News</b>",
    )

    if not telegram_messages:
        if github_topic_messages or facebook_topic_messages:
            logger.info(
                "✅ Không có brief chính, nhưng đã dựng %d GitHub topic messages và %d Facebook topic messages.",
                len(github_topic_messages),
                len(facebook_topic_messages),
            )
            return {
                "summary_vn": "",
                "telegram_messages": [],
                "github_topic_messages": github_topic_messages,
                "facebook_topic_messages": facebook_topic_messages,
                "summary_mode": "aux_topic_only",
            }
        safe_summary = build_safe_digest(
            briefing_articles,
            notion_pages,
            history_articles=history_articles,
            allow_archive_replay=True,
            include_empty_sections=True,
            allow_high_priority_overflow=True,
        )
        logger.info("✅ Không dựng được sections, dùng safe digest deterministic.")
        return {
            "summary_vn": safe_summary,
            "telegram_messages": [safe_summary],
            "github_topic_messages": [],
            "facebook_topic_messages": [],
            "summary_mode": "deterministic_fallback",
        }

    summary = "\n\n".join(telegram_messages)
    logger.info("✅ Built %d Telegram type messages.", len(telegram_messages))
    return {
        "summary_vn": summary,
        "telegram_messages": telegram_messages,
        "github_topic_messages": github_topic_messages,
        "facebook_topic_messages": facebook_topic_messages,
        "summary_mode": "deterministic_sections",
    }
