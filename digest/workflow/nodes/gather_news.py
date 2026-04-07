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
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from ddgs import DDGS
from trafilatura import extract, fetch_url


from digest.sources.source_catalog import (
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
from digest.sources.source_history import (
    annotate_articles_with_source_history,
    annotate_sources_with_history,
    filter_discovered_sources_by_history,
    load_source_history,
)
from digest.workflow.nodes.adapters.hackernews_adapter import fetch_hackernews_top_stories
from digest.sources.adapters import facebook_adapter as _facebook_adapter
from digest.sources.adapters.github_adapter import fetch_github_articles as _fetch_github_articles_impl
from digest.sources.adapters.grok_scout_adapter import (
    DEFAULT_X_SCOUT_HANDLES,
    WEB_SCOUT_PLANS,
    X_SCOUT_PLANS,
    build_grok_scout_article as _build_grok_scout_article_impl,
    build_grok_x_scout_article as _build_grok_x_scout_article_impl,
    run_grok_scout as _run_grok_scout_impl,
    run_grok_x_scout as _run_grok_x_scout_impl,
    runtime_x_scout_handles as _runtime_x_scout_handles_impl,
)
from digest.runtime.xai_grok import (
    grok_scout_enabled,
    grok_scout_max_articles,
    grok_scout_max_queries,
    grok_x_scout_enabled,
    grok_x_scout_max_articles,
    grok_x_scout_max_queries,
    scout_x_posts,
    scout_web_search_articles,
)
from digest.runtime.temporal_snapshots import write_temporal_snapshot

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SAFE_DDGS_TEXT_BACKEND = os.getenv("DDGS_TEXT_BACKEND", "duckduckgo").strip().lower() or "duckduckgo"
MAX_GITHUB_RATIO = 0.30
MAX_GITHUB_ONLY_ARTICLES = 3
REQUEST_HEADERS = {
    "User-Agent": "AvalookDigestBot/1.0 (+https://avalook.local)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
FACEBOOK_SESSION_MAX_AGE_DAYS = 7.0

FOUNDER_GRADE_SIGNAL_KEYWORDS = (
    "ai",
    "a.i",
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
    "chatgpt",
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
    "mcp",
    "cursor",
    "copilot",
    "prompt",
    "workflow",
    "ứng dụng ai",
    "công cụ ai",
    "mô hình",
    "dữ liệu",
    "nghiện ai",
    "openclaw",
    "deep learning",
    "machine learning",
    "fine-tune",
    "finetune",
    "rag",
    "vector",
    "embedding",
    "transformer",
    "diffusion",
    "stable diffusion",
    "midjourney",
    "sora",
    "llama",
    "mistral",
    "qwen",
    "phi-",
    "microsoft",
    "google",
    "meta ai",
    "apple intelligence",
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


def _filter_history_muted_discoveries(articles: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    kept: list[dict[str, Any]] = []
    filtered = 0
    for article in articles:
        if not isinstance(article, dict):
            continue
        source_kind = str(article.get("source_kind", "unknown") or "unknown").lower()
        history_penalty = int(article.get("source_history_penalty", 0) or 0)
        history_quality = int(article.get("source_history_quality_score", 50) or 50)
        watchlist_hit = bool(article.get("watchlist_hit", False))
        social_signal = bool(article.get("social_signal", False))
        if (
            history_penalty >= 8
            and history_quality <= 35
            and source_kind in {"search", "community"}
            and not watchlist_hit
            and not social_signal
        ):
            filtered += 1
            continue
        kept.append(article)
    return kept, filtered


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
    return _build_grok_scout_article_impl(
        item,
        plan_name=plan_name,
        is_blocked_url_fn=_is_blocked_url,
        is_social_signal_url_fn=_is_social_signal_url,
        is_founder_grade_candidate_fn=_is_founder_grade_candidate,
        domain_from_url_fn=_domain_from_url,
        extract_full_text_fn=_extract_full_text,
        truncate_text_fn=_truncate_text,
    )


def _build_grok_x_scout_article(item: dict[str, Any], *, plan_name: str) -> dict[str, Any] | None:
    return _build_grok_x_scout_article_impl(
        item,
        plan_name=plan_name,
        is_blocked_url_fn=_is_blocked_url,
        is_founder_grade_candidate_fn=_is_founder_grade_candidate,
        domain_from_url_fn=_domain_from_url,
        truncate_text_fn=_truncate_text,
    )


def _run_grok_scout(state: dict[str, Any], raw_articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return _run_grok_scout_impl(
        should_run=_should_run_grok_scout(state, raw_articles),
        raw_articles=raw_articles,
        max_queries=grok_scout_max_queries(_runtime_config(state)),
        max_articles_total=grok_scout_max_articles(_runtime_config(state)),
        scout_web_search_articles_fn=scout_web_search_articles,
        is_blocked_url_fn=_is_blocked_url,
        is_social_signal_url_fn=_is_social_signal_url,
        is_founder_grade_candidate_fn=_is_founder_grade_candidate,
        domain_from_url_fn=_domain_from_url,
        extract_full_text_fn=_extract_full_text,
        truncate_text_fn=_truncate_text,
        logger=logger,
        plans=WEB_SCOUT_PLANS,
    )


def _runtime_x_scout_handles(state: dict[str, Any]) -> list[str]:
    configured = _cfg_list(state, "grok_x_scout_allowed_handles", "GROK_X_SCOUT_ALLOWED_HANDLES")
    return _runtime_x_scout_handles_impl(configured_handles=configured, default_handles=DEFAULT_X_SCOUT_HANDLES)


def _run_grok_x_scout(state: dict[str, Any], raw_articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return _run_grok_x_scout_impl(
        enabled=grok_x_scout_enabled(_runtime_config(state)),
        raw_articles=raw_articles,
        max_queries=grok_x_scout_max_queries(_runtime_config(state)),
        max_articles_total=grok_x_scout_max_articles(_runtime_config(state)),
        allowed_handles=_runtime_x_scout_handles(state),
        excluded_handles=_cfg_list(state, "grok_x_scout_excluded_handles", "GROK_X_SCOUT_EXCLUDED_HANDLES"),
        scout_x_posts_fn=scout_x_posts,
        is_blocked_url_fn=_is_blocked_url,
        is_founder_grade_candidate_fn=_is_founder_grade_candidate,
        domain_from_url_fn=_domain_from_url,
        truncate_text_fn=_truncate_text,
        logger=logger,
        plans=X_SCOUT_PLANS,
    )


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
                "delivery_lane_hint": "",
                "community_reactions": comments,
                "source_kind": "community",
                "source_priority": 74,
                "community_signal_strength": 5,
                "watchlist_hit": False,
            }
        )

    return articles


def _github_headers() -> dict[str, str]:
    from digest.sources.adapters.github_adapter import github_headers

    return github_headers(request_headers=REQUEST_HEADERS, github_token=os.getenv("GITHUB_TOKEN", "").strip())


def _github_get_json(path: str, *, params: dict[str, Any] | None = None) -> Any:
    from digest.sources.adapters.github_adapter import github_get_json

    return github_get_json(
        path,
        request_headers=REQUEST_HEADERS,
        github_token=os.getenv("GITHUB_TOKEN", "").strip(),
        params=params,
        logger=logger,
    )


def _truncate_text(value: str, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"

_normalize_facebook_permalink = _facebook_adapter.normalize_facebook_permalink
_extract_facebook_permalink = _facebook_adapter.extract_facebook_permalink
_looks_like_interaction_line = _facebook_adapter.looks_like_interaction_line
_is_loaded_facebook_payload = _facebook_adapter.is_loaded_facebook_payload
_clean_facebook_lines = _facebook_adapter.clean_facebook_lines
_detect_facebook_content_style = _facebook_adapter.detect_facebook_content_style
_score_facebook_boss_style = _facebook_adapter.score_facebook_boss_style
_score_facebook_authority = _facebook_adapter.score_facebook_authority
_facebook_published_hint_rank = _facebook_adapter.facebook_published_hint_rank
_normalize_facebook_source_url = _facebook_adapter.normalize_facebook_source_url
_infer_facebook_source_type = _facebook_adapter.infer_facebook_source_type
_score_facebook_source = _facebook_adapter.score_facebook_source
_facebook_source_status = _facebook_adapter.facebook_source_status
_normalize_facebook_source_entry = _facebook_adapter.normalize_facebook_source_entry
_clean_facebook_discovery_label = _facebook_adapter.clean_facebook_discovery_label
_extract_facebook_anchor_candidates = _facebook_adapter.extract_facebook_anchor_candidates
_is_real_group_source_url = _facebook_adapter.is_real_group_source_url
_is_profile_like_source_url = _facebook_adapter.is_profile_like_source_url
_merge_facebook_source_entries = _facebook_adapter.merge_facebook_source_entries
_discover_group_sources = _facebook_adapter.discover_group_sources
_discover_followed_page_sources = _facebook_adapter.discover_followed_page_sources
_discover_followed_profile_sources = _facebook_adapter.discover_followed_profile_sources
_try_set_facebook_newest_first = _facebook_adapter.try_set_facebook_newest_first


def _facebook_target_file() -> Path:
    return _project_path_from_env(FACEBOOK_AUTO_TARGETS_DEFAULT_FILE, "FACEBOOK_AUTO_TARGETS_FILE")


def _facebook_profile_dir() -> Path:
    return _project_path_from_env("config/facebook_chrome_profile", "FACEBOOK_CHROME_PROFILE_DIR")


def _facebook_storage_state_file() -> Path:
    return _project_path_from_env(FACEBOOK_STORAGE_STATE_DEFAULT_FILE, "FACEBOOK_STORAGE_STATE_FILE")


def _file_age_days(path: Path) -> float | None:
    try:
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None
    return max(0.0, (datetime.now(timezone.utc) - modified).total_seconds() / 86400.0)


def _facebook_session_needs_refresh() -> bool:
    storage_state = _facebook_storage_state_file()
    age_days = _file_age_days(storage_state)
    if not storage_state.exists():
        return True
    return age_days is None or age_days > FACEBOOK_SESSION_MAX_AGE_DAYS


def _send_telegram_alert(text: str, *, thread_env: str = "TELEGRAM_THREAD_ID") -> bool:
    bot_token = str(os.getenv("TELEGRAM_BOT_TOKEN", "") or "").strip()
    chat_id = str(os.getenv("TELEGRAM_CHAT_ID", "") or "").strip()
    if not bot_token or not chat_id or not str(text or "").strip():
        return False

    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    thread_value = str(os.getenv(thread_env, "") or "").strip()
    if thread_value:
        with suppress(ValueError):
            payload["message_thread_id"] = int(thread_value)

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json=payload,
            timeout=10,
        )
        return response.status_code == 200
    except Exception as exc:
        logger.warning("Telegram alert send failed: %s", exc)
        return False


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
    return _facebook_adapter.utc_now_iso()


def _read_json_file(path: Path) -> Any:
    return _facebook_adapter.read_json_file(path)


def _write_json_file(path: Path, payload: Any) -> None:
    _facebook_adapter.write_json_file(path, payload)


def _load_facebook_auto_targets() -> list[dict[str, str]]:
    path = _facebook_target_file()
    try:
        return _facebook_adapter.load_facebook_auto_targets(path, domain_from_url_fn=_domain_from_url)
    except Exception as exc:
        logger.warning("Facebook auto targets read failed: %s", exc)
        return []


def _facebook_auto_enabled(state: dict[str, Any]) -> bool:
    return _cfg_bool(state, "enable_facebook_auto", "ENABLE_FACEBOOK_AUTO", False)


def _facebook_discovery_cache_is_fresh(path: Path, *, refresh_hours: int) -> bool:
    return _facebook_adapter.facebook_discovery_cache_is_fresh(path, refresh_hours=refresh_hours)


def _load_facebook_discovery_cache() -> list[dict[str, Any]]:
    return _facebook_adapter.load_facebook_discovery_cache(_facebook_discovery_cache_file())


def _save_facebook_discovery_cache(sources: list[dict[str, Any]]) -> None:
    _facebook_adapter.save_facebook_discovery_cache(_facebook_discovery_cache_file(), sources)


def _refresh_facebook_discovery_cache(*, headless: bool, allow_profiles: bool) -> list[dict[str, Any]]:
    return _facebook_adapter.refresh_facebook_discovery_cache(
        headless=headless,
        allow_profiles=allow_profiles,
        chrome_executable=_facebook_chrome_executable(),
        storage_state_file=_facebook_storage_state_file(),
        logger=logger,
    )


def _resolve_facebook_source_registry(state: dict[str, Any], *, headless: bool) -> dict[str, list[dict[str, Any]]]:
    return _facebook_adapter.resolve_facebook_source_registry(
        manual_sources=_load_facebook_auto_targets(),
        discovery_enabled=_facebook_discovery_enabled(state),
        cache_file=_facebook_discovery_cache_file(),
        refresh_hours=_facebook_discovery_refresh_hours(state),
        allow_profiles=_facebook_allow_profile_sources(state),
        max_candidates_per_run=_facebook_discovery_max_candidates_per_run(state),
        max_active_sources=_facebook_discovery_max_active_sources(state),
        headless=headless,
        load_cache_fn=_load_facebook_discovery_cache,
        refresh_cache_fn=_refresh_facebook_discovery_cache,
        save_cache_fn=_save_facebook_discovery_cache,
    )


def _scrape_facebook_target_payloads(
    *,
    target: dict[str, str],
    posts_per_target: int,
    headless: bool,
    force_newest_first: bool = True,
) -> list[dict[str, Any]]:
    return _facebook_adapter.scrape_facebook_target_payloads(
        target=target,
        posts_per_target=posts_per_target,
        headless=headless,
        force_newest_first=force_newest_first,
        chrome_executable=_facebook_chrome_executable(),
        storage_state_file=_facebook_storage_state_file(),
        logger=logger,
    )


def _build_facebook_auto_article(
    payload: dict[str, Any],
    *,
    target: dict[str, str],
) -> dict[str, Any] | None:
    return _facebook_adapter.build_facebook_auto_article(
        payload,
        target=target,
        is_founder_grade_candidate_fn=_is_founder_grade_candidate,
        truncate_text_fn=_truncate_text,
    )


def _build_facebook_auto_articles(
    state: dict[str, Any],
    *,
    targets: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    return _facebook_adapter.build_facebook_auto_articles(
        state,
        targets=targets,
        auto_enabled=_facebook_auto_enabled(state),
        load_targets_fn=_load_facebook_auto_targets,
        cfg_int_fn=_cfg_int,
        cfg_bool_fn=_cfg_bool,
        force_newest_first_fn=_facebook_force_newest_first,
        scrape_payloads_fn=_scrape_facebook_target_payloads,
        build_article_fn=_build_facebook_auto_article,
        logger=logger,
        published_hint_rank_fn=_facebook_published_hint_rank,
    )


def _valid_github_repo_full_name(value: str) -> bool:
    from digest.sources.adapters.github_adapter import valid_github_repo_full_name

    return valid_github_repo_full_name(value)


def _build_github_repo_article(repo: dict[str, Any], *, source: str, query_context: str = "") -> dict[str, Any] | None:
    from digest.sources.adapters.github_adapter import build_github_repo_article

    return build_github_repo_article(
        repo,
        source=source,
        query_context=query_context,
        is_founder_grade_candidate_fn=_is_founder_grade_candidate,
        truncate_text_fn=_truncate_text,
    )


def _build_github_release_article(
    release: dict[str, Any],
    *,
    repo_full_name: str,
    source: str,
) -> dict[str, Any] | None:
    from digest.sources.adapters.github_adapter import build_github_release_article

    return build_github_release_article(
        release,
        repo_full_name=repo_full_name,
        source=source,
        is_founder_grade_candidate_fn=_is_founder_grade_candidate,
        truncate_text_fn=_truncate_text,
    )


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
    Wrapper giữ backward compatibility cho gather/tests, nhưng phần fetch thật
    đã được tách sang digest/workflow/nodes/adapters/hackernews_adapter.py.
    """
    collected: list[dict[str, Any]] = []
    for item in fetch_hackernews_top_stories(limit=limit):
        title = str(item.get("title", "") or "")
        story_url = str(item.get("url", "") or "")
        story_text = str(item.get("content", "") or "")
        if not title or not _is_ai_relevant_text(f"{title} {story_url} {story_text}"):
            continue
        collected.append(
            {
                "title": title,
                "url": story_url,
                "source": str(item.get("source", "Hacker News Algolia") or "Hacker News Algolia"),
                "snippet": story_text[:500],
                "content": story_text,
                "published_at": str(item.get("published_at", "") or ""),
                "published": str(item.get("published_at", "") or ""),
                "community_hint": str(item.get("community_hint", story_url) or story_url),
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "source_kind": str(item.get("source_kind", "api") or "api"),
                "source_priority": 74,
                "score": int(item.get("score", 0) or 0),
                "community_signal_strength": int(item.get("community_signal_strength", 4) or 0),
                "hn_points": int(item.get("hn_points", 0) or 0),
                "hn_num_comments": int(item.get("hn_num_comments", 0) or 0),
            }
        )
    return collected


def _is_github_article(article: dict[str, Any]) -> bool:
    url = str(article.get("url", "") or "").strip().lower()
    source_domain = str(article.get("source_domain", "") or "").strip().lower()
    source_kind = str(article.get("source_kind", "") or "").strip().lower()
    return "github.com" in url or source_domain == "github.com" or source_kind == "github"


def _github_retention_sort_key(article: dict[str, Any]) -> tuple[int, int, int]:
    return (
        int(article.get("community_signal_strength", 0) or 0),
        int(article.get("github_stars", 0) or 0),
        int(article.get("source_priority", 0) or 0),
    )


def _cap_github_article_ratio(raw_articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    github_articles = [article for article in raw_articles if _is_github_article(article)]
    non_github_articles = [article for article in raw_articles if not _is_github_article(article)]
    if not github_articles:
        return raw_articles

    if not non_github_articles:
        retained_github = sorted(github_articles, key=_github_retention_sort_key, reverse=True)[:MAX_GITHUB_ONLY_ARTICLES]
        logger.info(
            "🐙 GitHub ratio cap active: 0 non-GitHub articles, giữ %d/%d GitHub-only articles mạnh nhất.",
            len(retained_github),
            len(github_articles),
        )
        return retained_github

    max_github = int(len(non_github_articles) * MAX_GITHUB_RATIO / (1 - MAX_GITHUB_RATIO))
    if len(github_articles) <= max_github:
        return raw_articles

    retained_github = sorted(github_articles, key=_github_retention_sort_key, reverse=True)[:max_github]
    logger.info(
        "🐙 GitHub ratio cap giữ %d/%d bài GitHub để source mix không vượt %.0f%%.",
        len(retained_github),
        len(github_articles),
        MAX_GITHUB_RATIO * 100,
    )
    return non_github_articles + retained_github


def _fetch_reddit_posts(limit_per_subreddit: int = 3) -> list[dict[str, Any]]:
    """
    Lấy community signals trực tiếp từ Reddit.
    Ưu tiên PRAW nếu có credentials; nếu không thì fallback sang JSON endpoint công khai.
    """
    configured = [item.strip() for item in os.getenv("REDDIT_SUBREDDITS", "").split(",") if item.strip()]
    subreddits = configured or DEFAULT_REDDIT_SUBREDDITS
    articles: list[dict[str, Any]] = []
    now_ts = datetime.now(timezone.utc).timestamp()
    cutoff_ts = now_ts - 86400
    max_items = max(1, min(10, int(limit_per_subreddit or 3) * 5))

    reddit_client_id = str(os.getenv("REDDIT_CLIENT_ID", "") or "").strip()
    reddit_client_secret = str(os.getenv("REDDIT_CLIENT_SECRET", "") or "").strip()
    reddit_user_agent = str(os.getenv("REDDIT_USER_AGENT", "ai-digest-bot/1.0") or "ai-digest-bot/1.0").strip()

    def _append_post(*, subreddit: str, title: str, selftext: str, permalink: str, linked_url: str, created_utc: float, score: int, num_comments: int) -> None:
        if not title or created_utc < cutoff_ts or score <= 100:
            return
        if not _is_ai_relevant_text(f"{title} {selftext} {linked_url}"):
            return

        articles.append(
            {
                "title": title,
                "url": f"https://www.reddit.com{permalink}" if permalink else linked_url,
                "source": f"Reddit r/{subreddit}",
                "snippet": selftext[:500],
                "content": selftext[:4000],
                "published": datetime.fromtimestamp(float(created_utc), tz=timezone.utc).isoformat(),
                "community_hint": linked_url,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "source_kind": "community",
                "source_priority": 74,
                "reddit_score": int(score or 0),
                "reddit_num_comments": int(num_comments or 0),
                "community_signal_strength": min(
                    5,
                    3
                    + (1 if int(score or 0) >= 250 else 0)
                    + (1 if int(num_comments or 0) >= 40 else 0),
                ),
            }
        )

    if reddit_client_id and reddit_client_secret:
        try:
            import praw

            reddit = praw.Reddit(
                client_id=reddit_client_id,
                client_secret=reddit_client_secret,
                user_agent=reddit_user_agent,
                check_for_async=False,
            )
            for subreddit in subreddits:
                try:
                    for post in reddit.subreddit(subreddit).hot(limit=max_items):
                        _append_post(
                            subreddit=subreddit,
                            title=str(getattr(post, "title", "") or ""),
                            selftext=str(getattr(post, "selftext", "") or ""),
                            permalink=str(getattr(post, "permalink", "") or ""),
                            linked_url=str(getattr(post, "url", "") or ""),
                            created_utc=float(getattr(post, "created_utc", 0) or 0),
                            score=int(getattr(post, "score", 0) or 0),
                            num_comments=int(getattr(post, "num_comments", 0) or 0),
                        )
                except Exception as exc:
                    logger.debug("PRAW fetch failed for r/%s: %s", subreddit, exc)
            if articles:
                return articles
        except Exception as exc:
            logger.debug("PRAW client unavailable, fallback to Reddit JSON: %s", exc)

    for subreddit in subreddits:
        url = f"https://www.reddit.com/r/{subreddit}/hot.json?limit={max_items}&raw_json=1"
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
            _append_post(
                subreddit=subreddit,
                title=str(data.get("title", "") or ""),
                selftext=str(data.get("selftext", "") or ""),
                permalink=str(data.get("permalink", "") or ""),
                linked_url=str(data.get("url", "") or ""),
                created_utc=float(data.get("created_utc", 0) or 0),
                score=int(data.get("score", 0) or 0),
                num_comments=int(data.get("num_comments", 0) or 0),
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
    return _fetch_github_articles_impl(
        repo_watchlist=repo_watchlist,
        org_watchlist=org_watchlist,
        query_watchlist=query_watchlist,
        max_releases_per_repo=max_releases_per_repo,
        max_org_repos=max_org_repos,
        max_search_results=max_search_results,
        request_headers=REQUEST_HEADERS,
        github_token=os.getenv("GITHUB_TOKEN", "").strip(),
        logger=logger,
        is_founder_grade_candidate_fn=_is_founder_grade_candidate,
        truncate_text_fn=_truncate_text,
    )


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
    ddg_max_results = _cfg_int(state, "ddg_max_results_per_query", "DDG_MAX_RESULTS_PER_QUERY", 2)
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
    facebook_auto_enabled = _facebook_auto_enabled(state)
    facebook_headless = _cfg_bool(state, "facebook_auto_headless", "FACEBOOK_AUTO_HEADLESS", True)

    watchlist_seeds = load_watchlist_seeds(PROJECT_ROOT)
    source_history_map = load_source_history()
    facebook_registry = {
        "discovered_sources": [],
        "auto_active_sources": [],
        "candidate_sources": [],
    }
    if facebook_auto_enabled and _facebook_session_needs_refresh():
        logger.warning("Facebook session missing or older than 7 days; skipping Facebook auto sources.")
        if str(state.get("run_mode", "publish") or "publish").strip().lower() == "publish":
            _send_telegram_alert("⚠️ Facebook session cũ hơn 7 ngày. Cần chạy scripts/facebook_login_setup.py")
        facebook_auto_enabled = False
    if facebook_auto_enabled:
        facebook_registry = _resolve_facebook_source_registry(state, headless=facebook_headless)
        filtered_auto_active, muted_sources = filter_discovered_sources_by_history(
            list(facebook_registry.get("auto_active_sources", []) or []),
            source_history_map,
            max_active=_facebook_discovery_max_active_sources(state),
        )
        existing_candidates = {
            str(source.get("url", "") or "").strip().lower(): dict(source)
            for source in list(facebook_registry.get("candidate_sources", []) or [])
            if isinstance(source, dict)
        }
        for source in muted_sources:
            existing_candidates[str(source.get("url", "") or "").strip().lower()] = source
        facebook_registry["auto_active_sources"] = filtered_auto_active
        facebook_registry["candidate_sources"] = list(existing_candidates.values())
    facebook_registry["discovered_sources"] = annotate_sources_with_history(
        list(facebook_registry.get("discovered_sources", []) or []),
        source_history_map,
    )
    facebook_registry["auto_active_sources"] = annotate_sources_with_history(
        list(facebook_registry.get("auto_active_sources", []) or []),
        source_history_map,
    )
    facebook_registry["candidate_sources"] = annotate_sources_with_history(
        list(facebook_registry.get("candidate_sources", []) or []),
        source_history_map,
    )

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

    facebook_auto_articles: list[dict[str, Any]] = []
    if facebook_auto_enabled:
        facebook_auto_articles = _build_facebook_auto_articles(
            state,
            targets=list(facebook_registry.get("auto_active_sources", []) or []),
        )
        if facebook_auto_articles:
            logger.info("📘 Facebook auto: %d bài", len(facebook_auto_articles))
            raw_articles.extend(facebook_auto_articles)
        else:
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

    raw_articles = _cap_github_article_ratio(raw_articles)
    deduped_raw_articles = _local_deduplicate_articles(raw_articles)
    deduped_raw_articles = annotate_articles_with_source_history(deduped_raw_articles, source_history_map)
    deduped_raw_articles, history_muted_count = _filter_history_muted_discoveries(deduped_raw_articles)
    gather_snapshot_path = write_temporal_snapshot(
        state=state,
        stage="gather",
        articles=deduped_raw_articles,
        extra={
            "raw_count_before_batch_dedup": len(raw_articles),
            "raw_count_after_batch_dedup": len(deduped_raw_articles),
            "grok_scout_count": len(grok_scout_articles) + len(grok_x_scout_articles),
            "history_muted_count": history_muted_count,
        },
    )
    logger.info(
        "✅ Gathered %d raw articles total (%d sau dedup trong batch)",
        len(raw_articles),
        len(deduped_raw_articles),
    )
    return {
        "raw_articles": deduped_raw_articles,
        "grok_scout_count": len(grok_scout_articles) + len(grok_x_scout_articles),
        "gather_snapshot_path": gather_snapshot_path,
    }
