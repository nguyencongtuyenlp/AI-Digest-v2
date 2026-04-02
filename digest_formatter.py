"""
digest_formatter.py — Shared helpers to build the daily digest layout.

The business-facing format is:
- 6 fixed topics
- Up to 3 articles per topic
- Each article shows title, summary, and source link
"""

from __future__ import annotations

from html import escape as html_escape
from typing import Any

TYPE_ORDER: list[tuple[str, str]] = [
    ("Research", "🔬"),
    ("Product", "🚀"),
    ("Business", "💼"),
    ("Policy & Ethics", "⚖️"),
    ("Society & Culture", "🌍"),
    ("Practical", "🛠️"),
]

TYPE_ALIASES: dict[str, str] = {
    "policy": "Policy & Ethics",
    "policy & ethics": "Policy & Ethics",
    "society": "Society & Culture",
    "society & culture": "Society & Culture",
    "practical": "Practical",
    "business": "Business",
    "product": "Product",
    "research": "Research",
}

TYPE_TO_EMOJI = {type_name: emoji for type_name, emoji in TYPE_ORDER}


def canonical_type_name(raw_type: Any) -> str:
    """Normalize historical and model-generated type names into the fixed digest set."""
    cleaned = str(raw_type or "").strip()
    if not cleaned:
        return "Practical"
    return TYPE_ALIASES.get(cleaned.lower(), cleaned)


def type_emoji(raw_type: Any) -> str:
    """Return the canonical emoji for a type name."""
    return TYPE_TO_EMOJI.get(canonical_type_name(raw_type), "📄")


def select_digest_articles(
    classified_articles: list[dict[str, Any]],
    per_type: int = 3,
) -> list[dict[str, Any]]:
    """Pick the top N articles for each primary type in a fixed order."""
    normalized_articles = []
    for article in classified_articles:
        normalized = dict(article)
        normalized["primary_type"] = canonical_type_name(article.get("primary_type"))
        normalized["primary_emoji"] = type_emoji(normalized["primary_type"])
        normalized_articles.append(normalized)

    selected: list[dict[str, Any]] = []
    for type_name, emoji in TYPE_ORDER:
        bucket = [
            article
            for article in normalized_articles
            if article.get("primary_type") == type_name
        ]
        bucket.sort(key=lambda article: article.get("relevance_score", 0), reverse=True)
        if bucket:
            selected.extend(bucket[:per_type])
            continue

        selected.append(
            {
                "primary_type": type_name,
                "primary_emoji": emoji,
                "title": "",
                "summary_vi": "",
                "url": "",
                "relevance_score": 0,
                "is_placeholder": True,
            }
        )
    return selected


def group_digest_articles(
    classified_articles: list[dict[str, Any]],
    per_type: int = 3,
) -> list[dict[str, Any]]:
    """Return digest sections with a stable type order."""
    selected = select_digest_articles(classified_articles, per_type=per_type)
    sections: list[dict[str, Any]] = []
    for type_name, emoji in TYPE_ORDER:
        items = [
            article
            for article in selected
            if article.get("primary_type") == type_name
        ]
        sections.append(
            {
                "type": type_name,
                "emoji": emoji,
                "articles": items,
            }
        )
    return sections


def build_digest_markdown(classified_articles: list[dict[str, Any]], per_type: int = 3) -> str:
    """Build a deterministic Markdown digest for Notion and text previews."""
    sections = group_digest_articles(classified_articles, per_type=per_type)
    lines = ["# AI Daily Digest", ""]

    for section in sections:
        lines.append(f"## {section['emoji']} {section['type']}")
        lines.append("")

        articles = section["articles"]
        if not articles or articles[0].get("is_placeholder"):
            lines.append("- Chưa có bài nổi bật cho chủ đề này hôm nay.")
            lines.append("")
            continue

        for index, article in enumerate(articles, 1):
            title = article.get("title", "Untitled")
            summary = article.get("summary_vi") or "Chưa có tóm tắt."
            url = article.get("url") or "Không có link gốc."
            lines.append(f"{index}. **{title}**")
            lines.append(summary)
            lines.append(url)
            lines.append("")

    return "\n".join(lines).strip()


def build_digest_html_sections(
    classified_articles: list[dict[str, Any]],
    per_type: int = 3,
) -> list[str]:
    """Build per-topic HTML sections suitable for Telegram chunking."""
    sections_html: list[str] = []
    for section in group_digest_articles(classified_articles, per_type=per_type):
        lines = [f"<b>{section['emoji']} {html_escape(section['type'])}</b>"]
        articles = section["articles"]

        if not articles or articles[0].get("is_placeholder"):
            lines.append("Chua co bai noi bat cho chu de nay hom nay.")
            sections_html.append("\n".join(lines))
            continue

        for index, article in enumerate(articles, 1):
            title = html_escape(article.get("title", "Untitled"))
            summary = html_escape(article.get("summary_vi") or "Chua co tom tat.")
            url = html_escape(article.get("url") or "")
            lines.append(f"{index}. <b>{title}</b>")
            lines.append(summary)
            if url:
                lines.append(f"<a href=\"{url}\">Doc bai goc</a>")
            lines.append("")

        sections_html.append("\n".join(lines).strip())

    return sections_html
