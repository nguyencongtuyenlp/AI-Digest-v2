"""
run_health.py - Deterministic health checks for a digest run.

This gives the team a stable answer to:
- is this batch publish-ready?
- what is weak about the current source mix?
- should we trust this preview or hold it for review?
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from source_catalog import OFFICIAL_SOURCE_DOMAINS, STRONG_MEDIA_DOMAINS


def _domain_matches(domain: str, candidates: list[str]) -> bool:
    return any(domain == item or domain.endswith(f".{item}") for item in candidates)


def assess_run_health(state: dict[str, Any]) -> dict[str, Any]:
    raw_articles = [item for item in state.get("raw_articles", []) if isinstance(item, dict)]
    scored_articles = [item for item in state.get("scored_articles", []) if isinstance(item, dict)]
    telegram_candidates = [item for item in state.get("telegram_candidates", []) if isinstance(item, dict)]
    github_topic_candidates = [item for item in state.get("github_topic_candidates", []) if isinstance(item, dict)]
    facebook_topic_candidates = [item for item in state.get("facebook_topic_candidates", []) if isinstance(item, dict)]
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
    if telegram_candidates and strong_main_candidates == 0:
        add_issue("Main candidates chưa có nguồn tier A/B đủ rõ.", "red")
    if telegram_candidates and official_main_candidates == 0:
        add_issue("Main brief chưa có candidate từ official source.", "yellow")
    if telegram_candidates and github_ratio >= 0.6 and not github_topic_candidates:
        add_issue("GitHub signals nhiều nhưng lane GitHub riêng chưa tạo được điểm nhấn.", "yellow")

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
            "github_candidate_count": len(github_topic_candidates),
            "facebook_candidate_count": len(facebook_topic_candidates),
            "source_diversity": source_diversity,
            "github_ratio": round(github_ratio, 2),
            "strong_scored_count": strong_scored,
            "strong_main_candidate_count": strong_main_candidates,
            "official_main_candidate_count": official_main_candidates,
            "official_source_count": source_kind_counter.get("official", 0),
            "strong_media_source_count": source_kind_counter.get("strong_media", 0),
            "github_source_count": source_kind_counter.get("github", 0),
            "community_source_count": source_kind_counter.get("community", 0),
            "watchlist_source_count": source_kind_counter.get("watchlist", 0),
            "search_source_count": source_kind_counter.get("search", 0),
        },
    }
