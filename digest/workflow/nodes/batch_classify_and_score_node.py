"""
batch_classify_and_score_node.py — Batch classify + score cho nhiều bài cùng lúc.
"""

from __future__ import annotations

import json
import logging
import math
import os
from typing import Any


from digest.runtime.mlx_runner import (
    resolve_pipeline_mlx_path,
    run_json_inference_large_meta,
    run_json_inference_small_meta,
)
from digest.runtime.temporal_snapshots import write_temporal_snapshot
from digest.runtime.xai_grok import grok_classify_enabled, merge_grok_observability

from digest.workflow.nodes.classify_and_score import (
    CLASSIFY_RESPONSE_FORMAT,
    CLASSIFY_SCORE_SYSTEM,
    CLASSIFY_SCORE_USER_TEMPLATE,
    CLASSIFY_JSON_STATUS_REPAIRED,
    CLASSIFY_JSON_STATUS_VALID,
    _apply_structured_classify_result,
    _annotate_event_clusters,
    _apply_freshness_penalty,
    _apply_source_history_adjustment,
    _apply_strategic_boost,
    _build_related_context,
    _cfg_int,
    _classify_prose_rescue,
    _held_out_article_fallback,
    _llm_failure_fallback,
    _prepare_classify_candidates,
    _recover_structured_json_dict,
    _resolve_classify_inference_details,
    _finalize_scored_article,
    _select_top_articles,
    run_json_inference as single_article_json_inference,
)

logger = logging.getLogger(__name__)

# Backward compatibility for older tests that patch this symbol directly.
run_json_inference_meta = run_json_inference_small_meta


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


def _batch_system_prompt(*, compact_for_light_model: bool = False) -> str:
    base = (
        f"{CLASSIFY_SCORE_SYSTEM}\n\n"
        "# // MVP3 Speed Optimized - Batch + Parallel\n"
        "Bạn xử lý nhiều bài viết ĐỘC LẬP. Không so sánh chéo.\n"
        "primary_type CHỈ được dùng các giá trị: Product, Society & Culture, Practical.\n"
        "Mỗi item phải điền đủ factual_summary_vi, why_it_matters_vi, optional_editorial_angle dù ngắn.\n"
        "factual_summary_vi và why_it_matters_vi phải là câu ngắn, rõ, không lan man, không liệt kê.\n"
        "Giữ nguyên tên riêng/thuật ngữ tiếng Anh nếu có, nhưng diễn giải phần còn lại bằng tiếng Việt tự nhiên.\n"
        "Trả về đúng JSON với key `articles`. Mỗi item phải có `item_id`.\n"
        "Tuân thủ nghiêm ngặt schema. Không thêm text ngoài JSON."
    )
    if compact_for_light_model:
        base += (
            "\n\n# FAST LOCAL MODEL\n"
            "factual_summary_vi và why_it_matters_vi mỗi field 1-2 câu ngắn, đủ ý; "
            "optional_editorial_angle có thể một cụm ngắn. Bám chặt schema JSON.\n"
        )
    return base
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


def _apply_result(
    article: dict[str, Any],
    result: dict[str, Any],
    min_score: int,
    *,
    json_status: str = CLASSIFY_JSON_STATUS_VALID,
    provider_used: str = "local",
) -> None:
    _apply_structured_classify_result(article, result, min_score, json_status=json_status)
    article["classify_provider_used"] = str(provider_used or "local")
    article["component_score_source"] = "model"
    article["base_total_score"] = int(article.get("c1_score", 0) or 0) + int(article.get("c2_score", 0) or 0) + int(article.get("c3_score", 0) or 0)
    article["adjusted_total_score"] = article["base_total_score"]
    article["score_adjustment_total"] = 0
    article["applied_adjustments"] = []
    _apply_strategic_boost(article, min_score)
    _apply_freshness_penalty(article, min_score)
    _apply_source_history_adjustment(article, min_score)
    _finalize_scored_article(article, min_score)


def _extract_batch_result_map(parsed: Any, raw_output: str) -> tuple[dict[str, dict[str, Any]], str]:
    result_map: dict[str, dict[str, Any]] = {}
    status = CLASSIFY_JSON_STATUS_VALID

    payload = parsed if isinstance(parsed, dict) else None
    if payload is None:
        recovered = _recover_structured_json_dict(raw_output)
        if isinstance(recovered, dict):
            payload = recovered
            status = CLASSIFY_JSON_STATUS_REPAIRED

    if isinstance(payload, dict):
        for item in payload.get("articles", []) or []:
            if isinstance(item, dict) and str(item.get("item_id", "")).strip():
                result_map[str(item.get("item_id"))] = item
    return result_map, status


def _fallback_single_article(
    article: dict[str, Any],
    *,
    min_score: int,
    classify_content_limit: int,
    classify_max_tokens: int,
    feedback_summary_text: str,
    runtime_config: dict[str, Any] | None = None,
    heavy_mlx_path: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
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
    details: dict[str, Any] = {
        "grok_request_count": 0,
        "grok_success_count": 0,
        "grok_fallback_count": 0,
        "grok_items_processed": 0,
        "classify_local_failure_count": 0,
        "classify_grok_rescue_count": 0,
        "classify_benchmark_request_count": 0,
        "classify_benchmark_success_count": 0,
    }
    try:
        heavy = heavy_mlx_path or resolve_pipeline_mlx_path("heavy", runtime_config)
        result, json_status, raw_output, provider_used, details = _resolve_classify_inference_details(
            user_prompt,
            max_tokens=classify_max_tokens,
            temperature=0.1,
            initial_response=single_article_json_inference(
                CLASSIFY_SCORE_SYSTEM,
                user_prompt,
                max_tokens=classify_max_tokens,
                temperature=0.1,
                response_format=CLASSIFY_RESPONSE_FORMAT,
                model_path=heavy,
            ),
            runtime_config=runtime_config,
            local_model_path=heavy,
        )
        article["classify_provider_used"] = str(provider_used or "local")
        if result and isinstance(result, dict):
            _apply_result(
                article,
                result,
                min_score,
                json_status=json_status or CLASSIFY_JSON_STATUS_VALID,
                provider_used=str(provider_used or "local"),
            )
        else:
            _classify_prose_rescue(article, raw_output, min_score)
            _apply_source_history_adjustment(article, min_score)
            _finalize_scored_article(article, min_score)
    except Exception as exc:
        logger.error("❌ Batch classify single fallback failed: '%s': %s", article.get("title", "N/A")[:42], exc)
        article["classify_provider_used"] = str(article.get("classify_provider_used", "local") or "local")
        _llm_failure_fallback(article, min_score)
        _apply_source_history_adjustment(article, min_score)
        _finalize_scored_article(article, min_score)
    return article, details


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
    grok_classify_is_enabled = grok_classify_enabled(runtime_config)

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
    classify_grok_request_count = 0
    classify_grok_success_count = 0
    classify_grok_fallback_count = 0
    classify_grok_items_processed = 0
    classify_local_failure_count = 0
    classify_grok_rescue_count = 0
    classify_benchmark_request_count = 0
    classify_benchmark_success_count = 0
    classify_provider_counts = {"local": 0, "grok": 0, "local_then_grok": 0}

    light_mlx = resolve_pipeline_mlx_path("light", runtime_config)
    heavy_mlx = resolve_pipeline_mlx_path("heavy", runtime_config)
    use_light_tier = light_mlx != heavy_mlx

    for batch_index in range(0, len(llm_articles), batch_size):
        batch_articles = llm_articles[batch_index: batch_index + batch_size]
        item_batch = [(f"article_{batch_index + offset}", article) for offset, article in enumerate(batch_articles)]
        logger.info(
            "🏷️  Batch Classify+Score [%d/%d]: %d bài",
            batch_index // batch_size + 1,
            max(1, math.ceil(len(llm_articles) / batch_size)),
            len(item_batch),
        )
        user_blob = _build_batch_user_prompt(
            item_batch,
            classify_content_limit=classify_content_limit,
            feedback_summary_text=feedback_summary_text,
        )
        fmt = _batch_response_format(len(item_batch))
        max_tok = max(1200, classify_max_tokens * len(item_batch))
        primary_path = light_mlx if use_light_tier else heavy_mlx
        parsed, raw_output, _looks_structured = run_json_inference_meta(
            _batch_system_prompt(compact_for_light_model=use_light_tier),
            user_blob,
            max_tokens=max_tok,
            temperature=0.1,
            model_path=primary_path,
            response_format=fmt,
        )
        result_map, batch_json_status = _extract_batch_result_map(parsed, raw_output)

        if use_light_tier and result_map and len(result_map) < len(item_batch):
            logger.warning(
                "⚠️ Batch classify light thiếu item (%d/%d); thử lại batch bằng heavy.",
                len(result_map),
                len(item_batch),
            )
            parsed_h, raw_h, _ = run_json_inference_large_meta(
                _batch_system_prompt(compact_for_light_model=False),
                user_blob,
                max_tokens=max_tok,
                temperature=0.1,
                model_path=heavy_mlx,
                response_format=fmt,
            )
            alt_map, alt_status = _extract_batch_result_map(parsed_h, raw_h)
            if len(alt_map) >= len(result_map):
                result_map, batch_json_status = alt_map, alt_status
                raw_output = raw_h

        if not result_map and use_light_tier:
            logger.warning("⚠️ Batch classify (light MLX) thất bại; thử lại batch bằng heavy model.")
            parsed, raw_output, _looks_structured = run_json_inference_large_meta(
                _batch_system_prompt(compact_for_light_model=False),
                user_blob,
                max_tokens=max_tok,
                temperature=0.1,
                model_path=heavy_mlx,
                response_format=fmt,
            )
            result_map, batch_json_status = _extract_batch_result_map(parsed, raw_output)

        if not result_map:
            logger.warning("⚠️ Batch classify không parse được JSON ổn định; fallback từng bài.")

        for item_id, article in item_batch:
            if item_id in result_map:
                _apply_result(
                    article,
                    result_map[item_id],
                    min_score,
                    json_status=batch_json_status,
                    provider_used="local",
                )
                classify_provider_counts["local"] += 1
            else:
                if raw_output:
                    logger.debug("Batch classify raw output sample: %s", raw_output[:280])
                article, details = _fallback_single_article(
                    article,
                    min_score=min_score,
                    classify_content_limit=classify_content_limit,
                    classify_max_tokens=classify_max_tokens,
                    feedback_summary_text=feedback_summary_text,
                    runtime_config=runtime_config,
                    heavy_mlx_path=heavy_mlx,
                )
                provider_key = str(article.get("classify_provider_used", "local") or "local")
                if provider_key not in classify_provider_counts:
                    provider_key = "local"
                classify_provider_counts[provider_key] += 1
                classify_grok_request_count += int(details.get("grok_request_count", 0) or 0)
                classify_grok_success_count += int(details.get("grok_success_count", 0) or 0)
                classify_grok_fallback_count += int(details.get("grok_fallback_count", 0) or 0)
                classify_grok_items_processed += int(details.get("grok_items_processed", 0) or 0)
                classify_local_failure_count += int(details.get("classify_local_failure_count", 0) or 0)
                classify_grok_rescue_count += int(details.get("classify_grok_rescue_count", 0) or 0)
                classify_benchmark_request_count += int(details.get("classify_benchmark_request_count", 0) or 0)
                classify_benchmark_success_count += int(details.get("classify_benchmark_success_count", 0) or 0)
            scored.append(article)

    for article in held_out_articles:
        _held_out_article_fallback(article)
        classify_provider_counts["local"] += 1
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

    grok_metrics = merge_grok_observability(
        state,
        stage="classify",
        enabled=grok_classify_is_enabled,
        request_count=classify_grok_request_count,
        success_count=classify_grok_success_count,
        fallback_count=classify_grok_fallback_count,
        items_processed=classify_grok_items_processed,
        applied=classify_grok_rescue_count > 0,
        extra={
            "local_failure_count": classify_local_failure_count,
            "grok_rescue_count": classify_grok_rescue_count,
            "benchmark_request_count": classify_benchmark_request_count,
            "benchmark_success_count": classify_benchmark_success_count,
            "provider_local_count": classify_provider_counts.get("local", 0),
            "provider_grok_count": classify_provider_counts.get("grok", 0),
            "provider_local_then_grok_count": classify_provider_counts.get("local_then_grok", 0),
        },
    )

    return {
        "scored_articles": scored,
        "top_articles": top,
        "low_score_articles": low,
        "scored_snapshot_path": scored_snapshot_path,
        **grok_metrics,
    }
