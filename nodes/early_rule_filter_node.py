"""
early_rule_filter_node.py — Rule-based prefilter trước lớp classify 32B.

Node này giữ nguyên tinh thần agentic của pipeline hiện tại nhưng cắt bớt
những bài có khả năng rất thấp sẽ tạo giá trị cho lớp LLM classify.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nodes.classify_and_score import _prefilter_predicted_type, _prefilter_score, _prefilter_sort_key
from source_catalog import classify_source_kind

logger = logging.getLogger(__name__)


def _normalized_title_key(title: str) -> str:
    return " ".join(str(title or "").strip().lower().split())


def _drop_reason(article: dict[str, Any], prefilter_score: int, reasons: list[str]) -> str:
    inferred_kind, _priority = classify_source_kind(
        source=str(article.get("source", "") or ""),
        domain=str(article.get("source_domain", "") or ""),
        acquisition_quality=str(article.get("acquisition_quality", "") or ""),
        social_signal=bool(article.get("social_signal", False)),
        github_signal_type=str(article.get("github_signal_type", "") or ""),
    )
    source_kind = str(article.get("source_kind") or inferred_kind or "unknown").strip().lower()
    content_available = bool(article.get("content_available", False))
    ai_relevant = article.get("is_ai_relevant")
    source_penalty = int(article.get("source_history_penalty", 0) or 0)
    title = str(article.get("title", "") or "").lower()

    if any(reason.startswith(("blocked_domain", "soft_blocked_domain", "editorial_noise")) for reason in reasons):
        return "editorial_noise"
    if ai_relevant is False and prefilter_score < 12:
        return "not_ai_relevant"
    if source_penalty >= 10 and prefilter_score < 22:
        return "source_history_penalty"
    if (
        source_kind in {"community", "github", "search", "watchlist", "unknown"}
        and not content_available
        and prefilter_score < 18
    ):
        return "thin_supplemental_source"
    if "task manager" in title or "smartphone" in title:
        return "off_scope_title"
    return ""


def early_rule_filter_node(state: dict[str, Any]) -> dict[str, Any]:
    """
    // MVP3 Speed Optimized - Batch + Parallel
    Rule-based gate để loại sớm bài rác trước khi vào batch classify_and_score.
    """
    articles = list(state.get("new_articles", []) or [])
    if not articles:
        logger.info("📭 Early rule filter: không có bài mới.")
        return {"filtered_articles": []}

    filtered: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    seen_titles: set[str] = set()

    for article in articles:
        title_key = _normalized_title_key(article.get("title", ""))
        if title_key and title_key in seen_titles:
            article["early_rule_filter_reason"] = "duplicate_title"
            dropped.append(article)
            continue

        prefilter_score, reasons = _prefilter_score(
            article,
            feedback_preferences=state.get("feedback_preference_profile", {}),
        )
        article["prefilter_score"] = prefilter_score
        article["prefilter_reasons"] = reasons
        article["prefilter_primary_type"] = _prefilter_predicted_type(article)

        reason = _drop_reason(article, prefilter_score, reasons)
        if reason:
            article["early_rule_filter_reason"] = reason
            dropped.append(article)
            continue

        if title_key:
            seen_titles.add(title_key)
        filtered.append(article)

    runtime_config = dict(state.get("runtime_config", {}) or {})
    configured_min_keep = runtime_config.get("early_rule_filter_min_keep")
    if configured_min_keep in (None, ""):
        min_keep = max(8, len(articles) // 2)
    else:
        try:
            min_keep = max(1, int(configured_min_keep))
        except (TypeError, ValueError):
            min_keep = max(8, len(articles) // 2)

    if len(filtered) < min_keep and dropped:
        rescue_pool = [
            article
            for article in dropped
            if str(article.get("early_rule_filter_reason", "") or "").strip().lower()
            not in {"duplicate_title", "editorial_noise", "not_ai_relevant", "off_scope_title"}
        ]
        rescue_pool.sort(key=_prefilter_sort_key, reverse=True)
        rescued = rescue_pool[: max(0, min_keep - len(filtered))]
        for article in rescued:
            article.pop("early_rule_filter_reason", None)
            filtered.append(article)

    logger.info(
        "⚡ Early rule filter giữ %d/%d bài cho classify 32B (drop=%d)",
        len(filtered),
        len(articles),
        max(0, len(articles) - len(filtered)),
    )
    return {"filtered_articles": filtered}
