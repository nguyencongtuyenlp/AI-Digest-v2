"""
run_health.py - Deterministic health checks for a digest run.

This gives the team a stable answer to:
- is this batch publish-ready?
- what is weak about the current source mix?
- should we trust this preview or hold it for review?
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Any

import requests

from digest.sources.source_catalog import OFFICIAL_SOURCE_DOMAINS, STRONG_MEDIA_DOMAINS
from digest.sources.source_registry import CURATED_RSS_FEEDS

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FACEBOOK_STORAGE_STATE_DEFAULT = PROJECT_ROOT / "config" / "facebook_storage_state.json"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _file_age_days(path: Path) -> float | None:
    try:
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None
    return max(0.0, (_utc_now() - modified).total_seconds() / 86400.0)


def _facebook_storage_state_path() -> Path:
    configured = str(os.getenv("FACEBOOK_STORAGE_STATE_FILE", "") or "").strip()
    if configured:
        path = Path(configured).expanduser()
        return path if path.is_absolute() else (PROJECT_ROOT / path)
    return FACEBOOK_STORAGE_STATE_DEFAULT


def _telethon_session_path() -> Path:
    session_name = str(os.getenv("TELETHON_SESSION_NAME", "digest_session") or "digest_session").strip()
    if not session_name.endswith(".session"):
        session_name = f"{session_name}.session"
    path = Path(session_name).expanduser()
    return path if path.is_absolute() else (PROJECT_ROOT / path)


def _request_indicates_live(status_code: int) -> bool:
    return status_code < 400 or status_code in {401, 403, 405, 406, 429}


def _ping_feed(feed_url: str) -> str:
    try:
        response = requests.head(feed_url, allow_redirects=True, timeout=5)
        if _request_indicates_live(response.status_code):
            return "ok"
    except Exception:
        pass

    try:
        response = requests.get(
            feed_url,
            allow_redirects=True,
            timeout=5,
            headers={"User-Agent": "AI-Digest/1.0"},
        )
        if _request_indicates_live(response.status_code):
            return "ok"
    except Exception:
        pass
    return "dead"


def collect_source_health() -> dict[str, str]:
    source_health: dict[str, str] = {}

    for feed_url in CURATED_RSS_FEEDS:
        source_health[feed_url] = _ping_feed(feed_url)

    telethon_session = _telethon_session_path()
    telethon_age = _file_age_days(telethon_session)
    telethon_enabled = all(
        bool(str(os.getenv(env_key, "") or "").strip())
        for env_key in ("TELEGRAM_CHANNELS", "TELETHON_API_ID", "TELETHON_API_HASH")
    )
    if not telethon_enabled:
        source_health["telethon_session"] = "optional"
    elif not telethon_session.exists():
        source_health["telethon_session"] = "dead"
    elif telethon_age is not None and telethon_age > 30:
        source_health["telethon_session"] = "stale"
    else:
        source_health["telethon_session"] = "ok"

    facebook_state = _facebook_storage_state_path()
    facebook_age = _file_age_days(facebook_state)
    facebook_enabled = str(os.getenv("ENABLE_FACEBOOK_AUTO", "0") or "0").strip().lower() in {"1", "true", "yes", "on"}
    if not facebook_enabled:
        source_health["facebook_storage_state"] = "optional"
    elif not facebook_state.exists():
        source_health["facebook_storage_state"] = "dead"
    elif facebook_age is not None and facebook_age > 7:
        source_health["facebook_storage_state"] = "stale"
    else:
        source_health["facebook_storage_state"] = "ok"

    return source_health


def notify_source_health_if_needed(source_health: dict[str, str], *, run_mode: str = "publish") -> bool:
    if str(run_mode or "").strip().lower() != "publish":
        return False

    dead_items = [name for name, status in dict(source_health or {}).items() if status == "dead"]
    if not dead_items:
        return False

    bot_token = str(os.getenv("TELEGRAM_BOT_TOKEN", "") or "").strip()
    chat_id = str(os.getenv("TELEGRAM_CHAT_ID", "") or "").strip()
    if not bot_token or not chat_id:
        return False

    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": "⚠️ Source health warning\n\n" + "\n".join(f"- {name}: dead" for name in dead_items[:12]),
        "disable_web_page_preview": True,
    }
    thread_id = str(os.getenv("TELEGRAM_THREAD_ID", "") or "").strip()
    if thread_id:
        try:
            payload["message_thread_id"] = int(thread_id)
        except ValueError:
            pass

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json=payload,
            timeout=10,
        )
        return response.status_code == 200
    except Exception:
        return False


def _domain_matches(domain: str, candidates: list[str]) -> bool:
    return any(domain == item or domain.endswith(f".{item}") for item in candidates)


def assess_run_health(state: dict[str, Any]) -> dict[str, Any]:
    raw_articles = [item for item in state.get("raw_articles", []) if isinstance(item, dict)]
    scored_articles = [item for item in state.get("scored_articles", []) if isinstance(item, dict)]
    telegram_candidates = [item for item in state.get("telegram_candidates", []) if isinstance(item, dict)]
    source_health = dict(state.get("source_health", {}) or {})
    summary_mode = str(state.get("summary_mode", "") or "unknown")
    summary_warnings = [str(item or "") for item in state.get("summary_warnings", []) or [] if str(item or "").strip()]

    issues: list[str] = []
    severity = 0

    def add_issue(message: str, level: str) -> None:
        nonlocal severity
        issues.append(message)
        severity = max(severity, 2 if level == "red" else 1)

    scored_domains = [str(article.get("source_domain", "") or "").strip().lower() for article in scored_articles]
    scored_domains = [domain for domain in scored_domains if domain]
    domain_counter = Counter(scored_domains)
    source_diversity = len(domain_counter)
    github_scored = sum(1 for domain in scored_domains if domain == "github.com")
    github_ratio = (github_scored / len(scored_domains)) if scored_domains else 0.0
    source_kind_counter = Counter(
        str(article.get("source_kind", "unknown") or "unknown").strip().lower()
        for article in scored_articles
        if isinstance(article, dict)
    )
    stale_scored = sum(1 for article in scored_articles if bool(article.get("is_old_news") or article.get("is_stale_candidate")))
    penalized_sources = sum(
        1
        for article in scored_articles
        if int(article.get("source_history_penalty", 0) or 0) >= 8
    )

    strong_main_candidates = 0
    official_main_candidates = 0
    for article in telegram_candidates:
        domain = str(article.get("source_domain", "") or "").strip().lower()
        tier = str(article.get("source_tier", "unknown") or "unknown").strip().lower()
        if tier in {"a", "b"}:
            strong_main_candidates += 1
        if _domain_matches(domain, OFFICIAL_SOURCE_DOMAINS):
            official_main_candidates += 1

    strong_scored = sum(
        1
        for article in scored_articles
        if str(article.get("source_tier", "unknown") or "unknown").strip().lower() in {"a", "b"}
    )
    dead_sources = sorted(name for name, status in source_health.items() if status == "dead")
    dead_feed_sources = [name for name in dead_sources if name.startswith("http")]
    stale_sources = sorted(name for name, status in source_health.items() if status == "stale")

    if not raw_articles:
        add_issue("Không gather được tín hiệu nào trong run này.", "red")
    if not scored_articles:
        add_issue("Không có bài nào đi qua lớp classify/scoring.", "red")
    if summary_mode == "safe_fallback":
        add_issue("Summary rơi vào safe_fallback; batch này chưa nên publish thật.", "red")
    if summary_warnings:
        add_issue("Summary còn warning từ quality gate.", "red")
    if not telegram_candidates:
        add_issue("Không có main Telegram candidate đủ mạnh.", "red")
    elif len(telegram_candidates) == 1:
        add_issue("Main brief hiện chỉ có 1 candidate mới.", "yellow")

    if scored_articles and source_diversity <= 2:
        add_issue("Độ đa dạng nguồn rất thấp; batch dễ bị lệch góc nhìn.", "yellow")
    if scored_articles and github_ratio >= 0.75:
        add_issue("Batch đang bị GitHub chi phối quá mạnh so với media/official sources.", "yellow")
    if scored_articles and strong_scored < min(3, max(1, len(scored_articles) // 3)):
        add_issue("Coverage từ nguồn tier A/B còn mỏng.", "yellow")
    if scored_articles and stale_scored >= max(3, len(scored_articles) // 4):
        add_issue("Batch còn khá nhiều bài có dấu hiệu cũ/stale trước khi vào brief.", "yellow")
    if scored_articles and penalized_sources >= max(3, len(scored_articles) // 5):
        add_issue("Nhiều bài đang đến từ các nguồn có lịch sử noise cao.", "yellow")
    if telegram_candidates and strong_main_candidates == 0:
        add_issue("Main candidates chưa có nguồn tier A/B đủ rõ.", "red")
    if telegram_candidates and official_main_candidates == 0:
        add_issue("Main brief chưa có candidate từ official source.", "yellow")
    if dead_feed_sources:
        add_issue("Có source health ở trạng thái dead trước khi pipeline chạy.", "yellow")
    if stale_sources:
        add_issue("Có session/source health ở trạng thái stale.", "yellow")

    status = "green"
    if severity >= 2:
        status = "red"
    elif severity == 1:
        status = "yellow"

    publish_ready = status == "green" or (
        status == "yellow"
        and bool(telegram_candidates)
        and not summary_warnings
        and summary_mode != "safe_fallback"
        and strong_main_candidates >= 1
    )

    return {
        "status": status,
        "publish_ready": publish_ready,
        "issues": issues,
        "metrics": {
            "raw_count": len(raw_articles),
            "scored_count": len(scored_articles),
            "main_candidate_count": len(telegram_candidates),
            "source_diversity": source_diversity,
            "github_ratio": round(github_ratio, 2),
            "dead_source_count": len(dead_sources),
            "stale_source_count": len(stale_sources),
            "strong_scored_count": strong_scored,
            "strong_main_candidate_count": strong_main_candidates,
            "official_main_candidate_count": official_main_candidates,
            "official_source_count": source_kind_counter.get("official", 0),
            "strong_media_source_count": source_kind_counter.get("strong_media", 0),
            "github_source_count": source_kind_counter.get("github", 0),
            "community_source_count": source_kind_counter.get("community", 0),
            "watchlist_source_count": source_kind_counter.get("watchlist", 0),
            "search_source_count": source_kind_counter.get("search", 0),
            "stale_scored_count": stale_scored,
            "source_history_penalized_count": penalized_sources,
        },
        "source_health": source_health,
    }
