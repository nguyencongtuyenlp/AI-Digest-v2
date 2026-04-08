"""
batch_quick_compose_node.py — Batch short note cho low_score_articles.
"""

from __future__ import annotations

import logging
import math
from typing import Any


from digest.editorial.editorial_guardrails import build_article_grounding, sanitize_delivery_text
from digest.runtime.mlx_runner import (
    resolve_pipeline_mlx_path,
    run_json_inference_large_meta,
    run_json_inference_small_meta,
)
from digest.workflow.nodes.classify_and_score import _cfg_int
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


def _batch_system_prompt(*, compact_for_light_model: bool = False) -> str:
    base = (
        f"{NOTE_SUMMARY_SYSTEM}\n\n"
        "# // MVP3 Speed Optimized - Batch + Parallel\n"
        "Bạn sẽ nén nhiều bài viết trong một lần gọi.\n"
        "Trả về đúng 1 JSON object có key `articles`.\n"
        "Mỗi item phải có `item_id` và `note_summary_vi`.\n"
        "note_summary_vi chỉ nên 1 đoạn 35-70 từ, 1-2 câu, mở bằng diễn biến chính, không bullet, không khuyến nghị hành động.\n"
        "Không bỏ sót item. Không thêm prose ngoài JSON."
    )
    if compact_for_light_model:
        base += "\n\n# FAST MODEL: note_summary_vi phải cực gọn, bám fact, ưu tiên 1-2 câu ngắn.\n"
    return base


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

    light_mlx = resolve_pipeline_mlx_path("light", runtime_config)
    heavy_mlx = resolve_pipeline_mlx_path("heavy", runtime_config)
    use_light = light_mlx != heavy_mlx
    chunk_size = max(
        4,
        min(
            14,
            _cfg_int(state, "batch_quick_compose_size", "BATCH_QUICK_COMPOSE_SIZE", 10),
        ),
    )

    result_map: dict[str, dict[str, Any]] = {}
    raw_tail = ""

    def _merge_quick_items(parsed_obj: Any) -> None:
        if not isinstance(parsed_obj, dict):
            return
        for item in parsed_obj.get("articles", []) or []:
            if not isinstance(item, dict):
                continue
            iid = str(item.get("item_id", "") or "").strip()
            if not iid:
                continue
            if not str(item.get("note_summary_vi", "") or "").strip() and str(item.get("summary_vi", "") or "").strip():
                item = {**item, "note_summary_vi": str(item.get("summary_vi", "") or "").strip()}
            result_map[iid] = item

    total_chunks = max(1, math.ceil(len(batch) / chunk_size))
    for chunk_index in range(0, len(batch), chunk_size):
        sub_batch = batch[chunk_index : chunk_index + chunk_size]
        logger.info(
            "📝 Batch quick compose [%d/%d]: %d bài (chunk_size=%d)",
            chunk_index // chunk_size + 1,
            total_chunks,
            len(sub_batch),
            chunk_size,
        )
        user_blob = _build_batch_user_prompt(sub_batch)
        fmt = _batch_response_format(len(sub_batch))
        max_tok = max(140 * len(sub_batch), 360)

        parsed, raw_output, _looks_structured = run_json_inference_small_meta(
            _batch_system_prompt(compact_for_light_model=use_light),
            user_blob,
            max_tokens=max_tok,
            temperature=0.2,
            model_path=light_mlx if use_light else heavy_mlx,
            response_format=fmt,
        )
        raw_tail = raw_output
        _merge_quick_items(parsed)

        sub_ids = [sid for sid, _ in sub_batch]
        if use_light and sum(1 for sid in sub_ids if sid in result_map) < len(sub_batch):
            logger.warning(
                "⚠️ Quick compose light thiếu item chunk (%d/%d); thử heavy MLX.",
                sum(1 for sid in sub_ids if sid in result_map),
                len(sub_batch),
            )
            parsed_h, raw_h, _ = run_json_inference_large_meta(
                _batch_system_prompt(compact_for_light_model=False),
                user_blob,
                max_tokens=max_tok,
                temperature=0.2,
                model_path=heavy_mlx,
                response_format=fmt,
            )
            raw_tail = raw_h
            _merge_quick_items(parsed_h)

    processed: list[dict[str, Any]] = []
    for item_id, article in batch:
        result = result_map.get(item_id)
        if result:
            note = sanitize_delivery_text(str(result.get("note_summary_vi", "") or "").strip())
            article["note_summary_vi"] = note or _fallback_note(article)
        else:
            if raw_tail:
                logger.debug("Batch quick compose raw output sample: %s", raw_tail[:240])
            article["note_summary_vi"] = _fallback_note(article)
        article.pop("_mvp3_grounding", None)
        processed.append(article)

    logger.info("✅ Batch quick compose hoàn tất: %d bài", len(processed))
    return {"low_score_articles": processed}
