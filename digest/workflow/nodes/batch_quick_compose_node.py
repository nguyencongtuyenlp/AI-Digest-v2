"""
batch_quick_compose_node.py — Batch short note cho low_score_articles.
"""

from __future__ import annotations

import logging
from typing import Any


from digest.editorial.editorial_guardrails import build_article_grounding, sanitize_delivery_text
from digest.runtime.mlx_runner import run_json_inference_meta
from digest.workflow.nodes.compose_note_summary import NOTE_SUMMARY_SYSTEM, NOTE_SUMMARY_USER_TEMPLATE, _analysis_excerpt, _fallback_note

logger = logging.getLogger(__name__)


def _batch_response_format(max_items: int) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "batch_quick_note_summary",
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
                            "properties": {
                                "item_id": {"type": "string"},
                                "note_summary_vi": {"type": "string"},
                            },
                            "required": ["item_id", "note_summary_vi"],
                        },
                    }
                },
                "required": ["articles"],
            },
        },
    }


def _batch_system_prompt() -> str:
    return (
        f"{NOTE_SUMMARY_SYSTEM}\n\n"
        "# // MVP3 Speed Optimized - Batch + Parallel\n"
        "Bạn sẽ nén nhiều bài viết trong một lần gọi.\n"
        "Trả về đúng 1 JSON object có key `articles`.\n"
        "Mỗi item phải có `item_id` và `note_summary_vi`.\n"
        "Không bỏ sót item. Không thêm prose ngoài JSON."
    )


def _build_batch_user_prompt(batch: list[tuple[str, dict[str, Any]]]) -> str:
    blocks: list[str] = []
    for item_id, article in batch:
        grounding = article.get("_mvp3_grounding", {}) or {}
        blocks.append(
            f"=== ITEM {item_id} ===\n"
            + NOTE_SUMMARY_USER_TEMPLATE.format(
                title=article.get("title", "N/A"),
                primary_type=article.get("primary_type", "Unknown"),
                total_score=article.get("total_score", 0),
                published_at=article.get("published_at", ""),
                source=article.get("source", "Unknown"),
                editorial_angle=article.get("editorial_angle", "N/A"),
                summary_vi=article.get("summary_vi", ""),
                fact_anchors=grounding.get("fact_anchors_text", "- Chưa có fact anchor mạnh."),
                analysis=_analysis_excerpt(article)[:4500],
            )
        )
    return "\n\n".join(blocks)


def batch_quick_compose_node(state: dict[str, Any]) -> dict[str, Any]:
    """
    // MVP3 Speed Optimized - Batch + Parallel
    Batch compose note summary cho low_score_articles.
    """
    low_score_articles = list(state.get("low_score_articles", []) or [])
    if not low_score_articles:
        logger.info("📭 Batch quick compose: không có low_score_articles.")
        return {"low_score_articles": []}

    runtime_config = dict(state.get("runtime_config", {}) or {})
    run_profile = str(state.get("run_profile", "") or "").strip().lower()
    fast_mode = run_profile == "fast" or bool(runtime_config.get("deterministic_note_summary", False))

    batch: list[tuple[str, dict[str, Any]]] = []
    for index, article in enumerate(low_score_articles, 1):
        grounding = build_article_grounding(article)
        article.update(grounding)
        article["_mvp3_grounding"] = grounding
        batch.append((f"low_{index}", article))

    if fast_mode:
        for _item_id, article in batch:
            article["note_summary_vi"] = _fallback_note(article)
            article.pop("_mvp3_grounding", None)
        logger.info("⚡ Batch quick compose: dùng deterministic fallback cho %d bài", len(batch))
        return {"low_score_articles": [article for _item_id, article in batch]}

    parsed, raw_output, _looks_structured = run_json_inference_meta(
        _batch_system_prompt(),
        _build_batch_user_prompt(batch),
        max_tokens=max(180 * len(batch), 360),
        temperature=0.3,
        response_format=_batch_response_format(len(batch)),
    )
    result_map: dict[str, dict[str, Any]] = {}
    if isinstance(parsed, dict):
        for item in parsed.get("articles", []) or []:
            if isinstance(item, dict) and str(item.get("item_id", "")).strip():
                result_map[str(item.get("item_id"))] = item

    processed: list[dict[str, Any]] = []
    for item_id, article in batch:
        result = result_map.get(item_id)
        if result:
            note = sanitize_delivery_text(str(result.get("note_summary_vi", "") or "").strip())
            article["note_summary_vi"] = note or _fallback_note(article)
        else:
            if raw_output:
                logger.debug("Batch quick compose raw output sample: %s", raw_output[:240])
            article["note_summary_vi"] = _fallback_note(article)
        article.pop("_mvp3_grounding", None)
        processed.append(article)

    logger.info("✅ Batch quick compose hoàn tất: %d bài", len(processed))
    return {"low_score_articles": processed}
