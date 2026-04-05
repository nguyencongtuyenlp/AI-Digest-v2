"""
save_notion.py — LangGraph node: Lưu bài viết vào Notion Database.

Tạo 1 page Notion cho MỖI bài viết (không phải 1 page chung).
Mỗi page có đầy đủ 13 properties theo schema MVP2.

Đồng thời lưu vào ChromaDB memory để Agent nhớ dài hạn.

Properties Notion:
  1.  Name (Title) → emoji + tiêu đề
  2.  Type (Select) → 6 Primary Types
  3.  Created time (Date) → auto
  4.  Link gốc (URL)
  5.  Score (Number 1-100)
  6.  Tiêu chí chọn tin (Rich Text) → C1 reason
  7.  Mức độ phù hợp dự án (Select: High/Medium/Low)
  8.  Hệ đánh giá dự án (Rich Text) → C2 + C3 reason
  9.  Tổng hợp phân tích (Rich Text) → content_page_md / deep_analysis
  10. Recommend idea (Rich Text)
  11. Summarize (Rich Text) → note_summary_vi
  12. Tag (Multi-select)
  13. Status (Status)
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import sys
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import save_article
from editorial_guardrails import build_article_grounding
from memory import store_article
from source_history import record_source_history_run

logger = logging.getLogger(__name__)

STORAGE_PRODUCT_KEYWORDS = ("launch", "release", "api", "sdk", "feature", "model", "platform", "capability")
STORAGE_BUSINESS_KEYWORDS = (
    "startup",
    "funding",
    "raises",
    "partnership",
    "partners with",
    "market",
    "competition",
    "competitive",
    "race",
    "lead",
    "dominance",
)
STORAGE_POLICY_KEYWORDS = (
    "law",
    "regulation",
    "policy",
    "governance",
    "compliance",
    "safety",
    "security",
    "cyberattack",
    "breach",
    "hack",
    "lawsuit",
    "investigation",
)


PROPERTY_ALIASES: dict[str, list[tuple[str, str]]] = {
    "title": [("Name", "title"), ("Title", "title"), ("Tiêu đề", "title"), ("Tieu de", "title")],
    "type": [("Type", "select"), ("Loại tin", "select"), ("Loai tin", "select")],
    "url": [("Link gốc", "url"), ("Link goc", "url"), ("Link", "url"), ("URL", "url"), ("Source URL", "url"), ("Original URL", "url")],
    "score": [("Score", "number"), ("Điểm", "number"), ("Diem", "number")],
    "summary": [("Summarize", "rich_text"), ("Summary", "rich_text"), ("Tóm tắt", "rich_text"), ("Tom tat", "rich_text")],
    "recommend": [("Recommend idea", "rich_text"), ("Recommendation", "rich_text"), ("Khuyến nghị", "rich_text"), ("Khuyen nghi", "rich_text")],
    "tags": [("Tag", "multi_select"), ("Tags", "multi_select")],
    "relevance": [("Mức độ phù hợp", "select"), ("Muc do phu hop", "select"), ("Relevance", "select")],
    "project_fit": [("Mức độ phù hợp dự án", "select"), ("Muc do phu hop du an", "select"), ("Project fit", "select")],
    "selection_reason": [("Tiêu chí chọn tin", "rich_text"), ("Tieu chi chon tin", "rich_text")],
    "evaluation": [("Hệ đánh giá dự án", "rich_text"), ("He danh gia du an", "rich_text")],
    "analysis": [("Tổng hợp phân tích", "rich_text"), ("Tong hop phan tich", "rich_text"), ("Analysis", "rich_text")],
    "status": [("Status", "status")],
    "delivery_decision": [("Delivery decision", "select"), ("Telegram decision", "select"), ("Delivery", "select")],
    "delivery_score": [("Delivery score", "number"), ("Telegram score", "number")],
    "source": [("Source", "rich_text"), ("Nguồn", "rich_text"), ("Nguon", "rich_text")],
    "source_domain": [("Domain", "rich_text"), ("Source domain", "rich_text"), ("Nguồn domain", "rich_text")],
    "published_at_date": [("Published at", "date"), ("Published", "date"), ("Ngày đăng", "date"), ("Ngay dang", "date")],
    "published_at_text": [("Published at", "rich_text"), ("Published", "rich_text"), ("Ngày đăng", "rich_text"), ("Ngay dang", "rich_text")],
    "freshness": [("Freshness", "select"), ("Độ mới", "select"), ("Do moi", "select")],
    "confidence": [("Confidence", "select"), ("Độ chắc", "select"), ("Do chac", "select")],
    "source_tier": [("Source tier", "select"), ("Tier", "select")],
}

REQUIRED_NOTION_FIELDS = (
    "url",
    "score",
    "summary",
    "recommend",
    "type",
    "relevance",
    "project_fit",
    "tags",
)

OPTIONAL_NOTION_FIELDS = (
    "source",
    "source_domain",
    "published_at_date",
    "published_at_text",
    "freshness",
    "confidence",
    "source_tier",
    "selection_reason",
    "evaluation",
    "analysis",
    "delivery_decision",
    "delivery_score",
)


def _truncate(text: str, max_len: int = 2000) -> str:
    """Cắt text cho Notion API (limit 2000 chars/block)."""
    if not text:
        return ""
    return text[:max_len]


def _normalize_property_name(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode("ascii")
    return "".join(ch for ch in ascii_name.lower() if ch.isalnum())


def _resolve_property_name(
    database_properties: dict[str, Any],
    aliases: list[tuple[str, str]],
) -> str | None:
    normalized_map = {
        _normalize_property_name(name): (name, prop)
        for name, prop in database_properties.items()
        if isinstance(prop, dict)
    }

    for alias, expected_type in aliases:
        match = normalized_map.get(_normalize_property_name(alias))
        if match and match[1].get("type") == expected_type:
            return match[0]

    for alias, expected_type in aliases:
        alias_norm = _normalize_property_name(alias)
        for normalized_name, (actual_name, prop) in normalized_map.items():
            if prop.get("type") == expected_type and (alias_norm in normalized_name or normalized_name in alias_norm):
                return actual_name

    return None


def _resolve_property_map(database_properties: dict[str, Any]) -> dict[str, str]:
    resolved: dict[str, str] = {}
    for semantic_key, aliases in PROPERTY_ALIASES.items():
        actual_name = _resolve_property_name(database_properties, aliases)
        if actual_name:
            resolved[semantic_key] = actual_name
    return resolved


def _project_fit_level(c3_score: int) -> str:
    if c3_score >= 24:
        return "High"
    if c3_score >= 12:
        return "Medium"
    return "Low"


def _storage_primary_type(article: dict[str, Any], confidence_label: str = "") -> str:
    current_type = str(article.get("primary_type", "Practical") or "Practical").strip() or "Practical"
    title = str(article.get("title", "") or "").lower()
    total_score = int(article.get("total_score", 0) or 0)
    content_available = bool(article.get("content_available", False))
    source_tier = str(article.get("source_tier", "unknown") or "unknown").lower()
    ai_relevant = article.get("is_ai_relevant", True) is not False
    confidence = str(confidence_label or article.get("confidence_label", "") or "").strip().lower()

    if not ai_relevant:
        return "Practical"

    weak_thin_article = (
        not content_available
        and source_tier in {"c", "unknown"}
        and total_score < 40
        and confidence in {"", "low", "medium"}
    )
    if not weak_thin_article:
        return current_type

    if any(keyword in title for keyword in STORAGE_POLICY_KEYWORDS):
        return "Policy"
    if any(keyword in title for keyword in STORAGE_BUSINESS_KEYWORDS):
        return "Business"
    if any(keyword in title for keyword in STORAGE_PRODUCT_KEYWORDS):
        return "Product"
    return "Practical"


def _storage_tags(article: dict[str, Any], confidence_label: str = "") -> list[str]:
    raw_tags = article.get("tags", [])
    if not isinstance(raw_tags, list):
        return []

    tags = [str(tag or "").strip() for tag in raw_tags if str(tag or "").strip()]
    if not tags:
        return []

    total_score = int(article.get("total_score", 0) or 0)
    content_available = bool(article.get("content_available", False))
    source_tier = str(article.get("source_tier", "unknown") or "unknown").lower()
    ai_relevant = article.get("is_ai_relevant", True) is not False
    is_old_news = bool(article.get("is_old_news", False) or article.get("is_stale_candidate", False))
    confidence = str(confidence_label or article.get("confidence_label", "") or "").strip().lower()

    if not ai_relevant or is_old_news or total_score < 40:
        return []
    if source_tier in {"c", "unknown"} and (not content_available or confidence == "low" or total_score < 55):
        return []

    return tags[:5]


def _notion_date_start(value: str) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except Exception:
        return None


def _append_text_blocks(children: list[dict[str, Any]], heading: str, items: list[str]) -> None:
    """Append a simple heading + paragraph blocks for short evidence lists."""
    if not items:
        return

    children.append({
        "object": "block",
        "type": "heading_2",
        "heading_2": {
            "rich_text": [{"type": "text", "text": {"content": heading}}]
        },
    })

    for item in items[:5]:
        children.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{"type": "text", "text": {"content": _truncate(f"- {item}")}}]
            },
        })


def _text_annotations(*, bold: bool = False, italic: bool = False) -> dict[str, bool | str]:
    return {
        "bold": bold,
        "italic": italic,
        "strikethrough": False,
        "underline": False,
        "code": False,
        "color": "default",
    }


def _split_markdown_sections(text: str) -> list[tuple[str, list[str]]]:
    sections: list[tuple[str, list[str]]] = []
    heading = ""
    lines: list[str] = []

    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line:
            if lines:
                lines.append("")
            continue

        match = re.match(r"^#{1,6}\s+(.*)$", line)
        if match:
            if heading or lines:
                sections.append((heading, lines))
            heading = match.group(1).strip()
            lines = []
            continue

        lines.append(line)

    if heading or lines:
        sections.append((heading, lines))

    return sections


def _build_formatted_rich_text(text: str) -> list[dict[str, Any]]:
    """
    Chuyển markdown-ish text thành Notion rich_text sạch hơn:
    - bỏ ### thừa
    - heading thành bold
    - giữ line break để property nhìn dễ đọc hơn
    """
    sections = _split_markdown_sections(text)
    if not sections:
        clean = _truncate(str(text or ""))
        return [{"type": "text", "text": {"content": clean}}] if clean else []

    rich_text: list[dict[str, Any]] = []
    remaining = 1900

    def append_segment(content: str, *, bold: bool = False, italic: bool = False) -> None:
        nonlocal remaining
        if not content or remaining <= 0:
            return
        chunk = content[:remaining]
        rich_text.append({
            "type": "text",
            "text": {"content": chunk},
            "annotations": _text_annotations(bold=bold, italic=italic),
        })
        remaining -= len(chunk)

    for index, (heading, lines) in enumerate(sections):
        if heading:
            append_segment(f"{heading}\n", bold=True)

        content_lines = [line for line in lines if line]
        if content_lines:
            append_segment("\n".join(content_lines))

        if index < len(sections) - 1:
            append_segment("\n\n")

    return rich_text


def _append_formatted_markdown_blocks(
    children: list[dict[str, Any]],
    heading: str,
    text: str,
) -> None:
    if not text:
        return

    children.append({
        "object": "block",
        "type": "heading_2",
        "heading_2": {
            "rich_text": [{"type": "text", "text": {"content": heading}}]
        },
    })

    sections = _split_markdown_sections(text)
    if not sections:
        sections = [("", [line.strip() for line in str(text).splitlines() if line.strip()])]

    for section_heading, lines in sections:
        if section_heading:
            children.append({
                "object": "block",
                "type": "heading_3",
                "heading_3": {
                    "rich_text": [{"type": "text", "text": {"content": _truncate(section_heading, 200)}}]
                },
            })

        paragraph_lines: list[str] = []

        def flush_paragraph() -> None:
            if not paragraph_lines:
                return
            children.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{
                        "type": "text",
                        "text": {"content": _truncate(" ".join(paragraph_lines), 1800)},
                    }]
                },
            })
            paragraph_lines.clear()

        for line in lines:
            stripped = line.strip()
            if not stripped:
                flush_paragraph()
                continue

            item_match = re.match(r"^\d+\.\s+(.*)$", stripped)
            if item_match:
                flush_paragraph()
                children.append({
                    "object": "block",
                    "type": "numbered_list_item",
                    "numbered_list_item": {
                        "rich_text": [{
                            "type": "text",
                            "text": {"content": _truncate(item_match.group(1), 1800)},
                        }]
                    },
                })
                continue

            paragraph_lines.append(stripped)

        flush_paragraph()


def _get_notion_parent_and_properties(
    notion,
    database_id: str,
) -> tuple[dict[str, str], dict[str, Any]]:
    """
    Đọc schema Notion theo cả 2 kiểu:
      - API cũ: database trả thẳng `properties`
      - API mới: database trả `data_sources`, schema nằm ở data source

    Returns:
      parent payload cho pages.create và property schema tương ứng
    """
    try:
        database = notion.databases.retrieve(database_id=database_id)
    except Exception as e:
        logger.warning("⚠️ Không đọc được schema database Notion: %s", e)
        return {"database_id": database_id}, {}

    if isinstance(database, dict):
        database_properties = database.get("properties", {})
        if isinstance(database_properties, dict) and database_properties:
            return {"database_id": database_id}, database_properties

        data_sources = database.get("data_sources", [])
        if isinstance(data_sources, list) and data_sources:
            data_source_id = str(data_sources[0].get("id", "") or "").strip()
            if data_source_id:
                try:
                    data_source = notion.data_sources.retrieve(data_source_id=data_source_id)
                    data_source_properties = data_source.get("properties", {})
                    if isinstance(data_source_properties, dict) and data_source_properties:
                        return {"data_source_id": data_source_id, "database_id": database_id}, data_source_properties
                    logger.warning(
                        "⚠️ Data source '%s' đọc được nhưng không có properties.",
                        data_source_id,
                    )
                except Exception as e:
                    logger.warning("⚠️ Không đọc được schema data source Notion: %s", e)
                return {"data_source_id": data_source_id, "database_id": database_id}, {}

    return {"database_id": database_id}, {}


def _create_notion_page(
    notion,
    parent: dict[str, str],
    article: dict[str, Any],
    database_properties: dict[str, Any],
) -> str | None:
    """
    Tạo 1 page Notion cho 1 bài viết.
    Trả về URL page nếu thành công, None nếu lỗi.
    """
    emoji = article.get("primary_emoji", "📄")
    title = article.get("title", "Untitled")
    ptype = article.get("primary_type", "Practical")
    url = article.get("url", "")
    score = article.get("total_score", 0)
    summary_vi = article.get("summary_vi", "")
    note_summary_vi = article.get("note_summary_vi", "")
    deep_analysis = article.get("content_page_md") or article.get("deep_analysis", "")
    recommend = article.get("recommend_idea", "")
    relevance = article.get("relevance_level", "Low")
    tags = article.get("tags", [])
    c1_reason = article.get("c1_reason", "")
    c2_reason = article.get("c2_reason", "")
    c3_reason = article.get("c3_reason", "")
    c3_score = int(article.get("c3_score", 0) or 0)
    source = article.get("source", "")
    source_domain = article.get("source_domain", "")
    published_at = article.get("published_at", article.get("published", ""))
    source_verified = article.get("source_verified", False)
    source_tier = article.get("source_tier", "unknown")
    age_hours = article.get("age_hours", None)
    freshness_bucket = str(article.get("freshness_bucket", "unknown") or "unknown")
    grounding = article if article.get("grounding_note") else build_article_grounding(article)
    confidence_label = grounding.get("confidence_label", "low")
    grounding_note = grounding.get("grounding_note", "")
    fact_anchors = grounding.get("fact_anchors", [])
    reasonable_inferences = grounding.get("reasonable_inferences", [])
    unknowns = grounding.get("unknowns", [])
    ptype = _storage_primary_type(article, confidence_label)
    tags = _storage_tags(article, confidence_label)
    project_fit_level = _project_fit_level(c3_score)
    property_map = _resolve_property_map(database_properties)
    delivery_decision = str(article.get("delivery_decision", "") or "")
    delivery_score = int(article.get("delivery_score", 0) or 0)

    # Hệ đánh giá: gộp C2 + C3
    he_danh_gia = ""
    if c2_reason:
        he_danh_gia += f"Startup VN: {c2_reason}\n"
    if c3_reason:
        he_danh_gia += f"Dự án hiện tại: {c3_reason}"

    # Properties cho Notion
    properties = {
        property_map.get("title", "Name"): {
            "title": [{"text": {"content": f"{emoji} {title}"[:100]}}]
        },
    }

    # Type (Select) — chỉ thêm nếu Notion DB đã có property này
    type_prop = property_map.get("type")
    if ptype and type_prop:
        properties[type_prop] = {"select": {"name": ptype}}

    # Link gốc (URL)
    url_prop = property_map.get("url")
    if url and url_prop:
        properties[url_prop] = {"url": url}

    # Score (Number)
    score_prop = property_map.get("score")
    if score_prop:
        properties[score_prop] = {"number": score}

    relevance_prop = property_map.get("relevance")
    if relevance and relevance_prop:
        properties[relevance_prop] = {"select": {"name": relevance}}

    project_fit_prop = property_map.get("project_fit")
    if project_fit_prop:
        properties[project_fit_prop] = {"select": {"name": project_fit_level}}

    delivery_decision_prop = property_map.get("delivery_decision")
    if delivery_decision and delivery_decision_prop:
        properties[delivery_decision_prop] = {"select": {"name": delivery_decision}}

    delivery_score_prop = property_map.get("delivery_score")
    if delivery_score_prop:
        properties[delivery_score_prop] = {"number": delivery_score}

    source_prop = property_map.get("source")
    if source and source_prop:
        properties[source_prop] = {
            "rich_text": [{"type": "text", "text": {"content": _truncate(source)}}]
        }

    source_domain_prop = property_map.get("source_domain")
    if source_domain and source_domain_prop:
        properties[source_domain_prop] = {
            "rich_text": [{"type": "text", "text": {"content": _truncate(source_domain)}}]
        }

    freshness_prop = property_map.get("freshness")
    if freshness_bucket and freshness_prop:
        properties[freshness_prop] = {"select": {"name": freshness_bucket}}

    confidence_prop = property_map.get("confidence")
    if confidence_label and confidence_prop:
        properties[confidence_prop] = {"select": {"name": confidence_label}}

    source_tier_prop = property_map.get("source_tier")
    if source_tier and source_tier_prop:
        properties[source_tier_prop] = {"select": {"name": str(source_tier).upper()}}

    published_at_date_prop = property_map.get("published_at_date")
    published_at_start = _notion_date_start(str(published_at or ""))
    if published_at_start and published_at_date_prop:
        properties[published_at_date_prop] = {"date": {"start": published_at_start}}

    published_at_text_prop = property_map.get("published_at_text")
    if published_at and published_at_text_prop:
        properties[published_at_text_prop] = {
            "rich_text": [{"type": "text", "text": {"content": _truncate(str(published_at))}}]
        }

    # Tag (Multi-select)
    tags_prop = property_map.get("tags")
    if tags and tags_prop:
        properties[tags_prop] = {
            "multi_select": [{"name": t[:50]} for t in tags[:5]]
        }

    # Rich text properties — chỉ set nếu DB thực sự có schema tương ứng
    summary_prop = property_map.get("summary")
    if note_summary_vi and summary_prop:
        properties[summary_prop] = {
            "rich_text": [{"type": "text", "text": {"content": _truncate(note_summary_vi)}}]
        }

    selection_reason_prop = property_map.get("selection_reason")
    if c1_reason and selection_reason_prop:
        properties[selection_reason_prop] = {
            "rich_text": [{"type": "text", "text": {"content": _truncate(c1_reason)}}]
        }

    evaluation_prop = property_map.get("evaluation")
    if he_danh_gia and evaluation_prop:
        properties[evaluation_prop] = {
            "rich_text": [{"type": "text", "text": {"content": _truncate(he_danh_gia)}}]
        }

    analysis_prop = property_map.get("analysis")
    if deep_analysis and analysis_prop:
        properties[analysis_prop] = {
            "rich_text": [{"type": "text", "text": {"content": _truncate(deep_analysis)}}]
        }

    recommend_prop = property_map.get("recommend")
    if recommend and recommend_prop:
        properties[recommend_prop] = {
            "rich_text": _build_formatted_rich_text(recommend)
        }

    # Children blocks (nội dung page)
    children = []

    source_snapshot = []
    if source:
        source_snapshot.append(f"Nguồn: {source}")
    if source_domain:
        source_snapshot.append(f"Domain: {source_domain}")
    if published_at:
        source_snapshot.append(f"Published: {published_at}")
    if age_hours is not None:
        source_snapshot.append(f"Age: {age_hours}h")
    source_snapshot.append(f"Verified heuristic: {'yes' if source_verified else 'no'}")
    source_snapshot.append(f"Tier: {source_tier.upper()}")

    children.append({
        "object": "block",
        "type": "callout",
        "callout": {
            "icon": {"type": "emoji", "emoji": "🧭"},
            "rich_text": [{
                "type": "text",
                "text": {"content": _truncate(" | ".join(source_snapshot), 1800)},
            }],
        },
    })

    children.append({
        "object": "block",
        "type": "callout",
        "callout": {
            "icon": {"type": "emoji", "emoji": "🔎"},
            "rich_text": [{
                "type": "text",
                "text": {
                    "content": _truncate(
                        f"Grounding: {grounding_note} | Confidence: {confidence_label.upper()}",
                        1800,
                    )
                },
            }],
        },
    })

    _append_text_blocks(children, "✅ Fact Anchors", fact_anchors)
    _append_text_blocks(children, "🧠 Reasonable Inferences", reasonable_inferences)
    _append_text_blocks(children, "❓ Unknown / Need Verification", unknowns)

    # Tóm tắt
    if note_summary_vi:
        children.append({
            "object": "block",
            "type": "heading_2",
            "heading_2": {
                "rich_text": [{"type": "text", "text": {"content": "📝 Note tóm tắt"}}]
            },
        })
        children.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{"type": "text", "text": {"content": _truncate(note_summary_vi)}}]
            },
        })

    if summary_vi:
        children.append({
            "object": "block",
            "type": "heading_2",
            "heading_2": {
                "rich_text": [{"type": "text", "text": {"content": "📌 Tóm tắt nguồn"}}]
            },
        })
        children.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{"type": "text", "text": {"content": _truncate(summary_vi)}}]
            },
        })

    # Tiêu chí chọn tin
    if c1_reason:
        children.append({
            "object": "block",
            "type": "heading_2",
            "heading_2": {
                "rich_text": [{"type": "text", "text": {"content": "📊 Tiêu chí chọn tin"}}]
            },
        })
        children.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{"type": "text", "text": {"content": _truncate(c1_reason)}}]
            },
        })

    # Hệ đánh giá dự án
    if he_danh_gia:
        children.append({
            "object": "block",
            "type": "heading_2",
            "heading_2": {
                "rich_text": [{"type": "text", "text": {"content": "🎯 Hệ đánh giá dự án"}}]
            },
        })
        children.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{"type": "text", "text": {"content": _truncate(he_danh_gia)}}]
            },
        })

    # Phân tích sâu
    if deep_analysis:
        children.append({
            "object": "block",
            "type": "heading_2",
            "heading_2": {
                "rich_text": [{"type": "text", "text": {"content": "🔬 Tổng hợp phân tích"}}]
            },
        })
        # Chia nhỏ nếu dài > 2000 chars
        for chunk_start in range(0, len(deep_analysis), 2000):
            chunk = deep_analysis[chunk_start:chunk_start + 2000]
            children.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text", "text": {"content": chunk}}]
                },
            })

    # Recommend idea
    if recommend:
        _append_formatted_markdown_blocks(children, "💡 Recommend Idea", recommend)

    # Score breakdown
    children.append({
        "object": "block",
        "type": "callout",
        "callout": {
            "icon": {"type": "emoji", "emoji": "📊"},
            "rich_text": [{
                "type": "text",
                "text": {
                    "content": (
                        f"Score: {score}/100 | C1: {article.get('c1_score', 0)} | C2: {article.get('c2_score', 0)} | "
                        f"C3: {article.get('c3_score', 0)} | Delivery: {delivery_score}/15 | "
                        f"Source kind: {article.get('source_kind', 'unknown')}"
                    )
                },
            }],
        },
    })

    surfaced_reason = article.get("why_surfaced") or article.get("prefilter_reasons", [])
    if surfaced_reason:
        children.append({
            "object": "block",
            "type": "callout",
            "callout": {
                "icon": {"type": "emoji", "emoji": "🧩"},
                "rich_text": [{
                    "type": "text",
                    "text": {"content": _truncate("Why surfaced: " + "; ".join(str(item) for item in surfaced_reason[:5]), 1800)},
                }],
            },
        })

    # Limit to 100 blocks
    children = children[:100]

    try:
        page = notion.pages.create(
            parent=parent,
            properties=properties,
            children=children,
        )
        return page.get("url", "")
    except Exception as e:
        logger.error("❌ Notion page failed for '%s': %s", title[:40], e)
        return None


def _create_notion_page_with_fallback(
    notion,
    parent: dict[str, str],
    database_id: str,
    article: dict[str, Any],
    database_properties: dict[str, Any],
) -> str | None:
    """
    Thử create theo parent tốt nhất trước, rồi fallback về database_id nếu
    client/API hiện tại không hỗ trợ multi data source đầy đủ.
    """
    url = _create_notion_page(notion, parent, article, database_properties)
    if url:
        return url

    if not parent.get("data_source_id") or not database_id:
        return None

    fallback_parent = {"database_id": database_id}
    logger.info(
        "↩️ Falling back to database_id parent for Notion create on '%s'.",
        str(article.get("title", "") or "")[:40],
    )
    return _create_notion_page(notion, fallback_parent, article, database_properties)


def _find_existing_notion_page_url(
    notion,
    parent: dict[str, str],
    property_map: dict[str, str],
    source_url: str,
) -> str | None:
    """
    Tìm page Notion đã tồn tại theo URL gốc để tránh tạo duplicate.

    Nếu query/filter không tương thích với schema/API hiện tại, fail mềm và tiếp tục tạo page mới.
    """
    url_prop = property_map.get("url")
    if not notion or not url_prop or not source_url:
        return None

    payload = {
        "filter": {
            "property": url_prop,
            "url": {"equals": source_url},
        },
        "page_size": 1,
    }

    try:
        if parent.get("data_source_id") and hasattr(notion, "data_sources"):
            response = notion.data_sources.query(data_source_id=parent["data_source_id"], **payload)
        elif parent.get("database_id"):
            response = notion.databases.query(database_id=parent["database_id"], **payload)
        else:
            return None
    except Exception as exc:
        logger.debug("Notion existing-page lookup skipped for %s: %s", source_url, exc)
        if parent.get("data_source_id") and parent.get("database_id"):
            try:
                response = notion.databases.query(database_id=parent["database_id"], **payload)
            except Exception as fallback_exc:
                logger.debug("Notion database fallback lookup skipped for %s: %s", source_url, fallback_exc)
                return None
        else:
            return None

    results = response.get("results", []) if isinstance(response, dict) else []
    if not results:
        return None

    first = results[0] if isinstance(results[0], dict) else {}
    return str(first.get("url", "") or "").strip() or None


def save_notion_node(state: dict[str, Any]) -> dict[str, Any]:
    """
    LangGraph node: lưu tất cả bài vào Notion + ChromaDB memory.

    Input:
        analyzed_articles: bài đã phân tích sâu (top)
        low_score_articles: bài score thấp (lưu cơ bản)
        scored_articles: fallback nếu không có analyzed

    Output:
        notion_pages: list {title, url} cho summarize_vn
    """
    notion_token = os.getenv("NOTION_TOKEN")
    database_id = os.getenv("NOTION_DATABASE_ID")
    publish_notion = bool(state.get("publish_notion", True))
    persist_local = bool(state.get("persist_local", True))

    # ── Thu thập tất cả bài cần lưu ────────────────────────────────
    all_articles = list(state.get("final_articles", []))

    if not all_articles:
        analyzed = state.get("analyzed_articles", [])
        low_score = state.get("low_score_articles", [])
        all_articles = analyzed + low_score

    # Fallback: nếu không có analyzed, lấy scored
    if not all_articles:
        all_articles = state.get("scored_articles", [])

    if not all_articles:
        logger.info("📭 Không có bài nào để lưu.")
        return {"notion_pages": []}

    # Không lưu các bài không đủ liên quan AI hoặc nhìn giống landing/search page.
    # Mục tiêu là tránh làm bẩn Notion/history rồi vài hôm sau archive lại nhắc nhầm.
    filtered_articles = [
        article for article in all_articles
        if article.get("is_ai_relevant", True) is not False
        and article.get("is_news_candidate", True) is not False
    ]
    skipped_count = len(all_articles) - len(filtered_articles)
    if skipped_count:
        logger.info("🧹 Bỏ qua %d bài off-topic / non-news trước khi lưu.", skipped_count)
    all_articles = filtered_articles

    if not all_articles:
        logger.info("📭 Không còn bài nào phù hợp để lưu sau khi lọc off-topic.")
        return {"notion_pages": []}

    notion_pages = []
    notion_available = bool(notion_token and database_id and publish_notion)
    database_properties: dict[str, Any] = {}
    notion_parent: dict[str, str] = {"database_id": database_id} if database_id else {}
    property_map: dict[str, str] = {}

    if notion_available:
        from notion_client import Client
        notion = Client(auth=notion_token)
        notion_parent, database_properties = _get_notion_parent_and_properties(notion, database_id)
        property_map = _resolve_property_map(database_properties)
        missing_required_fields = [field for field in REQUIRED_NOTION_FIELDS if field not in property_map]
        if missing_required_fields:
            logger.warning(
                "⚠️ Notion DB thiếu hoặc lệch tên/type cho các cột lõi: %s | available=%s",
                ", ".join(missing_required_fields),
                ", ".join(sorted(database_properties.keys())) or "(none)",
            )
        missing_optional_fields = [field for field in OPTIONAL_NOTION_FIELDS if field not in property_map]
        if missing_optional_fields:
            logger.info(
                "ℹ️ Notion DB chưa có một số cột phụ, sẽ bỏ qua nhẹ nhàng: %s",
                ", ".join(missing_optional_fields),
            )
    else:
        if publish_notion:
            logger.warning("⚠️ Notion credentials chưa cấu hình — chỉ lưu vào memory.")
        else:
            logger.info("🧪 Preview mode: bỏ qua publish Notion.")
        notion = None

    for article in all_articles:
        title = article.get("title", "N/A")
        url = article.get("url", "")
        url_hash = hashlib.sha256(url.encode()).hexdigest()[:16]

        # ── Lưu Notion (nếu có) ──────────────────────────────────
        notion_url = ""
        if notion and notion_available:
            notion_url = _find_existing_notion_page_url(
                notion,
                notion_parent,
                property_map,
                url,
            ) or ""
            if notion_url:
                logger.info("↩️ Notion reuse existing page for '%s' → %s", title[:40], notion_url)
            else:
                notion_url = _create_notion_page_with_fallback(
                    notion,
                    notion_parent,
                    database_id,
                    article,
                    database_properties,
                ) or ""
            if notion_url:
                logger.info("✅ Notion: '%s' → %s", title[:40], notion_url)

        # Preview không có Notion URL thật, nên không cần đẩy placeholder vào UI.
        if notion_available and notion_url:
            notion_pages.append({"title": title, "url": notion_url, "source_url": url})

        # ── Preview mode không ghi lịch sử để tránh pollute dữ liệu thật ──────
        if persist_local:
            save_article(
                url=url,
                title=title,
                source=article.get("source", ""),
                primary_type=article.get("primary_type", ""),
                summary=article.get("note_summary_vi", "") or article.get("summary_vi", ""),
                full_content=article.get("content_page_md", "") or article.get("deep_analysis", ""),
                relevance_score=article.get("total_score", 0),
            )

            store_article(
                article_id=url_hash,
                title=title,
                summary=article.get("note_summary_vi", "") or article.get("summary_vi", ""),
                primary_type=article.get("primary_type", ""),
                score=article.get("total_score", 0),
                url=url,
            )

    logger.info(
        "✅ Saved %d articles (Notion: %s, Memory/SQLite: %s)",
        len(all_articles),
        "✅" if notion_available else "⏭️",
        "✅" if persist_local else "⏭️",
    )

    if persist_local:
        try:
            record_source_history_run(state)
            logger.info("📚 Source history updated from current publish batch.")
        except Exception as exc:
            logger.warning("Source history update skipped: %s", exc)

    return {"notion_pages": notion_pages}
