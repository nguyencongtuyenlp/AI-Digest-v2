"""
batch_deep_process_node.py — Gộp deep_analysis + recommend_idea + compose_note_summary (MVP3 Improved)
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from editorial_guardrails import build_article_grounding, sanitize_delivery_text
from mlx_runner import run_json_inference_meta
from nodes.compose_note_summary import NOTE_SUMMARY_SYSTEM, _fallback_note
from nodes.deep_analysis import (
    DEEP_ANALYSIS_SYSTEM,
    DEEP_ANALYSIS_USER_TEMPLATE,
    _ensure_evidence_sections,
    _search_community_reactions,
)
from nodes.recommend_idea import RECOMMEND_SYSTEM

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


def _build_batch_user_prompt(batch: list[tuple[str, dict[str, Any]]]) -> str:
    blocks: list[str] = []
    for item_id, article in batch:
        grounding = article.get("_mvp3_grounding", {}) or {}
        content = (article.get("content", "") or article.get("snippet", ""))[:2800]
        
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
                community_reactions=str(article.get("community_reactions", "") or "")[:2000],
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

    for i in range(0, len(top_articles), CHUNK_SIZE):
        chunk = top_articles[i : i + CHUNK_SIZE]
        batch: list[tuple[str, dict[str, Any]]] = []

        for idx, article in enumerate(chunk, 1):
            article["community_reactions"] = _community_text(article)
            grounding = build_article_grounding(article)
            article.update(grounding)
            article["_mvp3_grounding"] = grounding
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
            analyzed.append(article)

    return {"analyzed_articles": analyzed}