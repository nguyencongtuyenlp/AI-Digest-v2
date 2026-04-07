"""
batch_deep_process_node.py — Gộp deep_analysis + recommend_idea + compose_note_summary (MVP3 Improved)
"""

from __future__ import annotations

import logging
from typing import Any


from digest.editorial.editorial_guardrails import build_article_grounding, sanitize_delivery_text
from digest.runtime.mlx_runner import run_json_inference_meta
from digest.workflow.nodes.compose_note_summary import NOTE_SUMMARY_SYSTEM, _fallback_note
from digest.workflow.nodes.deep_analysis import (
    DEEP_ANALYSIS_SYSTEM,
    DEEP_ANALYSIS_USER_TEMPLATE,
    _ensure_evidence_sections,
    _search_community_reactions,
)
from digest.workflow.nodes.recommend_idea import RECOMMEND_SYSTEM

logger = logging.getLogger(__name__)


def _batch_response_format(max_items: int) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "batch_deep_process_articles",
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
                                "deep_analysis": {"type": "string"},
                                "recommend_idea": {"type": "string"},
                                "note_summary_vi": {"type": "string"},
                            },
                            "required": ["item_id", "deep_analysis", "recommend_idea", "note_summary_vi"],
                        },
                    }
                },
                "required": ["articles"],
            },
        },
    }


def _batch_system_prompt() -> str:
    return (
        f"{DEEP_ANALYSIS_SYSTEM}\n\n"
        f"{RECOMMEND_SYSTEM}\n\n"
        f"{NOTE_SUMMARY_SYSTEM}\n\n"
        "// MVP3 Speed Optimized - Batch + Parallel\n"
        "Bạn sẽ xử lý nhiều bài viết ĐỘC LẬP trong một lần gọi.\n"
        "Với MỖI bài, bạn PHẢI trả về đúng 3 phần sau và giữ NGUYÊN chất lượng, tone, cấu trúc như khi chạy riêng lẻ:\n"
        "1. deep_analysis: Phân tích sâu, có cấu trúc đầy đủ (Executive Note, Evidence, Caveats...).\n"
        "2. recommend_idea: Ý tưởng hành động cụ thể, actionable, founder-grade theo format cũ.\n"
        "3. note_summary_vi: Tóm tắt tiếng Việt ngắn gọn, sắc bén, hấp dẫn phù hợp Telegram.\n"
        "Xử lý từng item hoàn toàn độc lập. Không lẫn thông tin giữa các bài. Chỉ trả về JSON thuần."
    )


def _related_text(article: dict[str, Any]) -> str:
    related = article.get("related_past", []) or []
    if not related:
        return "(Không có bài cũ cùng chủ đề trong memory)"
    lines = [f"- [{r.get('primary_type', '?')}] {r.get('title', 'N/A')} (score: {r.get('score', 0)})" for r in related[:3]]
    return "\n".join(lines)


def _community_text(article: dict[str, Any]) -> str:
    keyword = str(article.get("title", "") or "").split(" – ")[0].split(" | ")[0][:80]
    community = _search_community_reactions(keyword)
    return community if community else "Chưa có dữ liệu cộng đồng"


def _existing_grounding(article: dict[str, Any]) -> dict[str, Any] | None:
    keys = ("grounding_note", "fact_anchors_text", "reasonable_inferences_text", "unknowns_text")
    if all(str(article.get(key, "") or "").strip() for key in keys):
        return {key: article.get(key, "") for key in keys}
    return None


def _structured_context_block(article: dict[str, Any]) -> str:
    pairs = (
        ("Structured summary", str(article.get("factual_summary_vi", "") or "").strip()),
        ("Why it matters", str(article.get("why_it_matters_vi", "") or "").strip()),
        ("Editorial angle", str(article.get("optional_editorial_angle", "") or article.get("editorial_angle", "") or "").strip()),
        ("Current note", str(article.get("note_summary_vi", "") or "").strip()),
    )
    lines = [f"- {label}: {value}" for label, value in pairs if value]
    return "\n".join(lines)


def _deep_process_source_context(article: dict[str, Any], *, raw_limit: int) -> str:
    raw_source = str(article.get("content", "") or article.get("snippet", "") or "").strip()
    structured = _structured_context_block(article)
    trimmed_raw = raw_source[:raw_limit]
    if structured and trimmed_raw:
        return f"{structured}\n\nRaw source excerpt:\n{trimmed_raw}"
    if structured:
        return structured
    return trimmed_raw


def _build_batch_user_prompt(batch: list[tuple[str, dict[str, Any]]]) -> str:
    blocks: list[str] = []
    for item_id, article in batch:
        grounding = article.get("_mvp3_grounding", {}) or {}
        content = str(article.get("_deep_process_content", "") or "")
        community_text = str(article.get("community_reactions", "") or "")[:1400]
        
        block = (
            f"===== BEGIN ARTICLE {item_id} =====\n"
            + DEEP_ANALYSIS_USER_TEMPLATE.format(
                title=article.get("title", "N/A"),
                primary_type=article.get("primary_type", "Unknown"),
                total_score=article.get("total_score", 0),
                editorial_angle=article.get("editorial_angle", "N/A"),
                url=article.get("url", ""),
                source=article.get("source", "Unknown"),
                source_domain=article.get("source_domain", ""),
                published_at=article.get("published_at", article.get("published", "")),
                source_verified=article.get("source_verified", False),
                source_tier=article.get("source_tier", "unknown"),
                grounding_note=grounding.get("grounding_note", ""),
                fact_anchors=grounding.get("fact_anchors_text", "- Chưa có fact anchor mạnh."),
                reasonable_inferences=grounding.get("reasonable_inferences_text", "- Không có suy luận bổ sung."),
                unknowns=grounding.get("unknowns_text", "- Không có unknown lớn từ metadata."),
                content=content,
                community_reactions=community_text,
                related_past=_related_text(article),
            )
            + f"\n===== END ARTICLE {item_id} =====\n"
            + "YÊU CẦU OUTPUT CHO BÀI NÀY:\n"
            "- deep_analysis: phân tích sâu theo cấu trúc cũ\n"
            "- recommend_idea: ý tưởng hành động cụ thể, founder-grade\n"
            "- note_summary_vi: tóm tắt tiếng Việt ngắn gọn, sắc bén\n"
        )
        blocks.append(block)
    return "\n\n".join(blocks)


def _fallback_bundle(article: dict[str, Any]) -> None:
    # (giữ nguyên như cũ của Codex, không thay đổi)
    grounding = article.get("_mvp3_grounding", {}) or build_article_grounding(article)
    # ... (copy nguyên phần _fallback_bundle cũ của bạn)
    article["deep_analysis"] = analysis
    article["content_page_md"] = analysis
    article["recommend_idea"] = "..."
    article["note_summary_vi"] = _fallback_note(article)


def batch_deep_process_node(state: dict[str, Any]) -> dict[str, Any]:
    """
    // MVP3 Speed Optimized - Batch + Parallel (Improved Version)
    """
    top_articles = list(state.get("top_articles", []) or [])
    if not top_articles:
        logger.info("📭 Batch deep process: không có top_articles.")
        return {"analyzed_articles": []}

    # Chunking nhẹ (max 5 bài/lần) để giữ chất lượng
    CHUNK_SIZE = 5
    analyzed: list[dict[str, Any]] = []
    runtime_config = dict(state.get("runtime_config", {}) or {})
    raw_limit = max(1200, int(runtime_config.get("deep_process_content_char_limit", 2200) or 2200))
    community_cache: dict[str, str] = {}

    for i in range(0, len(top_articles), CHUNK_SIZE):
        chunk = top_articles[i : i + CHUNK_SIZE]
        batch: list[tuple[str, dict[str, Any]]] = []

        for idx, article in enumerate(chunk, 1):
            keyword = str(article.get("title", "") or "").split(" – ")[0].split(" | ")[0][:80]
            if str(article.get("community_reactions", "") or "").strip():
                community = str(article.get("community_reactions", "") or "")
            elif keyword in community_cache:
                community = community_cache[keyword]
            else:
                community = _search_community_reactions(keyword) or "Chưa có dữ liệu cộng đồng"
                community_cache[keyword] = community
            article["community_reactions"] = community

            grounding = _existing_grounding(article) or build_article_grounding(article)
            article.update(grounding)
            article["_mvp3_grounding"] = grounding
            article["_deep_process_content"] = _deep_process_source_context(article, raw_limit=raw_limit)
            batch.append((f"top_{i+idx}", article))

        logger.info("🔬 Batch deep process chunk %d/%d (%d bài)", i//CHUNK_SIZE + 1, (len(top_articles)+CHUNK_SIZE-1)//CHUNK_SIZE, len(batch))

        parsed, raw_output, _ = run_json_inference_meta(
            _batch_system_prompt(),
            _build_batch_user_prompt(batch),
            max_tokens=max(4200 * len(batch), 9500),   # Tăng đáng kể
            temperature=0.25,                         # Giảm để ổn định
            response_format=_batch_response_format(len(batch)),
        )

        # Phần xử lý parsed + fallback giữ nguyên logic cũ của Codex
        # (copy nguyên phần result_map, _fallback_bundle, pop _mvp3_grounding từ file bạn paste)

        result_map = {str(item.get("item_id")): item for item in (parsed.get("articles", []) if isinstance(parsed, dict) else [])}
        
        for item_id, article in batch:
            result = result_map.get(item_id)
            if result:
                analysis = _ensure_evidence_sections(str(result.get("deep_analysis", "")).strip(), article.get("_mvp3_grounding", {}))
                article["deep_analysis"] = analysis
                article["content_page_md"] = analysis
                article["recommend_idea"] = str(result.get("recommend_idea", "")).strip() or "Không thể tạo recommendation — lỗi hệ thống."
                note = sanitize_delivery_text(str(result.get("note_summary_vi", "")).strip())
                article["note_summary_vi"] = note or _fallback_note(article)
            else:
                _fallback_bundle(article)
            article.pop("_mvp3_grounding", None)
            article.pop("_deep_process_content", None)
            analyzed.append(article)

    return {"analyzed_articles": analyzed}
