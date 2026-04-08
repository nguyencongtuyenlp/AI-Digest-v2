"""
delivery_judge.py - Decide which articles are worthy of Telegram delivery.

This node mixes deterministic rules with an optional LLM judge so the pipeline
can be event-aware, freshness-aware, and less likely to push stale or duplicate
stories into the executive brief.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any
from urllib.parse import urlparse


from digest.editorial.editorial_guardrails import build_article_grounding
from digest.editorial.delivery_policy import (
    canonical_skip_reason,
    ensure_main_brief_contract,
    is_preferred_main_brief_source,
    project_fit_bucket,
    source_quality_rank,
)
from digest.editorial.digest_formatter import canonical_type_name, type_emoji
from digest.runtime.mlx_runner import resolve_pipeline_mlx_path, run_json_inference
from digest.runtime.xai_grok import (
    grok_delivery_enabled,
    grok_delivery_max_articles,
    grok_final_editor_enabled,
    grok_final_editor_max_articles,
    merge_grok_observability,
    rerank_delivery_articles,
    rerank_final_digest_articles,
)

logger = logging.getLogger(__name__)
FACEBOOK_TOPIC_DOMAINS = {
    "facebook.com",
    "www.facebook.com",
    "m.facebook.com",
    "mbasic.facebook.com",
    "fb.com",
}

LANE_TAG_HINTS: dict[str, set[str]] = {
    "Product": {"product_update", "api_platform", "model_release", "enterprise_ai", "infrastructure"},
    "Society & Culture": {"regulation", "safety", "government", "education", "healthcare", "vietnam", "southeast_asia"},
    "Practical": {"developer_tools", "ai_agents", "open_source"},
}

LANE_TEXT_HINTS: dict[str, tuple[str, ...]] = {
    "Product": ("launch", "launched", "release", "released", "feature", "api", "sdk", "model", "platform", "preview", "beta"),
    "Society & Culture": (
        "regulation",
        "policy",
        "law",
        "compliance",
        "governance",
        "security",
        "safety",
        "cyberattack",
        "cyber attack",
        "breach",
        "compromise",
        "lawsuit",
        "investigation",
        "education",
        "student",
        "school",
        "community",
        "culture",
        "workforce",
        "jobs",
        "hospital",
        "medical",
    ),
    "Practical": ("tutorial", "guide", "workflow", "how to", "playbook", "best practice", "tooling"),
}
MAIN_BRIEF_SOCIETY_BUILDER_TAGS = {
    "developer_tools",
    "api_platform",
    "infrastructure",
    "ai_agents",
}
MAIN_BRIEF_SOCIETY_DIRECT_SYSTEM_HINTS = (
    "pricing",
    "pay extra",
    "cost",
    "compliance",
    "policy",
    "regulation",
    "licensing",
    "compute",
    "data center",
    "datacenter",
    "power",
    "infrastructure",
    "deployment",
    "deployers",
    "security",
    "safety",
    "content moderation",
    "model access",
    "api",
)

VERGE_NON_AI_KEYWORDS = [
    "gaming glasses",
    "ar glasses",
    "music review",
    "album review",
    "film",
    "movie",
    "tv show",
    "headphones",
    "speaker",
    "camera",
    "gaming",
]

DELIVERY_JUDGE_SYSTEM = """Bạn là Delivery Judge cho một sản phẩm AI Daily Digest.
Nhiệm vụ: quyết định bài nào xứng đáng xuất hiện trong bản brief Telegram buổi sáng.

Tiêu chí:
- Ưu tiên bài mới, có bằng chứng tốt, có giá trị quyết định với founder/operator/team AI.
- Tránh bài stale, thiếu freshness, thiếu nội dung, hoặc trùng event với bài mạnh hơn.
- Không chọn bài chỉ vì nghe thú vị; phải có ích thực tế.

Trả về JSON:
{
  "groundedness_score": 0-5,
  "freshness_score": 0-5,
  "operator_value_score": 0-5,
  "decision": "include|review|skip",
  "rationale": "1 câu ngắn"
}
"""

DELIVERY_JUDGE_RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "delivery_judge_article",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "groundedness_score": {"type": "integer", "minimum": 0, "maximum": 5},
                "freshness_score": {"type": "integer", "minimum": 0, "maximum": 5},
                "operator_value_score": {"type": "integer", "minimum": 0, "maximum": 5},
                "decision": {"type": "string", "enum": ["include", "review", "skip"]},
                "rationale": {"type": "string"},
            },
            "required": [
                "groundedness_score",
                "freshness_score",
                "operator_value_score",
                "decision",
                "rationale",
            ],
        },
    },
}

DELIVERY_JUDGE_USER_TEMPLATE = """Hãy chấm bài sau cho Telegram brief:

Tiêu đề: {title}
Type: {primary_type}
Score: {total_score}/100
Source: {source}
Source tier: {source_tier}
Published_at: {published_at}
Published_at_source: {published_at_source}
Freshness_unknown: {freshness_unknown}
Is_stale_candidate: {is_stale_candidate}
Content_available: {content_available}
Event_cluster_size: {event_cluster_size}
Event_is_primary: {event_is_primary}
Confidence label: {confidence_label}
Grounding note: {grounding_note}
Short note: {note_summary_vi}
Unknowns:
{unknowns}

Trả về JSON đúng format."""

COMMUNITY_SPECULATION_TITLE_RE = re.compile(
    r"(\?{1,}|coming\b|what is going on|rumou?r|leak|unverified|speculation)",
    re.IGNORECASE,
)
ROUNDUP_TITLE_RE = re.compile(
    r"\b(roundup|recap|the latest .* we announced|monthly recap|look back at)\b",
    re.IGNORECASE,
)
EVENT_PROMO_TITLE_RE = re.compile(
    r"\b(strictlyvc|register now|get your ticket|tickets? are limited|webinar|conference|summit|event)\b",
    re.IGNORECASE,
)
FACEBOOK_OLD_HINT_RE = re.compile(r"^\d+\s*(tháng|năm|month|months|year|years)\b", re.IGNORECASE)
MAX_MAIN_BRIEF_AGE_HOURS = 24 * 7
MAIN_BRIEF_MAX_ITEMS = 6
MAIN_BRIEF_MIN_SELECTION_SCORE = 62
MAIN_BRIEF_MIN_STRONG_SOURCE_SCORE = 58


def _runtime_int(runtime_config: dict[str, Any], runtime_key: str, env_key: str, default: int) -> int:
    raw = runtime_config.get(runtime_key)
    if raw in (None, ""):
        raw = os.getenv(env_key, str(default))
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _article_source_domain(article: dict[str, Any]) -> str:
    source_domain = str(article.get("source_domain", "") or article.get("source", "") or "").strip().lower()
    if "://" in source_domain:
        source_domain = urlparse(source_domain).netloc.lower()
    if not source_domain:
        url = str(article.get("url", "") or "").strip()
        if url:
            source_domain = urlparse(url).netloc.lower()
    if source_domain.startswith("www."):
        source_domain = source_domain[4:]
    return source_domain


def _fails_verge_non_ai_keyword_filter(article: dict[str, Any]) -> bool:
    if _article_source_domain(article) != "theverge.com":
        return False
    title_lower = str(article.get("title", "") or "").lower()
    return any(keyword in title_lower for keyword in VERGE_NON_AI_KEYWORDS)


def _source_preference_rank(article: dict[str, Any]) -> int:
    return source_quality_rank(article)


def _deterministic_delivery_assessment(article: dict[str, Any]) -> dict[str, Any]:
    ensure_main_brief_contract(article)
    main_brief_score = int(article.get("main_brief_score", article.get("total_score", 0)) or 0)
    source_kind = str(article.get("source_kind", "unknown") or "unknown").lower()
    confidence = str(article.get("confidence_label", "low")).lower()
    content_available = bool(article.get("content_available", False))
    freshness_unknown = bool(article.get("freshness_unknown", False))
    is_stale_candidate = bool(article.get("is_stale_candidate", False))
    is_old_news = bool(article.get("is_old_news", False))
    freshness_bucket = str(article.get("freshness_bucket", "unknown")).lower()
    age_hours = article.get("age_hours")
    event_is_primary = bool(article.get("event_is_primary", True))
    event_cluster_size = int(article.get("event_cluster_size", 1) or 1)
    is_ai_relevant = article.get("is_ai_relevant", True) is not False
    title = str(article.get("title", "") or "")
    project_fit = project_fit_bucket(article)
    lane = str(article.get("delivery_lane_candidate", "archive_only") or "archive_only").strip().lower()
    eligibility = str(article.get("main_brief_eligibility", "ineligible") or "ineligible").strip().lower()
    route_reason = canonical_skip_reason(article.get("main_brief_skip_reason", ""))
    reason_codes = list(article.get("main_brief_reason_codes", []) or [])

    groundedness_score = 4 if confidence == "high" else 3 if confidence == "medium" else 2
    freshness_score = 0 if is_stale_candidate else 2 if freshness_unknown else 4
    operator_value_score = 4 if main_brief_score >= 65 else 3 if main_brief_score >= 48 else 2 if main_brief_score >= 36 else 1

    if source_kind in {"official", "watchlist"} and main_brief_score >= 46:
        operator_value_score = max(operator_value_score, 4)
    elif source_kind == "strong_media" and main_brief_score >= 50:
        operator_value_score = max(operator_value_score, 4)
    elif source_kind == "github":
        operator_value_score = min(operator_value_score, 3)

    if not is_ai_relevant or _fails_verge_non_ai_keyword_filter(article):
        return {
            "groundedness_score": groundedness_score,
            "freshness_score": freshness_score,
            "operator_value_score": 0,
            "decision": "skip",
            "rationale": "Bài chưa đủ liên quan trực tiếp tới AI để xuất hiện trong brief.",
            "skip_reason": "not_ai",
            "route_reason": "not_ai",
        }

    if event_cluster_size >= 2:
        groundedness_score = min(5, groundedness_score + 1)
        operator_value_score = min(5, operator_value_score + 1)

    if not event_is_primary:
        return {
            "groundedness_score": groundedness_score,
            "freshness_score": freshness_score,
            "operator_value_score": operator_value_score,
            "decision": "skip",
            "rationale": "Bài này trùng event với một bài mạnh hơn trong cùng batch.",
            "skip_reason": "duplicate_event",
            "route_reason": "duplicate_event",
        }

    if (
        is_stale_candidate
        or is_old_news
        or freshness_bucket in {"aging", "stale"}
        or (isinstance(age_hours, (int, float)) and age_hours > MAX_MAIN_BRIEF_AGE_HOURS)
    ):
        return {
            "groundedness_score": groundedness_score,
            "freshness_score": 0,
            "operator_value_score": operator_value_score,
            "decision": "skip",
            "rationale": "Bài đã cũ hơn ngưỡng brief buổi sáng hiện tại.",
            "skip_reason": "stale",
            "route_reason": "stale",
        }

    if EVENT_PROMO_TITLE_RE.search(title):
        return {
            "groundedness_score": groundedness_score,
            "freshness_score": freshness_score,
            "operator_value_score": max(0, operator_value_score - 1),
            "decision": "skip",
            "rationale": "Bài mang tính promo/event marketing, không phù hợp cho main brief.",
            "skip_reason": "promo",
            "route_reason": "promo",
        }

    if source_kind in {"community", "github"} and COMMUNITY_SPECULATION_TITLE_RE.search(title):
        return {
            "groundedness_score": max(1, groundedness_score - 1),
            "freshness_score": freshness_score,
            "operator_value_score": max(0, operator_value_score - 1),
            "decision": "skip",
            "rationale": "Bài cộng đồng mang tính đồn đoán hoặc headline dạng speculation, không phù hợp cho main brief.",
            "skip_reason": "speculation",
            "route_reason": "speculation",
        }

    if isinstance(age_hours, (int, float)) and age_hours <= 72:
        freshness_score = min(5, freshness_score + 1)

    if lane == "github":
        decision = "skip"
        route_reason = "github_topic_only"
        rationale = "GitHub item này vẫn hữu ích cho topic/repo lane, nhưng chưa đủ tiêu chuẩn vào main Telegram brief."
    elif lane in {"facebook", "archive_only"} or eligibility == "ineligible":
        decision = "skip"
        route_reason = route_reason or ("low_operator_value" if project_fit == "low" else "weak_signal")
        if route_reason == "low_operator_value":
            rationale = "Bài có tín hiệu nhưng giá trị quyết định cho founder/operator còn thấp cho main brief."
        else:
            rationale = "Bài chưa vượt được lớp source-aware rule cho main brief hiện tại."
    elif eligibility == "review":
        decision = "review"
        route_reason = route_reason or ("low_operator_value" if project_fit == "low" else "weak_signal")
        rationale = (
            "Bài nên giữ ở lớp review vì nguồn/độ mới ổn, nhưng giá trị quyết định cho main brief chưa đủ chắc."
            if route_reason == "low_operator_value"
            else "Bài có tín hiệu tốt nhưng cần thêm bằng chứng hoặc độ sắc để vào thẳng main brief."
        )
    else:
        decision = "include"
        rationale = (
            "Nguồn mạnh, còn mới và có giá trị quyết định rõ cho founder/operator; nên giữ vào main brief."
            if source_kind in {"official", "watchlist", "strong_media"}
            else "Bài đủ mới và có giá trị vận hành rõ để vào main brief."
        )

    return {
        "groundedness_score": groundedness_score,
        "freshness_score": freshness_score,
        "operator_value_score": operator_value_score,
        "decision": decision,
        "rationale": rationale,
        "skip_reason": route_reason if decision == "skip" else "",
        "route_reason": route_reason if decision != "include" else "",
        "lane": lane,
        "main_brief_score": main_brief_score,
        "main_brief_reason_codes": reason_codes,
    }

def _is_facebook_topic_article(article: dict[str, Any]) -> bool:
    if str(article.get("social_platform", "") or "").strip().lower() == "facebook":
        return True
    domain = str(article.get("source_domain", "") or "").strip().lower()
    if domain in FACEBOOK_TOPIC_DOMAINS:
        return True
    url = str(article.get("url", "") or "").strip().lower()
    return any(host in url for host in ("facebook.com", "fb.com", "fb.watch"))


def _facebook_topic_delivery_assessment(
    article: dict[str, Any],
    *,
    runtime_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Judge riêng cho topic Facebook News.

    Lane này mềm hơn main brief vì đây là community/news-following lane:
    - chấp nhận nguồn thủ công / social signal
    - ưu tiên post có note, content, comment signal rõ
    - không bắt freshness/source-tier gắt như main brief
    """
    runtime = dict(runtime_config or {})
    score = int(article.get("total_score", 0) or 0)
    confidence = str(article.get("confidence_label", "low")).lower()
    content_available = bool(article.get("content_available", False))
    freshness_unknown = bool(article.get("freshness_unknown", False))
    is_stale_candidate = bool(article.get("is_stale_candidate", False))
    is_old_news = bool(article.get("is_old_news", False))
    age_hours = article.get("post_age_hours", article.get("age_hours"))
    event_is_primary = bool(article.get("event_is_primary", True))
    note_summary_vi = str(article.get("note_summary_vi", "") or "").strip()
    snippet = str(article.get("snippet", "") or "").strip()
    content = str(article.get("content", "") or "").strip()
    comments = str(article.get("community_reactions", "") or "").strip()
    community_strength = int(article.get("community_signal_strength", 0) or 0)
    is_ai_relevant = article.get("is_ai_relevant", True) is not False
    boss_style_score = int(article.get("facebook_boss_style_score", 0) or 0)
    authority_score = int(article.get("facebook_authority_score", 0) or 0)
    content_style = str(article.get("facebook_content_style", "") or "").strip().lower() or "news_recap"
    sort_mode = str(article.get("facebook_sort_mode", "") or "default_fallback").strip().lower()
    published_hint_raw = str(
        article.get("published_hint_raw", article.get("published_hint", "")) or ""
    ).strip()
    max_age_hours = _runtime_int(runtime, "facebook_max_post_age_hours", "FACEBOOK_MAX_POST_AGE_HOURS", 72)
    review_max_age_hours = _runtime_int(
        runtime,
        "facebook_review_max_post_age_hours",
        "FACEBOOK_REVIEW_MAX_POST_AGE_HOURS",
        168,
    )

    if boss_style_score <= 0:
        boss_style_score = 62 if score >= 45 and content_available else 50 if score >= 30 and (content or note_summary_vi) else 36
        if comments:
            boss_style_score = min(100, boss_style_score + 8)
    if authority_score <= 0:
        authority_score = 64 if article.get("social_signal") else 58 if article.get("facebook_auto") else 52
    if not article.get("facebook_content_style"):
        lowered = " ".join(part for part in [article.get("title", ""), content, note_summary_vi] if part).lower()
        if any(keyword in lowered for keyword in ("benchmark", " vs ", "so sánh", "chi phí", "claude", "gpt", "gemini")):
            content_style = "benchmark"
        elif any(keyword in lowered for keyword in ("workflow", "mcp", "claude code", "quy trình")):
            content_style = "workflow"
    if content_style in {"benchmark", "case_study", "workflow"} and boss_style_score < 58:
        boss_style_score = max(boss_style_score, 60 if score >= 40 and content_available else 52)

    groundedness_score = 4 if content_available else 3 if content or note_summary_vi else 2
    if comments:
        groundedness_score = min(5, groundedness_score + 1)
    if confidence == "high" or authority_score >= 75:
        groundedness_score = min(5, groundedness_score + 1)

    if freshness_unknown:
        freshness_score = 2
    elif isinstance(age_hours, (int, float)):
        if age_hours <= 24:
            freshness_score = 5
        elif age_hours <= max_age_hours:
            freshness_score = 4
        elif age_hours <= review_max_age_hours:
            freshness_score = 2
        else:
            freshness_score = 0
    else:
        freshness_score = 3
    if sort_mode == "newest" and freshness_score >= 4:
        freshness_score = min(5, freshness_score + 1)

    operator_value_score = 5 if boss_style_score >= 82 else 4 if boss_style_score >= 68 else 3 if boss_style_score >= 52 else 2 if boss_style_score >= 38 else 1
    if content_style in {"benchmark", "case_study", "workflow"}:
        operator_value_score = min(5, operator_value_score + 1)
    if authority_score >= 72 or community_strength >= 4:
        operator_value_score = min(5, operator_value_score + 1)

    skip_reason = ""
    rationale = "Đủ tốt để vào topic Facebook News."
    decision = "include"

    if not is_ai_relevant or _fails_verge_non_ai_keyword_filter(article):
        skip_reason = "not_ai"
        decision = "skip"
        operator_value_score = 0
        rationale = "Post chưa đủ liên quan trực tiếp tới AI để đưa vào topic Facebook News."
    elif not event_is_primary:
        skip_reason = "duplicate_event"
        decision = "skip"
        rationale = "Post này trùng event với một post Facebook mạnh hơn trong cùng batch."
    elif not content and not note_summary_vi and not snippet:
        skip_reason = "weak_signal"
        decision = "skip"
        rationale = "Post còn quá mỏng để đưa vào topic Facebook riêng."
    elif content_style == "promo":
        skip_reason = "promo"
        decision = "skip"
        rationale = "Post mang tính promo/event/bán hàng, không phù hợp cho topic Facebook News."
    elif content_style == "speculation" and boss_style_score < 78:
        skip_reason = "speculation"
        decision = "skip"
        rationale = "Post thiên về speculation hoặc đồn đoán, không nên đưa vào Facebook News."
    elif isinstance(age_hours, (int, float)) and age_hours > review_max_age_hours:
        skip_reason = "stale"
        decision = "skip"
        rationale = "Post đã quá cũ cho topic Facebook News."
    elif is_stale_candidate or (is_old_news and authority_score < 78) or FACEBOOK_OLD_HINT_RE.search(published_hint_raw):
        skip_reason = "stale"
        decision = "review" if authority_score >= 78 and boss_style_score >= 75 and age_hours and age_hours <= review_max_age_hours else "skip"
        rationale = "Post có dấu hiệu cũ hoặc bị ghim; chỉ nên giữ nếu nguồn đủ mạnh và nội dung vẫn rất đáng đọc."
    elif freshness_unknown:
        skip_reason = "weak_signal"
        decision = "review" if authority_score >= 78 and boss_style_score >= 78 and content_style in {"benchmark", "case_study", "workflow"} else "skip"
        rationale = "Không đọc được thời gian đăng rõ ràng; chỉ giữ nếu đây là post rất mạnh từ nguồn đủ uy tín."
    elif isinstance(age_hours, (int, float)) and age_hours > max_age_hours:
        skip_reason = "stale"
        decision = "review" if authority_score >= 70 and boss_style_score >= 72 else "skip"
        rationale = "Post không còn mới hẳn; chỉ nên giữ ở mức review nếu vẫn có giá trị thực chiến rõ."
    else:
        avg = (groundedness_score + freshness_score + operator_value_score) / 3
        if content_style in {"benchmark", "case_study", "workflow"}:
            if boss_style_score >= 58 and avg >= 3.0 and score >= 24:
                decision = "include"
                rationale = "Post đủ mới, có chiều sâu và phù hợp kiểu Facebook News mà team đang cần."
            elif boss_style_score >= 48 and avg >= 2.5:
                decision = "review"
                rationale = "Post có tín hiệu tốt nhưng chưa đủ mạnh để ưu tiên cao nhất."
            else:
                skip_reason = "weak_signal"
                decision = "skip"
                rationale = "Post chưa đủ sắc về độ mới hoặc giá trị thực chiến cho topic Facebook."
        elif avg >= 3.3 and boss_style_score >= 62 and authority_score >= 60:
            decision = "include"
            rationale = "Post đủ chắc để đưa vào topic Facebook News."
        elif avg >= 2.6 and boss_style_score >= 50:
            decision = "review"
            rationale = "Post có tín hiệu nhưng nên xem đây là lane theo dõi phụ."
        else:
            skip_reason = "weak_signal"
            decision = "skip"
            rationale = "Tín hiệu vẫn còn quá yếu cho topic Facebook."

    return {
        "groundedness_score": groundedness_score,
        "freshness_score": freshness_score,
        "operator_value_score": operator_value_score,
        "decision": decision,
        "rationale": rationale,
        "skip_reason": skip_reason,
    }


def _merge_judge_result(article: dict[str, Any], base: dict[str, Any], judged: dict[str, Any] | None) -> None:
    result = dict(base)
    if isinstance(judged, dict):
        try:
            result["groundedness_score"] = int(judged.get("groundedness_score", base["groundedness_score"]))
            result["freshness_score"] = int(judged.get("freshness_score", base["freshness_score"]))
            result["operator_value_score"] = int(judged.get("operator_value_score", base["operator_value_score"]))
        except (TypeError, ValueError):
            pass

        decision = str(judged.get("decision", base["decision"])).strip().lower()
        if decision in {"include", "review", "skip"}:
            result["decision"] = decision
        rationale = str(judged.get("rationale", "")).strip()
        if rationale:
            result["rationale"] = rationale

    article["groundedness_score"] = max(0, min(5, int(result["groundedness_score"])))
    article["freshness_score"] = max(0, min(5, int(result["freshness_score"])))
    article["operator_value_score"] = max(0, min(5, int(result["operator_value_score"])))
    article["delivery_decision"] = result["decision"]
    article["delivery_rationale"] = result["rationale"]
    article["delivery_score"] = (
        article["groundedness_score"] + article["freshness_score"] + article["operator_value_score"]
    )
    article["delivery_score_breakdown"] = {
        "groundedness_score": article["groundedness_score"],
        "freshness_score": article["freshness_score"],
        "operator_value_score": article["operator_value_score"],
        "delivery_score": article["delivery_score"],
        "final_decision": article["delivery_decision"],
        "rationale": article["delivery_rationale"],
        "main_brief_score": int(article.get("main_brief_score", 0) or 0),
        "route_reason": str(result.get("route_reason", "") or ""),
    }
    article["delivery_route_reason"] = (
        canonical_skip_reason(result.get("route_reason", ""))
        if article["delivery_decision"] != "include" and result.get("route_reason")
        else ""
    )
    article["delivery_skip_reason"] = (
        canonical_skip_reason(result.get("skip_reason", ""))
        if article["delivery_decision"] == "skip"
        else ""
    )


def _infer_skip_reason_from_article(article: dict[str, Any]) -> str:
    for key in (
        "delivery_skip_reason",
        "delivery_route_reason",
        "main_brief_skip_reason",
        "facebook_topic_skip_reason",
    ):
        reason = canonical_skip_reason(article.get(key, ""))
        if reason:
            return reason

    rationale = " ".join(
        part for part in [
            str(article.get("delivery_rationale", "") or "").strip(),
        ]
        if part
    ).lower()

    if any(token in rationale for token in ("trùng event", "trung event")):
        return "duplicate_event"
    if any(token in rationale for token in ("đồn đoán", "speculation", "rumor", "leak")):
        return "speculation"
    if any(token in rationale for token in ("không còn đủ mới", "dấu hiệu cũ", "bị ghim", "không còn mới")):
        return "stale"
    if any(token in rationale for token in ("không đủ liên quan", "liên quan trực tiếp tới ai")):
        return "not_ai"
    if "github" in rationale and "main brief" in rationale:
        return "github_topic_only"
    if any(token in rationale for token in ("giá trị quyết định", "operator còn thấp")):
        return "low_operator_value"
    if any(token in rationale for token in ("tín hiệu yếu", "quá mỏng", "quá yếu")):
        return "weak_signal"
    return "weak_signal" if str(article.get("delivery_decision", "") or "").lower() in {"skip", "review"} else ""


def _candidate_sort_key(article: dict[str, Any]) -> tuple[int, int, int, int, int]:
    return (
        int(article.get("grok_priority_score", -1) or -1),
        int(article.get("main_brief_score", article.get("total_score", 0)) or 0),
        int(article.get("delivery_score", 0) or 0),
        _source_preference_rank(article),
        int(article.get("event_source_count", 1) or 1),
    )


def _reason_code_set(article: dict[str, Any]) -> set[str]:
    return {str(code or "").strip().lower() for code in list(article.get("main_brief_reason_codes", []) or [])}


def _society_direct_system_signal(article: dict[str, Any]) -> tuple[int, int]:
    combined = " ".join(
        str(
            article.get(field, "") or ""
        )
        for field in ("title", "summary_vi", "editorial_angle", "why_it_matters_vi", "snippet", "note_summary_vi")
    ).lower()
    tags = {
        str(tag or "").strip().lower()
        for tag in article.get("tags", []) or []
        if str(tag or "").strip()
    }
    builder_tags = len(tags & MAIN_BRIEF_SOCIETY_BUILDER_TAGS)
    direct_hits = sum(1 for keyword in MAIN_BRIEF_SOCIETY_DIRECT_SYSTEM_HINTS if keyword in combined)
    return builder_tags, direct_hits


def _build_main_brief_selection_context(
    articles: list[dict[str, Any]],
    reviewed_articles: list[dict[str, Any]],
    *,
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    run_metrics = dict((state or {}).get("run_health", {}).get("metrics", {}) or {})
    total_pool = len(articles)
    preferred_pool = sum(1 for article in articles if is_preferred_main_brief_source(article))
    official_pool = sum(1 for article in articles if str(article.get("source_kind", "") or "").strip().lower() == "official")
    official_viable_pool = sum(
        1
        for article in articles
        if str(article.get("source_kind", "") or "").strip().lower() == "official"
        and int(article.get("main_brief_score", article.get("total_score", 0)) or 0) >= 60
        and int(article.get("delivery_score", 0) or 0) >= 11
    )
    github_pool = sum(1 for article in articles if str(article.get("source_kind", "") or "").strip().lower() == "github")
    proxy_pool = sum(
        1
        for article in articles
        if str(article.get("source_kind", "") or "").strip().lower() == "regional_media"
        or "source_penalty:proxy" in _reason_code_set(article)
    )
    reviewed_count = max(1, len(reviewed_articles))
    reviewed_github_ratio = (
        sum(1 for article in reviewed_articles if str(article.get("source_kind", "") or "").strip().lower() == "github")
        / reviewed_count
    )
    penalized_reviewed = sum(
        1 for article in reviewed_articles if int(article.get("source_history_penalty", 0) or 0) >= 8
    )
    weak_source_mix = total_pool > 0 and (
        preferred_pool < min(2, total_pool)
        or proxy_pool >= max(1, total_pool // 3)
    )
    high_noise_presence = reviewed_github_ratio >= 0.30 or penalized_reviewed >= max(3, len(reviewed_articles) // 5)
    official_gap = int(run_metrics.get("official_main_candidate_count", official_viable_pool) or 0) == 0
    conservative = official_gap or weak_source_mix or high_noise_presence
    max_items = MAIN_BRIEF_MAX_ITEMS
    if official_gap:
        max_items = min(max_items, 5)
    if official_gap and weak_source_mix:
        max_items = min(max_items, 4)

    return {
        "official_gap": official_gap,
        "weak_source_mix": weak_source_mix,
        "high_noise_presence": high_noise_presence,
        "conservative": conservative,
        "max_items": max_items,
        "preferred_pool": preferred_pool,
        "official_pool": official_pool,
        "official_viable_pool": official_viable_pool,
        "github_pool": github_pool,
        "proxy_pool": proxy_pool,
    }


def _main_brief_selection_score(article: dict[str, Any], selection_context: dict[str, Any] | None = None) -> int:
    score = int(article.get("main_brief_score", article.get("total_score", 0)) or 0)
    delivery_score = int(article.get("delivery_score", 0) or 0)
    source_kind = str(article.get("source_kind", "") or "").strip().lower()
    article_type = canonical_type_name(article.get("primary_type"))
    reason_codes = _reason_code_set(article)

    score += delivery_score
    score += _source_preference_rank(article) * 2

    if source_kind == "official":
        score += 10
    elif is_preferred_main_brief_source(article):
        score += 6
    elif source_kind == "regional_media":
        score -= 6
    elif source_kind == "github":
        score -= 14

    if "source_penalty:proxy" in reason_codes:
        score -= 8
    if "github_significant" in reason_codes:
        score += 10
    if "github_low_impact" in reason_codes:
        score -= 8
    if article_type == "Society & Culture":
        score -= 8
        if "society_high_consequence" in reason_codes:
            score += 6
        elif "society_ecosystem_implication" in reason_codes:
            score += 4

    if project_fit_bucket(article) == "low":
        score -= 6
    if selection_context:
        if selection_context.get("conservative") and not is_preferred_main_brief_source(article):
            score -= 3
        if selection_context.get("official_gap") and source_kind in {"regional_media", "github"}:
            score -= 4
        if selection_context.get("weak_source_mix") and source_kind not in {"official", "strong_media"}:
            score -= 2

    return score


def _passes_main_brief_quality_floor(article: dict[str, Any], selection_context: dict[str, Any] | None = None) -> bool:
    selection_score = _main_brief_selection_score(article, selection_context)
    delivery_score = int(article.get("delivery_score", 0) or 0)
    source_kind = str(article.get("source_kind", "") or "").strip().lower()
    reason_codes = _reason_code_set(article)
    article_type = canonical_type_name(article.get("primary_type"))
    preferred_source = is_preferred_main_brief_source(article)
    min_delivery = 11

    if source_kind == "official":
        floor = 60
    elif source_kind == "strong_media":
        floor = MAIN_BRIEF_MIN_STRONG_SOURCE_SCORE + 4
    elif source_kind == "regional_media":
        floor = MAIN_BRIEF_MIN_SELECTION_SCORE + 8
        min_delivery = 12
    elif source_kind == "github":
        if "github_significant" not in reason_codes:
            return False
        floor = MAIN_BRIEF_MIN_SELECTION_SCORE + 14
        min_delivery = 13
    else:
        floor = MAIN_BRIEF_MIN_SELECTION_SCORE

    if article_type == "Society & Culture":
        if not reason_codes & {"society_high_consequence", "society_ecosystem_implication"}:
            return False
        builder_tags, direct_hits = _society_direct_system_signal(article)
        if builder_tags == 0 and direct_hits < 2:
            return False
        floor += 6
        if "society_high_consequence" in reason_codes:
            floor -= 2
        if builder_tags == 0:
            floor += 2
    if "source_penalty:proxy" in reason_codes:
        floor += 6

    if selection_context:
        if selection_context.get("conservative"):
            floor += 3
            if not preferred_source:
                min_delivery = max(min_delivery, 12)
        if selection_context.get("official_gap") and not preferred_source:
            floor += 3
        if selection_context.get("weak_source_mix") and source_kind in {"regional_media", "github", "community"}:
            floor += 3
        if selection_context.get("high_noise_presence") and source_kind in {"github", "regional_media"}:
            floor += 2

    if delivery_score < min_delivery:
        return False
    if project_fit_bucket(article) == "low" and not preferred_source:
        return False
    return selection_score >= floor


def _select_main_brief_candidates(
    articles: list[dict[str, Any]],
    *,
    reviewed_articles: list[dict[str, Any]] | None = None,
    state: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    reviewed = list(reviewed_articles or articles)
    selection_context = _build_main_brief_selection_context(articles, reviewed, state=state)
    ordered = sorted(
        articles,
        key=lambda article: (_main_brief_selection_score(article, selection_context),) + _candidate_sort_key(article),
        reverse=True,
    )

    selected: list[dict[str, Any]] = []
    official_pool = [
        article for article in ordered if str(article.get("source_kind", "") or "").strip().lower() == "official"
    ]
    preferred_pool = [article for article in ordered if is_preferred_main_brief_source(article)]
    seed_pool = official_pool or preferred_pool
    if seed_pool:
        strongest_preferred = seed_pool[0]
        if _passes_main_brief_quality_floor(strongest_preferred, selection_context):
            selected.append(strongest_preferred)

    max_items = int(selection_context.get("max_items", MAIN_BRIEF_MAX_ITEMS))
    for article in ordered:
        if article in selected:
            continue
        if len(selected) >= max_items:
            break
        if not _passes_main_brief_quality_floor(article, selection_context):
            continue
        selected.append(article)

    # Khi đã include ở lớp judge nhưng floor chọn main rỗng (vd. official_gap + nguồn unknown),
    # vẫn giữ top theo điểm để brief/Telegram không rỗng.
    if not selected and ordered:
        selected = ordered[:max_items]

    return selected[:max_items]


def _selection_skip_reason(article: dict[str, Any], selection_context: dict[str, Any]) -> tuple[str, str]:
    source_kind = str(article.get("source_kind", "") or "").strip().lower()
    article_type = canonical_type_name(article.get("primary_type"))
    reason_codes = _reason_code_set(article)

    if source_kind == "github":
        return "github_topic_only", "selection_excluded:github_quality_floor"
    if "source_penalty:proxy" in reason_codes or source_kind == "regional_media":
        return "weak_signal", "selection_excluded:proxy_recap"
    if article_type == "Society & Culture":
        if "society_high_consequence" in reason_codes:
            return "low_operator_value", "selection_excluded:society_not_strong_enough"
        return "weak_signal", "selection_excluded:society_low_consequence"
    if selection_context.get("conservative") and not is_preferred_main_brief_source(article):
        return "weak_signal", "selection_excluded:conservative_quality_floor"
    if project_fit_bucket(article) == "low":
        return "low_operator_value", "selection_excluded:low_operator_value"
    return "weak_signal", "selection_excluded:quality_floor"


def _mark_main_brief_selection_skips(
    reviewed_articles: list[dict[str, Any]],
    telegram_candidates: list[dict[str, Any]],
    selection_context: dict[str, Any],
) -> None:
    selected_keys = {
        str(article.get("url", "") or article.get("title", "") or id(article))
        for article in telegram_candidates
    }

    for article in reviewed_articles:
        article_key = str(article.get("url", "") or article.get("title", "") or id(article))
        if article_key in selected_keys:
            continue
        if str(article.get("delivery_decision", "") or "").strip().lower() != "include":
            continue
        if str(article.get("delivery_lane_candidate", "") or "").strip().lower() != "main":
            continue

        reason, detail_code = _selection_skip_reason(article, selection_context)
        reason_codes = list(article.get("main_brief_reason_codes", []) or [])
        if detail_code not in reason_codes:
            reason_codes.append(detail_code)
        if selection_context.get("conservative") and "selection_mode:conservative" not in reason_codes:
            reason_codes.append("selection_mode:conservative")
        article["main_brief_reason_codes"] = reason_codes[:14]
        article["delivery_decision"] = "skip"
        article["delivery_route_reason"] = reason
        article["delivery_skip_reason"] = reason
        article["main_brief_skip_reason"] = reason
        article["delivery_rationale"] = (
            "Bài vượt qua routing ban đầu nhưng không vượt quality floor cuối của main brief."
        )


def _sync_primary_type(article: dict[str, Any]) -> None:
    canonical = canonical_type_name(article.get("primary_type"))
    article["primary_type"] = canonical
    article["primary_emoji"] = type_emoji(canonical)


def _lane_signal_counts(article: dict[str, Any]) -> dict[str, int]:
    text_parts = [
        article.get("title", ""),
        article.get("note_summary_vi", ""),
        article.get("summary_vi", ""),
        article.get("editorial_angle", ""),
        article.get("grounding_note", ""),
    ]
    combined = " ".join(str(part or "") for part in text_parts).lower()
    tags = {
        str(tag or "").strip().lower()
        for tag in article.get("tags", []) or []
        if str(tag or "").strip()
    }

    counts: dict[str, int] = {}
    for lane, keywords in LANE_TEXT_HINTS.items():
        text_hits = sum(1 for keyword in keywords if keyword in combined)
        tag_hits = len(tags & {tag.lower() for tag in LANE_TAG_HINTS.get(lane, set())})
        counts[lane] = text_hits + (tag_hits * 2)
    return counts


def _should_apply_lane_override(article: dict[str, Any], lane_override: str) -> bool:
    target_lane = canonical_type_name(lane_override)
    current_lane = canonical_type_name(article.get("primary_type"))
    if not target_lane or target_lane == current_lane:
        return False

    signal_counts = _lane_signal_counts(article)
    target_score = signal_counts.get(target_lane, 0)
    current_score = signal_counts.get(current_lane, 0)

    if target_lane == "Product":
        return target_score >= max(2, current_score)

    return target_score >= max(1, current_score + 1)


def _apply_lane_override(article: dict[str, Any], lane_override: str) -> None:
    requested_lane = canonical_type_name(lane_override)
    article["grok_primary_type_override_requested"] = requested_lane
    if _should_apply_lane_override(article, requested_lane):
        article["grok_primary_type_override"] = requested_lane
        article["primary_type"] = requested_lane
        article["primary_emoji"] = type_emoji(requested_lane)
        return
    article["grok_primary_type_override_rejected"] = requested_lane


def _apply_grok_delivery_rerank(
    reviewed_articles: list[dict[str, Any]],
    *,
    runtime_config: dict[str, Any],
    feedback_summary_text: str = "",
) -> dict[str, Any]:
    metrics = {
        "enabled": grok_delivery_enabled(runtime_config),
        "request_count": 0,
        "success_count": 0,
        "fallback_count": 0,
        "items_processed": 0,
        "applied": False,
        "shortlist_size": 0,
        "applied_article_count": 0,
    }
    if not grok_delivery_enabled(runtime_config):
        return metrics

    shortlist = _select_diverse_candidates(
        [
            article for article in reviewed_articles
            if not _is_facebook_topic_article(article)
            and str(article.get("delivery_decision", "") or "").lower() in {"include", "review"}
            and bool(article.get("event_is_primary", True))
        ],
        limit=grok_delivery_max_articles(runtime_config),
        diversify_by_type=False,
    )
    metrics["shortlist_size"] = len(shortlist)
    if not shortlist:
        return metrics

    logger.info("🧠 Grok rerank: sending %d shortlist articles for main brief selection.", len(shortlist))
    metrics["request_count"] = 1
    metrics["items_processed"] = len(shortlist)
    reranked = rerank_delivery_articles(shortlist, feedback_summary_text=feedback_summary_text)
    if not reranked:
        metrics["fallback_count"] = 1
        return metrics

    metrics["success_count"] = 1
    applied_count = 0
    for article in shortlist:
        article_key = article.get("url", "") or article.get("title", "")
        judged = reranked.get(article_key)
        if not judged:
            continue
        before_decision = str(article.get("delivery_decision", "") or "review")
        before_lane = canonical_type_name(article.get("primary_type")) or str(article.get("primary_type", "") or "")
        before_priority = int(article.get("grok_priority_score", article.get("main_brief_score", article.get("total_score", 0))) or 0)
        article["grok_priority_score"] = int(judged.get("priority_score", 0) or 0)
        article["grok_delivery_decision"] = str(judged.get("decision", article.get("delivery_decision", "review")) or "review")
        article["grok_delivery_rationale"] = str(judged.get("rationale", "") or "")
        article["grok_rerank_applied"] = True
        article["grok_rerank_reason"] = str(judged.get("rationale", "") or "")

        lane_override = str(judged.get("lane_override", "keep") or "keep")
        if lane_override != "keep":
            _apply_lane_override(article, lane_override)

        decision = str(judged.get("decision", "") or "").strip().lower()
        if decision in {"include", "review", "skip"}:
            article["delivery_decision"] = decision
        rationale = str(judged.get("rationale", "") or "").strip()
        if rationale:
            article["delivery_rationale"] = rationale
        article["grok_rerank_delta"] = {
            "decision_before": before_decision,
            "decision_after": str(article.get("delivery_decision", "") or before_decision),
            "priority_score_before": before_priority,
            "priority_score_after": int(article.get("grok_priority_score", before_priority) or before_priority),
            "lane_before": before_lane,
            "lane_after": canonical_type_name(article.get("primary_type")) or str(article.get("primary_type", "") or before_lane),
        }
        applied_count += 1

    metrics["applied"] = applied_count > 0
    metrics["applied_article_count"] = applied_count

    logger.info(
        "✅ Grok rerank applied to %d/%d shortlist articles.",
        sum(1 for article in shortlist if article.get("grok_delivery_decision")),
        len(shortlist),
    )
    return metrics


def _apply_grok_final_editor_pass(
    telegram_candidates: list[dict[str, Any]],
    *,
    runtime_config: dict[str, Any],
    feedback_summary_text: str = "",
) -> dict[str, Any]:
    metrics = {
        "enabled": grok_final_editor_enabled(runtime_config),
        "request_count": 0,
        "success_count": 0,
        "fallback_count": 0,
        "items_processed": 0,
        "applied": False,
        "shortlist_size": 0,
        "applied_article_count": 0,
    }
    if not grok_final_editor_enabled(runtime_config):
        return metrics

    shortlist = list(telegram_candidates[:grok_final_editor_max_articles(runtime_config)])
    metrics["shortlist_size"] = len(shortlist)
    if len(shortlist) < 2:
        return metrics

    logger.info("🧠 Grok final editor: ordering %d selected main brief articles.", len(shortlist))
    metrics["request_count"] = 1
    metrics["items_processed"] = len(shortlist)
    reranked = rerank_final_digest_articles(shortlist, feedback_summary_text=feedback_summary_text)
    if not reranked:
        metrics["fallback_count"] = 1
        return metrics

    metrics["success_count"] = 1
    updated = 0
    for article in shortlist:
        article_key = article.get("url", "") or article.get("title", "")
        judged = reranked.get(article_key)
        if not judged:
            continue
        article["grok_final_rank_score"] = int(judged.get("rank_score", 0) or 0)
        rationale = str(judged.get("rationale", "") or "").strip()
        if rationale:
            article["grok_final_editor_note"] = rationale
        updated += 1

    metrics["applied"] = updated > 0
    metrics["applied_article_count"] = updated
    logger.info("✅ Grok final editor ordered %d/%d main brief articles.", updated, len(shortlist))
    return metrics


def _select_diverse_candidates(
    articles: list[dict[str, Any]],
    *,
    limit: int,
    diversify_by_type: bool = True,
) -> list[dict[str, Any]]:
    ordered = sorted(articles, key=_candidate_sort_key, reverse=True)
    if not diversify_by_type or limit <= 1:
        return ordered[:limit]

    selected: list[dict[str, Any]] = []
    seen_types: set[str] = set()

    for article in ordered:
        article_type = canonical_type_name(article.get("primary_type"))
        if not article_type or article_type in seen_types:
            continue
        selected.append(article)
        seen_types.add(article_type)
        if len(selected) >= limit:
            return selected

    for article in ordered:
        if article in selected:
            continue
        selected.append(article)
        if len(selected) >= limit:
            break

    return selected


def delivery_judge_node(state: dict[str, Any]) -> dict[str, Any]:
    final_articles = list(state.get("final_articles", []))
    if not final_articles:
        return {
            "telegram_candidates": [],
        }
    runtime_config = dict(state.get("runtime_config", {}) or {})

    reviewed_articles: list[dict[str, Any]] = []
    for index, article in enumerate(final_articles, 1):
        _sync_primary_type(article)
        ensure_main_brief_contract(article)
        grounding = article if article.get("grounding_note") else build_article_grounding(article)
        article.update(grounding)
        is_facebook_article = _is_facebook_topic_article(article)
        if is_facebook_article:
            base = _facebook_topic_delivery_assessment(article, runtime_config=runtime_config)
        else:
            base = _deterministic_delivery_assessment(article)

        judged: dict[str, Any] | None = None
        should_call_judge = (
            not is_facebook_article
            and str(article.get("delivery_lane_candidate", "") or "").lower() == "main"
            and str(article.get("main_brief_eligibility", "") or "").lower() == "review"
            and base["decision"] == "review"
            and bool(article.get("event_is_primary", True))
        )
        if should_call_judge:
            logger.info("🧪 Delivery Judge [%d/%d]: %s", index, len(final_articles), article.get("title", "N/A")[:60])
            try:
                judged = run_json_inference(
                    DELIVERY_JUDGE_SYSTEM,
                    DELIVERY_JUDGE_USER_TEMPLATE.format(
                        title=article.get("title", "N/A"),
                        primary_type=article.get("primary_type", "Unknown"),
                        total_score=article.get("total_score", 0),
                        source=article.get("source", "Unknown"),
                        source_tier=article.get("source_tier", "unknown"),
                        published_at=article.get("published_at", ""),
                        published_at_source=article.get("published_at_source", "unknown"),
                        freshness_unknown=article.get("freshness_unknown", False),
                        is_stale_candidate=article.get("is_stale_candidate", False),
                        content_available=article.get("content_available", False),
                        event_cluster_size=article.get("event_cluster_size", 1),
                        event_is_primary=article.get("event_is_primary", True),
                        confidence_label=grounding.get("confidence_label", "low"),
                        grounding_note=grounding.get("grounding_note", ""),
                        note_summary_vi=article.get("note_summary_vi", ""),
                        unknowns=grounding.get("unknowns_text", "- Không có unknown lớn từ metadata."),
                    ),
                    max_tokens=250,
                    temperature=0.1,
                    model_path=resolve_pipeline_mlx_path("heavy", runtime_config),
                    response_format=DELIVERY_JUDGE_RESPONSE_FORMAT,
                )
            except Exception as exc:
                logger.debug("Delivery judge fallback for '%s': %s", article.get("title", "")[:50], exc)

        _merge_judge_result(article, base, judged)
        article.setdefault("grok_rerank_applied", False)
        article.setdefault("grok_rerank_delta", {})
        article.setdefault("grok_rerank_reason", "")
        if article.get("delivery_decision") == "skip":
            article["delivery_skip_reason"] = _infer_skip_reason_from_article(article)
        elif article.get("delivery_decision") != "include":
            article["delivery_route_reason"] = _infer_skip_reason_from_article(article)
        if is_facebook_article:
            article["facebook_topic_skip_reason"] = str(
                article.get("delivery_skip_reason", "") or base.get("skip_reason", "") or ""
            )
        reviewed_articles.append(article)

    rerank_metrics = _apply_grok_delivery_rerank(
        reviewed_articles,
        runtime_config=runtime_config,
        feedback_summary_text=str(state.get("feedback_summary_text", "") or ""),
    )

    main_include_articles = [
        article for article in reviewed_articles
        if article.get("delivery_decision") == "include"
        and str(article.get("delivery_lane_candidate", "") or "").lower() == "main"
        and not _is_facebook_topic_article(article)
    ]

    selection_context = _build_main_brief_selection_context(main_include_articles, reviewed_articles, state=state)
    telegram_candidates = _select_main_brief_candidates(
        main_include_articles,
        reviewed_articles=reviewed_articles,
        state=state,
    )
    facebook_includes = [
        article
        for article in reviewed_articles
        if _is_facebook_topic_article(article)
        and str(article.get("delivery_decision", "") or "").strip().lower() == "include"
    ]
    if facebook_includes:
        seen_tc = {
            str(article.get("url", "") or article.get("title", "") or id(article))
            for article in telegram_candidates
        }
        for article in facebook_includes:
            key = str(article.get("url", "") or article.get("title", "") or id(article))
            if key not in seen_tc:
                telegram_candidates.append(article)
                seen_tc.add(key)
    final_editor_metrics = _apply_grok_final_editor_pass(
        telegram_candidates,
        runtime_config=runtime_config,
        feedback_summary_text=str(state.get("feedback_summary_text", "") or ""),
    )
    _mark_main_brief_selection_skips(reviewed_articles, telegram_candidates, selection_context)

    selected_keys = {
        str(article.get("url", "") or article.get("title", "") or id(article))
        for article in telegram_candidates
    }
    for article in reviewed_articles:
        title = str(article.get("title", "") or "N/A")
        score = int(article.get("total_score", 0) or 0)
        fit = project_fit_bucket(article)
        article_key = str(article.get("url", "") or article.get("title", "") or id(article))
        if article_key in selected_keys:
            logger.info("PASS %s | score=%d | fit=%s", title[:40], score, fit)
            continue
        reason = str(
            article.get("delivery_skip_reason", "")
            or article.get("delivery_route_reason", "")
            or article.get("main_brief_skip_reason", "")
            or ""
        ).strip().lower()
        if not reason:
            reason = _infer_skip_reason_from_article(article)
        logger.info("SKIP %s | score=%d | fit=%s | reason=%s", title[:40], score, fit, reason or "-")

    logger.info(
        "✅ Delivery judge xong: main=%d include | total=%d",
        len(telegram_candidates),
        len(reviewed_articles),
    )
    grok_metrics = merge_grok_observability(
        state,
        stage="delivery_rerank",
        enabled=bool(rerank_metrics.get("enabled", False)),
        request_count=int(rerank_metrics.get("request_count", 0) or 0),
        success_count=int(rerank_metrics.get("success_count", 0) or 0),
        fallback_count=int(rerank_metrics.get("fallback_count", 0) or 0),
        items_processed=int(rerank_metrics.get("items_processed", 0) or 0),
        applied=bool(rerank_metrics.get("applied", False)),
        extra={
            "shortlist_size": int(rerank_metrics.get("shortlist_size", 0) or 0),
            "applied_article_count": int(rerank_metrics.get("applied_article_count", 0) or 0),
        },
    )
    grok_metrics = {
        **grok_metrics,
        **merge_grok_observability(
            {**state, **grok_metrics},
            stage="final_editor_order",
            enabled=bool(final_editor_metrics.get("enabled", False)),
            request_count=int(final_editor_metrics.get("request_count", 0) or 0),
            success_count=int(final_editor_metrics.get("success_count", 0) or 0),
            fallback_count=int(final_editor_metrics.get("fallback_count", 0) or 0),
            items_processed=int(final_editor_metrics.get("items_processed", 0) or 0),
            applied=bool(final_editor_metrics.get("applied", False)),
            extra={
                "shortlist_size": int(final_editor_metrics.get("shortlist_size", 0) or 0),
                "applied_article_count": int(final_editor_metrics.get("applied_article_count", 0) or 0),
            },
        ),
    }
    return {
        "final_articles": reviewed_articles,
        "telegram_candidates": telegram_candidates,
        **grok_metrics,
    }
