from __future__ import annotations

import json
import logging
import re
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

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


def normalize_facebook_permalink(url: str) -> str:
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


def extract_facebook_permalink(links: list[str]) -> str:
    preferred_patterns = (
        "/posts/",
        "/groups/",
        "/permalink.php",
        "/share/p/",
        "/videos/",
    )
    normalized_links = [normalize_facebook_permalink(link) for link in links if str(link or "").strip()]
    for pattern in preferred_patterns:
        for link in normalized_links:
            if pattern == "/groups/" and "/posts/" not in link:
                continue
            if pattern in link:
                return link
    return normalized_links[0] if normalized_links else ""


def looks_like_interaction_line(text: str) -> bool:
    lowered = str(text or "").strip().lower()
    return lowered in {
        "thích",
        "bình luận",
        "chia sẻ",
        "like",
        "comment",
        "share",
    } or lowered.startswith("tất cả cảm xúc")


def is_loaded_facebook_payload(payload: dict[str, Any]) -> bool:
    text = str(payload.get("text", "") or "").strip()
    if not text:
        return False
    if FACEBOOK_ARTICLE_LOADING_RE.search(text):
        return False
    return True


def clean_facebook_lines(lines: list[str]) -> list[str]:
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


def detect_facebook_content_style(title: str, content: str) -> str:
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


def score_facebook_boss_style(title: str, content: str, *, content_style: str) -> int:
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


def score_facebook_authority(
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


def facebook_published_hint_rank(value: str) -> int:
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


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json_file(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def write_json_file(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def normalize_facebook_source_url(url: str) -> str:
    normalized = normalize_facebook_permalink(url)
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


def infer_facebook_source_type(url: str) -> str:
    normalized = normalize_facebook_source_url(url)
    if not normalized:
        return "group"
    lowered = normalized.lower()
    if "/groups/" in lowered:
        return "group"
    if "profile.php" in lowered:
        return "profile"
    return "profile"


def score_facebook_source(label: str, url: str, *, source_type: str, description: str = "") -> int:
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


def facebook_source_status(*, ai_source_score: int, discovery_origin: str) -> str:
    if discovery_origin == "manual":
        return "auto_active"
    if ai_source_score >= 70:
        return "auto_active"
    if ai_source_score >= 50:
        return "candidate"
    return "ignored"


def normalize_facebook_source_entry(entry: dict[str, Any]) -> dict[str, Any] | None:
    url = normalize_facebook_source_url(str(entry.get("url", "") or ""))
    label = str(entry.get("label", "") or "").strip()
    if not url or not label or FACEBOOK_GENERIC_LABEL_RE.fullmatch(label):
        return None
    source_type = str(entry.get("source_type", "") or "").strip().lower() or infer_facebook_source_type(url)
    discovery_origin = str(entry.get("discovery_origin", "") or "").strip().lower() or "manual"
    ai_source_score = int(entry.get("ai_source_score", 0) or score_facebook_source(label, url, source_type=source_type))
    status = str(entry.get("status", "") or "").strip().lower() or facebook_source_status(
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
        "last_seen_at": str(entry.get("last_seen_at", "") or utc_now_iso()),
        "last_crawled_at": str(entry.get("last_crawled_at", "") or ""),
    }


def load_facebook_auto_targets(target_file: Path, *, domain_from_url_fn: Callable[[str], str]) -> list[dict[str, str]]:
    if not target_file.exists():
        return []
    targets: list[dict[str, str]] = []
    try:
        lines = target_file.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split("|")]
        label = parts[0] if len(parts) >= 2 else ""
        url = parts[1] if len(parts) >= 2 else parts[0]
        source_type = parts[2].strip().lower() if len(parts) >= 3 else infer_facebook_source_type(url)
        if not url:
            continue
        entry = normalize_facebook_source_entry(
            {
                "label": label or domain_from_url_fn(url) or "Facebook target",
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


def facebook_discovery_cache_is_fresh(path: Path, *, refresh_hours: int) -> bool:
    if refresh_hours <= 0 or not path.exists():
        return False
    if path.stat().st_size <= 4:
        return False
    modified_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return (datetime.now(timezone.utc) - modified_at).total_seconds() <= refresh_hours * 3600


def load_facebook_discovery_cache(cache_file: Path) -> list[dict[str, Any]]:
    payload = read_json_file(cache_file)
    if isinstance(payload, dict):
        payload = payload.get("sources", [])
    if not isinstance(payload, list):
        return []
    items: list[dict[str, Any]] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        normalized = normalize_facebook_source_entry(entry)
        if normalized:
            items.append(normalized)
    return items


def save_facebook_discovery_cache(cache_file: Path, sources: list[dict[str, Any]]) -> None:
    write_json_file(cache_file, sources)


def clean_facebook_discovery_label(value: str) -> str:
    label = " ".join(str(value or "").split()).strip(" |·")
    label = re.sub(r"\s*Lần hoạt động gần nhất:.*$", "", label, flags=re.IGNORECASE).strip(" |·")
    if not label or len(label) < 3 or len(label) > 140:
        return ""
    if FACEBOOK_GENERIC_LABEL_RE.fullmatch(label):
        return ""
    return label


def extract_facebook_anchor_candidates(page: Any) -> list[dict[str, str]]:
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
                "label": clean_facebook_discovery_label(
                    str(item.get("text", "") or "")
                    or str(item.get("aria", "") or "")
                    or str(item.get("title", "") or "")
                ),
            }
        )
    return cleaned


def is_real_group_source_url(url: str) -> bool:
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


def is_profile_like_source_url(url: str) -> bool:
    normalized = normalize_facebook_source_url(url)
    if not normalized:
        return False
    parsed = urlparse(normalized)
    if parsed.path == "/profile.php":
        return True
    segments = [segment for segment in parsed.path.strip("/").split("/") if segment]
    if len(segments) != 1:
        return False
    return segments[0].lower() not in FACEBOOK_PROFILE_RESERVED_SEGMENTS


def merge_facebook_source_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for entry in entries:
        normalized = normalize_facebook_source_entry(entry)
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


def discover_group_sources(page: Any) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for url in ("https://www.facebook.com/groups/feed/", "https://www.facebook.com/groups/"):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2500)
        except Exception:
            continue
        for item in extract_facebook_anchor_candidates(page):
            href = normalize_facebook_source_url(item.get("href", ""))
            if not href or not is_real_group_source_url(href):
                continue
            label = item.get("label", "")
            score = score_facebook_source(label, href, source_type="group")
            entries.append(
                {
                    "label": label,
                    "url": href,
                    "source_type": "group",
                    "discovery_origin": "joined",
                    "ai_source_score": score,
                    "status": facebook_source_status(ai_source_score=score, discovery_origin="joined"),
                    "last_seen_at": utc_now_iso(),
                    "last_crawled_at": "",
                }
            )
    return merge_facebook_source_entries(entries)


def discover_followed_page_sources(page: Any) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    try:
        page.goto("https://www.facebook.com/pages/?category=liked&ref=bookmarks", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2500)
    except Exception:
        return []
    for item in extract_facebook_anchor_candidates(page):
        href = normalize_facebook_source_url(item.get("href", ""))
        label = item.get("label", "")
        if not href or not label or "/groups/" in href.lower():
            continue
        if not is_profile_like_source_url(href):
            continue
        score = score_facebook_source(label, href, source_type="page")
        entries.append(
            {
                "label": label,
                "url": href,
                "source_type": "page",
                "discovery_origin": "followed",
                "ai_source_score": score,
                "status": facebook_source_status(ai_source_score=score, discovery_origin="followed"),
                "last_seen_at": utc_now_iso(),
                "last_crawled_at": "",
            }
        )
    return merge_facebook_source_entries(entries)


def discover_followed_profile_sources(page: Any) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for url in ("https://www.facebook.com/following/", "https://www.facebook.com/bookmarks/"):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2500)
        except Exception:
            continue
        for item in extract_facebook_anchor_candidates(page):
            href = normalize_facebook_source_url(item.get("href", ""))
            label = item.get("label", "")
            if not href or not label or not is_profile_like_source_url(href):
                continue
            score = score_facebook_source(label, href, source_type="profile")
            if score < 50:
                continue
            entries.append(
                {
                    "label": label,
                    "url": href,
                    "source_type": "profile",
                    "discovery_origin": "followed",
                    "ai_source_score": score,
                    "status": facebook_source_status(ai_source_score=score, discovery_origin="followed"),
                    "last_seen_at": utc_now_iso(),
                    "last_crawled_at": "",
                }
            )
    return merge_facebook_source_entries(entries)


def refresh_facebook_discovery_cache(
    *,
    headless: bool,
    allow_profiles: bool,
    chrome_executable: str,
    storage_state_file: Path,
    logger: logging.Logger,
) -> list[dict[str, Any]]:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        logger.warning("Playwright unavailable for Facebook discovery: %s", exc)
        return []
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
                discovered.extend(discover_group_sources(page))
                discovered.extend(discover_followed_page_sources(page))
                if allow_profiles:
                    discovered.extend(discover_followed_profile_sources(page))
                return merge_facebook_source_entries(discovered)
            finally:
                with suppress(Exception):
                    context.close()
                with suppress(Exception):
                    browser.close()
    except Exception as exc:
        logger.warning("Facebook discovery failed: %s", exc)
        return []


def resolve_facebook_source_registry(
    *,
    manual_sources: list[dict[str, Any]],
    discovery_enabled: bool,
    cache_file: Path,
    refresh_hours: int,
    allow_profiles: bool,
    max_candidates_per_run: int,
    max_active_sources: int,
    headless: bool,
    load_cache_fn: Callable[[], list[dict[str, Any]]],
    refresh_cache_fn: Callable[..., list[dict[str, Any]]],
    save_cache_fn: Callable[[list[dict[str, Any]]], None],
) -> dict[str, list[dict[str, Any]]]:
    discovered_sources: list[dict[str, Any]] = []
    auto_active_sources: list[dict[str, Any]] = list(manual_sources)
    candidate_sources: list[dict[str, Any]] = []
    if discovery_enabled:
        if facebook_discovery_cache_is_fresh(cache_file, refresh_hours=refresh_hours):
            discovered_sources = load_cache_fn()
        else:
            discovered_sources = refresh_cache_fn(headless=headless, allow_profiles=allow_profiles)
            if discovered_sources:
                save_cache_fn(discovered_sources)
        discovered_sources = merge_facebook_source_entries(discovered_sources)
        candidate_sources = [
            source for source in discovered_sources
            if str(source.get("status", "") or "").lower() == "candidate"
        ][: max_candidates_per_run]
        discovered_auto_active = [
            source for source in discovered_sources
            if str(source.get("status", "") or "").lower() == "auto_active"
        ]
        remaining_slots = max(0, max_active_sources - len(manual_sources))
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


def try_set_facebook_newest_first(page: Any) -> str:
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


def scrape_facebook_target_payloads(
    *,
    target: dict[str, str],
    posts_per_target: int,
    headless: bool,
    force_newest_first: bool,
    chrome_executable: str,
    storage_state_file: Path,
    logger: logging.Logger,
) -> list[dict[str, Any]]:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        logger.warning("Playwright unavailable for Facebook auto adapter: %s", exc)
        return []
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
                page.wait_for_timeout(4500)
                if "/login" in page.url or page.locator('input[name="email"]').count() > 0:
                    logger.warning("Facebook auto target requires login session: %s", target.get("url", ""))
                    return []
                sort_mode = "default_fallback"
                if force_newest_first:
                    sort_mode = try_set_facebook_newest_first(page)
                desired_payloads = max(4, posts_per_target * 3)
                for scroll_round in range(12):
                    raw_payloads = page.locator('div[role="feed"] > div').evaluate_all(
                        """(nodes) => nodes.map((node) => {
                            const article = node.querySelector('div[role="article"]');
                            if (!article) return null;
                            const text = (article.innerText || "").trim();
                            const links = Array.from(article.querySelectorAll('a[href]'))
                              .map((a) => a.href)
                              .filter(Boolean);
                            return { text, links };
                        }).filter(Boolean)"""
                    )
                    if not raw_payloads:
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
                        if isinstance(payload, dict) and is_loaded_facebook_payload(payload)
                    ]
                    if len(payloads) >= desired_payloads:
                        break
                    page.mouse.wheel(0, 3000)
                    page.wait_for_timeout(2000)
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


def build_facebook_auto_article(
    payload: dict[str, Any],
    *,
    target: dict[str, str],
    is_founder_grade_candidate_fn: Callable[[str, str, str, str], bool],
    truncate_text_fn: Callable[[str, int], str],
) -> dict[str, Any] | None:
    text = str(payload.get("text", "") or "").strip()
    links = [str(item).strip() for item in payload.get("links", []) if str(item).strip()]
    if not text or len(text) < 60:
        return None
    if FACEBOOK_ARTICLE_SKIP_RE.search(text):
        return None
    lines = clean_facebook_lines([line.strip() for line in text.splitlines() if line.strip()])
    if not lines:
        return None
    author = lines[0]
    published_label = ""
    content_lines: list[str] = []
    for line in lines[1:]:
        if looks_like_interaction_line(line):
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
    if not content_text or len(content_text) < 50:
        return None
    ai_source_score = int(target.get("ai_source_score", 0) or 0)
    is_known_ai_source = ai_source_score >= 60 or str(target.get("discovery_origin", "") or "").lower() == "manual"
    if not is_known_ai_source:
        if not is_founder_grade_candidate_fn(content_text[:180], content_text[:600], " ".join(links), target.get("label", "")):
            return None
    permalink = extract_facebook_permalink(links)
    if not permalink:
        return None
    title = truncate_text_fn(content_lines[0] if content_lines else content_text, 160)
    snippet = truncate_text_fn(content_text.replace("\n", " "), 500)
    content = truncate_text_fn(content_text, 4000)
    source_type = str(target.get("source_type", "") or infer_facebook_source_type(str(target.get("url", "") or ""))).strip().lower() or "group"
    discovery_origin = str(target.get("discovery_origin", "") or "manual").strip().lower()
    content_style = detect_facebook_content_style(title, content)
    boss_style_score = score_facebook_boss_style(title, content, content_style=content_style)
    authority_score = score_facebook_authority(target=target, author=author, source_type=source_type)
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


def build_facebook_auto_articles(
    state: dict[str, Any],
    *,
    targets: list[dict[str, Any]] | None,
    auto_enabled: bool,
    load_targets_fn: Callable[[], list[dict[str, Any]]],
    cfg_int_fn: Callable[[dict[str, Any], str, str, int], int],
    cfg_bool_fn: Callable[[dict[str, Any], str, str, bool], bool],
    force_newest_first_fn: Callable[[dict[str, Any]], bool],
    scrape_payloads_fn: Callable[..., list[dict[str, Any]]],
    build_article_fn: Callable[..., dict[str, Any] | None],
    logger: logging.Logger,
    published_hint_rank_fn: Callable[[str], int] = facebook_published_hint_rank,
) -> list[dict[str, Any]]:
    if not auto_enabled:
        return []
    resolved_targets = list(targets or load_targets_fn())
    if not resolved_targets:
        logger.info("⏭️ Facebook auto bật nhưng chưa có target nào.")
        return []
    posts_per_target = cfg_int_fn(state, "facebook_auto_posts_per_target", "FACEBOOK_AUTO_POSTS_PER_TARGET", 2)
    headless = cfg_bool_fn(state, "facebook_auto_headless", "FACEBOOK_AUTO_HEADLESS", True)
    max_targets = cfg_int_fn(state, "facebook_auto_max_targets", "FACEBOOK_AUTO_MAX_TARGETS", 4)
    force_newest_first = force_newest_first_fn(state)
    articles: list[dict[str, Any]] = []
    for target in resolved_targets[:max_targets]:
        payloads = scrape_payloads_fn(
            target=target,
            posts_per_target=posts_per_target,
            headless=headless,
            force_newest_first=force_newest_first,
        )
        target_articles: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        for payload in payloads:
            article = build_article_fn(payload, target=target)
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
                published_hint_rank_fn(
                    str(article.get("published_hint_raw", article.get("published_hint", "")) or "")
                ),
                int(article.get("facebook_boss_style_score", 0) or 0),
                len(str(article.get("content", "") or "")),
            ),
            reverse=True,
        )[:posts_per_target]
        logger.info("   Facebook auto [%s]: %d bài", target.get("label", ""), len(target_articles))
        target["last_crawled_at"] = utc_now_iso()
        articles.extend(target_articles)
    return articles
