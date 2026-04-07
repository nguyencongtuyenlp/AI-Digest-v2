"""
executive_intelligence.py - Lớp intelligence nhẹ cho weekly memo, watchlist và topic pages.

Mục tiêu:
- tận dụng history + batch hiện tại để tạo insight cấp founder/operator
- không phụ thuộc cloud
- graceful fallback nếu thiếu Notion credentials hoặc watchlist chi tiết
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
import re
from typing import Any

from digest.editorial.digest_formatter import TYPE_ORDER, canonical_type_name
from digest.sources.source_runtime import load_watchlist_seeds

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REPORTS_DIR = PROJECT_ROOT / "reports"
TOPIC_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _clean_term(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _slugify(value: str) -> str:
    lowered = _clean_term(value).lower()
    lowered = TOPIC_SLUG_RE.sub("-", lowered).strip("-")
    return lowered or "untitled"


def _article_score(article: dict[str, Any]) -> int:
    return int(
        article.get("relevance_score", article.get("total_score", article.get("score", 0))) or 0
    )


def _article_recency(article: dict[str, Any]) -> str:
    return str(
        article.get("created_at", article.get("published_at", article.get("published", ""))) or ""
    ).strip()


def _sort_articles(articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        articles,
        key=lambda article: (_article_score(article), _article_recency(article)),
        reverse=True,
    )


def _article_text(article: dict[str, Any]) -> str:
    parts: list[str] = [
        str(article.get("title", "") or ""),
        str(article.get("summary", "") or ""),
        str(article.get("summary_vi", "") or ""),
        str(article.get("note_summary_vi", "") or ""),
        str(article.get("source", "") or ""),
        str(article.get("source_domain", "") or ""),
    ]
    tags = article.get("tags", [])
    if isinstance(tags, list):
        parts.extend(str(tag or "") for tag in tags)
    return " ".join(part for part in parts if part).lower()


def _match_topic(article: dict[str, Any], topic: str) -> bool:
    normalized = _clean_term(topic).lower()
    if not normalized:
        return False
    haystack = _article_text(article)
    if normalized in haystack:
        return True
    topic_tokens = [token for token in re.findall(r"[a-z0-9]+", normalized) if len(token) >= 3]
    if not topic_tokens:
        return False
    hits = sum(1 for token in topic_tokens if token in haystack)
    return hits >= max(1, min(2, len(topic_tokens)))


def _bucket_terms(raw: dict[str, list[str]]) -> dict[str, list[str]]:
    derived_topics = [
        query
        for query in raw.get("queries", [])
        if len(query.split()) <= 6
    ]
    buckets = {
        "companies": list(raw.get("company_watchlist", [])),
        "products": list(raw.get("product_watchlist", [])),
        "tools": list(raw.get("tool_watchlist", [])),
        "policy": list(raw.get("policy_watchlist", [])),
        "topics": list(raw.get("topic_watchlist", [])),
    }
    buckets["topics"].extend(derived_topics[:12])
    buckets["companies"].extend(
        org for org in raw.get("github_orgs", []) if org and len(org) <= 32
    )
    buckets["tools"].extend(
        repo.split("/", 1)[-1]
        for repo in raw.get("github_repos", [])
        if "/" in repo
    )
    return {key: list(dict.fromkeys(_clean_term(item) for item in values if _clean_term(item))) for key, values in buckets.items()}


def load_strategic_watchlist(project_root: Path | None = None) -> dict[str, list[str]]:
    seeds = load_watchlist_seeds(project_root or PROJECT_ROOT)
    buckets = _bucket_terms(seeds)
    return {
        **seeds,
        **buckets,
    }


def _match_bucket(
    history: list[dict[str, Any]],
    topics: list[str],
    *,
    limit_per_topic: int = 5,
) -> dict[str, list[dict[str, Any]]]:
    matched: dict[str, list[dict[str, Any]]] = {}
    for topic in topics:
        bucket = [article for article in history if _match_topic(article, topic)]
        if bucket:
            matched[topic] = _sort_articles(bucket)[:limit_per_topic]
    return matched


def _lane_breakdown(articles: list[dict[str, Any]]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for article in articles:
        counter[canonical_type_name(article.get("primary_type"))] += 1
    return dict(counter)


def _topic_summary(topic: str, articles: list[dict[str, Any]]) -> str:
    if not articles:
        return f"Chưa có tín hiệu đủ mạnh cho chủ đề {topic}."

    top = articles[0]
    lane_counts = _lane_breakdown(articles)
    lane_hint = ", ".join(f"{lane}={count}" for lane, count in lane_counts.items()) or "mixed"
    source_domains = sorted(
        {
            str(article.get("source_domain", "") or "").strip()
            for article in articles
            if str(article.get("source_domain", "") or "").strip()
        }
    )
    source_hint = ", ".join(source_domains[:3]) or "multi-source"
    return (
        f"{topic} đang có {len(articles)} tín hiệu gần đây, nghiêng về {lane_hint}. "
        f"Tín hiệu mạnh nhất hiện là '{str(top.get('title', '') or 'Untitled').strip()}' "
        f"và nguồn nổi bật gồm {source_hint}."
    )


def build_watchlist_intelligence(
    history: list[dict[str, Any]],
    *,
    watchlist: dict[str, list[str]] | None = None,
    days: int = 14,
    today: date | None = None,
) -> str:
    active_watchlist = watchlist or load_strategic_watchlist()
    sections = [
        ("Competitor Watch", _match_bucket(history, active_watchlist.get("companies", []))),
        ("Product / Model Watch", _match_bucket(history, active_watchlist.get("products", []))),
        ("Tool / Framework Watch", _match_bucket(history, active_watchlist.get("tools", []))),
        ("Policy / Risk Watch", _match_bucket(history, active_watchlist.get("policy", []))),
        ("Topic Watch", _match_bucket(history, active_watchlist.get("topics", []))),
    ]
    report_date = (today or datetime.now(timezone.utc).date()).strftime("%d/%m/%Y")
    lines = [
        f"# Watchlist Intelligence ({report_date})",
        "",
        f"- Lookback window: {days} days",
        f"- History rows scanned: {len(history)}",
        "",
    ]

    any_signal = False
    for title, matched in sections:
        lines.extend([f"## {title}", ""])
        if not matched:
            lines.append("- Chưa có tín hiệu nổi bật trong window này.")
            lines.append("")
            continue
        any_signal = True
        for topic, articles in matched.items():
            lines.append(f"- {topic}: {len(articles)} signals")
            lines.append(f"  {_topic_summary(topic, articles)}")
            for article in articles[:3]:
                lines.append(
                    "  "
                    f"- [{canonical_type_name(article.get('primary_type'))}] "
                    f"{str(article.get('title', '') or 'Untitled').strip()} "
                    f"({_article_score(article)}/100)"
                )
        lines.append("")

    lines.extend(["## Executive Takeaways", ""])
    if any_signal:
        lines.append("- Watchlist đang tạo được lớp theo dõi chiến lược, không chỉ gom headline rời rạc.")
        lines.append("- Chủ đề nào lặp lại qua nhiều nguồn nên được ưu tiên vào weekly memo hoặc review nội bộ.")
    else:
        lines.append("- Watchlist hiện chưa bắt được enough repeated signals; nên siết lại seed theo đối thủ/chủ đề cụ thể hơn.")
    lines.append("")
    return "\n".join(lines).strip() + "\n"


def write_watchlist_intelligence(markdown: str, *, today: date | None = None) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = (today or datetime.now(timezone.utc).date()).strftime("%Y-%m-%d")
    path = REPORTS_DIR / f"watchlist_intelligence_{stamp}.md"
    path.write_text(markdown, encoding="utf-8")
    return path


def build_topic_pages_payload(
    history: list[dict[str, Any]],
    *,
    watchlist: dict[str, list[str]] | None = None,
    limit_topics: int = 12,
    limit_articles: int = 5,
) -> list[dict[str, Any]]:
    active_watchlist = watchlist or load_strategic_watchlist()
    grouped_topics: list[tuple[str, str]] = []
    for bucket_name, topics in (
        ("Company", active_watchlist.get("companies", [])),
        ("Product", active_watchlist.get("products", [])),
        ("Tool", active_watchlist.get("tools", [])),
        ("Policy", active_watchlist.get("policy", [])),
        ("Topic", active_watchlist.get("topics", [])),
    ):
        grouped_topics.extend((bucket_name, topic) for topic in topics)

    pages: list[dict[str, Any]] = []
    for bucket_name, topic in grouped_topics[: max(limit_topics * 2, limit_topics)]:
        matches = _match_bucket(history, [topic], limit_per_topic=limit_articles).get(topic, [])
        if not matches:
            continue
        pages.append(
            {
                "topic": topic,
                "topic_group": bucket_name,
                "topic_slug": _slugify(topic),
                "signal_count": len(matches),
                "summary": _topic_summary(topic, matches),
                "lane_breakdown": _lane_breakdown(matches),
                "articles": matches[:limit_articles],
            }
        )

    pages.sort(
        key=lambda item: (
            int(item.get("signal_count", 0) or 0),
            max((_article_score(article) for article in item.get("articles", [])), default=0),
        ),
        reverse=True,
    )
    return pages[:limit_topics]


def write_topic_page_artifacts(
    topic_pages: list[dict[str, Any]],
    *,
    today: date | None = None,
) -> list[dict[str, Any]]:
    if not topic_pages:
        return []

    stamp = (today or datetime.now(timezone.utc).date()).strftime("%Y-%m-%d")
    output_dir = REPORTS_DIR / f"topics_{stamp}"
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[dict[str, Any]] = []

    for page in topic_pages:
        topic = str(page.get("topic", "") or "").strip() or "Untitled"
        path = output_dir / f"{_slugify(topic)}.md"
        lines = [
            f"# {topic}",
            "",
            f"- Group: {page.get('topic_group', 'Topic')}",
            f"- Signals: {page.get('signal_count', 0)}",
            f"- Summary: {page.get('summary', '')}",
            "",
            "## Signals",
            "",
        ]
        for article in page.get("articles", []):
            lines.append(
                "- "
                f"[{canonical_type_name(article.get('primary_type'))}] "
                f"{str(article.get('title', '') or 'Untitled').strip()} "
                f"({_article_score(article)}/100)"
            )
            url = str(article.get("url", "") or "").strip()
            if url:
                lines.append(f"  {url}")
        path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
        written.append(
            {
                "topic": topic,
                "topic_group": str(page.get("topic_group", "Topic") or "Topic"),
                "signal_count": int(page.get("signal_count", 0) or 0),
                "path": str(path),
            }
        )
    return written


def build_executive_intelligence_bundle(
    history: list[dict[str, Any]],
    *,
    days: int = 14,
    today: date | None = None,
) -> dict[str, Any]:
    watchlist = load_strategic_watchlist()
    watchlist_markdown = build_watchlist_intelligence(
        history,
        watchlist=watchlist,
        days=days,
        today=today,
    )
    watchlist_path = write_watchlist_intelligence(watchlist_markdown, today=today)
    topic_pages = build_topic_pages_payload(history, watchlist=watchlist)
    topic_page_artifacts = write_topic_page_artifacts(topic_pages, today=today)
    return {
        "watchlist": watchlist,
        "watchlist_markdown": watchlist_markdown,
        "watchlist_path": str(watchlist_path),
        "topic_pages": topic_pages,
        "topic_page_artifacts": topic_page_artifacts,
    }
