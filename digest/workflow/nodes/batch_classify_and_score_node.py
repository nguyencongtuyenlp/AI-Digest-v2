"""
batch_classify_and_score_node.py — Batch classify + score cho nhiều bài cùng lúc.
"""

from __future__ import annotations

import json
import logging
import math
import os
from typing import Any


from digest.runtime.mlx_runner import run_json_inference_meta
from digest.runtime.temporal_snapshots import write_temporal_snapshot

from digest.workflow.nodes.classify_and_score import (
    CLASSIFY_RESPONSE_FORMAT,
    CLASSIFY_SCORE_SYSTEM,
    CLASSIFY_SCORE_USER_TEMPLATE,
    _annotate_event_clusters,
    _apply_freshness_penalty,
    _apply_source_history_adjustment,
    _apply_strategic_boost,
    _build_related_context,
    _cfg_int,
    _classify_inference_with_retry,
    _classify_prose_rescue,
    _finalize_scored_article,
    _held_out_article_fallback,
    _is_likely_prose_response,
    _llm_failure_fallback,
    _normalize_classify_inference_response,
    _prepare_classify_candidates,
    _select_top_articles,
    run_json_inference as single_article_json_inference,
)

logger = logging.getLogger(__name__)


def _batch_response_format(max_items: int) -> dict[str, Any]:
    item_schema = dict(CLASSIFY_RESPONSE_FORMAT["json_schema"]["schema"])
    properties = dict(item_schema.get("properties", {}))
    required = list(item_schema.get("required", []))

    properties["item_id"] = {"type": "string"}
    required = ["item_id", *required]

    return {
        "type": "json_schema",
        "json_schema": {
            "name": "batch_classify_score_articles",
            "strict": True,
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "articles": {
                        "type": "array",
                        "maxItems": max_items,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": properties,
                            "required": required,
                        },
                    }
                },
                "required": ["articles"],
            },
        },
    }


def _batch_system_prompt() -> str:
    return (
        f"{CLASSIFY_SCORE_SYSTEM}\n\n"
        "# // MVP3 Speed Optimized - Batch + Parallel\n"
        "Bạn xử lý nhiều bài viết ĐỘC LẬP. Không so sánh chéo.\n"
        "primary_type CHỈ được dùng các giá trị: Product, Society & Culture, Practical.\n"
        "Trả về đúng JSON với key `articles`. Mỗi item phải có `item_id`.\n"
        "Tuân thủ nghiêm ngặt schema. Không thêm text ngoài JSON."
    )
def _build_batch_user_prompt(
    batch: list[tuple[str, dict[str, Any]]],
    *,
    classify_content_limit: int,
    feedback_summary_text: str,
) -> str:
    blocks: list[str] = []
    for item_id, article in batch:
        blocks.append(
            f"=== ITEM {item_id} ===\n"
            + CLASSIFY_SCORE_USER_TEMPLATE.format(
                title=article.get("title", "N/A"),
                url=article.get("url", ""),
                source=article.get("source", "Unknown"),
                source_domain=article.get("source_domain", ""),
                published_at=article.get("published_at", article.get("published", "")),
                published_at_source=article.get("published_at_source", "unknown"),
                discovered_at=article.get("discovered_at", article.get("fetched_at", "")),
                age_hours=article.get("age_hours", ""),
                freshness_unknown=article.get("freshness_unknown", False),
                is_stale_candidate=article.get("is_stale_candidate", False),
                source_verified=article.get("source_verified", False),
                content_available=article.get("content_available", False),
                content=(article.get("content", "") or article.get("snippet", ""))[:classify_content_limit],
                related_context=_build_related_context(article),
                feedback_context=feedback_summary_text or "Chưa có feedback mới từ team.",
            )
        )
    return "\n\n".join(blocks)


def _apply_result(article: dict[str, Any], result: dict[str, Any], min_score: int) -> None:
    try:
        c1 = int(result.get("c1_score", 0) or 0)
        c2 = int(result.get("c2_score", 0) or 0)
        c3 = int(result.get("c3_score", 0) or 0)
    except (TypeError, ValueError):
        c1, c2, c3 = 0, 0, 0

    analysis_tier = str(result.get("analysis_tier", "")).strip().lower()
    if analysis_tier not in {"deep", "basic", "skip"}:
        projected_total = c1 + c2 + c3
        if projected_total >= min_score:
            analysis_tier = "deep"
        elif projected_total >= 30:
            analysis_tier = "basic"
        else:
            analysis_tier = "skip"

    article.update(
        {
            "primary_type": result.get("primary_type", "Practical"),
            "primary_emoji": result.get("primary_emoji", "🛠️"),
            "c1_score": c1,
            "c1_reason": str(result.get("c1_reason", "")),
            "c2_score": c2,
            "c2_reason": str(result.get("c2_reason", "")),
            "c3_score": c3,
            "c3_reason": str(result.get("c3_reason", "")),
            "total_score": c1 + c2 + c3,
            "summary_vi": str(result.get("summary_vi", "")),
            "editorial_angle": str(result.get("editorial_angle", "")),
            "analysis_tier": analysis_tier,
            "tags": result.get("tags", []) if isinstance(result.get("tags"), list) else [],
            "relevance_level": str(result.get("relevance_level", "Low")),
        }
    )
    article["component_score_source"] = "model"
    article["base_total_score"] = c1 + c2 + c3
    article["adjusted_total_score"] = c1 + c2 + c3
    article["score_adjustment_total"] = 0
    article["applied_adjustments"] = []
    _apply_strategic_boost(article, min_score)
    _apply_freshness_penalty(article, min_score)
    _apply_source_history_adjustment(article, min_score)
    _finalize_scored_article(article, min_score)


def _fallback_single_article(
    article: dict[str, Any],
    *,
    min_score: int,
    classify_content_limit: int,
    classify_max_tokens: int,
    feedback_summary_text: str,
) -> dict[str, Any]:
    user_prompt = CLASSIFY_SCORE_USER_TEMPLATE.format(
        title=article.get("title", "N/A"),
        url=article.get("url", ""),
        source=article.get("source", "Unknown"),
        source_domain=article.get("source_domain", ""),
        published_at=article.get("published_at", article.get("published", "")),
        published_at_source=article.get("published_at_source", "unknown"),
        discovered_at=article.get("discovered_at", article.get("fetched_at", "")),
        age_hours=article.get("age_hours", ""),
        freshness_unknown=article.get("freshness_unknown", False),
        is_stale_candidate=article.get("is_stale_candidate", False),
        source_verified=article.get("source_verified", False),
        content_available=article.get("content_available", False),
        content=(article.get("content", "") or article.get("snippet", ""))[:classify_content_limit],
        related_context=_build_related_context(article),
        feedback_context=feedback_summary_text or "Chưa có feedback mới từ team.",
    )
    try:
        inference = _normalize_classify_inference_response(
            single_article_json_inference(
                CLASSIFY_SCORE_SYSTEM,
                user_prompt,
                max_tokens=classify_max_tokens,
                temperature=0.1,
                response_format=CLASSIFY_RESPONSE_FORMAT,
            )
        )
        result, raw_output, looks_structured = inference
        if result is None and (_is_likely_prose_response(raw_output) or not looks_structured):
            logger.warning("⚠️ Batch classify fallback prose rescue cho '%s'.", article.get("title", "N/A")[:42])
        elif result is None:
            result = _classify_inference_with_retry(
                user_prompt,
                max_tokens=classify_max_tokens,
                temperature=0.1,
                initial_response=inference,
            )
        if result and isinstance(result, dict):
            _apply_result(article, result, min_score)
        else:
            _classify_prose_rescue(article, raw_output, min_score)
            _apply_source_history_adjustment(article, min_score)
            _finalize_scored_article(article, min_score)
    except Exception as exc:
        logger.error("❌ Batch classify single fallback failed: '%s': %s", article.get("title", "N/A")[:42], exc)
        _llm_failure_fallback(article, min_score)
        _apply_source_history_adjustment(article, min_score)
        _finalize_scored_article(article, min_score)
    return article


def batch_classify_and_score_node(state: dict[str, Any]) -> dict[str, Any]:
    """
    // MVP3 Speed Optimized - Batch + Parallel
    Batch classify + score cho toàn bộ shortlist nhằm giảm số lần gọi MLX.
    """
    articles = list(state.get("filtered_articles", []) or state.get("new_articles", []) or [])
    if not articles:
        logger.info("📭 Không có bài mới để batch classify.")
        return {
            "scored_articles": [],
            "top_articles": [],
            "low_score_articles": [],
            "scored_snapshot_path": "",
        }

    min_score = _cfg_int(state, "min_deep_analysis_score", "MIN_DEEP_ANALYSIS_SCORE", 55)
    max_top = _cfg_int(state, "max_deep_analysis_articles", "MAX_DEEP_ANALYSIS_ARTICLES", 10)
    max_classify = _cfg_int(state, "max_classify_articles", "MAX_CLASSIFY_ARTICLES", 25)
    classify_content_limit = _cfg_int(state, "classify_content_char_limit", "CLASSIFY_CONTENT_CHAR_LIMIT", 900)
    classify_max_tokens = _cfg_int(state, "classify_max_tokens", "CLASSIFY_MAX_TOKENS", 320)
    runtime_config = dict(state.get("runtime_config", {}) or {})
    batch_size = max(2, int(runtime_config.get("batch_classify_size", 8) or 8))

    llm_articles, held_out_articles = _prepare_classify_candidates(
        list(articles),
        max_classify,
        runtime_config=runtime_config,
        feedback_summary_text=state.get("feedback_summary_text", ""),
        feedback_preferences=state.get("feedback_preference_profile", {}),
    )
    logger.info(
        "🧮 Batch prefilter giữ %d/%d bài cho batch classify (held_out=%d, batch_size=%d)",
        len(llm_articles),
        len(articles),
        len(held_out_articles),
        batch_size,
    )

    scored: list[dict[str, Any]] = []
    feedback_summary_text = str(state.get("feedback_summary_text", "") or "")

    for batch_index in range(0, len(llm_articles), batch_size):
        batch_articles = llm_articles[batch_index: batch_index + batch_size]
        item_batch = [(f"article_{batch_index + offset}", article) for offset, article in enumerate(batch_articles)]
        logger.info(
            "🏷️  Batch Classify+Score [%d/%d]: %d bài",
            batch_index // batch_size + 1,
            max(1, math.ceil(len(llm_articles) / batch_size)),
            len(item_batch),
        )
        parsed, raw_output, _looks_structured = run_json_inference_meta(
            _batch_system_prompt(),
            _build_batch_user_prompt(
                item_batch,
                classify_content_limit=classify_content_limit,
                feedback_summary_text=feedback_summary_text,
            ),
            max_tokens=max(1800, classify_max_tokens * len(item_batch) * 1.8),
            temperature=0.1,
            response_format=_batch_response_format(len(item_batch)),
        )
        result_map: dict[str, dict[str, Any]] = {}
        if isinstance(parsed, dict):
            for item in parsed.get("articles", []) or []:
                if isinstance(item, dict) and str(item.get("item_id", "")).strip():
                    result_map[str(item.get("item_id"))] = item

        if not result_map:
            logger.warning("⚠️ Batch classify không parse được JSON ổn định; fallback từng bài.")

        for item_id, article in item_batch:
            if item_id in result_map:
                _apply_result(article, result_map[item_id], min_score)
            else:
                if raw_output:
                    logger.debug("Batch classify raw output sample: %s", raw_output[:280])
                _fallback_single_article(
                    article,
                    min_score=min_score,
                    classify_content_limit=classify_content_limit,
                    classify_max_tokens=classify_max_tokens,
                    feedback_summary_text=feedback_summary_text,
                )
            scored.append(article)

    for article in held_out_articles:
        _held_out_article_fallback(article)
        _apply_source_history_adjustment(article, min_score)
        _finalize_scored_article(article, min_score)
        scored.append(article)

    scored.sort(key=lambda a: a.get("total_score", 0), reverse=True)
    primary_event_articles = _annotate_event_clusters(scored, min_score)
    for article in scored:
        _finalize_scored_article(article, min_score)
    primary_event_articles.sort(key=lambda a: a.get("total_score", 0), reverse=True)
    top, score_cutoff = _select_top_articles(primary_event_articles, max_items=max_top)
    low = [article for article in scored if article not in top]

    logger.info(
        "✅ Batch classify xong: %d bài / %d event → %d top (cutoff=%d, max=%d) + %d low",
        len(scored),
        len(primary_event_articles),
        len(top),
        score_cutoff,
        max_top,
        len(low),
    )

    scored_snapshot_path = write_temporal_snapshot(
        state=state,
        stage="scored",
        articles=scored,
        extra={
            "scored_count": len(scored),
            "primary_event_count": len(primary_event_articles),
            "top_count": len(top),
            "low_score_count": len(low),
            "min_deep_analysis_score": min_score,
            "dynamic_score_cutoff": score_cutoff,
            "max_deep_analysis_articles": max_top,
            "max_classify_articles": max_classify,
            "batch_classify_size": batch_size,
        },
    )

    return {
        "scored_articles": scored,
        "top_articles": top,
        "low_score_articles": low,
        "scored_snapshot_path": scored_snapshot_path,
    }
