"""
delivery_policy.py - Shared deterministic routing policy for main brief selection.

This module is the single source of truth for:
- canonical skip reasons
- delivery lane routing
- main brief eligibility
- source-aware operator/novelty heuristics
"""

from __future__ import annotations

from typing import Any

from digest.editorial.digest_formatter import canonical_type_name

DELIVERY_LANE_CANDIDATES: tuple[str, ...] = ("main", "github", "facebook", "archive_only")
MAIN_BRIEF_ELIGIBILITY_STATES: tuple[str, ...] = ("eligible", "review", "ineligible")
CANONICAL_SKIP_REASONS: tuple[str, ...] = (
    "duplicate_event",
    "weak_signal",
    "stale",
    "not_ai",
    "promo",
    "speculation",
    "github_topic_only",
    "low_operator_value",
)

MAIN_BRIEF_FRESH_BUCKETS = {"breaking", "fresh", "recent", "fresh_boost"}
MAIN_BRIEF_ACTIONABLE_TAGS = {
    "model_release",
    "product_update",
    "api_platform",
    "developer_tools",
    "ai_agents",
    "enterprise_ai",
    "infrastructure",
    "funding",
    "partnership",
    "acquisition",
    "regulation",
    "safety",
    "government",
}
MAIN_BRIEF_GITHUB_SIGNIFICANCE_TAGS = {
    "model_release",
    "product_update",
    "api_platform",
    "developer_tools",
    "ai_agents",
    "enterprise_ai",
    "infrastructure",
    "open_source",
}
MAIN_BRIEF_ECOSYSTEM_IMPLICATION_TAGS = {
    "product_update",
    "api_platform",
    "enterprise_ai",
    "infrastructure",
    "developer_tools",
    "ai_agents",
    "model_release",
    "regulation",
    "safety",
    "government",
    "partnership",
    "acquisition",
    "funding",
}
MAIN_BRIEF_GITHUB_ADOPTION_HINTS = (
    "adoption",
    "adopted",
    "used by",
    "integrated",
    "integration",
    "sdk",
    "api",
    "enterprise",
    "production",
    "deploy",
    "deployment",
    "mcp",
    "agent sdk",
    "ecosystem",
)
MAIN_BRIEF_SOCIETY_ECOSYSTEM_HINTS = (
    "enterprise",
    "developers",
    "developer",
    "api",
    "sdk",
    "platform",
    "ecosystem",
    "distribution",
    "compliance",
    "security",
    "infrastructure",
    "pricing",
    "deployment",
    "model access",
    "operator",
    "workflow",
)
MAIN_BRIEF_SOCIETY_FILLER_HINTS = (
    "personal reflection",
    "personal reflections",
    "my thoughts",
    "thoughts on",
    "what i learned",
    "lessons learned",
    "opinion",
    "essay",
    "journal",
    "reflections on",
    "musings",
    "blog post",
)
MAIN_BRIEF_SOCIETY_HIGH_CONSEQUENCE_TAGS = {
    "regulation",
    "safety",
    "government",
    "infrastructure",
    "enterprise_ai",
}
MAIN_BRIEF_SOCIETY_HIGH_CONSEQUENCE_HINTS = (
    "regulation",
    "policy",
    "compliance",
    "licensing",
    "export controls",
    "safety",
    "alignment",
    "security",
    "breach",
    "compute",
    "datacenter",
    "data center",
    "infrastructure",
    "power grid",
    "chip export",
    "foundation model",
)
MAIN_BRIEF_PROXY_SOURCE_TAGS = {
    "product_update",
    "api_platform",
    "model_release",
    "enterprise_ai",
    "funding",
    "partnership",
    "acquisition",
}
MAIN_BRIEF_WORKFLOW_HINTS = (
    "workflow",
    "workflow automation",
    "agent workflow",
    "orchestration",
    "orchestrator",
    "handoff",
    "handoffs",
    "multi-step workflow",
    "multi step workflow",
    "review loop",
    "approval loop",
    "human-in-the-loop",
    "human in the loop",
    "human review",
    "tool use",
    "tool-use",
)
MAIN_BRIEF_HEALTHCARE_HINTS = (
    "clinic",
    "clinical",
    "healthcare",
    "medical",
    "patient workflow",
    "patient support",
    "patient scheduling",
    "scheduling",
    "appointment",
    "intake",
    "triage",
)
MAIN_BRIEF_OPERATIONS_HINTS = (
    "operations",
    "system automation",
    "monitoring",
    "observability",
    "reliability",
    "incident response",
    "runbook",
    "guardrails",
    "eval",
    "evaluation",
)
MAIN_BRIEF_DEPLOYMENT_HINTS = (
    "simulation",
    "simulator",
    "scenario",
    "scenario-based",
    "deployment",
    "deployable",
    "local deployment",
    "private deployment",
    "local-first",
    "local first",
    "on-device",
    "on device",
    "edge deployment",
    "self-hosted",
    "self hosted",
    "cost-aware deployment",
    "cost-aware",
)
MAIN_BRIEF_OPERATOR_HINTS = tuple(
    dict.fromkeys(
        (
            "api",
            "sdk",
            "platform",
            "developer",
            "developers",
            "enterprise",
            "integration",
            "workflow",
            "deployment",
            "pricing",
            "security",
            "compliance",
            "infrastructure",
            "benchmark",
            "agent",
            "agents",
            "model",
            "release",
            "launch",
            "rollout",
        )
        + MAIN_BRIEF_WORKFLOW_HINTS
        + MAIN_BRIEF_HEALTHCARE_HINTS
        + MAIN_BRIEF_OPERATIONS_HINTS
        + MAIN_BRIEF_DEPLOYMENT_HINTS
    )
)
MAIN_BRIEF_NOVELTY_HINTS = (
    "release",
    "released",
    "launch",
    "launched",
    "announce",
    "announced",
    "introduces",
    "introduced",
    "new model",
    "new api",
    "new sdk",
    "general availability",
    "ga",
    "preview",
    "public beta",
    "open source",
    "open-source",
    "pricing",
    "partner",
    "partners with",
    "acquisition",
    "funding",
)
MAIN_BRIEF_PROMO_HINTS = (
    "register now",
    "get your ticket",
    "tickets are limited",
    "webinar",
    "conference",
    "summit",
    "join us",
    "event",
)
MAIN_BRIEF_SPECULATION_HINTS = (
    "rumor",
    "rumour",
    "speculation",
    "unverified",
    "what could go wrong",
    "might",
    "could",
    "reportedly",
    "leak",
)
MAIN_BRIEF_GITHUB_NOISE_HINTS = (
    "tutorial",
    "guide",
    "boilerplate",
    "starter kit",
    "starter",
    "template",
    "templates",
    "prompt pack",
    "prompt-pack",
    "plugin",
    "plugins",
    "workflow kit",
    "example",
    "examples",
    "awesome list",
    "collection of",
)
MAIN_BRIEF_MAJOR_GITHUB_OWNERS = {
    "openai",
    "anthropics",
    "langchain-ai",
    "huggingface",
    "microsoft",
    "google",
    "google-deepmind",
    "deepmind",
    "meta",
    "facebookresearch",
    "nvidia",
    "vercel",
}


def is_github_signal_article(article: dict[str, Any]) -> bool:
    source_domain = str(article.get("source_domain", "") or "").strip().lower()
    return (
        source_domain == "github.com"
        or bool(str(article.get("github_full_name", "") or "").strip())
        or str(article.get("github_signal_type", "") or "").strip().lower() in {"repository", "release"}
    )


def source_quality_rank(article: dict[str, Any]) -> int:
    source_kind = str(article.get("source_kind", "") or "").strip().lower()
    return {
        "official": 6,
        "watchlist": 5,
        "strong_media": 4,
        "regional_media": 3,
        "manual": 2,
        "review": 2,
        "github": 1,
        "community": 0,
    }.get(source_kind, 1)


def is_preferred_main_brief_source(article: dict[str, Any]) -> bool:
    return is_official_or_strong_source(article)


def is_official_or_strong_source(article: dict[str, Any]) -> bool:
    return str(article.get("source_kind", "") or "").strip().lower() in {"official", "strong_media"}


def project_fit_bucket(article: dict[str, Any]) -> str:
    explicit = str(article.get("project_fit", "") or "").strip().lower()
    if explicit in {"high", "medium", "low"}:
        return explicit
    relevance = str(article.get("relevance_level", "") or "").strip().lower()
    if relevance in {"high", "medium", "low"}:
        return relevance
    return "low"


def canonical_skip_reason(reason: str) -> str:
    normalized = str(reason or "").strip().lower()
    if not normalized:
        return ""

    reason_map = {
        "old": "stale",
        "aging": "stale",
        "duplicate": "duplicate_event",
        "thin": "weak_signal",
        "unknown_time": "weak_signal",
        "filtered_out": "weak_signal",
        "review_threshold": "weak_signal",
    }
    normalized = reason_map.get(normalized, normalized)
    if normalized in CANONICAL_SKIP_REASONS:
        return normalized
    return "weak_signal"


def _article_signal_text(article: dict[str, Any]) -> str:
    fields = [
        article.get("title", ""),
        article.get("summary_vi", ""),
        article.get("editorial_angle", ""),
        article.get("snippet", ""),
        article.get("content", ""),
        article.get("source", ""),
        article.get("source_domain", ""),
    ]
    return " ".join(str(field or "") for field in fields).lower()


def _article_tag_set(article: dict[str, Any]) -> set[str]:
    return {
        str(tag or "").strip().lower()
        for tag in article.get("tags", []) or []
        if str(tag or "").strip()
    }


def _count_keyword_hits(text: str, keywords: tuple[str, ...]) -> int:
    return sum(1 for keyword in keywords if keyword and keyword in text)


def _is_society_article(article: dict[str, Any]) -> bool:
    return canonical_type_name(article.get("primary_type")) == "Society & Culture"


def _ecosystem_implication_strength(article: dict[str, Any], text: str, tags: set[str]) -> int:
    strength = len(tags & MAIN_BRIEF_ECOSYSTEM_IMPLICATION_TAGS)
    strength += min(3, _count_keyword_hits(text, MAIN_BRIEF_SOCIETY_ECOSYSTEM_HINTS))
    if project_fit_bucket(article) == "high":
        strength += 1
    return strength


def _society_high_consequence_strength(article: dict[str, Any], text: str, tags: set[str]) -> int:
    strength = len(tags & MAIN_BRIEF_SOCIETY_HIGH_CONSEQUENCE_TAGS)
    strength += min(3, _count_keyword_hits(text, MAIN_BRIEF_SOCIETY_HIGH_CONSEQUENCE_HINTS))
    if is_official_or_strong_source(article):
        strength += 1
    return strength


def _github_owner(article: dict[str, Any]) -> str:
    full_name = str(article.get("github_full_name", "") or "").strip().lower()
    if "/" not in full_name:
        return ""
    return full_name.split("/", 1)[0].strip()


def has_real_ai_ecosystem_impact(
    article: dict[str, Any],
    *,
    text: str | None = None,
    tags: set[str] | None = None,
) -> bool:
    combined_text = text or _article_signal_text(article)
    article_tags = tags or _article_tag_set(article)
    operator_strength = operator_signal_strength(article, combined_text, article_tags)
    ecosystem_strength = _ecosystem_implication_strength(article, combined_text, article_tags)
    consequence_strength = _society_high_consequence_strength(article, combined_text, article_tags)
    return ecosystem_strength >= 3 or operator_strength >= 5 or consequence_strength >= 2


def _is_society_filler(article: dict[str, Any], text: str, tags: set[str]) -> bool:
    if not _is_society_article(article):
        return False
    if tags & MAIN_BRIEF_ECOSYSTEM_IMPLICATION_TAGS:
        return False
    return _count_keyword_hits(text, MAIN_BRIEF_SOCIETY_FILLER_HINTS) > 0


def is_weak_society_item(
    article: dict[str, Any],
    *,
    text: str | None = None,
    tags: set[str] | None = None,
) -> bool:
    if not _is_society_article(article):
        return False

    combined_text = text or _article_signal_text(article)
    article_tags = tags or _article_tag_set(article)
    if has_real_ai_ecosystem_impact(article, text=combined_text, tags=article_tags):
        return False

    if _is_society_filler(article, combined_text, article_tags):
        return True

    operator_strength = operator_signal_strength(article, combined_text, article_tags)
    consequence_strength = _society_high_consequence_strength(article, combined_text, article_tags)
    return operator_strength < 5 and consequence_strength < 2


def _regional_proxy_penalty(article: dict[str, Any], text: str, tags: set[str]) -> int:
    if str(article.get("source_kind", "") or "").strip().lower() != "regional_media":
        return 0

    proxy_signal = len(tags & MAIN_BRIEF_PROXY_SOURCE_TAGS)
    proxy_signal += min(2, _count_keyword_hits(text, MAIN_BRIEF_NOVELTY_HINTS))
    if proxy_signal <= 0:
        return 0

    penalty = 4
    if not bool(article.get("source_verified", False)):
        penalty += 1
    return penalty


def is_main_brief_fresh(article: dict[str, Any]) -> bool:
    freshness_bucket = str(article.get("freshness_bucket", "unknown") or "unknown").lower()
    if freshness_bucket in MAIN_BRIEF_FRESH_BUCKETS:
        return True
    age_hours = article.get("age_hours")
    return isinstance(age_hours, (int, float)) and age_hours <= 72


def is_github_main_brief_significant(
    article: dict[str, Any],
    *,
    text: str | None = None,
    tags: set[str] | None = None,
) -> bool:
    if not is_github_signal_article(article):
        return False

    combined_text = text or _article_signal_text(article)
    article_tags = tags or _article_tag_set(article)
    github_signal_type = str(article.get("github_signal_type", "") or "").strip().lower()
    tag_hits = len(article_tags & MAIN_BRIEF_GITHUB_SIGNIFICANCE_TAGS)
    novelty_hits = _count_keyword_hits(combined_text, MAIN_BRIEF_NOVELTY_HINTS)
    operator_hits = _count_keyword_hits(combined_text, MAIN_BRIEF_OPERATOR_HINTS)
    adoption_hits = _count_keyword_hits(combined_text, MAIN_BRIEF_GITHUB_ADOPTION_HINTS)
    noise_hits = _count_keyword_hits(combined_text, MAIN_BRIEF_GITHUB_NOISE_HINTS)
    stars = int(article.get("github_stars", 0) or 0)
    owner = _github_owner(article)
    major_owner = owner in MAIN_BRIEF_MAJOR_GITHUB_OWNERS
    ecosystem_strength = 0
    if stars >= 5000:
        ecosystem_strength += 2
    elif stars >= 1500:
        ecosystem_strength += 1
    if bool(article.get("watchlist_hit", False)):
        ecosystem_strength += 1
    if int(article.get("event_source_count", 1) or 1) >= 2:
        ecosystem_strength += 1
    ecosystem_strength += min(2, adoption_hits)
    if major_owner:
        ecosystem_strength += 1

    if noise_hits > 0 and adoption_hits == 0 and stars < 8000:
        return False

    real_builder_value = tag_hits >= 2 and operator_hits >= 4
    adoption_signal = adoption_hits >= 2 or ecosystem_strength >= 4
    official_release_signal = github_signal_type == "release" and major_owner

    if official_release_signal and real_builder_value and (novelty_hits >= 1 or adoption_signal):
        return True

    return real_builder_value and adoption_signal and novelty_hits >= 1


def operator_signal_strength(article: dict[str, Any], text: str, tags: set[str]) -> int:
    strength = len(tags & MAIN_BRIEF_ACTIONABLE_TAGS)
    strength += min(3, _count_keyword_hits(text, MAIN_BRIEF_OPERATOR_HINTS))

    fit_bucket = project_fit_bucket(article)
    if fit_bucket == "high":
        strength += 2
    elif fit_bucket == "medium":
        strength += 1

    if bool(article.get("content_available", False)):
        strength += 1
    if bool(article.get("source_verified", False)):
        strength += 1
    return strength


def novelty_signal_strength(article: dict[str, Any], text: str) -> int:
    strength = min(3, _count_keyword_hits(text, MAIN_BRIEF_NOVELTY_HINTS))
    if is_main_brief_fresh(article):
        strength += 1
    if bool(article.get("watchlist_hit", False)):
        strength += 1
    if str(article.get("github_signal_type", "") or "").strip().lower() == "release":
        strength += 1
    return strength


def apply_main_brief_routing(article: dict[str, Any]) -> None:
    source_kind = str(article.get("source_kind", "unknown") or "unknown").lower()
    score = int(article.get("total_score", 0) or 0)
    is_ai_relevant = article.get("is_ai_relevant", True) is not False
    freshness_unknown = bool(article.get("freshness_unknown", False))
    is_stale_candidate = bool(article.get("is_stale_candidate", False))
    is_old_news = bool(article.get("is_old_news", False))
    content_available = bool(article.get("content_available", False))
    delivery_lane_hint = str(article.get("delivery_lane_hint", "") or "").strip().lower()
    text = _article_signal_text(article)
    tags = _article_tag_set(article)
    fit_bucket = project_fit_bucket(article)
    fresh_signal = is_main_brief_fresh(article) and not is_stale_candidate and not is_old_news
    github_signal = is_github_signal_article(article)
    github_significant = is_github_main_brief_significant(article, text=text, tags=tags)
    operator_strength = operator_signal_strength(article, text, tags)
    novelty_strength = novelty_signal_strength(article, text)
    ecosystem_strength = _ecosystem_implication_strength(article, text, tags)
    consequence_strength = _society_high_consequence_strength(article, text, tags)
    society_article = _is_society_article(article)
    society_filler = _is_society_filler(article, text, tags)
    weak_society_item = is_weak_society_item(article, text=text, tags=tags)
    real_ecosystem_impact = has_real_ai_ecosystem_impact(article, text=text, tags=tags)
    regional_proxy_penalty = _regional_proxy_penalty(article, text, tags)

    route_reason = ""
    lane = "main"
    eligibility = "review"
    reason_codes: list[str] = [f"source_kind:{source_kind}"]
    main_brief_score = score

    if fresh_signal:
        main_brief_score += 4
        reason_codes.append("fresh_signal")
    if fit_bucket == "high":
        main_brief_score += 3
        reason_codes.append("fit:high")
    elif fit_bucket == "medium":
        main_brief_score += 1
        reason_codes.append("fit:medium")
    else:
        main_brief_score -= 2
        reason_codes.append("fit:low")

    if operator_strength >= 6:
        main_brief_score += 6
        reason_codes.append("operator_value:strong")
    elif operator_strength >= 3:
        main_brief_score += 2
        reason_codes.append("operator_value:ok")
    else:
        main_brief_score -= 6
        reason_codes.append("operator_value:low")

    if novelty_strength >= 4:
        main_brief_score += 4
        reason_codes.append("novelty:strong")
    elif novelty_strength >= 2:
        main_brief_score += 1
        reason_codes.append("novelty:ok")

    if freshness_unknown:
        main_brief_score -= 3
        reason_codes.append("time:unknown")
    if not content_available:
        main_brief_score -= 4
        reason_codes.append("content:thin")
    social_platform = str(article.get("social_platform", "") or "").strip().lower()
    community_proof = bool(article.get("social_signal") or str(article.get("community_reactions", "") or "").strip())
    social_operator_pattern = social_platform == "facebook" and any(
        marker in text for marker in ("benchmark", "workflow", "mcp", "claude code", "chi phí", "cost")
    )
    if fit_bucket == "high" and content_available and (community_proof or social_operator_pattern):
        main_brief_score += 4
        reason_codes.append("community_proof:strong")

    if source_kind in {"official", "watchlist"}:
        main_brief_score += 6
        reason_codes.append("source_advantage:official")
    elif source_kind == "strong_media":
        main_brief_score += 4
        reason_codes.append("source_advantage:strong_media")
    elif source_kind == "regional_media":
        main_brief_score -= 2
        reason_codes.append("source_advantage:regional")
        if regional_proxy_penalty:
            main_brief_score -= regional_proxy_penalty + 2
            reason_codes.append("source_penalty:proxy")
            reason_codes.append("source_penalty:proxy_strict")
    elif source_kind == "github":
        if github_significant:
            main_brief_score = min(main_brief_score + 2, 76)
            reason_codes.append("github_significant")
        else:
            main_brief_score = min(main_brief_score, 44)
            reason_codes.append("github_capped")
            reason_codes.append("github_low_impact")
    elif source_kind == "community":
        community_cap = 56 if operator_strength >= 4 and novelty_strength >= 3 else 44
        main_brief_score = min(main_brief_score, community_cap)
        reason_codes.append("community_capped")

    promo_detected = any(keyword in text for keyword in MAIN_BRIEF_PROMO_HINTS)
    speculation_detected = source_kind in {"community", "github"} and any(
        keyword in text for keyword in MAIN_BRIEF_SPECULATION_HINTS
    )

    if delivery_lane_hint == "facebook_topic":
        lane = "facebook"
        eligibility = "review"
        route_reason = "low_operator_value"
        reason_codes.append("lane_hint:facebook_topic")
    elif not is_ai_relevant:
        lane = "archive_only"
        eligibility = "ineligible"
        route_reason = "not_ai"
    elif is_stale_candidate or is_old_news:
        lane = "archive_only"
        eligibility = "ineligible"
        route_reason = "stale"
    elif promo_detected:
        lane = "archive_only"
        eligibility = "ineligible"
        route_reason = "promo"
    elif speculation_detected:
        lane = "archive_only"
        eligibility = "ineligible"
        route_reason = "speculation"
    elif weak_society_item:
        lane = "archive_only"
        eligibility = "ineligible"
        route_reason = "weak_signal"
        reason_codes.append("society_filler" if society_filler else "society_low_consequence")
    elif society_article:
        if consequence_strength >= 2:
            reason_codes.append("society_high_consequence")
        if ecosystem_strength >= 2 or real_ecosystem_impact:
            reason_codes.append("society_ecosystem_implication")
        else:
            lane = "archive_only"
            eligibility = "review" if max(score, main_brief_score) >= 60 else "ineligible"
            route_reason = "low_operator_value" if operator_strength >= 5 or ecosystem_strength >= 1 else "weak_signal"
    elif source_kind == "regional_media" and regional_proxy_penalty:
        if operator_strength >= 6 and main_brief_score >= 68 and fresh_signal:
            reason_codes.append("proxy_exception:high_operator_value")
        else:
            lane = "archive_only"
            eligibility = "review" if max(score, main_brief_score) >= 64 else "ineligible"
            route_reason = "low_operator_value" if operator_strength >= 5 else "weak_signal"
    elif github_signal:
        if github_significant and operator_strength >= 5 and novelty_strength >= 3 and fresh_signal and main_brief_score >= 64:
            lane = "main"
            reason_codes.append("github_main_brief_candidate")
        else:
            lane = "github"
            eligibility = "review" if github_significant and score >= 52 else ("review" if score >= 40 else "ineligible")
            route_reason = "github_topic_only" if score >= 40 else "weak_signal"
    elif source_kind == "community":
        if operator_strength >= 4 and novelty_strength >= 3 and fresh_signal and score >= 52:
            lane = "main"
        else:
            lane = "archive_only"
            eligibility = "review" if score >= 44 else "ineligible"
            route_reason = "low_operator_value" if score >= 44 else "weak_signal"

    if lane == "main" and not route_reason:
        thresholds = {
            "official": (50, 42, 4),
            "watchlist": (52, 44, 4),
            "strong_media": (56, 48, 4),
            "regional_media": (68, 62, 6),
            "manual": (62, 54, 5),
            "review": (62, 54, 5),
            "search": (64, 56, 5),
            "unknown": (62, 54, 5),
        }
        eligible_threshold, review_threshold, min_operator = thresholds.get(source_kind, (62, 54, 5))
        if github_signal:
            eligible_threshold, review_threshold, min_operator = (74, 68, 6)
        if society_article:
            eligible_threshold = max(eligible_threshold, 72 if consequence_strength < 2 else 68)
            review_threshold = max(review_threshold, 64 if consequence_strength < 2 else 60)
            min_operator = max(min_operator, 5)
        if source_kind == "regional_media" and regional_proxy_penalty:
            eligible_threshold += 4
            review_threshold += 4
            min_operator = max(min_operator, 6)

        if main_brief_score >= eligible_threshold and operator_strength >= min_operator:
            eligibility = "eligible"
        elif main_brief_score >= review_threshold:
            eligibility = "review"
            route_reason = "low_operator_value" if operator_strength < min_operator else "weak_signal"
        else:
            eligibility = "ineligible"
            route_reason = "low_operator_value" if operator_strength < min_operator else "weak_signal"

    article["interesting_signal_score"] = score
    article["delivery_lane_candidate"] = lane if lane in DELIVERY_LANE_CANDIDATES else "archive_only"
    article["main_brief_eligibility"] = (
        eligibility if eligibility in MAIN_BRIEF_ELIGIBILITY_STATES else "ineligible"
    )
    article["main_brief_score"] = max(0, min(100, int(main_brief_score)))
    article["main_brief_reason_codes"] = reason_codes[:12]
    article["main_brief_skip_reason"] = canonical_skip_reason(route_reason)


def ensure_main_brief_contract(article: dict[str, Any]) -> None:
    lane = str(article.get("delivery_lane_candidate", "") or "").strip().lower()
    eligibility = str(article.get("main_brief_eligibility", "") or "").strip().lower()
    if lane in DELIVERY_LANE_CANDIDATES and eligibility in MAIN_BRIEF_ELIGIBILITY_STATES:
        article["main_brief_skip_reason"] = canonical_skip_reason(article.get("main_brief_skip_reason", ""))
        return
    apply_main_brief_routing(article)
