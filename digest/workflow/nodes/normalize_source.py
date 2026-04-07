"""
normalize_source.py — LangGraph node: Source verification + date normalization.

Mục tiêu:
- Chuẩn hóa thời gian xuất bản về `published_at` (ISO-8601, UTC) để scoring timeliness ổn định.
- Trích xuất domain để model + hệ thống hiểu độ uy tín nguồn.
- Gắn cờ `source_verified` và `source_tier` bằng heuristic (không phải fact-check tuyệt đối).
- Không dùng `fetched_at` để giả làm `published_at`, nhằm tránh kéo bài cũ thành bài mới.
"""

from __future__ import annotations

import html
import logging
import re
import sys
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


from digest.sources.source_catalog import classify_source_kind

logger = logging.getLogger(__name__)


# Domain tiers (heuristic) — ưu tiên các nguồn tin/tech uy tín.
TIER_A = {
    "reuters.com",
    "apnews.com",
    "bloomberg.com",
    "wsj.com",
    "ft.com",
    "economist.com",
    "nature.com",
    "science.org",
    "arxiv.org",
    "openai.com",
    "anthropic.com",
    "about.fb.com",
    "deepmind.google",
    "blog.google",
    "ai.googleblog.com",
    "aws.amazon.com",
    "blogs.microsoft.com",
    "nvidianews.nvidia.com",
    "github.com",
    "huggingface.co",
    "techcrunch.com",
    "theverge.com",
    "arstechnica.com",
    "cnbc.com",
}

TIER_B = {
    "vnexpress.net",
    "vietnamnet.vn",
    "vtv.vn",
    "genk.vn",
    "nhandan.vn",
    "fortune.com",
    "thenewstack.io",
}

NON_NEWS_DOMAINS = {
    "bing.com",
    "search.yahoo.com",
    "yahoo.com",
    "doubleclick.net",
    "googleadservices.com",
    "dichvucong.gov.vn",
    "stackoverflow.com",
    "support.google.com",
}

NON_NEWS_URL_PATTERNS = (
    "aclick",
    "/search?",
    "/search;",
    "/ads/",
    "/p/home/",
    "/home/dvc-",
)

ARTICLE_HINTS = ("/news/", "/blog/", "/posts/", "/article", "/press", "/stories/")

AI_SIGNAL_RE = re.compile(
    r"\b(ai|artificial intelligence|tri tue nhan tao|trí tuệ nhân tạo|llm|model|agent|agents|"
    r"openai|anthropic|claude|gpt|gemini|deepmind|hugging face|huggingface|xai|grok|"
    r"nvidia|inference|training|transcription|asr|benchmark|robotics|robot|chip|gpu)\b",
    re.IGNORECASE,
)

OFF_TOPIC_HARD_RE = re.compile(
    r"\b(oil|tankers|hormuz|iran|middle east|zelenskyy|ukraine aid|troops|stocks|wall street|"
    r"private credit|saudi|football|showbiz|weather|iphone case|camera roundup|celebrity|"
    r"ios|iphone|ipad|apple music|sleep tracking|playlist|playstation|ps5)\b",
    re.IGNORECASE,
)

AI_FRIENDLY_DOMAINS = {
    "openai.com",
    "anthropic.com",
    "about.fb.com",
    "deepmind.google",
    "ai.googleblog.com",
    "huggingface.co",
    "developer.nvidia.com",
    "research.google",
    "aws.amazon.com",
    "blogs.microsoft.com",
    "nvidianews.nvidia.com",
}

RELATIVE_TIME_HINT_RE = re.compile(
    r"^(?P<value>\d+)\s*(?P<unit>"
    r"phút|phut|minute|minutes|min|mins|"
    r"giờ|gio|hour|hours|hr|hrs|"
    r"ngày|ngay|day|days|"
    r"tuần|tuan|week|weeks|"
    r"tháng|thang|month|months|"
    r"năm|nam|year|years"
    r")\b",
    re.IGNORECASE,
)


def _domain_from_url(url: str) -> str:
    try:
        netloc = urlparse(url).netloc.lower()
    except Exception:
        return ""
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc


def _clean_signal_text(value: Any, limit: int = 1800) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _parse_datetime(value: str) -> datetime | None:
    """
    Parse các định dạng phổ biến vào datetime có timezone.
    Trả None nếu không parse được.
    """
    if not value:
        return None
    v = value.strip()

    # ISO-8601 (RSS node đã dùng)
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass

    # RFC 2822 (feedparser đôi khi dùng)
    try:
        dt = parsedate_to_datetime(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass

    # Một vài format hay gặp
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            dt = datetime.strptime(v, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue

    return None


def _parse_datetime_from_url(url: str) -> datetime | None:
    if not url:
        return None

    lowered = url.lower()

    match = re.search(r"/(20\d{2})/(0[1-9]|1[0-2])/(0[1-9]|[12]\d|3[01])(?:/|$)", lowered)
    if match:
        year, month, day = map(int, match.groups())
        try:
            return datetime(year, month, day, tzinfo=timezone.utc)
        except ValueError:
            return None

    for raw in re.findall(r"\d{6,}", lowered):
        for start in range(0, max(1, len(raw) - 5)):
            chunk6 = raw[start:start + 6]
            if len(chunk6) == 6:
                year = int(chunk6[:2])
                month = int(chunk6[2:4])
                day = int(chunk6[4:6])
                if 20 <= year <= 39:
                    try:
                        return datetime(2000 + year, month, day, tzinfo=timezone.utc)
                    except ValueError:
                        pass

            chunk8 = raw[start:start + 8]
            if len(chunk8) == 8 and chunk8.startswith("20"):
                try:
                    return datetime.strptime(chunk8, "%Y%m%d").replace(tzinfo=timezone.utc)
                except ValueError:
                    pass

    return None


def _parse_relative_time_hint(value: str, *, now: datetime) -> datetime | None:
    raw = str(value or "").strip().lower()
    if not raw:
        return None
    if raw in {"vừa xong", "just now"}:
        return now
    if raw in {"hôm qua", "hom qua", "yesterday"}:
        return now.replace(minute=0, second=0, microsecond=0) - timedelta(days=1)

    match = RELATIVE_TIME_HINT_RE.match(raw)
    if not match:
        return None

    amount = int(match.group("value"))
    unit = match.group("unit").lower()
    if unit in {"phút", "phut", "minute", "minutes", "min", "mins"}:
        delta = timedelta(minutes=amount)
    elif unit in {"giờ", "gio", "hour", "hours", "hr", "hrs"}:
        delta = timedelta(hours=amount)
    elif unit in {"ngày", "ngay", "day", "days"}:
        delta = timedelta(days=amount)
    elif unit in {"tuần", "tuan", "week", "weeks"}:
        delta = timedelta(weeks=amount)
    elif unit in {"tháng", "thang", "month", "months"}:
        delta = timedelta(days=30 * amount)
    elif unit in {"năm", "nam", "year", "years"}:
        delta = timedelta(days=365 * amount)
    else:
        return None
    return now - delta


def _freshness_bucket(age_hours: float | None) -> str:
    if age_hours is None:
        return "unknown"
    if age_hours <= 24:
        return "breaking"
    if age_hours <= 72:
        return "fresh"
    if age_hours <= 7 * 24:
        return "recent"
    if age_hours <= 30 * 24:
        return "aging"
    return "stale"


def _looks_like_news_candidate(domain: str, url: str, title: str) -> tuple[bool, list[str]]:
    lowered_url = str(url or "").lower()
    lowered_title = str(title or "").lower().strip()
    flags: list[str] = []

    if domain in NON_NEWS_DOMAINS:
        flags.append("blocked_domain")

    if any(pattern in lowered_url for pattern in NON_NEWS_URL_PATTERNS):
        flags.append("search_or_landing_url")

    if domain in {"aws.amazon.com", "cloud.google.com"} and not any(hint in lowered_url for hint in ARTICLE_HINTS):
        flags.append("vendor_landing_page")

    if lowered_title in {"xai", "cổng dịch vụ công quốc gia"}:
        flags.append("generic_or_offscope_title")

    if "trusted by" in lowered_title or "get started" in lowered_title:
        flags.append("marketing_title")

    if domain == "cnbc.com" and any(
        token in lowered_title
        for token in ("iran", "oil", "hormuz", "saudi", "wall street", "stocks", "ukraine", "troops")
    ):
        flags.append("offtopic_macro_story")

    return (not flags), flags


def _assess_ai_relevance(domain: str, source: str, title: str, content: str) -> tuple[bool, list[str]]:
    """
    Heuristic hẹp để chặn tin lạc đề trước khi lên brief.
    Không yêu cầu tuyệt đối phải có từ "AI" trong title nếu domain là nguồn AI rất rõ.
    """
    signals: list[str] = []
    title_text = _clean_signal_text(title, 300)
    content_text = _clean_signal_text(content, 1500)
    combined = f"{title_text} {content_text}".strip()

    ai_hits = len(AI_SIGNAL_RE.findall(combined))
    off_topic_hits = len(OFF_TOPIC_HARD_RE.findall(combined))

    if domain in AI_FRIENDLY_DOMAINS:
        signals.append("ai_domain")
    if ai_hits:
        signals.append(f"ai_hits:{ai_hits}")
    if off_topic_hits:
        signals.append(f"off_topic_hits:{off_topic_hits}")

    # Nếu domain AI rõ hoặc có nhiều AI signals, cho qua.
    if domain in AI_FRIENDLY_DOMAINS or ai_hits >= 2:
        # Trường hợp bài địa chính trị/tài chính chung chung nhưng chỉ nhắc AI lướt qua vẫn nên chặn.
        if off_topic_hits >= 2 and ai_hits < 4:
            signals.append("blocked:offtopic_dominant")
            return False, signals
        return True, signals

    # Có 1 tín hiệu AI thì chỉ cho qua khi không bị off-topic đè.
    if ai_hits == 1 and off_topic_hits == 0:
        signals.append("borderline_ai_signal")
        return True, signals

    signals.append("blocked:weak_ai_signal")
    return False, signals


def _tier_for(domain: str) -> str:
    if not domain:
        return "unknown"
    if domain in TIER_A:
        return "a"
    if domain in TIER_B:
        return "b"
    return "c"


def normalize_source_node(state: dict[str, Any]) -> dict[str, Any]:
    """
    Input: raw_articles
    Output: raw_articles (được enrich thêm metadata)
    """
    raw_articles = list(state.get("raw_articles", []))
    if not raw_articles:
        return {"raw_articles": []}

    now = datetime.now(timezone.utc)

    for a in raw_articles:
        url = a.get("url", "") or ""
        domain = a.get("source_domain") or _domain_from_url(url)
        a["source_domain"] = domain

        # published_at chỉ lấy từ metadata nguồn, KHÔNG fallback sang fetched_at
        published_raw = (
            a.get("published_at")
            or a.get("published")
            or a.get("date")
            or ""
        )
        dt = _parse_datetime(str(published_raw)) if published_raw else None
        if dt is None:
            dt = _parse_datetime_from_url(url)
        if dt is None:
            dt = _parse_relative_time_hint(str(a.get("published_hint", "") or ""), now=now)
        fetched_raw = a.get("fetched_at", "") or ""
        fetched_dt = _parse_datetime(str(fetched_raw)) if fetched_raw else None

        if dt:
            a["published_at"] = dt.astimezone(timezone.utc).isoformat()
            a["age_hours"] = round((now - dt).total_seconds() / 3600, 2)
            a["post_age_hours"] = a["age_hours"]
            if published_raw:
                a["published_at_source"] = "source_metadata"
            elif a.get("published_hint"):
                a["published_at_source"] = "published_hint"
            else:
                a["published_at_source"] = "url_pattern"
        else:
            a["published_at"] = ""
            a["age_hours"] = None
            a["post_age_hours"] = None
            a["published_at_source"] = "unknown"

        if fetched_dt:
            a["discovered_at"] = fetched_dt.astimezone(timezone.utc).isoformat()
        elif fetched_raw:
            a["discovered_at"] = str(fetched_raw)
        else:
            a["discovered_at"] = ""

        a["freshness_unknown"] = not bool(dt)
        a["freshness_bucket"] = _freshness_bucket(a["age_hours"])
        a["is_fresh"] = a["freshness_bucket"] in {"breaking", "fresh"}
        a["is_old_news"] = bool(a["age_hours"] is not None and a["age_hours"] > 7 * 24)
        a["is_stale_candidate"] = bool(dt and (now - dt).total_seconds() > 14 * 24 * 3600)

        # Source verification (heuristic): RSS và domain tier A/B thì coi là "verified"
        source = (a.get("source", "") or "").lower()
        tier = _tier_for(domain)
        a["source_tier"] = tier

        is_rss = source.startswith("rss:")
        verified = bool(is_rss or tier in {"a", "b"})
        a["source_verified"] = verified

        source_kind, source_priority = classify_source_kind(
            source=source,
            domain=domain,
            acquisition_quality=str(a.get("acquisition_quality", "") or ""),
            social_signal=bool(a.get("social_signal", False)),
            github_signal_type=str(a.get("github_signal_type", "") or ""),
        )
        if a.get("watchlist_hit") is None:
            a["watchlist_hit"] = bool(
                source.startswith("watchlist")
                or bool(a.get("watchlist_query"))
                or bool(a.get("query_context"))
            )
        if source_kind == "community":
            community_strength = 4 if a.get("community_reactions") or a.get("community_hint") else 3
        elif source_kind == "github":
            community_strength = 2 if a.get("github_signal_type") else 1
        elif source_kind == "watchlist":
            community_strength = 1
        else:
            community_strength = 0
        a["source_kind"] = source_kind
        a["source_priority"] = source_priority
        a["community_signal_strength"] = max(
            int(a.get("community_signal_strength", 0) or 0),
            community_strength,
        )

        # Flag nếu content rỗng (tín hiệu chất lượng thấp)
        content = (a.get("content", "") or "").strip()
        a["content_available"] = bool(content and len(content) >= 200)

        is_news_candidate, acquisition_flags = _looks_like_news_candidate(
            domain=domain,
            url=url,
            title=a.get("title", ""),
        )
        a["is_news_candidate"] = is_news_candidate
        a["acquisition_flags"] = acquisition_flags

        # Gắn thêm cờ "đủ liên quan AI hay không" để chặn các tin tài chính/chính trị chung.
        is_ai_relevant, ai_relevance_reasons = _assess_ai_relevance(
            domain=domain,
            source=source,
            title=a.get("title", ""),
            content=content or a.get("snippet", ""),
        )
        a["is_ai_relevant"] = is_ai_relevant
        a["ai_relevance_reasons"] = ai_relevance_reasons

    logger.info("✅ Normalized %d articles (date+domain+verification)", len(raw_articles))
    return {"raw_articles": raw_articles}
