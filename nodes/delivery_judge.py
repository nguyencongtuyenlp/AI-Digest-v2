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
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from editorial_guardrails import build_article_grounding
from digest_formatter import canonical_type_name, type_emoji
from mlx_runner import run_json_inference
from xai_grok import (
    grok_delivery_enabled,
    grok_delivery_max_articles,
    grok_final_editor_enabled,
    grok_final_editor_max_articles,
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


def _runtime_int(runtime_config: dict[str, Any], runtime_key: str, env_key: str, default: int) -> int:
    raw = runtime_config.get(runtime_key)
    if raw in (None, ""):
        raw = os.getenv(env_key, str(default))
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _project_fit_bucket(article: dict[str, Any]) -> str:
    explicit = str(article.get("project_fit", "") or "").strip().lower()
    if explicit in {"high", "medium", "low"}:
        return explicit
    relevance = str(article.get("relevance_level", "") or "").strip().lower()
    if relevance in {"high", "medium", "low"}:
        return relevance
    return "low"


def _main_brief_threshold(_article: dict[str, Any]) -> tuple[int, set[str]]:
    return 45, {"high", "medium"}


def _deterministic_delivery_assessment(article: dict[str, Any]) -> dict[str, Any]:
    score = int(article.get("total_score", 0) or 0)
    source_tier = str(article.get("source_tier", "unknown")).lower()
    source_kind = str(article.get("source_kind", "unknown")).lower()
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
    project_fit = _project_fit_bucket(article)

    groundedness_score = 4 if confidence == "high" else 3 if confidence == "medium" else 2
    freshness_score = 0 if is_stale_candidate else 2 if freshness_unknown else 4
    operator_value_score = 4 if score >= 65 else 3 if score >= 45 else 2 if score >= 30 else 1

    if source_kind == "official" and score >= 40:
        operator_value_score = max(operator_value_score, 3)
    if source_kind == "strong_media" and score >= 45:
        operator_value_score = max(operator_value_score, 3)
    if source_kind == "github" and score >= 50:
        operator_value_score = max(operator_value_score, 3)

    if not is_ai_relevant:
        return {
            "groundedness_score": groundedness_score,
            "freshness_score": freshness_score,
            "operator_value_score": 0,
            "decision": "skip",
            "rationale": "Bài chưa đủ liên quan trực tiếp tới AI để xuất hiện trong brief.",
            "skip_reason": "not_ai",
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
            "skip_reason": "old",
        }

    if EVENT_PROMO_TITLE_RE.search(title):
        return {
            "groundedness_score": groundedness_score,
            "freshness_score": freshness_score,
            "operator_value_score": max(0, operator_value_score - 1),
            "decision": "skip",
            "rationale": "Bài mang tính promo/event marketing, không phù hợp cho main brief.",
            "skip_reason": "promo",
        }

    if source_kind == "community" and COMMUNITY_SPECULATION_TITLE_RE.search(title):
        return {
            "groundedness_score": max(1, groundedness_score - 1),
            "freshness_score": freshness_score,
            "operator_value_score": max(0, operator_value_score - 1),
            "decision": "skip",
            "rationale": "Bài cộng đồng mang tính đồn đoán hoặc headline dạng speculation, không phù hợp cho main brief.",
            "skip_reason": "speculation",
        }

    if score < 35:
        return {
            "groundedness_score": groundedness_score,
            "freshness_score": freshness_score,
            "operator_value_score": operator_value_score,
            "decision": "skip",
            "rationale": "Tín hiệu yếu hoặc thiếu bằng chứng để chiếm chỗ trong brief.",
            "skip_reason": "weak_signal",
        }

    if isinstance(age_hours, (int, float)) and age_hours <= 72:
        freshness_score = min(5, freshness_score + 1)

    min_score, fit_whitelist = _main_brief_threshold(article)
    if score >= 55 or (score >= min_score and project_fit in fit_whitelist):
        decision = "include"
    elif score < 40:
        decision = "skip"
    else:
        decision = "review"

    rationale = (
        "Đủ mạnh để đưa vào brief."
        if decision == "include"
        else "Chưa đủ điểm để đưa thẳng vào brief, nhưng vẫn nên giữ lại ở lớp review."
        if decision == "review"
        else "Tín hiệu yếu hoặc thiếu bằng chứng để chiếm chỗ trong brief."
    )

    return {
        "groundedness_score": groundedness_score,
        "freshness_score": freshness_score,
        "operator_value_score": operator_value_score,
        "decision": decision,
        "rationale": rationale,
        "skip_reason": "weak_signal" if decision == "skip" else "",
    }

def _is_facebook_topic_article(article: dict[str, Any]) -> bool:
    # Facebook lane riêng đã bị loại khỏi delivery path.
    return False


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

    if not is_ai_relevant:
        skip_reason = "not_ai"
        decision = "skip"
        operator_value_score = 0
        rationale = "Post chưa đủ liên quan trực tiếp tới AI để đưa vào topic Facebook News."
    elif not event_is_primary:
        skip_reason = "duplicate"
        decision = "skip"
        rationale = "Post này trùng event với một post Facebook mạnh hơn trong cùng batch."
    elif not content and not note_summary_vi and not snippet:
        skip_reason = "thin"
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
        skip_reason = "old"
        decision = "skip"
        rationale = "Post đã quá cũ cho topic Facebook News."
    elif is_stale_candidate or (is_old_news and authority_score < 78) or FACEBOOK_OLD_HINT_RE.search(published_hint_raw):
        skip_reason = "old"
        decision = "review" if authority_score >= 78 and boss_style_score >= 75 and age_hours and age_hours <= review_max_age_hours else "skip"
        rationale = "Post có dấu hiệu cũ hoặc bị ghim; chỉ nên giữ nếu nguồn đủ mạnh và nội dung vẫn rất đáng đọc."
    elif freshness_unknown:
        skip_reason = "unknown_time"
        decision = "review" if authority_score >= 78 and boss_style_score >= 78 and content_style in {"benchmark", "case_study", "workflow"} else "skip"
        rationale = "Không đọc được thời gian đăng rõ ràng; chỉ giữ nếu đây là post rất mạnh từ nguồn đủ uy tín."
    elif isinstance(age_hours, (int, float)) and age_hours > max_age_hours:
        skip_reason = "aging"
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
        skip_reason = str(judged.get("skip_reason", "")).strip().lower()
        if skip_reason:
            result["skip_reason"] = skip_reason

    if base.get("decision") == "include":
        result["decision"] = "include"
        result["skip_reason"] = ""

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
        "base_decision": base["decision"],
        "final_decision": article["delivery_decision"],
        "rationale": article["delivery_rationale"],
    }
    article["delivery_skip_reason"] = (
        str(result.get("skip_reason", "") or "").strip().lower()
        if article["delivery_decision"] == "skip"
        else ""
    )


def _infer_skip_reason_from_article(article: dict[str, Any]) -> str:
    explicit = str(article.get("delivery_skip_reason", "") or "").strip().lower()
    if explicit:
        return explicit

    rationale = " ".join(
        part for part in [
            str(article.get("delivery_rationale", "") or "").strip(),
            str(article.get("facebook_topic_skip_reason", "") or "").strip(),
        ]
        if part
    ).lower()

    if any(token in rationale for token in ("trùng event", "trung event")):
        return "duplicate_event"
    if any(token in rationale for token in ("đồn đoán", "speculation", "rumor", "leak")):
        return "speculation"
    if any(token in rationale for token in ("không còn đủ mới", "dấu hiệu cũ", "bị ghim", "không còn mới")):
        return "old"
    if any(token in rationale for token in ("không đủ liên quan", "liên quan trực tiếp tới ai")):
        return "not_ai"
    if any(token in rationale for token in ("tín hiệu yếu", "quá mỏng", "quá yếu")):
        return "weak_signal"
    if int(article.get("total_score", 0) or 0) < 40:
        return "weak_signal"
    return ""


def _candidate_sort_key(article: dict[str, Any]) -> tuple[int, int, int]:
    return (
        int(article.get("grok_priority_score", -1) or -1),
        int(article.get("delivery_score", 0) or 0),
        int(article.get("total_score", 0) or 0),
        int(article.get("event_source_count", 1) or 1),
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
) -> None:
    if not grok_delivery_enabled(runtime_config):
        return

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
    if not shortlist:
        return

    logger.info("🧠 Grok rerank: sending %d shortlist articles for main brief selection.", len(shortlist))
    reranked = rerank_delivery_articles(shortlist, feedback_summary_text=feedback_summary_text)
    if not reranked:
        return

    for article in shortlist:
        article_key = article.get("url", "") or article.get("title", "")
        judged = reranked.get(article_key)
        if not judged:
            continue
        article["grok_priority_score"] = int(judged.get("priority_score", 0) or 0)
        article["grok_delivery_decision"] = str(judged.get("decision", article.get("delivery_decision", "review")) or "review")
        article["grok_delivery_rationale"] = str(judged.get("rationale", "") or "")

        lane_override = str(judged.get("lane_override", "keep") or "keep")
        if lane_override != "keep":
            _apply_lane_override(article, lane_override)

        decision = str(judged.get("decision", "") or "").strip().lower()
        if decision in {"include", "review", "skip"}:
            article["delivery_decision"] = decision
        rationale = str(judged.get("rationale", "") or "").strip()
        if rationale:
            article["delivery_rationale"] = rationale

    logger.info(
        "✅ Grok rerank applied to %d/%d shortlist articles.",
        sum(1 for article in shortlist if article.get("grok_delivery_decision")),
        len(shortlist),
    )


def _apply_grok_final_editor_pass(
    telegram_candidates: list[dict[str, Any]],
    *,
    runtime_config: dict[str, Any],
    feedback_summary_text: str = "",
) -> None:
    if not grok_final_editor_enabled(runtime_config):
        return

    shortlist = list(telegram_candidates[:grok_final_editor_max_articles(runtime_config)])
    if len(shortlist) < 2:
        return

    logger.info("🧠 Grok final editor: ordering %d selected main brief articles.", len(shortlist))
    reranked = rerank_final_digest_articles(shortlist, feedback_summary_text=feedback_summary_text)
    if not reranked:
        return

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

    logger.info("✅ Grok final editor ordered %d/%d main brief articles.", updated, len(shortlist))


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
            and base["decision"] != "skip"
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
                    response_format=DELIVERY_JUDGE_RESPONSE_FORMAT,
                )
            except Exception as exc:
                logger.debug("Delivery judge fallback for '%s': %s", article.get("title", "")[:50], exc)

        _merge_judge_result(article, base, judged)
        if article.get("delivery_decision") == "skip":
            article["delivery_skip_reason"] = _infer_skip_reason_from_article(article)
        if is_facebook_article:
            article["facebook_topic_skip_reason"] = str(
                article.get("delivery_skip_reason", "") or base.get("skip_reason", "") or ""
            )
        reviewed_articles.append(article)

    _apply_grok_delivery_rerank(
        reviewed_articles,
        runtime_config=runtime_config,
        feedback_summary_text=str(state.get("feedback_summary_text", "") or ""),
    )
    for article in reviewed_articles:
        if article.get("delivery_decision") != "include" and article.get("delivery_score_breakdown", {}).get("base_decision") == "include":
            article["delivery_decision"] = "include"
            article["delivery_skip_reason"] = ""

    main_include_articles = [
        article for article in reviewed_articles
        if article.get("delivery_decision") == "include"
        and not _is_facebook_topic_article(article)
    ]

    telegram_candidates = _select_diverse_candidates(
        main_include_articles,
        limit=max(1, len(main_include_articles)),
        diversify_by_type=True,
    )
    _apply_grok_final_editor_pass(
        telegram_candidates,
        runtime_config=runtime_config,
        feedback_summary_text=str(state.get("feedback_summary_text", "") or ""),
    )

    selected_keys = {
        str(article.get("url", "") or article.get("title", "") or id(article))
        for article in telegram_candidates
    }
    for article in reviewed_articles:
        title = str(article.get("title", "") or "N/A")
        score = int(article.get("total_score", 0) or 0)
        fit = _project_fit_bucket(article)
        article_key = str(article.get("url", "") or article.get("title", "") or id(article))
        if article_key in selected_keys:
            logger.info("PASS %s | score=%d | fit=%s", title[:40], score, fit)
            continue
        reason = str(article.get("delivery_skip_reason", "") or "").strip().lower()
        if not reason:
            decision = str(article.get("delivery_decision", "") or "").strip().lower()
            reason = "review_threshold" if decision == "review" else _infer_skip_reason_from_article(article) or "filtered_out"
        logger.info("SKIP %s | score=%d | fit=%s | reason=%s", title[:40], score, fit, reason or "-")

    logger.info(
        "✅ Delivery judge xong: main=%d include | total=%d",
        len(telegram_candidates),
        len(reviewed_articles),
    )
    return {
        "final_articles": reviewed_articles,
        "telegram_candidates": telegram_candidates,
    }
