"""
gather_news.py — LangGraph node: Thu thập tin tức AI/Tech.

Chiến lược mới của node này:
- RSS/official feeds là nguồn lõi để giảm phụ thuộc vào search.
- DDG chỉ còn là lớp bổ sung để mở rộng coverage.
- Có thêm Hacker News, Reddit, watchlist seed để tăng "hơi thở cộng đồng".
- Cố gắng né các lỗi runtime đã từng gặp: 403 scrape, DDG fail, URL không phải news.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from ddgs import DDGS
from trafilatura import extract, fetch_url

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from source_catalog import (
    BLOCKED_DOMAINS,
    CURATED_RSS_FEEDS,
    DEFAULT_HN_KEYWORDS,
    DEFAULT_GITHUB_ORGS,
    DEFAULT_GITHUB_REPOS,
    DEFAULT_GITHUB_SEARCH_QUERIES,
    DEFAULT_REDDIT_SUBREDDITS,
    DEFAULT_TELEGRAM_CHANNELS,
    EXTRACTION_BLOCKED_DOMAINS,
    OFFICIAL_SOURCE_DOMAINS,
    SEARCH_QUERIES_EN,
    STRONG_MEDIA_DOMAINS,
    SUPPLEMENTAL_BLOCKED_DOMAINS,
    SUPPLEMENTAL_LOW_QUALITY_DOMAINS,
    SUPPLEMENTAL_REVIEW_DOMAINS,
    SUPPLEMENTAL_TRUSTED_DOMAINS,
    build_search_queries_vn,
    classify_source_kind,
    load_watchlist_seeds,
    social_signal_inbox_path,
)
from xai_grok import (
    grok_scout_enabled,
    grok_scout_max_articles,
    grok_scout_max_queries,
    grok_x_scout_enabled,
    grok_x_scout_max_articles,
    grok_x_scout_max_queries,
    scout_x_posts,
    scout_web_search_articles,
)

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SAFE_DDGS_TEXT_BACKEND = os.getenv("DDGS_TEXT_BACKEND", "duckduckgo").strip().lower() or "duckduckgo"
REQUEST_HEADERS = {
    "User-Agent": "AvalookDigestBot/1.0 (+https://avalook.local)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

FOUNDER_GRADE_SIGNAL_KEYWORDS = (
    "ai",
    "artificial intelligence",
    "tri tue nhan tao",
    "trí tuệ nhân tạo",
    "llm",
    "model",
    "agent",
    "api",
    "openai",
    "anthropic",
    "claude",
    "gpt",
    "gemini",
    "deepmind",
    "xai",
    "grok",
    "huggingface",
    "hugging face",
    "nvidia",
    "research",
    "benchmark",
    "inference",
    "training",
    "robot",
    "robotics",
    "startup",
    "funding",
    "enterprise",
    "automation",
    "chip",
    "gpu",
    "regulation",
    "policy",
    "vietnam",
)

FOUNDER_GRADE_NOISE_KEYWORDS = (
    "task manager",
    "quan ly tac vu",
    "quản lý tác vụ",
    "任务管理器",
    "win10",
    "win11",
    "smartphone",
    "điện thoại",
    "dien thoai",
    "camera",
    "xăng dầu",
    "xang dau",
    "dầu mỏ",
    "dau mo",
    "mặt trăng",
    "mat trang",
    "sephora",
    "benefit",
    "geforce now",
    "skincare",
)

GITHUB_AGENT_SIGNAL_KEYWORDS = (
    "agent",
    "agents",
    "agentic",
    "claude code",
    "codex",
    "mcp",
    "model context protocol",
    "browser-use",
    "browser use",
    "plugin",
    "tool use",
    "workflow",
    "orchestration",
    "memory",
    "multi-agent",
    "multi agent",
    "server",
    "sdk",
)

SOCIAL_SIGNAL_ALLOWED_DOMAINS = (
    "facebook.com",
    "fb.com",
    "m.facebook.com",
    "www.facebook.com",
)

FACEBOOK_CHROME_EXECUTABLE_DEFAULT = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
FACEBOOK_AUTO_TARGETS_DEFAULT_FILE = "config/facebook_auto_targets.txt"
FACEBOOK_STORAGE_STATE_DEFAULT_FILE = "config/facebook_storage_state.json"
FACEBOOK_DISCOVERY_CACHE_DEFAULT_FILE = "config/facebook_discovered_sources.json"
FACEBOOK_ARTICLE_SKIP_RE = re.compile(
    r"\b(đã cập nhật ảnh đại diện|updated (his|her|their) profile picture|"
    r"đã thay đổi ảnh bìa|cover photo|avatar)\b",
    re.IGNORECASE,
)
FACEBOOK_ARTICLE_LOADING_RE = re.compile(r"\b(đang tải|loading)\b", re.IGNORECASE)
FACEBOOK_TIME_HINT_RE = re.compile(
    r"^\d+\s*(phút|giờ|ngày|tuần|tháng|năm)\b|"
    r"^(hôm qua|vừa xong|just now|yesterday)\b|"
    r"^\d{1,2}\s*tháng\s*\d{1,2},?\s*\d{4}\b",
    re.IGNORECASE,
)
FACEBOOK_METADATA_LINE_RE = re.compile(
    r"^(tác giả|uy tín|người kiểm duyệt|quản trị viên|admin|moderator|"
    r"người đóng góp nổi bật|top contributor|super contributor|see translation|xem bản dịch|"
    r"theo dõi|follow|like|comment|share|thích|trả lời|chia sẻ)$",
    re.IGNORECASE,
)
FACEBOOK_SEE_MORE_RE = re.compile(r"^(…\s*)?(xem thêm|see more)$", re.IGNORECASE)
FACEBOOK_NEWEST_OPTION_RE = re.compile(
    r"^(bài viết mới|mới nhất|newest posts|recent posts|most recent)$",
    re.IGNORECASE,
)
FACEBOOK_RECENT_ACTIVITY_OPTION_RE = re.compile(
    r"^(hoạt động mới đây|recent activity)$",
    re.IGNORECASE,
)
FACEBOOK_GROUP_SORT_BUTTON_RE = re.compile(
    r"(sắp xếp bảng feed nhóm theo|sort group feed by|sort feed by)",
    re.IGNORECASE,
)
FACEBOOK_DISCOVERY_KEYWORDS: dict[str, int] = {
    "ai": 28,
    "a.i": 28,
    "artificial intelligence": 24,
    "chatgpt": 24,
    "openai": 22,
    "claude": 20,
    "grok": 14,
    "gemini": 14,
    "openclaw": 20,
    "llm": 20,
    "mcp": 18,
    "agent": 16,
    "automation": 14,
    "data": 12,
    "benchmark": 16,
    "workflow": 16,
    "prompt": 10,
    "model": 14,
    "machine learning": 14,
    "nghiện ai": 24,
    "bình dân học ai": 22,
    "cộng đồng ai": 18,
}
FACEBOOK_SOURCE_BLOCKLIST_RE = re.compile(
    r"\b(?:marketplace|watch|gaming|video|messenger|reels|shop|dating|j2team|vật vờ|vat vo studio)\b",
    re.IGNORECASE,
)
FACEBOOK_GENERIC_LABEL_RE = re.compile(
    r"^(facebook|xem thêm|see more|home|bookmarks|groups|pages|notifications|menu|feed|messenger)$",
    re.IGNORECASE,
)
FACEBOOK_PROFILE_RESERVED_SEGMENTS = {
    "about",
    "ads",
    "bookmarks",
    "business",
    "events",
    "friends",
    "fundraisers",
    "gaming",
    "groups",
    "help",
    "marketplace",
    "messages",
    "notifications",
    "pages",
    "privacy",
    "reel",
    "reels",
    "saved",
    "search",
    "settings",
    "share",
    "stories",
    "story.php",
    "watch",
}
FACEBOOK_MODEL_COMPARE_RE = re.compile(
    r"\b(vs|benchmark|so sánh|compare|cost|chi phí|latency|quality|chất lượng|điểm|points?)\b",
    re.IGNORECASE,
)
FACEBOOK_CASE_STUDY_RE = re.compile(
    r"\b(case study|production|triển khai|workflow|playbook|quy trình|use case|thực chiến|mcp|claude code)\b",
    re.IGNORECASE,
)
FACEBOOK_PROMO_RE = re.compile(
    r"\b(webinar|event|sự kiện|đăng ký|register|course|khóa học|bán|sale|giảm giá|acc|tài khoản|tuyển dụng|hiring)\b",
    re.IGNORECASE,
)
FACEBOOK_SPECULATION_RE = re.compile(
    r"(\?{1,}|rumou?r|leak|đồn đoán|tin đồn|có vẻ|có thể|maybe|sắp ra mắt\??)",
    re.IGNORECASE,
)
FACEBOOK_NEWS_RECAP_RE = re.compile(
    r"\b(roundup|recap|tổng hợp|news recap|tin tháng|điểm tin)\b",
    re.IGNORECASE,
)

GROK_SCOUT_SEARCH_PLANS: tuple[dict[str, Any], ...] = (
    {
        "name": "official-vendors",
        "domains": ["openai.com", "anthropic.com", "blog.google", "deepmind.google", "huggingface.co"],
        "query": (
            "Most important new AI model, API, enterprise, agent, or release-note announcements "
            "from official vendor sources in the last 72 hours"
        ),
        "per_query_limit": 3,
    },
    {
        "name": "official-platforms",
        "domains": ["nvidianews.nvidia.com", "blogs.microsoft.com", "aws.amazon.com", "databricks.com", "cloudflare.com"],
        "query": (
            "Most important new AI infrastructure, enterprise platform, or deployment announcements "
            "from official sources in the last 72 hours"
        ),
        "per_query_limit": 3,
    },
    {
        "name": "strong-media-backstop",
        "domains": ["reuters.com", "techcrunch.com", "cnbc.com", "theverge.com", "arstechnica.com"],
        "query": (
            "Most decision-useful AI product, business, or policy stories in the last 72 hours "
            "for startup founders and operators"
        ),
        "per_query_limit": 2,
    },
)

GROK_X_SCOUT_DEFAULT_HANDLES: tuple[str, ...] = (
    "openai",
    "OpenAIDevs",
    "AnthropicAI",
    "GoogleDeepMind",
    "huggingface",
    "LangChainAI",
    "Replit",
    "cursor_ai",
)

GROK_X_SCOUT_PLANS: tuple[dict[str, Any], ...] = (
    {
        "name": "vendor-posts",
        "query": (
            "Find the most important new X posts about AI model launches, API updates, release notes, "
            "enterprise announcements, benchmarks, or open-source releases."
        ),
        "per_query_limit": 3,
    },
    {
        "name": "builder-posts",
        "query": (
            "Find new X posts about agent workflows, coding tools, GitHub repo launches, MCP ecosystem updates, "
            "or practical AI tooling that is genuinely useful to builders."
        ),
        "per_query_limit": 3,
    },
)

SUPPLEMENTAL_BLOCKED_PATH_RE = re.compile(
    r"^/$|"
    r"^/(tag|tags|topic|topics|category|categories|author|authors|search)(/|$)|"
    r"^/(vi_vi|en|vi)/[a-z]{1,4}-\d+/?$",
    re.IGNORECASE,
)

SUPPLEMENTAL_NOISE_TITLE_RE = re.compile(
    r"\b(là gì|la gi|từ đồng nghĩa|trái nghĩa|dictionary|từ điển|danh từ|nghĩa là gì)\b",
    re.IGNORECASE,
)


def _runtime_config(state: dict[str, Any]) -> dict[str, Any]:
    return dict(state.get("runtime_config", {}) or {})


def _cfg_int(state: dict[str, Any], key: str, env_key: str, default: int) -> int:
    config = _runtime_config(state)
    value = config.get(key)
    if value in (None, ""):
        value = os.getenv(env_key, str(default))
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _cfg_bool(state: dict[str, Any], key: str, env_key: str, default: bool) -> bool:
    config = _runtime_config(state)
    value = config.get(key)
    if value in (None, ""):
        raw = os.getenv(env_key, "1" if default else "0")
    else:
        raw = str(value)
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def _cfg_list(state: dict[str, Any], key: str, env_key: str, separator: str = ",") -> list[str]:
    config = _runtime_config(state)
    value = config.get(key)
    if value is None or value == "":
        raw = os.getenv(env_key, "")
        if not raw:
            return []
        if separator == "||":
            return [item.strip() for item in raw.split("||") if item.strip()]
        return [item.strip() for item in raw.split(separator) if item.strip()]

    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]

    text = str(value).strip()
    if not text:
        return []
    if separator == "||":
        return [item.strip() for item in text.split("||") if item.strip()]
    return [item.strip() for item in text.split(separator) if item.strip()]


def _maybe_limit(items: list[str], limit: int) -> list[str]:
    if limit <= 0:
        return items
    return items[:limit]


def _project_path_from_env(default_relative_path: str, env_key: str) -> Path:
    raw = os.getenv(env_key, "").strip()
    if raw:
        return Path(raw).expanduser()
    return PROJECT_ROOT / default_relative_path


def _domain_from_url(url: str) -> str:
    try:
        netloc = urlparse(url).netloc.lower()
    except Exception:
        return ""
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc


def _domain_matches(domain: str, candidates: list[str] | tuple[str, ...]) -> bool:
    return any(domain == item or domain.endswith(f".{item}") for item in candidates)


def _is_blocked_url(url: str) -> bool:
    lowered = url.lower()
    domain = _domain_from_url(url)

    if _domain_matches(domain, BLOCKED_DOMAINS):
        return True

    blocked_fragments = (
        "aclick",
        "/search?",
        "/search;",
        "/ads/",
        "googleads",
        "doubleclick",
        "utm_source=chatgpt.com",
    )
    if any(fragment in lowered for fragment in blocked_fragments):
        return True

    return False


def _is_social_signal_url(url: str) -> bool:
    domain = _domain_from_url(url)
    return _domain_matches(domain, SOCIAL_SIGNAL_ALLOWED_DOMAINS)


def _is_trusted_supplemental_domain(domain: str) -> bool:
    return _domain_matches(domain, SUPPLEMENTAL_TRUSTED_DOMAINS)


def _supplemental_domain_quality(domain: str) -> str:
    if _domain_matches(domain, SUPPLEMENTAL_LOW_QUALITY_DOMAINS):
        return "blocked"
    if _domain_matches(domain, SUPPLEMENTAL_TRUSTED_DOMAINS):
        return "high"
    if _domain_matches(domain, SUPPLEMENTAL_REVIEW_DOMAINS):
        return "review"
    return "unknown"


def _looks_like_article_url(url: str, *, trusted_domain: bool) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False

    path = (parsed.path or "").strip()
    if SUPPLEMENTAL_BLOCKED_PATH_RE.search(path or "/"):
        return False

    segments = [segment for segment in path.split("/") if segment]
    if not segments:
        return False

    leaf = segments[-1].lower()
    if leaf in {"home", "index", "news", "ai", "technology", "artificial-intelligence"}:
        return False

    if trusted_domain:
        return True

    if len(leaf) < 8 and "-" not in leaf and not any(char.isdigit() for char in leaf):
        return False

    return True


def _is_supplemental_noise_candidate(title: str, snippet: str, url: str) -> bool:
    domain = _domain_from_url(url)
    combined = " ".join(part for part in [title, snippet, url] if part).lower()
    if _domain_matches(domain, SUPPLEMENTAL_BLOCKED_DOMAINS):
        return True
    if SUPPLEMENTAL_NOISE_TITLE_RE.search(combined):
        return True
    return False


def _has_founder_grade_signal(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(keyword in lowered for keyword in FOUNDER_GRADE_SIGNAL_KEYWORDS)


def _has_founder_grade_noise(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(keyword in lowered for keyword in FOUNDER_GRADE_NOISE_KEYWORDS)


def _has_github_agent_signal(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(keyword in lowered for keyword in GITHUB_AGENT_SIGNAL_KEYWORDS)


def _build_search_article(
    *,
    url: str,
    title: str,
    snippet: str,
    source: str,
    published: str,
    query_context: str,
) -> dict[str, Any] | None:
    if not url or _is_blocked_url(url):
        return None
    surface_text = " ".join(part for part in [title, snippet, url] if part)
    if not _has_founder_grade_signal(surface_text):
        return None
    if _has_founder_grade_noise(surface_text) or _is_supplemental_noise_candidate(title, snippet, url):
        return None

    domain = _domain_from_url(url)
    domain_quality = _supplemental_domain_quality(domain)
    if domain_quality == "blocked":
        return None

    trusted_domain = domain_quality == "high"
    if not _looks_like_article_url(url, trusted_domain=trusted_domain):
        return None

    content = _extract_full_text(url)
    content_len = len((content or "").strip())
    if domain_quality == "review" and content_len < 320:
        return None
    if domain_quality == "unknown" and content_len < 220:
        return None

    if content and not _is_founder_grade_candidate(title, snippet, url, content):
        return None
    if domain_quality in {"review", "unknown"} and not _has_founder_grade_signal(
        " ".join(part for part in [title, snippet, content[:1200]] if part)
    ):
        return None

    return {
        "title": title,
        "url": url,
        "source": source,
        "snippet": snippet,
        "published": published,
        "content": content,
        "acquisition_quality": domain_quality,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source_kind": "search",
        "source_priority": 66 if domain_quality == "high" else 58 if domain_quality == "review" else 52,
        "community_signal_strength": 0,
        "watchlist_hit": "watchlist" in source.lower(),
    }


def _count_source_mix(articles: list[dict[str, Any]]) -> dict[str, int]:
    counts = {
        "official": 0,
        "strong_media": 0,
        "non_github": 0,
        "grok_scout": 0,
    }
    for article in articles:
        if not isinstance(article, dict):
            continue
        source = str(article.get("source", "") or "")
        domain = _domain_from_url(str(article.get("url", "") or "")) or str(article.get("source_domain", "") or "")
        source_kind, _ = classify_source_kind(
            source=source,
            domain=domain,
            acquisition_quality=str(article.get("acquisition_quality", "") or ""),
            social_signal=bool(article.get("social_signal", False)),
            github_signal_type=str(article.get("github_signal_type", "") or ""),
        )
        if source_kind == "official":
            counts["official"] += 1
        if source_kind == "strong_media":
            counts["strong_media"] += 1
        if source_kind != "github":
            counts["non_github"] += 1
        if str(article.get("grok_scout", False)).lower() in {"1", "true", "yes"} or source.startswith("Grok Scout"):
            counts["grok_scout"] += 1
    return counts


def _should_run_grok_scout(state: dict[str, Any], articles: list[dict[str, Any]]) -> bool:
    if not grok_scout_enabled(_runtime_config(state)):
        return False

    counts = _count_source_mix(articles)
    min_official = _cfg_int(state, "grok_scout_min_official_articles", "GROK_SCOUT_MIN_OFFICIAL_ARTICLES", 8)
    min_strong = _cfg_int(state, "grok_scout_min_official_plus_media", "GROK_SCOUT_MIN_OFFICIAL_PLUS_MEDIA", 10)
    min_non_github = _cfg_int(state, "grok_scout_min_non_github_articles", "GROK_SCOUT_MIN_NON_GITHUB_ARTICLES", 18)

    if counts["grok_scout"] > 0:
        return False
    if counts["official"] < min_official:
        return True
    if counts["official"] + counts["strong_media"] < min_strong:
        return True
    if counts["non_github"] < min_non_github:
        return True
    return False


def _build_grok_scout_article(item: dict[str, Any], *, plan_name: str) -> dict[str, Any] | None:
    url = str(item.get("url", "") or "").strip()
    title = str(item.get("title", "") or "").strip()
    snippet = " ".join(
        part for part in [
            str(item.get("summary", "") or "").strip(),
            str(item.get("why_it_matters", "") or "").strip(),
        ]
        if part
    )
    if not url or not title or _is_blocked_url(url):
        return None

    domain = _domain_from_url(url)
    if domain == "github.com" or _is_social_signal_url(url):
        return None
    if not _is_founder_grade_candidate(title, snippet, url, domain):
        return None

    content = _extract_full_text(url)
    if content and not _is_founder_grade_candidate(title, snippet, url, content[:1800]):
        return None

    source_kind, source_priority = classify_source_kind(
        source=f"Grok Scout: {plan_name}",
        domain=domain,
        acquisition_quality="review",
    )
    return {
        "title": title,
        "url": url,
        "source": f"Grok Scout: {plan_name}",
        "snippet": _truncate_text(snippet or title, 500),
        "content": _truncate_text(content or snippet, 4000),
        "published": str(item.get("published_at", "") or "").strip(),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "acquisition_quality": "review",
        "source_kind": source_kind,
        "source_priority": source_priority,
        "community_signal_strength": 0,
        "watchlist_hit": False,
        "grok_scout": True,
        "grok_scout_plan": plan_name,
    }


def _build_grok_x_scout_article(item: dict[str, Any], *, plan_name: str) -> dict[str, Any] | None:
    post_url = str(item.get("post_url", "") or "").strip()
    linked_url = str(item.get("linked_url", "") or "").strip()
    title = str(item.get("title", "") or "").strip()
    summary = str(item.get("summary", "") or "").strip()
    why_it_matters = str(item.get("why_it_matters", "") or "").strip()
    author_handle = str(item.get("author_handle", "") or "").strip().lstrip("@")
    target_url = linked_url or post_url
    if not target_url or not title or _is_blocked_url(target_url):
        return None

    if linked_url and _is_blocked_url(linked_url):
        linked_url = ""
        target_url = post_url

    domain = _domain_from_url(target_url)
    source_text = " ".join(part for part in [title, summary, why_it_matters, author_handle, linked_url, post_url] if part)
    if not _is_founder_grade_candidate(title, summary, target_url, source_text):
        return None

    content = "\n\n".join(
        part
        for part in [
            f"X post by @{author_handle}" if author_handle else "",
            f"Summary: {summary}" if summary else "",
            f"Why it matters: {why_it_matters}" if why_it_matters else "",
            f"Original X post: {post_url}" if post_url and post_url != target_url else "",
        ]
        if part
    )

    source_kind, source_priority = classify_source_kind(
        source=f"Grok X Scout: {plan_name}",
        domain=domain,
        acquisition_quality="review" if linked_url else "manual",
        social_signal=not bool(linked_url),
    )
    return {
        "title": title,
        "url": target_url,
        "source": f"Grok X Scout: {plan_name}{f' | @{author_handle}' if author_handle else ''}",
        "snippet": _truncate_text(" ".join(part for part in [summary, why_it_matters] if part), 500),
        "content": _truncate_text(content, 4000),
        "published": str(item.get("published_at", "") or "").strip(),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "acquisition_quality": "review" if linked_url else "manual",
        "source_kind": source_kind,
        "source_priority": source_priority,
        "community_signal_strength": 4,
        "watchlist_hit": False,
        "grok_x_scout": True,
        "grok_x_scout_plan": plan_name,
        "social_signal": not bool(linked_url),
        "social_platform": "x",
        "x_post_url": post_url,
        "x_author_handle": author_handle,
        "community_hint": post_url if post_url and post_url != target_url else "",
    }


def _run_grok_scout(state: dict[str, Any], raw_articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not _should_run_grok_scout(state, raw_articles):
        logger.info("⏭️ Skip Grok scout vì source mix hiện tại đã đủ mạnh.")
        return []

    max_queries = grok_scout_max_queries(_runtime_config(state))
    max_articles_total = grok_scout_max_articles(_runtime_config(state))
    existing_urls = [str(article.get("url", "") or "").strip() for article in raw_articles if str(article.get("url", "") or "").strip()]
    existing_titles = [str(article.get("title", "") or "").strip() for article in raw_articles if str(article.get("title", "") or "").strip()]

    logger.info("🧠 Grok scout: source mix yếu, sẽ web search thêm tối đa %d query.", max_queries)
    collected: list[dict[str, Any]] = []
    for plan in GROK_SCOUT_SEARCH_PLANS[:max_queries]:
        remaining = max_articles_total - len(collected)
        if remaining <= 0:
            break
        result = scout_web_search_articles(
            query=str(plan.get("query", "") or ""),
            allowed_domains=list(plan.get("domains", []) or []),
            existing_urls=existing_urls,
            existing_titles=existing_titles,
            max_articles=min(int(plan.get("per_query_limit", 2) or 2), remaining),
        )
        plan_name = str(plan.get("name", "search") or "search")
        for item in result.get("articles", []) or []:
            article = _build_grok_scout_article(item, plan_name=plan_name)
            if not article:
                continue
            existing_urls.append(str(article.get("url", "") or ""))
            existing_titles.append(str(article.get("title", "") or ""))
            collected.append(article)
        logger.info("   Grok scout[%s]: %d bài", plan_name, len(collected))

    return collected[:max_articles_total]


def _runtime_x_scout_handles(state: dict[str, Any]) -> list[str]:
    configured = _cfg_list(state, "grok_x_scout_allowed_handles", "GROK_X_SCOUT_ALLOWED_HANDLES")
    handles = configured or list(GROK_X_SCOUT_DEFAULT_HANDLES)
    cleaned = []
    seen: set[str] = set()
    for handle in handles:
        normalized = str(handle or "").strip().lstrip("@")
        if not normalized or normalized.lower() in seen:
            continue
        seen.add(normalized.lower())
        cleaned.append(normalized)
    return cleaned[:10]


def _run_grok_x_scout(state: dict[str, Any], raw_articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not grok_x_scout_enabled(_runtime_config(state)):
        return []

    max_queries = grok_x_scout_max_queries(_runtime_config(state))
    max_articles_total = grok_x_scout_max_articles(_runtime_config(state))
    allowed_handles = _runtime_x_scout_handles(state)
    excluded_handles = _cfg_list(state, "grok_x_scout_excluded_handles", "GROK_X_SCOUT_EXCLUDED_HANDLES")
    existing_urls = [str(article.get("url", "") or "").strip() for article in raw_articles if str(article.get("url", "") or "").strip()]
    existing_titles = [str(article.get("title", "") or "").strip() for article in raw_articles if str(article.get("title", "") or "").strip()]

    logger.info(
        "🧠 Grok X scout: searching X with up to %d query and %d handles.",
        max_queries,
        len(allowed_handles),
    )
    collected: list[dict[str, Any]] = []
    for plan in GROK_X_SCOUT_PLANS[:max_queries]:
        remaining = max_articles_total - len(collected)
        if remaining <= 0:
            break
        result = scout_x_posts(
            query=str(plan.get("query", "") or ""),
            allowed_x_handles=allowed_handles,
            excluded_x_handles=excluded_handles,
            existing_urls=existing_urls,
            existing_titles=existing_titles,
            max_posts=min(int(plan.get("per_query_limit", 2) or 2), remaining),
        )
        plan_name = str(plan.get("name", "x-search") or "x-search")
        for item in result.get("posts", []) or []:
            article = _build_grok_x_scout_article(item, plan_name=plan_name)
            if not article:
                continue
            existing_urls.append(str(article.get("url", "") or ""))
            if article.get("x_post_url"):
                existing_urls.append(str(article.get("x_post_url", "") or ""))
            existing_titles.append(str(article.get("title", "") or ""))
            collected.append(article)
        logger.info("   Grok X scout[%s]: %d bài", plan_name, len(collected))

    return collected[:max_articles_total]


def _http_get_text(url: str, timeout: int = 20) -> str:
    """Wrapper requests đơn giản để gom lỗi mạng/403 về một chỗ."""
    try:
        response = requests.get(url, headers=REQUEST_HEADERS, timeout=timeout)
        response.raise_for_status()
        response.encoding = response.encoding or "utf-8"
        return response.text
    except Exception as exc:
        logger.debug("HTTP fetch skipped for %s: %s", url[:80], exc)
        return ""


def _extract_title_from_html(html_text: str, fallback: str = "") -> str:
    if not html_text:
        return fallback
    try:
        soup = BeautifulSoup(html_text, "html.parser")
        title = soup.title.get_text(strip=True) if soup.title else ""
        return title or fallback
    except Exception:
        return fallback


def _parse_social_signal_blocks(text: str) -> list[dict[str, str]]:
    """
    Parse a simple key-value inbox file.

    Each entry is separated by a line with `---`.
    Required practical fields:
    - platform
    - title
    - url or content
    """
    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    current_key = ""

    for raw_line in str(text or "").splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()

        if not stripped:
            if current_key and current.get(current_key):
                current[current_key] = current[current_key].rstrip() + "\n"
            continue

        if stripped == "---":
            if current:
                entries.append({key: value.strip() for key, value in current.items() if str(value).strip()})
            current = {}
            current_key = ""
            continue

        if ":" in line and not line.startswith((" ", "\t")):
            key, value = line.split(":", 1)
            current_key = key.strip().lower()
            current[current_key] = value.strip()
            continue

        if current_key:
            current[current_key] = f"{current.get(current_key, '').rstrip()}\n{stripped}".strip()

    if current:
        entries.append({key: value.strip() for key, value in current.items() if str(value).strip()})

    return entries


def _build_social_signal_articles() -> list[dict[str, Any]]:
    """
    Ingest manual social signals that the team already saw and wants the
    system to process.
    """
    inbox_path = social_signal_inbox_path(PROJECT_ROOT)
    if not inbox_path.exists():
        return []

    try:
        raw_text = inbox_path.read_text(encoding="utf-8")
    except Exception as exc:
        logger.warning("Social signal inbox read failed: %s", exc)
        return []

    entries = _parse_social_signal_blocks(raw_text)
    articles: list[dict[str, Any]] = []
    for entry in entries:
        platform = str(entry.get("platform", "") or "").strip().lower()
        title = str(entry.get("title", "") or "").strip()
        url = str(entry.get("url", "") or "").strip()
        content = str(entry.get("content", "") or "").strip()
        note = str(entry.get("note", "") or "").strip()
        author = str(entry.get("author", "") or "").strip()
        group_name = str(entry.get("group", "") or "").strip()
        comments = str(entry.get("comments", "") or "").strip()
        posted_at = str(entry.get("posted_at", "") or "").strip()
        source_label = str(entry.get("source", "") or "").strip()

        if not platform or platform not in {"facebook", "social", "manual"}:
            continue
        if not title and not content:
            continue

        synthesized_title = title or _truncate_text(content, 120)
        combined = " ".join(part for part in [synthesized_title, note, content, comments, group_name] if part)
        if not _has_founder_grade_signal(combined):
            continue
        if _has_founder_grade_noise(combined):
            continue

        source = source_label or f"Social Signal: {platform.title()}"
        if group_name:
            source = f"{source} | {group_name}"
        if author:
            source = f"{source} | {author}"

        article_content = "\n\n".join(
            part
            for part in [
                f"Note: {note}" if note else "",
                f"Post content:\n{content}" if content else "",
                f"Comment signals:\n{comments}" if comments else "",
            ]
            if part
        )
        articles.append(
            {
                "title": synthesized_title,
                "url": url,
                "source": source,
                "snippet": _truncate_text(note or content, 500),
                "content": _truncate_text(article_content, 4000),
                "published": posted_at,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "acquisition_quality": "manual",
                "source_verified": False,
                "social_signal": True,
                "social_platform": platform,
                "social_author": author,
                "social_group": group_name,
                "delivery_lane_hint": "facebook_topic" if platform == "facebook" or (url and _is_social_signal_url(url)) else "",
                "community_reactions": comments,
                "source_kind": "community",
                "source_priority": 74,
                "community_signal_strength": 5,
                "watchlist_hit": False,
            }
        )

    return articles


def _github_headers() -> dict[str, str]:
    headers = dict(REQUEST_HEADERS)
    headers["Accept"] = "application/vnd.github+json"
    headers["X-GitHub-Api-Version"] = "2022-11-28"
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _github_get_json(path: str, *, params: dict[str, Any] | None = None) -> Any:
    try:
        response = requests.get(
            f"https://api.github.com{path}",
            headers=_github_headers(),
            params=params or None,
            timeout=20,
        )
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        logger.warning("GitHub API request failed for %s: %s", path, exc)
        return None


def _truncate_text(value: str, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _normalize_facebook_permalink(url: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    raw = raw.replace("m.facebook.com", "www.facebook.com").replace("mbasic.facebook.com", "www.facebook.com")
    if "/groups/" in raw and ("/posts/" in raw or "/videos/" in raw):
        raw = raw.split("?", 1)[0]
    raw = raw.split("?__cft__=", 1)[0]
    raw = raw.split("&__cft__=", 1)[0]
    raw = raw.split("&__tn__=", 1)[0]
    raw = raw.split("?__tn__=", 1)[0]
    return raw


def _extract_facebook_permalink(links: list[str]) -> str:
    preferred_patterns = (
        "/posts/",
        "/groups/",
        "/permalink.php",
        "/share/p/",
        "/videos/",
    )
    normalized_links = [_normalize_facebook_permalink(link) for link in links if str(link or "").strip()]
    for pattern in preferred_patterns:
        for link in normalized_links:
            if pattern == "/groups/" and "/posts/" not in link:
                continue
            if pattern in link:
                return link
    return normalized_links[0] if normalized_links else ""


def _looks_like_interaction_line(text: str) -> bool:
    lowered = str(text or "").strip().lower()
    return lowered in {
        "thích",
        "bình luận",
        "chia sẻ",
        "like",
        "comment",
        "share",
    } or lowered.startswith("tất cả cảm xúc")


def _is_loaded_facebook_payload(payload: dict[str, Any]) -> bool:
    text = str(payload.get("text", "") or "").strip()
    if not text:
        return False
    if FACEBOOK_ARTICLE_LOADING_RE.search(text):
        return False
    return True


def _clean_facebook_lines(lines: list[str]) -> list[str]:
    cleaned: list[str] = []
    for line in lines:
        value = str(line or "").strip()
        if not value:
            continue
        if FACEBOOK_METADATA_LINE_RE.fullmatch(value):
            continue
        if FACEBOOK_SEE_MORE_RE.fullmatch(value):
            continue
        cleaned.append(value)
    return cleaned


def _detect_facebook_content_style(title: str, content: str) -> str:
    combined = " ".join(part for part in [title, content] if part)
    lowered = combined.lower()
    if FACEBOOK_PROMO_RE.search(combined):
        return "promo"
    if FACEBOOK_SPECULATION_RE.search(combined):
        return "speculation"
    if FACEBOOK_MODEL_COMPARE_RE.search(combined):
        return "benchmark"
    if FACEBOOK_CASE_STUDY_RE.search(combined):
        if any(keyword in lowered for keyword in ("workflow", "playbook", "mcp", "claude code", "quy trình")):
            return "workflow"
        return "case_study"
    if FACEBOOK_NEWS_RECAP_RE.search(combined):
        return "news_recap"
    if any(keyword in lowered for keyword in ("mình nghĩ", "quan điểm", "ý kiến", "theo mình")):
        return "opinion"
    return "case_study" if len(content) >= 700 else "workflow" if "tool" in lowered else "news_recap"


def _score_facebook_boss_style(title: str, content: str, *, content_style: str) -> int:
    combined = " ".join(part for part in [title, content] if part)
    lowered = combined.lower()
    score = 30
    score += min(16, len(content) // 180)
    if any(char.isdigit() for char in combined):
        score += 8
    if " vs " in lowered or " so sánh " in lowered:
        score += 10
    if any(keyword in lowered for keyword in ("openai", "anthropic", "claude", "gpt", "gemini", "minimax", "grok")):
        score += 10
    if any(keyword in lowered for keyword in ("benchmark", "workflow", "case study", "thực chiến", "production", "chi phí")):
        score += 10
    if any(keyword in lowered for keyword in ("bài 1", "bài test", "test 1", "kết quả", "yêu cầu", "module", "codebase")):
        score += 8

    style_bonus = {
        "benchmark": 26,
        "case_study": 18,
        "workflow": 16,
        "news_recap": 8,
        "opinion": 2,
        "speculation": -18,
        "promo": -34,
    }
    score += style_bonus.get(content_style, 0)
    if len(content.strip()) < 180:
        score -= 18
    return max(0, min(100, score))


def _score_facebook_authority(
    *,
    target: dict[str, Any],
    author: str,
    source_type: str,
) -> int:
    ai_source_score = int(target.get("ai_source_score", 0) or 0)
    discovery_origin = str(target.get("discovery_origin", "") or "").strip().lower()
    base = {
        "group": 42,
        "page": 52,
        "profile": 58,
    }.get(source_type, 45)
    if discovery_origin == "manual":
        base += 14
    elif discovery_origin == "followed":
        base += 8
    elif discovery_origin == "joined":
        base += 5
    if "ai" in author.lower():
        base += 6
    return max(0, min(100, base + min(28, ai_source_score // 2)))


def _facebook_published_hint_rank(value: str) -> int:
    raw = str(value or "").strip().lower()
    if not raw:
        return 40
    if raw in {"vừa xong", "just now"}:
        return 100
    if raw in {"hôm qua", "hom qua", "yesterday"}:
        return 76
    match = re.match(
        r"^(?P<value>\d+)\s*(?P<unit>phút|phut|minute|minutes|min|mins|giờ|gio|hour|hours|hr|hrs|ngày|ngay|day|days|tuần|tuan|week|weeks|tháng|thang|month|months|năm|nam|year|years)\b",
        raw,
        re.IGNORECASE,
    )
    if not match:
        return 35
    amount = int(match.group("value"))
    unit = match.group("unit").lower()
    if unit in {"phút", "phut", "minute", "minutes", "min", "mins"}:
        return max(88, 99 - amount)
    if unit in {"giờ", "gio", "hour", "hours", "hr", "hrs"}:
        return max(70, 95 - amount)
    if unit in {"ngày", "ngay", "day", "days"}:
        return max(48, 72 - (amount * 4))
    if unit in {"tuần", "tuan", "week", "weeks"}:
        return max(10, 32 - (amount * 6))
    if unit in {"tháng", "thang", "month", "months"}:
        return max(0, 6 - amount)
    if unit in {"năm", "nam", "year", "years"}:
        return 0
    return 35


def _facebook_target_file() -> Path:
    return _project_path_from_env(FACEBOOK_AUTO_TARGETS_DEFAULT_FILE, "FACEBOOK_AUTO_TARGETS_FILE")


def _facebook_profile_dir() -> Path:
    return _project_path_from_env("config/facebook_chrome_profile", "FACEBOOK_CHROME_PROFILE_DIR")


def _facebook_storage_state_file() -> Path:
    return _project_path_from_env(FACEBOOK_STORAGE_STATE_DEFAULT_FILE, "FACEBOOK_STORAGE_STATE_FILE")


def _facebook_chrome_executable() -> str:
    return os.getenv("FACEBOOK_CHROME_EXECUTABLE", FACEBOOK_CHROME_EXECUTABLE_DEFAULT).strip() or FACEBOOK_CHROME_EXECUTABLE_DEFAULT


def _facebook_discovery_cache_file() -> Path:
    return _project_path_from_env(FACEBOOK_DISCOVERY_CACHE_DEFAULT_FILE, "FACEBOOK_DISCOVERY_CACHE_FILE")


def _facebook_discovery_enabled(state: dict[str, Any]) -> bool:
    return _cfg_bool(state, "enable_facebook_discovery", "FACEBOOK_DISCOVERY_ENABLED", False)


def _facebook_discovery_refresh_hours(state: dict[str, Any]) -> int:
    return _cfg_int(state, "facebook_discovery_refresh_hours", "FACEBOOK_DISCOVERY_REFRESH_HOURS", 24)


def _facebook_discovery_max_active_sources(state: dict[str, Any]) -> int:
    return _cfg_int(state, "facebook_discovery_max_active_sources", "FACEBOOK_DISCOVERY_MAX_ACTIVE_SOURCES", 12)


def _facebook_discovery_max_candidates_per_run(state: dict[str, Any]) -> int:
    return _cfg_int(
        state,
        "facebook_discovery_max_candidates_per_run",
        "FACEBOOK_DISCOVERY_MAX_CANDIDATES_PER_RUN",
        20,
    )


def _facebook_allow_profile_sources(state: dict[str, Any]) -> bool:
    return _cfg_bool(state, "facebook_allow_profile_sources", "FACEBOOK_ALLOW_PROFILE_SOURCES", True)


def _facebook_force_newest_first(state: dict[str, Any]) -> bool:
    return _cfg_bool(state, "facebook_force_newest_first", "FACEBOOK_FORCE_NEWEST_FIRST", True)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json_file(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_json_file(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _normalize_facebook_source_url(url: str) -> str:
    normalized = _normalize_facebook_permalink(url)
    if not normalized:
        return ""
    try:
        parsed = urlparse(normalized)
    except Exception:
        return normalized
    path = (parsed.path or "/").rstrip("/")
    segments = [segment for segment in path.split("/") if segment]
    if segments[:1] == ["groups"] and len(segments) >= 2:
        path = f"/groups/{segments[1]}"
    elif segments[:1] == ["profile.php"]:
        path = "/profile.php"
    elif segments:
        path = f"/{segments[0]}"
    else:
        path = "/"
    if path != "/":
        path = f"{path}/"
    if path == "/profile.php/" and parsed.query:
        return f"https://www.facebook.com/profile.php?{parsed.query}"
    return f"https://www.facebook.com{path}"


def _infer_facebook_source_type(url: str) -> str:
    normalized = _normalize_facebook_source_url(url)
    if not normalized:
        return "group"
    lowered = normalized.lower()
    if "/groups/" in lowered:
        return "group"
    if "profile.php" in lowered:
        return "profile"
    return "profile"


def _score_facebook_source(label: str, url: str, *, source_type: str, description: str = "") -> int:
    text = " ".join(part for part in [label, description, url] if part).lower()
    if not text:
        return 0
    score = 10
    for keyword, weight in FACEBOOK_DISCOVERY_KEYWORDS.items():
        if keyword in text:
            score += weight
    if source_type == "group":
        score += 6
    elif source_type == "page":
        score += 10
    elif source_type == "profile":
        score += 12
    if re.search(r"\bai\b", text):
        score += 18
    if "chat gpt" in text or "chatgpt" in text:
        score += 18
    if "openclaw" in text:
        score += 22
    if source_type == "group" and any(
        keyword in text for keyword in ("ai", "chatgpt", "chat gpt", "openclaw", "claude", "llm", "workflow", "automation")
    ):
        score += 12
    if "community" in text or "cộng đồng" in text:
        score += 6
    if FACEBOOK_SOURCE_BLOCKLIST_RE.search(text):
        score -= 35
    if not any(keyword in text for keyword in FACEBOOK_DISCOVERY_KEYWORDS):
        score = min(score, 45)
    return max(0, min(100, score))


def _facebook_source_status(*, ai_source_score: int, discovery_origin: str) -> str:
    if discovery_origin == "manual":
        return "auto_active"
    if ai_source_score >= 70:
        return "auto_active"
    if ai_source_score >= 50:
        return "candidate"
    return "ignored"


def _normalize_facebook_source_entry(entry: dict[str, Any]) -> dict[str, Any] | None:
    url = _normalize_facebook_source_url(str(entry.get("url", "") or ""))
    label = str(entry.get("label", "") or "").strip()
    if not url or not label or FACEBOOK_GENERIC_LABEL_RE.fullmatch(label):
        return None
    source_type = str(entry.get("source_type", "") or "").strip().lower() or _infer_facebook_source_type(url)
    discovery_origin = str(entry.get("discovery_origin", "") or "").strip().lower() or "manual"
    ai_source_score = int(entry.get("ai_source_score", 0) or _score_facebook_source(label, url, source_type=source_type))
    status = str(entry.get("status", "") or "").strip().lower() or _facebook_source_status(
        ai_source_score=ai_source_score,
        discovery_origin=discovery_origin,
    )
    return {
        "label": label,
        "url": url,
        "source_type": source_type,
        "discovery_origin": discovery_origin,
        "ai_source_score": max(0, min(100, ai_source_score)),
        "status": status,
        "last_seen_at": str(entry.get("last_seen_at", "") or _utc_now_iso()),
        "last_crawled_at": str(entry.get("last_crawled_at", "") or ""),
    }


def _load_facebook_auto_targets() -> list[dict[str, str]]:
    path = _facebook_target_file()
    if not path.exists():
        return []

    targets: list[dict[str, str]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception as exc:
        logger.warning("Facebook auto targets read failed: %s", exc)
        return []

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split("|")]
        label = parts[0] if len(parts) >= 2 else ""
        url = parts[1] if len(parts) >= 2 else parts[0]
        source_type = parts[2].strip().lower() if len(parts) >= 3 else _infer_facebook_source_type(url)
        if not url:
            continue
        entry = _normalize_facebook_source_entry(
            {
                "label": label or _domain_from_url(url) or "Facebook target",
                "url": url,
                "source_type": source_type,
                "discovery_origin": "manual",
                "ai_source_score": 100,
                "status": "auto_active",
            }
        )
        if entry:
            targets.append(entry)
    return targets


def _facebook_auto_enabled(state: dict[str, Any]) -> bool:
    return _cfg_bool(state, "enable_facebook_auto", "ENABLE_FACEBOOK_AUTO", False)


def _facebook_discovery_cache_is_fresh(path: Path, *, refresh_hours: int) -> bool:
    if refresh_hours <= 0 or not path.exists():
        return False
    if path.stat().st_size <= 4:
        return False
    modified_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return (datetime.now(timezone.utc) - modified_at).total_seconds() <= refresh_hours * 3600


def _load_facebook_discovery_cache() -> list[dict[str, Any]]:
    path = _facebook_discovery_cache_file()
    payload = _read_json_file(path)
    if isinstance(payload, dict):
        payload = payload.get("sources", [])
    if not isinstance(payload, list):
        return []
    items: list[dict[str, Any]] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        normalized = _normalize_facebook_source_entry(entry)
        if normalized:
            items.append(normalized)
    return items


def _save_facebook_discovery_cache(sources: list[dict[str, Any]]) -> None:
    path = _facebook_discovery_cache_file()
    _write_json_file(path, sources)


def _clean_facebook_discovery_label(value: str) -> str:
    label = " ".join(str(value or "").split()).strip(" |·")
    label = re.sub(r"\s*Lần hoạt động gần nhất:.*$", "", label, flags=re.IGNORECASE).strip(" |·")
    if not label or len(label) < 3 or len(label) > 140:
        return ""
    if FACEBOOK_GENERIC_LABEL_RE.fullmatch(label):
        return ""
    return label


def _extract_facebook_anchor_candidates(page: Any) -> list[dict[str, str]]:
    try:
        anchors = page.locator("a[href]").evaluate_all(
            """(nodes) => nodes.map((node) => ({
                href: node.href || "",
                text: (node.innerText || "").trim(),
                aria: (node.getAttribute("aria-label") || "").trim(),
                title: (node.getAttribute("title") || "").trim()
            }))"""
        )
    except Exception:
        return []
    cleaned: list[dict[str, str]] = []
    for item in anchors:
        if not isinstance(item, dict):
            continue
        href = str(item.get("href", "") or "").strip()
        if not href or "facebook.com" not in href.lower():
            continue
        cleaned.append(
            {
                "href": href,
                "label": _clean_facebook_discovery_label(
                    str(item.get("text", "") or "")
                    or str(item.get("aria", "") or "")
                    or str(item.get("title", "") or "")
                ),
            }
        )
    return cleaned


def _is_real_group_source_url(url: str) -> bool:
    lowered = str(url or "").lower()
    if "/groups/" not in lowered:
        return False
    path = urlparse(url).path.strip("/").split("/")
    if len(path) < 2:
        return False
    return path[1] not in {
        "feed",
        "discover",
        "notifications",
        "search",
        "suggested_groups",
        "you_should_join",
    }


def _is_profile_like_source_url(url: str) -> bool:
    normalized = _normalize_facebook_source_url(url)
    if not normalized:
        return False
    parsed = urlparse(normalized)
    if parsed.path == "/profile.php":
        return True
    segments = [segment for segment in parsed.path.strip("/").split("/") if segment]
    if len(segments) != 1:
        return False
    return segments[0].lower() not in FACEBOOK_PROFILE_RESERVED_SEGMENTS


def _merge_facebook_source_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for entry in entries:
        normalized = _normalize_facebook_source_entry(entry)
        if not normalized:
            continue
        key = str(normalized.get("url", "") or "")
        existing = merged.get(key)
        if not existing:
            merged[key] = normalized
            continue
        if int(normalized.get("ai_source_score", 0) or 0) > int(existing.get("ai_source_score", 0) or 0):
            existing.update(normalized)
        else:
            existing["last_seen_at"] = normalized.get("last_seen_at", existing.get("last_seen_at", ""))
    return sorted(
        merged.values(),
        key=lambda item: (
            str(item.get("status", "") or "") != "auto_active",
            -(int(item.get("ai_source_score", 0) or 0)),
            str(item.get("label", "") or "").lower(),
        ),
    )


def _discover_group_sources(page: Any) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for url in ("https://www.facebook.com/groups/feed/", "https://www.facebook.com/groups/"):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2500)
        except Exception:
            continue
        for item in _extract_facebook_anchor_candidates(page):
            href = _normalize_facebook_source_url(item.get("href", ""))
            if not href or not _is_real_group_source_url(href):
                continue
            label = item.get("label", "")
            score = _score_facebook_source(label, href, source_type="group")
            entries.append(
                {
                    "label": label,
                    "url": href,
                    "source_type": "group",
                    "discovery_origin": "joined",
                    "ai_source_score": score,
                    "status": _facebook_source_status(ai_source_score=score, discovery_origin="joined"),
                    "last_seen_at": _utc_now_iso(),
                    "last_crawled_at": "",
                }
            )
    return _merge_facebook_source_entries(entries)


def _discover_followed_page_sources(page: Any) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    try:
        page.goto("https://www.facebook.com/pages/?category=liked&ref=bookmarks", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2500)
    except Exception:
        return []

    for item in _extract_facebook_anchor_candidates(page):
        href = _normalize_facebook_source_url(item.get("href", ""))
        label = item.get("label", "")
        if not href or not label or "/groups/" in href.lower():
            continue
        if not _is_profile_like_source_url(href):
            continue
        score = _score_facebook_source(label, href, source_type="page")
        entries.append(
            {
                "label": label,
                "url": href,
                "source_type": "page",
                "discovery_origin": "followed",
                "ai_source_score": score,
                "status": _facebook_source_status(ai_source_score=score, discovery_origin="followed"),
                "last_seen_at": _utc_now_iso(),
                "last_crawled_at": "",
            }
        )
    return _merge_facebook_source_entries(entries)


def _discover_followed_profile_sources(page: Any) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for url in ("https://www.facebook.com/following/", "https://www.facebook.com/bookmarks/"):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2500)
        except Exception:
            continue
        for item in _extract_facebook_anchor_candidates(page):
            href = _normalize_facebook_source_url(item.get("href", ""))
            label = item.get("label", "")
            if not href or not label or not _is_profile_like_source_url(href):
                continue
            score = _score_facebook_source(label, href, source_type="profile")
            if score < 50:
                continue
            entries.append(
                {
                    "label": label,
                    "url": href,
                    "source_type": "profile",
                    "discovery_origin": "followed",
                    "ai_source_score": score,
                    "status": _facebook_source_status(ai_source_score=score, discovery_origin="followed"),
                    "last_seen_at": _utc_now_iso(),
                    "last_crawled_at": "",
                }
            )
    return _merge_facebook_source_entries(entries)


def _refresh_facebook_discovery_cache(*, headless: bool, allow_profiles: bool) -> list[dict[str, Any]]:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        logger.warning("Playwright unavailable for Facebook discovery: %s", exc)
        return []

    chrome_executable = _facebook_chrome_executable()
    storage_state_file = _facebook_storage_state_file()
    if not storage_state_file.exists():
        logger.warning("Facebook discovery skipped because storage state is missing: %s", storage_state_file)
        return []

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=chrome_executable,
                headless=headless,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = browser.new_context(
                viewport={"width": 1280, "height": 2200},
                storage_state=str(storage_state_file),
            )
            page = context.new_page()
            try:
                discovered = []
                discovered.extend(_discover_group_sources(page))
                discovered.extend(_discover_followed_page_sources(page))
                if allow_profiles:
                    discovered.extend(_discover_followed_profile_sources(page))
                return _merge_facebook_source_entries(discovered)
            finally:
                with suppress(Exception):
                    context.close()
                with suppress(Exception):
                    browser.close()
    except Exception as exc:
        logger.warning("Facebook discovery failed: %s", exc)
        return []


def _resolve_facebook_source_registry(state: dict[str, Any], *, headless: bool) -> dict[str, list[dict[str, Any]]]:
    manual_sources = _load_facebook_auto_targets()
    discovered_sources: list[dict[str, Any]] = []
    auto_active_sources: list[dict[str, Any]] = list(manual_sources)
    candidate_sources: list[dict[str, Any]] = []

    if _facebook_discovery_enabled(state):
        cache_file = _facebook_discovery_cache_file()
        refresh_hours = _facebook_discovery_refresh_hours(state)
        allow_profiles = _facebook_allow_profile_sources(state)
        if _facebook_discovery_cache_is_fresh(cache_file, refresh_hours=refresh_hours):
            discovered_sources = _load_facebook_discovery_cache()
        else:
            discovered_sources = _refresh_facebook_discovery_cache(headless=headless, allow_profiles=allow_profiles)
            if discovered_sources:
                _save_facebook_discovery_cache(discovered_sources)
        discovered_sources = _merge_facebook_source_entries(discovered_sources)
        candidate_sources = [
            source for source in discovered_sources
            if str(source.get("status", "") or "").lower() == "candidate"
        ][: _facebook_discovery_max_candidates_per_run(state)]
        discovered_auto_active = [
            source for source in discovered_sources
            if str(source.get("status", "") or "").lower() == "auto_active"
        ]
        remaining_slots = max(0, _facebook_discovery_max_active_sources(state) - len(manual_sources))
        seen_urls = {str(item.get("url", "") or "") for item in manual_sources}
        for source in discovered_auto_active:
            url = str(source.get("url", "") or "")
            if not url or url in seen_urls:
                continue
            auto_active_sources.append(source)
            seen_urls.add(url)
            if remaining_slots and len(auto_active_sources) >= len(manual_sources) + remaining_slots:
                break

    return {
        "manual_sources": manual_sources,
        "discovered_sources": discovered_sources,
        "auto_active_sources": auto_active_sources,
        "candidate_sources": candidate_sources,
    }


def _try_set_facebook_newest_first(page: Any) -> str:
    sort_mode = "default_fallback"
    try:
        button = page.get_by_role("button", name=FACEBOOK_GROUP_SORT_BUTTON_RE)
        if button.count() <= 0:
            return sort_mode
        button.first.click(timeout=4000)
        page.wait_for_timeout(800)
        for pattern in (FACEBOOK_NEWEST_OPTION_RE, FACEBOOK_RECENT_ACTIVITY_OPTION_RE):
            option = page.get_by_text(pattern)
            if option.count() <= 0:
                continue
            option.first.click(timeout=4000)
            page.wait_for_timeout(1800)
            if pattern is FACEBOOK_NEWEST_OPTION_RE:
                return "newest"
            sort_mode = "recent_activity"
            break
    except Exception:
        return "default_fallback"
    return sort_mode


def _scrape_facebook_target_payloads(
    *,
    target: dict[str, str],
    posts_per_target: int,
    headless: bool,
    force_newest_first: bool = True,
) -> list[dict[str, Any]]:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        logger.warning("Playwright unavailable for Facebook auto adapter: %s", exc)
        return []

    chrome_executable = _facebook_chrome_executable()
    storage_state_file = _facebook_storage_state_file()

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=chrome_executable,
                headless=headless,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context_kwargs: dict[str, Any] = {
                "viewport": {"width": 1280, "height": 2200},
            }
            if storage_state_file.exists():
                context_kwargs["storage_state"] = str(storage_state_file)
            context = browser.new_context(**context_kwargs)
            page = context.new_page()
            payloads: list[dict[str, Any]] = []
            try:
                page.goto(str(target.get("url", "") or ""), wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(3500)
                if "/login" in page.url or page.locator('input[name="email"]').count() > 0:
                    logger.warning("Facebook auto target requires login session: %s", target.get("url", ""))
                    return []
                sort_mode = "default_fallback"
                if force_newest_first:
                    sort_mode = _try_set_facebook_newest_first(page)

                payloads = []
                desired_payloads = max(2, posts_per_target * 2)
                for _ in range(8):
                    raw_payloads = page.locator('div[role="article"]').evaluate_all(
                        """(nodes) => nodes.map((node) => {
                            const text = (node.innerText || "").trim();
                            const links = Array.from(node.querySelectorAll('a[href]'))
                              .map((a) => a.href)
                              .filter(Boolean);
                            return { text, links };
                        })"""
                    )
                    payloads = [
                        {
                            **payload,
                            "facebook_sort_mode": sort_mode,
                        }
                        for payload in raw_payloads
                        if isinstance(payload, dict) and _is_loaded_facebook_payload(payload)
                    ]
                    if len(payloads) >= desired_payloads:
                        break
                    page.mouse.wheel(0, 2200)
                    page.wait_for_timeout(1200)
            except PlaywrightTimeoutError:
                logger.warning("Facebook auto target timed out: %s", target.get("url", ""))
                return []
            finally:
                with suppress(Exception):
                    context.close()
                with suppress(Exception):
                    browser.close()
    except Exception as exc:
        logger.warning("Facebook auto adapter failed for %s: %s", target.get("url", ""), exc)
        return []

    return [payload for payload in payloads if isinstance(payload, dict)][: max(1, posts_per_target * 3)]


def _build_facebook_auto_article(
    payload: dict[str, Any],
    *,
    target: dict[str, str],
) -> dict[str, Any] | None:
    text = str(payload.get("text", "") or "").strip()
    links = [str(item).strip() for item in payload.get("links", []) if str(item).strip()]
    if not text or len(text) < 60:
        return None
    if FACEBOOK_ARTICLE_SKIP_RE.search(text):
        return None

    lines = _clean_facebook_lines([line.strip() for line in text.splitlines() if line.strip()])
    if not lines:
        return None

    author = lines[0]
    published_label = ""
    content_lines: list[str] = []
    for line in lines[1:]:
        if _looks_like_interaction_line(line):
            break
        if FACEBOOK_TIME_HINT_RE.search(line):
            if not published_label:
                published_label = line
            continue
        if FACEBOOK_METADATA_LINE_RE.fullmatch(line):
            continue
        if FACEBOOK_SEE_MORE_RE.fullmatch(line):
            continue
        if re.fullmatch(r"\d+", line):
            continue
        content_lines.append(line)
    content_text = "\n".join(content_lines).strip()
    if not content_text or len(content_text) < 80:
        return None
    if not _is_founder_grade_candidate(content_text[:180], content_text[:600], " ".join(links), target.get("label", "")):
        return None

    permalink = _extract_facebook_permalink(links)
    if not permalink:
        return None

    title = _truncate_text(content_lines[0] if content_lines else content_text, 160)
    snippet = _truncate_text(content_text.replace("\n", " "), 500)
    content = _truncate_text(content_text, 4000)
    source_type = str(target.get("source_type", "") or _infer_facebook_source_type(str(target.get("url", "") or ""))).strip().lower() or "group"
    discovery_origin = str(target.get("discovery_origin", "") or "manual").strip().lower()
    content_style = _detect_facebook_content_style(title, content)
    boss_style_score = _score_facebook_boss_style(title, content, content_style=content_style)
    authority_score = _score_facebook_authority(target=target, author=author, source_type=source_type)
    source = f"Facebook Auto | {target.get('label', 'Facebook target')}"
    if author and author.lower() != str(target.get("label", "") or "").strip().lower():
        source = f"{source} | {author}"

    return {
        "title": title,
        "url": permalink,
        "source": source,
        "snippet": snippet,
        "content": content,
        "published": "",
        "published_hint": published_label,
        "published_hint_raw": published_label,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "acquisition_quality": "auto_browser",
        "source_verified": False,
        "social_signal": True,
        "social_platform": "facebook",
        "social_author": author,
        "social_group": str(target.get("label", "") or "").strip(),
        "delivery_lane_hint": "facebook_topic",
        "community_reactions": "",
        "source_kind": "community",
        "source_priority": 76,
        "community_signal_strength": 3,
        "watchlist_hit": False,
        "facebook_auto": True,
        "facebook_source_type": source_type,
        "facebook_discovery_origin": discovery_origin,
        "facebook_sort_mode": str(payload.get("facebook_sort_mode", "") or "default_fallback"),
        "facebook_content_style": content_style,
        "facebook_boss_style_score": boss_style_score,
        "facebook_authority_score": authority_score,
        "facebook_ai_source_score": int(target.get("ai_source_score", 0) or 0),
        "post_age_hours": None,
    }


def _build_facebook_auto_articles(
    state: dict[str, Any],
    *,
    targets: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if not _facebook_auto_enabled(state):
        return []

    targets = list(targets or _load_facebook_auto_targets())
    if not targets:
        logger.info("⏭️ Facebook auto bật nhưng chưa có target nào.")
        return []

    posts_per_target = _cfg_int(state, "facebook_auto_posts_per_target", "FACEBOOK_AUTO_POSTS_PER_TARGET", 2)
    headless = _cfg_bool(state, "facebook_auto_headless", "FACEBOOK_AUTO_HEADLESS", True)
    max_targets = _cfg_int(state, "facebook_auto_max_targets", "FACEBOOK_AUTO_MAX_TARGETS", 4)
    force_newest_first = _facebook_force_newest_first(state)

    articles: list[dict[str, Any]] = []
    for target in targets[:max_targets]:
        payloads = _scrape_facebook_target_payloads(
            target=target,
            posts_per_target=posts_per_target,
            headless=headless,
            force_newest_first=force_newest_first,
        )
        target_articles: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        for payload in payloads:
            article = _build_facebook_auto_article(payload, target=target)
            if not article:
                continue
            url = str(article.get("url", "") or "")
            if url in seen_urls:
                continue
            seen_urls.add(url)
            target_articles.append(article)
        target_articles = sorted(
            target_articles,
            key=lambda article: (
                _facebook_published_hint_rank(
                    str(article.get("published_hint_raw", article.get("published_hint", "")) or "")
                ),
                int(article.get("facebook_boss_style_score", 0) or 0),
                len(str(article.get("content", "") or "")),
            ),
            reverse=True,
        )[:posts_per_target]
        logger.info("   Facebook auto [%s]: %d bài", target.get("label", ""), len(target_articles))
        target["last_crawled_at"] = _utc_now_iso()
        articles.extend(target_articles)
    return articles


def _valid_github_repo_full_name(value: str) -> bool:
    parts = [segment.strip() for segment in str(value or "").split("/") if segment.strip()]
    return len(parts) == 2


def _build_github_repo_article(repo: dict[str, Any], *, source: str, query_context: str = "") -> dict[str, Any] | None:
    full_name = str(repo.get("full_name", "") or "").strip()
    html_url = str(repo.get("html_url", "") or "").strip()
    description = str(repo.get("description", "") or "").strip()
    topics = repo.get("topics", []) or []
    language = str(repo.get("language", "") or "").strip()
    owner = str((repo.get("owner") or {}).get("login", "") or "").strip()
    stars = int(repo.get("stargazers_count", 0) or 0)
    forks = int(repo.get("forks_count", 0) or 0)
    updated_at = str(repo.get("pushed_at") or repo.get("updated_at") or "")

    surface_text = " ".join(
        part for part in [full_name, description, " ".join(str(topic) for topic in topics), language, query_context] if part
    )
    if not _is_founder_grade_candidate(full_name, description, html_url, surface_text):
        return None
    if source in {"GitHub API Search"} or source.startswith("GitHub API Org:"):
        if not _has_github_agent_signal(surface_text):
            return None

    summary_bits = [
        f"GitHub repo: {full_name}" if full_name else "",
        f"owner={owner}" if owner else "",
        f"language={language}" if language else "",
        f"stars={stars}",
        f"forks={forks}",
        f"topics={', '.join(str(topic) for topic in topics[:8])}" if topics else "",
        f"watchlist_query={query_context}" if query_context else "",
    ]
    content = " | ".join(bit for bit in summary_bits if bit)
    if description:
        content = f"{content}\n\n{description}" if content else description

    return {
        "title": full_name or html_url,
        "url": html_url,
        "source": source,
        "snippet": _truncate_text(description or content, 500),
        "content": _truncate_text(content, 4000),
        "published": updated_at,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "acquisition_quality": "high",
        "github_signal_type": "repository",
        "github_full_name": full_name,
        "github_owner": owner,
        "github_stars": stars,
        "source_kind": "github",
        "source_priority": 90,
        "community_signal_strength": 2,
    }


def _build_github_release_article(
    release: dict[str, Any],
    *,
    repo_full_name: str,
    source: str,
) -> dict[str, Any] | None:
    html_url = str(release.get("html_url", "") or "").strip()
    tag_name = str(release.get("tag_name", "") or "").strip()
    name = str(release.get("name", "") or "").strip()
    body = str(release.get("body", "") or "").strip()
    published = str(release.get("published_at") or release.get("created_at") or "")
    title = name or tag_name or f"{repo_full_name} release"
    combined = " ".join(part for part in [repo_full_name, title, body] if part)
    if not _is_founder_grade_candidate(title, body[:800], html_url, combined):
        return None

    content = " | ".join(
        bit
        for bit in [
            f"GitHub release: {repo_full_name}",
            f"tag={tag_name}" if tag_name else "",
            f"title={title}" if title else "",
        ]
        if bit
    )
    if body:
        content = f"{content}\n\n{body}" if content else body

    return {
        "title": f"{repo_full_name} — {title}",
        "url": html_url,
        "source": source,
        "snippet": _truncate_text(body or title, 500),
        "content": _truncate_text(content, 4000),
        "published": published,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "acquisition_quality": "high",
        "github_signal_type": "release",
        "github_full_name": repo_full_name,
        "source_kind": "github",
        "source_priority": 90,
        "community_signal_strength": 2,
    }


def _looks_like_watchlist_query(line: str) -> bool:
    return bool(line and line.lower().startswith("query:"))


def _fetch_rss(hours: int = 72) -> list[dict[str, Any]]:
    """
    Lấy bài từ RSS feeds, ưu tiên nguồn chính thức và media mạnh.
    Tăng cửa sổ mặc định lên 72h để tránh bỏ sót bài quan trọng cuối tuần.
    """
    try:
        import feedparser
    except ImportError:
        logger.warning("feedparser chưa cài — bỏ qua RSS.")
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    articles: list[dict[str, Any]] = []
    extra_feeds = [item.strip() for item in os.getenv("RSS_EXTRA_FEEDS", "").split(",") if item.strip()]
    feed_urls = list(dict.fromkeys(CURATED_RSS_FEEDS + extra_feeds))

    for feed_url in feed_urls:
        try:
            # Dùng requests trước để chủ động User-Agent; nếu fail thì feedparser tự thử lại.
            raw_text = _http_get_text(feed_url, timeout=20)
            feed = feedparser.parse(raw_text if raw_text else feed_url)
            feed_title = feed.feed.get("title", feed_url)

            for entry in feed.entries[:12]:
                published = None
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    from time import mktime
                    published = datetime.fromtimestamp(mktime(entry.published_parsed), tz=timezone.utc)
                elif hasattr(entry, "updated_parsed") and entry.updated_parsed:
                    from time import mktime
                    published = datetime.fromtimestamp(mktime(entry.updated_parsed), tz=timezone.utc)

                if published and published < cutoff:
                    continue

                url = entry.get("link", "")
                if not url or _is_blocked_url(url):
                    continue

                snippet = ""
                if hasattr(entry, "summary"):
                    soup = BeautifulSoup(entry.summary, "html.parser")
                    snippet = soup.get_text(strip=True)[:500]

                if not _is_founder_grade_candidate(entry.get("title", ""), snippet, url, feed_title):
                    continue

                articles.append(
                    {
                        "title": entry.get("title", ""),
                        "url": url,
                        "source": f"RSS: {feed_title}",
                        "snippet": snippet,
                        "published": published.isoformat() if published else "",
                        "fetched_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
        except Exception as exc:
            logger.warning("RSS fetch failed for %s: %s", feed_url[:70], exc)

    return articles


def _search_ddg(query: str, max_results: int = 4, timelimit: str = "w") -> list[dict[str, Any]]:
    """
    DDG vẫn hữu ích để mở rộng coverage, nhưng vì hay lỗi nên chỉ coi là lớp bổ sung.
    Mọi exception đều bị nuốt mềm để pipeline không chết theo DDG.
    Ưu tiên backend thuần-httpx để tránh lỗi abort ở nhánh primp khi DNS/network chập chờn.
    """
    try:
        with DDGS(timeout=10) as ddgs:
            results = list(
                ddgs.text(
                    query,
                    max_results=max_results * 2,
                    timelimit=timelimit,
                    backend=SAFE_DDGS_TEXT_BACKEND,
                )
            )
    except Exception as exc:
        logger.warning("DDG search failed for '%s': %s", query, exc)
        return []

    filtered: list[dict[str, Any]] = []
    for result in results:
        url = result.get("href") or result.get("link", "")
        if not url or _is_blocked_url(url):
            continue
        filtered.append(result)
        if len(filtered) >= max_results:
            break
    return filtered


def _extract_full_text(url: str) -> str:
    """
    Trích xuất nội dung bài báo.
    Chủ động bỏ qua một số domain hay 403 để log đỡ bẩn và run đỡ chậm.
    """
    domain = _domain_from_url(url)
    if domain in EXTRACTION_BLOCKED_DOMAINS:
        return ""

    try:
        downloaded = fetch_url(url)
        if downloaded:
            text = extract(downloaded, include_comments=False, include_tables=True)
            if text:
                return text[:5000]

            soup = BeautifulSoup(downloaded, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "header"]):
                tag.decompose()
            return soup.get_text(separator="\n", strip=True)[:5000]
    except Exception as exc:
        logger.warning("Text extraction failed for %s: %s", url[:60], exc)
    return ""


def _is_ai_relevant_text(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(keyword in lowered for keyword in DEFAULT_HN_KEYWORDS)


def _is_founder_grade_candidate(title: str, snippet: str = "", url: str = "", source: str = "") -> bool:
    combined = " ".join(part for part in [title, snippet, url, source] if part).lower()
    if not combined:
        return False
    if _has_founder_grade_noise(combined):
        return False
    return _has_founder_grade_signal(combined)


def _fetch_hacker_news(limit: int = 10) -> list[dict[str, Any]]:
    """
    Lấy từ HN public API.
    Đây là nguồn cộng đồng chất lượng tốt hơn search chay, và không cần API key.
    """
    base_url = "https://hacker-news.firebaseio.com/v0"
    collected: list[dict[str, Any]] = []

    try:
        response = requests.get(f"{base_url}/topstories.json", headers=REQUEST_HEADERS, timeout=20)
        response.raise_for_status()
        story_ids = response.json()[:60]
    except Exception as exc:
        logger.warning("Hacker News fetch failed: %s", exc)
        return []

    for story_id in story_ids:
        try:
            item_resp = requests.get(f"{base_url}/item/{story_id}.json", headers=REQUEST_HEADERS, timeout=20)
            item_resp.raise_for_status()
            item = item_resp.json() or {}
        except Exception:
            continue

        title = str(item.get("title", "") or "")
        url = str(item.get("url", "") or "")
        if not title or not _is_ai_relevant_text(f"{title} {url}"):
            continue

        hn_url = f"https://news.ycombinator.com/item?id={story_id}"
        collected.append(
            {
                "title": title,
                "url": url or hn_url,
                "source": "Hacker News API",
                "snippet": f"HN score={item.get('score', 0)} comments={item.get('descendants', 0)}",
                "content": item.get("text", "") or "",
                "published": datetime.fromtimestamp(int(item.get("time", 0) or 0), tz=timezone.utc).isoformat()
                if item.get("time")
                else "",
                "community_hint": hn_url,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "source_kind": "community",
                "source_priority": 74,
                "community_signal_strength": min(
                    5,
                    3
                    + (1 if int(item.get("score", 0) or 0) >= 50 else 0)
                    + (1 if int(item.get("descendants", 0) or 0) >= 25 else 0),
                ),
            }
        )
        if len(collected) >= limit:
            break

    return collected


def _fetch_reddit_posts(limit_per_subreddit: int = 3) -> list[dict[str, Any]]:
    """
    Lấy community signals trực tiếp từ Reddit thay vì chỉ search vòng ngoài qua DDG.
    Dùng JSON endpoint công khai; nếu fail thì skip mềm.
    """
    configured = [item.strip() for item in os.getenv("REDDIT_SUBREDDITS", "").split(",") if item.strip()]
    subreddits = configured or DEFAULT_REDDIT_SUBREDDITS
    articles: list[dict[str, Any]] = []

    for subreddit in subreddits:
        url = f"https://www.reddit.com/r/{subreddit}/new.json?limit={limit_per_subreddit}&raw_json=1"
        try:
            response = requests.get(url, headers=REQUEST_HEADERS, timeout=20)
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            logger.debug("Reddit fetch failed for r/%s: %s", subreddit, exc)
            continue

        children = payload.get("data", {}).get("children", [])
        for child in children:
            data = child.get("data", {}) or {}
            title = str(data.get("title", "") or "")
            selftext = str(data.get("selftext", "") or "")
            permalink = str(data.get("permalink", "") or "")
            if not title or not _is_ai_relevant_text(f"{title} {selftext}"):
                continue

            articles.append(
                {
                    "title": title,
                    "url": f"https://www.reddit.com{permalink}" if permalink else data.get("url", ""),
                    "source": f"Reddit r/{subreddit}",
                    "snippet": selftext[:500],
                    "content": selftext[:4000],
                    "published": datetime.fromtimestamp(float(data.get("created_utc", 0) or 0), tz=timezone.utc).isoformat()
                    if data.get("created_utc")
                    else "",
                    "community_hint": data.get("url", ""),
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                    "source_kind": "community",
                    "source_priority": 74,
                    "community_signal_strength": min(
                        5,
                        2
                        + (1 if int(data.get("score", 0) or 0) >= 20 else 0)
                        + (1 if int(data.get("num_comments", 0) or 0) >= 10 else 0),
                    ),
                }
            )

    return articles


def _build_watchlist_articles() -> list[dict[str, Any]]:
    """
    Watchlist là đường practical để bám nguồn mà sếp/team thực sự quan tâm.
    Cho phép seed bằng URL hoặc query.
    """
    seeds = load_watchlist_seeds(PROJECT_ROOT)
    articles: list[dict[str, Any]] = []

    for url in seeds["urls"]:
        if not url or _is_blocked_url(url):
            continue
        html_text = _http_get_text(url, timeout=20)
        content = _extract_full_text(url) or BeautifulSoup(html_text or "", "html.parser").get_text(" ", strip=True)[:4000]
        title = _extract_title_from_html(html_text, fallback=url)
        articles.append(
            {
                "title": title,
                "url": url,
                "source": "Watchlist Seed",
                "snippet": content[:500],
                "content": content,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "watchlist_hit": True,
                "source_kind": "watchlist",
                "source_priority": 82,
            }
        )

    for query in seeds["queries"]:
        for result in _search_ddg(query, max_results=2, timelimit="m"):
            url = result.get("href") or result.get("link", "")
            title = result.get("title", "")
            snippet = result.get("body", "")
            article = _build_search_article(
                url=url,
                title=title,
                snippet=snippet,
                source=f"Watchlist Query: {query}",
                published=result.get("date", "") or result.get("published", ""),
                query_context=query,
            )
            if article:
                article["watchlist_hit"] = True
                article["source_kind"] = "watchlist"
                article["source_priority"] = 82
                articles.append(article)

    return articles


def _fetch_github_articles(
    *,
    repo_watchlist: list[str],
    org_watchlist: list[str],
    query_watchlist: list[str],
    max_releases_per_repo: int,
    max_org_repos: int,
    max_search_results: int,
) -> list[dict[str, Any]]:
    """
    Thu tín hiệu GitHub theo 3 lớp:
    - repo watchlist: repo metadata + release mới
    - org watchlist: repo update gần đây
    - query watchlist: search repo/topic mới nổi
    """
    articles: list[dict[str, Any]] = []

    for repo_full_name in repo_watchlist:
        if not _valid_github_repo_full_name(repo_full_name):
            logger.warning("Skip invalid GitHub repo watchlist entry: %s", repo_full_name)
            continue

        repo = _github_get_json(f"/repos/{repo_full_name}")
        if isinstance(repo, dict):
            article = _build_github_repo_article(repo, source=f"GitHub API Repo: {repo_full_name}")
            if article:
                articles.append(article)

        if max_releases_per_repo <= 0:
            continue
        releases = _github_get_json(f"/repos/{repo_full_name}/releases", params={"per_page": max_releases_per_repo})
        if not isinstance(releases, list):
            continue
        for release in releases[:max_releases_per_repo]:
            article = _build_github_release_article(
                release,
                repo_full_name=repo_full_name,
                source=f"GitHub API Release: {repo_full_name}",
            )
            if article:
                articles.append(article)

    for org in org_watchlist:
        org_name = str(org or "").strip()
        if not org_name:
            continue
        repos = _github_get_json(
            f"/orgs/{org_name}/repos",
            params={"sort": "updated", "direction": "desc", "per_page": max_org_repos},
        )
        if not isinstance(repos, list):
            continue
        for repo in repos[:max_org_repos]:
            article = _build_github_repo_article(
                repo,
                source=f"GitHub API Org: {org_name}",
                query_context=f"org:{org_name}",
            )
            if article:
                articles.append(article)

    search_cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).date().isoformat()
    for query in query_watchlist:
        search_query = str(query or "").strip()
        if not search_query:
            continue
        payload = _github_get_json(
            "/search/repositories",
            params={
                "q": f"{search_query} pushed:>={search_cutoff}",
                "sort": "updated",
                "order": "desc",
                "per_page": max_search_results,
            },
        )
        items = payload.get("items", []) if isinstance(payload, dict) else []
        for repo in items[:max_search_results]:
            article = _build_github_repo_article(
                repo,
                source="GitHub API Search",
                query_context=search_query,
            )
            if article:
                articles.append(article)

    return articles


async def _read_telegram_channels(channels: list[str], limit: int = 10) -> list[dict[str, Any]]:
    """
    Đọc Telegram channels nếu có Telethon credentials.
    Không ép buộc vì đây là optional source.
    """
    api_id = os.getenv("TELETHON_API_ID")
    api_hash = os.getenv("TELETHON_API_HASH")
    session_name = os.getenv("TELETHON_SESSION_NAME", "digest_session")

    if not api_id or not api_hash:
        logger.info("Telethon credentials not set — skipping Telegram channels.")
        return []

    articles: list[dict[str, Any]] = []
    try:
        from telethon import TelegramClient

        client = TelegramClient(session_name, int(api_id), api_hash)
        await client.start()

        for channel_name in channels:
            try:
                channel = await client.get_entity(channel_name)
                async for message in client.iter_messages(channel, limit=limit):
                    if message.text and len(message.text) > 50 and _is_ai_relevant_text(message.text):
                        articles.append(
                            {
                                "title": message.text[:120].replace("\n", " "),
                                "url": f"https://t.me/{channel_name}/{message.id}",
                                "source": f"Telegram @{channel_name}",
                                "content": message.text,
                                "fetched_at": datetime.now(timezone.utc).isoformat(),
                                "source_kind": "community",
                                "source_priority": 74,
                                "community_signal_strength": 4,
                            }
                        )
            except Exception as exc:
                logger.warning("Failed to read channel @%s: %s", channel_name, exc)

        await client.disconnect()
    except Exception as exc:
        logger.warning("Telethon error: %s", exc)

    return articles


def _local_deduplicate_articles(articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Dedup ngay trong batch để giảm lãng phí extract/classify trước khi vào node sau."""
    seen_urls: set[str] = set()
    deduped: list[dict[str, Any]] = []

    for article in articles:
        url = str(article.get("url", "") or "").strip()
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        deduped.append(article)
    return deduped


def gather_news_node(state: dict[str, Any]) -> dict[str, Any]:
    """
    LangGraph node: thu thập tin tức từ tất cả nguồn.

    Ghi chú:
    - Nếu một nguồn community/search fail thì pipeline vẫn phải sống.
    - Nguồn lõi là RSS/official feeds; search/community đóng vai trò bổ sung.
    """
    raw_articles: list[dict[str, Any]] = []

    rss_hours = _cfg_int(state, "gather_rss_hours", "GATHER_RSS_HOURS", 72)
    ddg_max_results = _cfg_int(state, "ddg_max_results_per_query", "DDG_MAX_RESULTS_PER_QUERY", 3)
    hn_limit = _cfg_int(state, "hn_max_items", "HN_MAX_ITEMS", 8)
    reddit_limit = _cfg_int(state, "reddit_max_posts_per_subreddit", "REDDIT_MAX_POSTS_PER_SUBREDDIT", 2)
    github_repo_watchlist_limit = _cfg_int(state, "github_max_watchlist_repos", "GITHUB_MAX_WATCHLIST_REPOS", 0)
    github_org_limit = _cfg_int(state, "github_max_orgs", "GITHUB_MAX_ORGS", 0)
    github_query_limit = _cfg_int(state, "github_max_queries", "GITHUB_MAX_QUERIES", 0)
    github_org_repo_limit = _cfg_int(state, "github_max_org_repos", "GITHUB_MAX_ORG_REPOS", 4)
    github_release_limit = _cfg_int(state, "github_max_releases_per_repo", "GITHUB_MAX_RELEASES_PER_REPO", 1)
    github_search_limit = _cfg_int(state, "github_max_search_results", "GITHUB_MAX_SEARCH_RESULTS", 4)

    enable_rss = _cfg_bool(state, "enable_rss", "ENABLE_RSS", True)
    enable_github = _cfg_bool(state, "enable_github", "GITHUB_ENABLED", True)
    enable_social_signals = _cfg_bool(state, "enable_social_signals", "ENABLE_SOCIAL_SIGNALS", False)
    enable_watchlist = _cfg_bool(state, "enable_watchlist", "ENABLE_WATCHLIST", True)
    enable_hn = _cfg_bool(state, "enable_hn", "HN_ENABLED", True)
    enable_reddit = _cfg_bool(state, "enable_reddit", "REDDIT_ENABLED", True)
    enable_ddg = _cfg_bool(state, "enable_ddg", "ENABLE_DDG", True)
    enable_telegram_channels = _cfg_bool(state, "enable_telegram_channels", "ENABLE_TELEGRAM_CHANNELS", True)
    facebook_headless = _cfg_bool(state, "facebook_auto_headless", "FACEBOOK_AUTO_HEADLESS", True)

    watchlist_seeds = load_watchlist_seeds(PROJECT_ROOT)
    facebook_registry = {
        "discovered_sources": [],
        "auto_active_sources": _load_facebook_auto_targets() if _facebook_auto_enabled(state) else [],
        "candidate_sources": [],
    }
    if _facebook_auto_enabled(state):
        facebook_registry = _resolve_facebook_source_registry(state, headless=facebook_headless)

    if enable_rss:
        logger.info("📡 Fetching curated RSS feeds (%dh qua) …", rss_hours)
        rss_articles = _fetch_rss(hours=rss_hours)
        logger.info("   RSS: %d bài", len(rss_articles))
        for article in rss_articles:
            url = article.get("url", "")
            if url and not article.get("content"):
                article["content"] = _extract_full_text(url)
            raw_articles.append(article)
    else:
        logger.info("⏭️ Skip RSS theo runtime config.")

    if enable_github:
        logger.info("🐙 Fetching GitHub repo/tool signals …")
        github_repos = list(
            dict.fromkeys(
                DEFAULT_GITHUB_REPOS
                + watchlist_seeds.get("github_repos", [])
                + _cfg_list(state, "github_watchlist_repos", "GITHUB_WATCHLIST_REPOS")
            )
        )
        github_repos = _maybe_limit(github_repos, github_repo_watchlist_limit)
        github_orgs = list(
            dict.fromkeys(
                DEFAULT_GITHUB_ORGS
                + watchlist_seeds.get("github_orgs", [])
                + _cfg_list(state, "github_watchlist_orgs", "GITHUB_WATCHLIST_ORGS")
            )
        )
        github_orgs = _maybe_limit(github_orgs, github_org_limit)
        github_queries = list(
            dict.fromkeys(
                DEFAULT_GITHUB_SEARCH_QUERIES
                + watchlist_seeds.get("github_queries", [])
                + _cfg_list(state, "github_search_queries", "GITHUB_SEARCH_QUERIES", separator="||")
            )
        )
        github_queries = _maybe_limit(github_queries, github_query_limit)
        github_articles = _fetch_github_articles(
            repo_watchlist=github_repos,
            org_watchlist=github_orgs,
            query_watchlist=github_queries,
            max_releases_per_repo=github_release_limit,
            max_org_repos=github_org_repo_limit,
            max_search_results=github_search_limit,
        )
        logger.info("   GitHub: %d bài", len(github_articles))
        raw_articles.extend(github_articles)
    else:
        logger.info("⏭️ Skip GitHub theo runtime config.")

    if enable_social_signals:
        logger.info("👥 Loading manual social signals …")
        social_articles = _build_social_signal_articles()
        logger.info("   Social signals: %d bài", len(social_articles))
        raw_articles.extend(social_articles)
    else:
        logger.info("⏭️ Skip manual social signals theo runtime config.")

    facebook_auto_articles = _build_facebook_auto_articles(
        state,
        targets=list(facebook_registry.get("auto_active_sources", []) or []),
    )
    if facebook_auto_articles:
        logger.info("📘 Facebook auto: %d bài", len(facebook_auto_articles))
        raw_articles.extend(facebook_auto_articles)
    elif _facebook_auto_enabled(state):
        logger.info("📘 Facebook auto: 0 bài")

    if enable_watchlist:
        logger.info("🧭 Loading watchlist seeds …")
        watchlist_articles = _build_watchlist_articles()
        logger.info("   Watchlist: %d bài", len(watchlist_articles))
        raw_articles.extend(watchlist_articles)
    else:
        logger.info("⏭️ Skip watchlist theo runtime config.")

    if enable_hn:
        logger.info("🗞️ Fetching Hacker News AI signals …")
        hn_articles = _fetch_hacker_news(limit=hn_limit)
        logger.info("   Hacker News: %d bài", len(hn_articles))
        raw_articles.extend(hn_articles)
    else:
        logger.info("⏭️ Skip Hacker News theo runtime config.")

    if enable_reddit:
        logger.info("👥 Fetching Reddit AI signals …")
        reddit_articles = _fetch_reddit_posts(limit_per_subreddit=reddit_limit)
        logger.info("   Reddit: %d bài", len(reddit_articles))
        raw_articles.extend(reddit_articles)
    else:
        logger.info("⏭️ Skip Reddit theo runtime config.")

    grok_x_scout_articles = _run_grok_x_scout(state, raw_articles)
    if grok_x_scout_articles:
        logger.info("🧠 Grok X scout bổ sung: %d bài", len(grok_x_scout_articles))
        raw_articles.extend(grok_x_scout_articles)

    if enable_ddg:
        logger.info("🔍 Searching English AI sources (supplemental) …")
        for query in SEARCH_QUERIES_EN:
            for result in _search_ddg(query, max_results=ddg_max_results, timelimit="w"):
                url = result.get("href") or result.get("link", "")
                title = result.get("title", "")
                snippet = result.get("body", "")
                article = _build_search_article(
                    url=url,
                    title=title,
                    snippet=snippet,
                    source="DuckDuckGo (EN)",
                    published=result.get("date", "") or result.get("published", ""),
                    query_context=query,
                )
                if article:
                    raw_articles.append(article)

        logger.info("🔍 Searching Vietnamese AI sources (supplemental) …")
        for query in build_search_queries_vn():
            for result in _search_ddg(query, max_results=ddg_max_results, timelimit="m"):
                url = result.get("href") or result.get("link", "")
                title = result.get("title", "")
                snippet = result.get("body", "")
                article = _build_search_article(
                    url=url,
                    title=title,
                    snippet=snippet,
                    source="DuckDuckGo (VN)",
                    published=result.get("date", "") or result.get("published", ""),
                    query_context=query,
                )
                if article:
                    raw_articles.append(article)
    else:
        logger.info("⏭️ Skip DDG theo runtime config.")

    if enable_telegram_channels:
        logger.info("📱 Reading Telegram channels …")
        channels_str = os.getenv("TELEGRAM_CHANNELS", "")
        channels = [c.strip() for c in channels_str.split(",") if c.strip()] or DEFAULT_TELEGRAM_CHANNELS
        try:
            telegram_articles = asyncio.run(_read_telegram_channels(channels))
        except RuntimeError:
            telegram_articles = []
        logger.info("   Telegram channels: %d bài", len(telegram_articles))
        raw_articles.extend(telegram_articles)
    else:
        logger.info("⏭️ Skip Telegram channels theo runtime config.")

    grok_scout_articles = _run_grok_scout(state, raw_articles)
    if grok_scout_articles:
        logger.info("🧠 Grok scout bổ sung: %d bài", len(grok_scout_articles))
        raw_articles.extend(grok_scout_articles)

    deduped_raw_articles = _local_deduplicate_articles(raw_articles)
    logger.info(
        "✅ Gathered %d raw articles total (%d sau dedup trong batch)",
        len(raw_articles),
        len(deduped_raw_articles),
    )
    return {
        "raw_articles": deduped_raw_articles,
        "grok_scout_count": len(grok_scout_articles) + len(grok_x_scout_articles),
        "facebook_discovered_sources": list(facebook_registry.get("discovered_sources", []) or []),
        "facebook_auto_active_sources": list(facebook_registry.get("auto_active_sources", []) or []),
        "facebook_candidate_sources": list(facebook_registry.get("candidate_sources", []) or []),
    }
