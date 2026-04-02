"""
xai_grok.py - Optional xAI/Grok helpers for editorial augmentation.

This module is intentionally lightweight:
- no extra SDK dependency
- OpenAI-compatible REST call via requests
- strict JSON output so downstream selection stays deterministic
- Grok augments the local pipeline, it does not replace the local core
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

logger = logging.getLogger(__name__)

VALID_LANES = {
    "Research",
    "Product",
    "Business",
    "Policy & Ethics",
    "Society & Culture",
    "Practical",
}
VALID_DECISIONS = {"include", "review", "skip"}

DEFAULT_XAI_BASE_URL = "https://api.x.ai/v1"
DEFAULT_GROK_DELIVERY_MODEL = "grok-4-1-fast-non-reasoning"
DEFAULT_GROK_DELIVERY_MAX_ARTICLES = 10
DEFAULT_GROK_PREFILTER_MAX_ARTICLES = 18
DEFAULT_GROK_FINAL_EDITOR_MAX_ARTICLES = 8
DEFAULT_GROK_NEWS_COPY_MAX_ARTICLES = 18
DEFAULT_GROK_FACEBOOK_MAX_ARTICLES = 8
DEFAULT_GROK_SOURCE_GAP_MAX_ARTICLES = 12
DEFAULT_GROK_SCOUT_MODEL = "grok-4-1-fast-reasoning"
DEFAULT_GROK_SCOUT_MAX_QUERIES = 2
DEFAULT_GROK_SCOUT_MAX_ARTICLES = 6
DEFAULT_GROK_X_SCOUT_MAX_QUERIES = 2
DEFAULT_GROK_X_SCOUT_MAX_ARTICLES = 6

BOOL_FALSE_VALUES = {"0", "false", "no", "off"}

GROK_PREFILTER_SYSTEM = """Bạn là headline triage editor cho AI Daily Digest.
Nhiệm vụ duy nhất: nhìn metadata headline-level và chọn bài nào nên được đưa qua local 32B classify.

Nguyên tắc:
- Đây là vòng cứu bài mạnh có thể bị heuristic bỏ sót, không phải vòng viết bài.
- Ưu tiên bài official/strong-source còn mới, có khả năng ảnh hưởng quyết định của founder/operator/team AI.
- Có thể rescue bài nguồn mạnh dù headline chưa quá hoàn chỉnh nếu tín hiệu sản phẩm, business, policy hoặc practical đủ rõ.
- Phạt bài mỏng, clickbait, repo/tool khoe nhẹ, hoặc ít giá trị vận hành.
- Không đổi lane GitHub/Facebook ở đây; đây chỉ là quyết định giữ bài cho local classify.

Trả về JSON hợp lệ đúng schema. Không thêm markdown hay giải thích ngoài JSON."""

GROK_DELIVERY_SYSTEM = """Bạn là Chief Editor cho AI Daily Digest của một startup AI.
Bạn không viết brief. Nhiệm vụ duy nhất là chọn bài nào đáng lên main Telegram brief buổi sáng.

Nguyên tắc:
- Ưu tiên bài mới, đáng hành động, có ích cho founder/operator/team AI.
- Phạt nặng bài mỏng, mơ hồ, chỉ mang tính khoe repo/tool, hoặc giá trị thị trường thấp.
- Phạt nặng bài community/speculation, headline dạng dấu hỏi, roundup cũ, hoặc event promo không có diễn biến AI thực chất.
- Không ưu tiên bài chỉ vì đang hot; phải hữu ích thực tế.
- Nếu bài không đủ mạnh cho main brief nhưng vẫn đáng để theo dõi, dùng review.
- Chỉ dùng lane_override khi lane hiện tại rõ ràng sai.
- Nếu có nhiều bài rất mạnh trong cùng một type, vẫn có thể giữ hơn 3 bài trong brief.
- Ưu tiên bài official/strong-source khi chúng đủ mới và có tác động vận hành rõ.

Trả về JSON hợp lệ đúng schema. Không thêm markdown hay giải thích ngoài JSON."""

GROK_FINAL_EDITOR_SYSTEM = """Bạn là Final Editor cho main Telegram brief.
Nhiệm vụ duy nhất: sắp thứ tự các bài đã được chọn sẵn cho main brief. Không được thêm, bớt, hay viết lại bài.

Nguyên tắc:
- Bài càng xứng đáng đứng sớm thì rank_score càng cao.
- Ưu tiên bài mới, mạnh, actionable, founder-grade.
- Khi có nhiều bài cùng type đều mạnh, vẫn có thể đẩy bài tốt nhất lên trước mà không loại bài còn lại.
- Không đụng GitHub topic hay Facebook topic.

Trả về JSON hợp lệ đúng schema. Không thêm markdown hay giải thích ngoài JSON."""

GROK_NEWS_COPY_SYSTEM = """Bạn là News Copy Editor cho AI Daily Digest.
Nhiệm vụ duy nhất: viết lại mỗi bài thành một đoạn bản tin ngắn bằng tiếng Việt để gửi Telegram.

Nguyên tắc:
- Giọng văn trung tính, chuyên nghiệp, ngắn gọn.
- Tóm tắt điều gì đã xảy ra và vì sao nó đáng chú ý trong 1-2 câu.
- Không viết khuyến nghị kiểu "nên theo dõi", "nên thử", "cần thận trọng", "chỉ nên", "tín hiệu yếu".
- Không chấm độ tin cậy nguồn, không bình luận "nguồn yếu/nguồn mạnh", trừ khi chính sự kiện xoay quanh tranh cãi về nguồn.
- Không bịa chi tiết ngoài metadata đã cho.
- Không lặp nguyên tiêu đề.
- Không đụng lane GitHub/Facebook/main; chỉ viết lại câu chữ.

Trả về JSON hợp lệ đúng schema. Không thêm markdown hay giải thích ngoài JSON."""

GROK_FACEBOOK_SCORE_SYSTEM = """Bạn là editor riêng cho lane Facebook News.
Nhiệm vụ: chấm shortlist post Facebook nào đáng đưa lên topic Facebook News của Telegram.

Nguyên tắc:
- Ưu tiên benchmark, case study, workflow thực chiến, so sánh model/tool, cost-vs-quality, trải nghiệm dùng AI có dữ liệu.
- Ưu tiên post còn mới, có nội dung đủ dày, có số liệu hoặc chi tiết cụ thể, và có ích thực tế với team AI.
- Profile chuyên gia hoặc group/page AI uy tín có thể được ưu tiên hơn nếu bài đủ mới và đủ chắc.
- Phạt nặng speculation, meme, tuyển dụng, event promo, chia sẻ link ngắn, bán tool/account, hoặc post cũ/pinned.
- Không được chuyển bài ra lane main hoặc GitHub.
- Viết blurb ngắn theo giọng news chuyên nghiệp, không đưa lời khuyên kiểu "nên theo dõi", "nguồn yếu", "dữ liệu yếu".

Trả về JSON hợp lệ đúng schema. Không thêm markdown hay giải thích ngoài JSON."""

GROK_SOURCE_GAP_SYSTEM = """Bạn là Source Strategist cho AI Daily Digest.
Nhiệm vụ: nhìn source mix hiện tại và gợi ý tối đa 4 hướng mở rộng source/query để bắt tin official mạnh hơn.

Nguyên tắc:
- Chỉ gợi ý nếu thật sự có khoảng trống đáng giá.
- Ưu tiên official blog/newsroom/research page/company announcements/query có khả năng bắt được tin founder-grade.
- Không gợi ý Facebook, GitHub hay nguồn yếu làm nguồn core.
- Không bịa URL cụ thể nếu không chắc; dùng feed hint hoặc query hint ngắn, dễ verify.

Trả về JSON hợp lệ đúng schema. Không thêm markdown hay giải thích ngoài JSON."""

GROK_SCOUT_SYSTEM = """Bạn là News Scout cho AI Daily Digest.
Nhiệm vụ: dùng web search để tìm thêm một số bài AI mới, mạnh, có ích cho founder/operator/team AI khi batch hiện tại còn yếu.

Nguyên tắc:
- Ưu tiên bài mới trong 72 giờ gần nhất.
- Ưu tiên official company posts, newsroom, release notes, research blogs; strong media chỉ là lớp phụ.
- Không tìm GitHub repo/release và không tìm Facebook social posts.
- Chỉ chọn bài có giá trị quyết định, tránh bài mỏng hoặc lặp lại.
- Trả về URL thật của bài.

Trả về JSON hợp lệ đúng schema. Không thêm markdown hay giải thích ngoài JSON."""

GROK_X_SCOUT_SYSTEM = """Bạn là X Scout cho AI Daily Digest.
Nhiệm vụ: dùng X Search để tìm một số post mới, đáng chú ý về AI/tech/GitHub repo trong 72 giờ gần nhất.

Nguyên tắc:
- Ưu tiên post từ handle uy tín hoặc post có link ra official source / GitHub repo / release notes thật.
- Ưu tiên tín hiệu product, model, benchmark, enterprise, open-source, agent workflow, repo/release mới đáng dùng.
- Tránh drama, meme, giveaway, tuyển dụng, quote ngắn vô thưởng vô phạt.
- Nếu post chỉ bàn luận mà không có link ra nguồn ngoài, vẫn có thể giữ nhưng chỉ khi nội dung đủ cụ thể và hữu ích.
- Trả về URL post thật; nếu post có link chính đi kèm, trả thêm linked_url.

Trả về JSON hợp lệ đúng schema. Không thêm markdown hay giải thích ngoài JSON."""

GROK_PREFILTER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "article_decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "article_id": {"type": "string"},
                    "keep_for_local": {"type": "boolean"},
                    "priority_score": {"type": "integer", "minimum": 0, "maximum": 100},
                    "rationale": {"type": "string"},
                },
                "required": ["article_id", "keep_for_local", "priority_score", "rationale"],
            },
        },
        "batch_note": {"type": "string"},
    },
    "required": ["article_decisions", "batch_note"],
}

GROK_DELIVERY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "article_decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "article_id": {"type": "string"},
                    "decision": {"type": "string", "enum": ["include", "review", "skip"]},
                    "priority_score": {"type": "integer", "minimum": 0, "maximum": 100},
                    "lane_override": {
                        "type": "string",
                        "enum": [
                            "keep",
                            "Research",
                            "Product",
                            "Business",
                            "Policy & Ethics",
                            "Society & Culture",
                            "Practical",
                        ],
                    },
                    "rationale": {"type": "string"},
                },
                "required": ["article_id", "decision", "priority_score", "lane_override", "rationale"],
            },
        },
        "batch_note": {"type": "string"},
    },
    "required": ["article_decisions", "batch_note"],
}

GROK_FINAL_EDITOR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "article_orders": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "article_id": {"type": "string"},
                    "rank_score": {"type": "integer", "minimum": 0, "maximum": 100},
                    "rationale": {"type": "string"},
                },
                "required": ["article_id", "rank_score", "rationale"],
            },
        },
        "batch_note": {"type": "string"},
    },
    "required": ["article_orders", "batch_note"],
}

GROK_NEWS_COPY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "article_blurbs": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "article_id": {"type": "string"},
                    "blurb": {"type": "string"},
                },
                "required": ["article_id", "blurb"],
            },
        },
        "batch_note": {"type": "string"},
    },
    "required": ["article_blurbs", "batch_note"],
}

GROK_FACEBOOK_SCORE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "article_decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "article_id": {"type": "string"},
                    "decision": {"type": "string", "enum": ["include", "review", "skip"]},
                    "priority_score": {"type": "integer", "minimum": 0, "maximum": 100},
                    "trust_score": {"type": "integer", "minimum": 0, "maximum": 100},
                    "usefulness_score": {"type": "integer", "minimum": 0, "maximum": 100},
                    "newsworthiness_score": {"type": "integer", "minimum": 0, "maximum": 100},
                    "blurb": {"type": "string"},
                    "rationale": {"type": "string"},
                },
                "required": [
                    "article_id",
                    "decision",
                    "priority_score",
                    "trust_score",
                    "usefulness_score",
                    "newsworthiness_score",
                    "blurb",
                    "rationale",
                ],
            },
        },
        "batch_note": {"type": "string"},
    },
    "required": ["article_decisions", "batch_note"],
}

GROK_SOURCE_GAP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "suggestions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "focus": {"type": "string"},
                    "priority": {"type": "string", "enum": ["high", "medium", "low"]},
                    "suggested_query": {"type": "string"},
                    "suggested_feed_hint": {"type": "string"},
                    "rationale": {"type": "string"},
                },
                "required": ["focus", "priority", "suggested_query", "suggested_feed_hint", "rationale"],
            },
        },
        "batch_note": {"type": "string"},
    },
    "required": ["suggestions", "batch_note"],
}

GROK_SCOUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "articles": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "title": {"type": "string"},
                    "url": {"type": "string"},
                    "published_at": {"type": "string"},
                    "source_domain": {"type": "string"},
                    "summary": {"type": "string"},
                    "why_it_matters": {"type": "string"},
                },
                "required": ["title", "url", "published_at", "source_domain", "summary", "why_it_matters"],
            },
        },
        "batch_note": {"type": "string"},
    },
    "required": ["articles", "batch_note"],
}

GROK_X_SCOUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "posts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "title": {"type": "string"},
                    "post_url": {"type": "string"},
                    "linked_url": {"type": "string"},
                    "published_at": {"type": "string"},
                    "author_handle": {"type": "string"},
                    "summary": {"type": "string"},
                    "why_it_matters": {"type": "string"},
                },
                "required": [
                    "title",
                    "post_url",
                    "linked_url",
                    "published_at",
                    "author_handle",
                    "summary",
                    "why_it_matters",
                ],
            },
        },
        "batch_note": {"type": "string"},
    },
    "required": ["posts", "batch_note"],
}


def _feature_enabled(
    runtime_config: dict[str, Any] | None,
    *,
    runtime_key: str,
    env_key: str,
) -> bool:
    config = dict(runtime_config or {})
    raw = config.get(runtime_key)
    if raw in (None, ""):
        raw = os.getenv(env_key, "")
    if str(raw).strip().lower() in BOOL_FALSE_VALUES:
        return False
    return bool(os.getenv("XAI_API_KEY", "").strip())


def grok_delivery_enabled(runtime_config: dict[str, Any] | None = None) -> bool:
    return _feature_enabled(
        runtime_config,
        runtime_key="enable_grok_delivery_judge",
        env_key="GROK_DELIVERY_JUDGE_ENABLED",
    )


def grok_prefilter_enabled(runtime_config: dict[str, Any] | None = None) -> bool:
    return _feature_enabled(
        runtime_config,
        runtime_key="enable_grok_prefilter",
        env_key="GROK_PREFILTER_ENABLED",
    )


def grok_final_editor_enabled(runtime_config: dict[str, Any] | None = None) -> bool:
    return _feature_enabled(
        runtime_config,
        runtime_key="enable_grok_final_editor",
        env_key="GROK_FINAL_EDITOR_ENABLED",
    )


def grok_news_copy_enabled(runtime_config: dict[str, Any] | None = None) -> bool:
    return _feature_enabled(
        runtime_config,
        runtime_key="enable_grok_news_copy",
        env_key="GROK_NEWS_COPY_ENABLED",
    )


def grok_facebook_score_enabled(runtime_config: dict[str, Any] | None = None) -> bool:
    return _feature_enabled(
        runtime_config,
        runtime_key="enable_grok_facebook_score",
        env_key="GROK_FACEBOOK_SCORE_ENABLED",
    )


def grok_source_gap_enabled(runtime_config: dict[str, Any] | None = None) -> bool:
    return _feature_enabled(
        runtime_config,
        runtime_key="enable_grok_source_gap",
        env_key="GROK_SOURCE_GAP_ENABLED",
    )


def grok_scout_enabled(runtime_config: dict[str, Any] | None = None) -> bool:
    return _feature_enabled(
        runtime_config,
        runtime_key="enable_grok_scout",
        env_key="GROK_SCOUT_ENABLED",
    )


def grok_x_scout_enabled(runtime_config: dict[str, Any] | None = None) -> bool:
    return _feature_enabled(
        runtime_config,
        runtime_key="enable_grok_x_scout",
        env_key="GROK_X_SCOUT_ENABLED",
    )


def grok_delivery_model() -> str:
    return os.getenv("GROK_DELIVERY_MODEL", DEFAULT_GROK_DELIVERY_MODEL).strip() or DEFAULT_GROK_DELIVERY_MODEL


def _feature_max_articles(
    runtime_config: dict[str, Any] | None,
    *,
    runtime_key: str,
    env_key: str,
    default: int,
    hard_cap: int = 24,
) -> int:
    config = dict(runtime_config or {})
    raw = config.get(runtime_key)
    if raw in (None, ""):
        raw = os.getenv(env_key, str(default))
    try:
        return max(1, min(hard_cap, int(raw)))
    except (TypeError, ValueError):
        return default


def grok_delivery_max_articles(runtime_config: dict[str, Any] | None = None) -> int:
    return _feature_max_articles(
        runtime_config,
        runtime_key="grok_delivery_max_articles",
        env_key="GROK_DELIVERY_MAX_ARTICLES",
        default=DEFAULT_GROK_DELIVERY_MAX_ARTICLES,
        hard_cap=20,
    )


def grok_prefilter_max_articles(runtime_config: dict[str, Any] | None = None) -> int:
    return _feature_max_articles(
        runtime_config,
        runtime_key="grok_prefilter_max_articles",
        env_key="GROK_PREFILTER_MAX_ARTICLES",
        default=DEFAULT_GROK_PREFILTER_MAX_ARTICLES,
        hard_cap=24,
    )


def grok_final_editor_max_articles(runtime_config: dict[str, Any] | None = None) -> int:
    return _feature_max_articles(
        runtime_config,
        runtime_key="grok_final_editor_max_articles",
        env_key="GROK_FINAL_EDITOR_MAX_ARTICLES",
        default=DEFAULT_GROK_FINAL_EDITOR_MAX_ARTICLES,
        hard_cap=12,
    )


def grok_news_copy_max_articles(runtime_config: dict[str, Any] | None = None) -> int:
    return _feature_max_articles(
        runtime_config,
        runtime_key="grok_news_copy_max_articles",
        env_key="GROK_NEWS_COPY_MAX_ARTICLES",
        default=DEFAULT_GROK_NEWS_COPY_MAX_ARTICLES,
        hard_cap=24,
    )


def grok_facebook_max_articles(runtime_config: dict[str, Any] | None = None) -> int:
    return _feature_max_articles(
        runtime_config,
        runtime_key="grok_facebook_max_articles",
        env_key="GROK_FACEBOOK_MAX_ARTICLES",
        default=DEFAULT_GROK_FACEBOOK_MAX_ARTICLES,
        hard_cap=12,
    )


def grok_source_gap_max_articles(runtime_config: dict[str, Any] | None = None) -> int:
    return _feature_max_articles(
        runtime_config,
        runtime_key="grok_source_gap_max_articles",
        env_key="GROK_SOURCE_GAP_MAX_ARTICLES",
        default=DEFAULT_GROK_SOURCE_GAP_MAX_ARTICLES,
        hard_cap=18,
    )


def grok_scout_model() -> str:
    return os.getenv("GROK_SCOUT_MODEL", DEFAULT_GROK_SCOUT_MODEL).strip() or DEFAULT_GROK_SCOUT_MODEL


def grok_scout_max_queries(runtime_config: dict[str, Any] | None = None) -> int:
    return _feature_max_articles(
        runtime_config,
        runtime_key="grok_scout_max_queries",
        env_key="GROK_SCOUT_MAX_QUERIES",
        default=DEFAULT_GROK_SCOUT_MAX_QUERIES,
        hard_cap=4,
    )


def grok_scout_max_articles(runtime_config: dict[str, Any] | None = None) -> int:
    return _feature_max_articles(
        runtime_config,
        runtime_key="grok_scout_max_articles",
        env_key="GROK_SCOUT_MAX_ARTICLES",
        default=DEFAULT_GROK_SCOUT_MAX_ARTICLES,
        hard_cap=10,
    )


def grok_x_scout_max_queries(runtime_config: dict[str, Any] | None = None) -> int:
    return _feature_max_articles(
        runtime_config,
        runtime_key="grok_x_scout_max_queries",
        env_key="GROK_X_SCOUT_MAX_QUERIES",
        default=DEFAULT_GROK_X_SCOUT_MAX_QUERIES,
        hard_cap=4,
    )


def grok_x_scout_max_articles(runtime_config: dict[str, Any] | None = None) -> int:
    return _feature_max_articles(
        runtime_config,
        runtime_key="grok_x_scout_max_articles",
        env_key="GROK_X_SCOUT_MAX_ARTICLES",
        default=DEFAULT_GROK_X_SCOUT_MAX_ARTICLES,
        hard_cap=10,
    )


def _xai_base_url() -> str:
    return os.getenv("XAI_BASE_URL", DEFAULT_XAI_BASE_URL).rstrip("/")


def _xai_timeout_seconds() -> int:
    raw = os.getenv("GROK_DELIVERY_TIMEOUT_SECONDS", "35")
    try:
        return max(5, min(120, int(raw)))
    except (TypeError, ValueError):
        return 35


def _compact_text(value: Any, limit: int = 280) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _article_payload(article: dict[str, Any], *, article_id: str) -> dict[str, Any]:
    return {
        "article_id": article_id,
        "title": _compact_text(article.get("title", ""), 180),
        "lane": str(article.get("primary_type", "") or "").strip(),
        "predicted_type": str(article.get("prefilter_primary_type", article.get("primary_type", "")) or "").strip(),
        "prefilter_score": int(article.get("prefilter_score", 0) or 0),
        "prefilter_reasons": [
            _compact_text(reason, 80)
            for reason in article.get("prefilter_reasons", [])
            if str(reason or "").strip()
        ][:4],
        "score": int(article.get("total_score", 0) or 0),
        "delivery_score": int(article.get("delivery_score", 0) or 0),
        "source": _compact_text(article.get("source", ""), 120),
        "source_domain": _compact_text(article.get("source_domain", ""), 80),
        "source_tier": str(article.get("source_tier", "unknown") or "unknown"),
        "source_kind": str(article.get("source_kind", "unknown") or "unknown"),
        "published_at": _compact_text(article.get("published_at", article.get("published", "")), 64),
        "age_hours": article.get("age_hours"),
        "freshness_bucket": str(article.get("freshness_bucket", "unknown") or "unknown"),
        "freshness_unknown": bool(article.get("freshness_unknown", False)),
        "content_available": bool(article.get("content_available", False)),
        "event_source_count": int(article.get("event_source_count", 1) or 1),
        "confidence_label": str(article.get("confidence_label", "low") or "low"),
        "facebook_source_type": str(article.get("facebook_source_type", "") or "").strip(),
        "facebook_discovery_origin": str(article.get("facebook_discovery_origin", "") or "").strip(),
        "facebook_sort_mode": str(article.get("facebook_sort_mode", "") or "").strip(),
        "facebook_content_style": str(article.get("facebook_content_style", "") or "").strip(),
        "facebook_boss_style_score": int(article.get("facebook_boss_style_score", 0) or 0),
        "facebook_authority_score": int(article.get("facebook_authority_score", 0) or 0),
        "post_age_hours": article.get("post_age_hours"),
        "social_author": _compact_text(article.get("social_author", ""), 120),
        "social_group": _compact_text(article.get("social_group", ""), 120),
        "tags": [str(tag).strip() for tag in article.get("tags", []) if str(tag).strip()][:8],
        "note_summary_vi": _compact_text(article.get("note_summary_vi", ""), 300),
        "grounding_note": _compact_text(article.get("grounding_note", ""), 220),
        "delivery_rationale": _compact_text(article.get("delivery_rationale", ""), 220),
    }


def _user_prompt(task: str, payload_articles: list[dict[str, Any]], *, feedback_summary_text: str = "") -> str:
    return json.dumps(
        {
            "task": task,
            "editor_feedback": " ".join(str(feedback_summary_text or "").split())[:800],
            "articles": payload_articles,
        },
        ensure_ascii=False,
        indent=2,
    )


def _extract_message_content(response_json: dict[str, Any]) -> str:
    choices = response_json.get("choices", []) or []
    if not choices:
        return ""
    message = choices[0].get("message", {}) or {}
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content") or ""
                if text:
                    parts.append(str(text))
        return "\n".join(parts).strip()
    return str(content or "").strip()


def _extract_response_output_text(response_json: dict[str, Any]) -> str:
    output_text = response_json.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    parts: list[str] = []
    for item in response_json.get("output", []) or []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []) or []:
            if not isinstance(content, dict):
                continue
            text = content.get("text") or content.get("output_text") or ""
            if text:
                parts.append(str(text))
    return "\n".join(parts).strip()


def _extract_json_object(text: str) -> dict[str, Any]:
    stripped = str(text or "").strip()
    if not stripped:
        return {}

    fence_match = stripped
    if "```" in stripped:
        parts = stripped.split("```")
        for part in parts:
            candidate = part.strip()
            if candidate.startswith("json"):
                candidate = candidate[4:].strip()
            if candidate.startswith("{") and candidate.endswith("}"):
                stripped = candidate
                break

    try:
        parsed = json.loads(stripped)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        pass

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            parsed = json.loads(stripped[start : end + 1])
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _call_xai_json(
    *,
    system_prompt: str,
    user_prompt: str,
    schema_name: str,
    schema: dict[str, Any],
    max_tokens: int,
) -> dict[str, Any]:
    api_key = os.getenv("XAI_API_KEY", "").strip()
    if not api_key:
        return {}

    payload = {
        "model": grok_delivery_model(),
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.1,
        "max_tokens": max_tokens,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "strict": True,
                "schema": schema,
            },
        },
    }

    try:
        response = requests.post(
            f"{_xai_base_url()}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=_xai_timeout_seconds(),
        )
        response.raise_for_status()
        content = _extract_message_content(response.json())
        return json.loads(content) if content else {}
    except Exception as exc:
        logger.warning("Grok JSON call failed for %s: %s", schema_name, exc)
        return {}


def _call_xai_responses_with_web_search(
    *,
    prompt: str,
    allowed_domains: list[str],
    max_output_tokens: int = 1200,
) -> dict[str, Any]:
    api_key = os.getenv("XAI_API_KEY", "").strip()
    if not api_key:
        return {}

    payload = {
        "model": grok_scout_model(),
        "input": [
            {"role": "system", "content": GROK_SCOUT_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        "tools": [
            {
                "type": "web_search",
                "filters": {
                    "allowed_domains": allowed_domains[:5],
                },
            }
        ],
        "max_output_tokens": max_output_tokens,
    }

    try:
        response = requests.post(
            f"{_xai_base_url()}/responses",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=_xai_timeout_seconds(),
        )
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        logger.warning("Grok scout web search failed: %s", exc)
        return {}


def _call_xai_responses_with_x_search(
    *,
    prompt: str,
    allowed_x_handles: list[str],
    excluded_x_handles: list[str] | None = None,
    from_date: str = "",
    to_date: str = "",
    max_output_tokens: int = 1200,
) -> dict[str, Any]:
    api_key = os.getenv("XAI_API_KEY", "").strip()
    if not api_key:
        return {}

    filters: dict[str, Any] = {}
    if allowed_x_handles:
        filters["allowed_x_handles"] = [str(item).strip().lstrip("@") for item in allowed_x_handles if str(item).strip()][:10]
    if excluded_x_handles:
        filters["excluded_x_handles"] = [str(item).strip().lstrip("@") for item in excluded_x_handles if str(item).strip()][:20]
    if from_date:
        filters["from_date"] = from_date
    if to_date:
        filters["to_date"] = to_date

    payload = {
        "model": grok_scout_model(),
        "input": [
            {"role": "system", "content": GROK_X_SCOUT_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        "tools": [{"type": "x_search", **filters}],
        "max_output_tokens": max_output_tokens,
    }

    try:
        response = requests.post(
            f"{_xai_base_url()}/responses",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=_xai_timeout_seconds(),
        )
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        logger.warning("Grok X scout failed: %s", exc)
        return {}


def rerank_prefilter_articles(
    articles: list[dict[str, Any]],
    *,
    feedback_summary_text: str = "",
) -> dict[str, dict[str, Any]]:
    indexed_articles = [(f"a{index + 1}", article) for index, article in enumerate(articles)]
    payload_articles = [_article_payload(article, article_id=article_id) for article_id, article in indexed_articles]
    parsed = _call_xai_json(
        system_prompt=GROK_PREFILTER_SYSTEM,
        user_prompt=_user_prompt(
            (
                "Chọn bài nào nên được cứu qua local 32B classify. "
                "Giữ lại bài founder-grade/official/new có khả năng bị heuristic bỏ sót. "
                "Nếu bài yếu hoặc ít giá trị vận hành thì không cần giữ."
            ),
            payload_articles,
            feedback_summary_text=feedback_summary_text,
        ),
        schema_name="grok_prefilter_rerank",
        schema=GROK_PREFILTER_SCHEMA,
        max_tokens=1400,
    )

    decisions = parsed.get("article_decisions", []) if isinstance(parsed, dict) else []
    resolved: dict[str, dict[str, Any]] = {}
    for article_id, article in indexed_articles:
        for item in decisions:
            if not isinstance(item, dict):
                continue
            if str(item.get("article_id", "") or "").strip() != article_id:
                continue
            try:
                priority_score = int(item.get("priority_score", 0) or 0)
            except (TypeError, ValueError):
                priority_score = 0
            resolved[article.get("url", article_id) or article_id] = {
                "keep_for_local": bool(item.get("keep_for_local", False)),
                "priority_score": max(0, min(100, priority_score)),
                "rationale": _compact_text(item.get("rationale", ""), 220),
            }
            break
    return resolved


def rerank_delivery_articles(
    articles: list[dict[str, Any]],
    *,
    feedback_summary_text: str = "",
) -> dict[str, dict[str, Any]]:
    indexed_articles = [(f"a{index + 1}", article) for index, article in enumerate(articles)]
    payload_articles = [_article_payload(article, article_id=article_id) for article_id, article in indexed_articles]
    parsed = _call_xai_json(
        system_prompt=GROK_DELIVERY_SYSTEM,
        user_prompt=_user_prompt(
            (
                "Chấm shortlist cho main Telegram brief. "
                "Nếu bài chỉ đáng theo dõi thì review, nếu không đủ mạnh thì skip. "
                "Ưu tiên founder-grade signal, freshness, source quality, operator value. "
                "Cố giữ brief đa dạng type khi có nhiều bài đủ mạnh."
            ),
            payload_articles,
            feedback_summary_text=feedback_summary_text,
        ),
        schema_name="grok_delivery_rerank",
        schema=GROK_DELIVERY_SCHEMA,
        max_tokens=1200,
    )

    article_decisions = parsed.get("article_decisions", []) if isinstance(parsed, dict) else []
    resolved: dict[str, dict[str, Any]] = {}
    for article_id, article in indexed_articles:
        for item in article_decisions:
            if not isinstance(item, dict):
                continue
            if str(item.get("article_id", "") or "").strip() != article_id:
                continue
            decision = str(item.get("decision", "") or "").strip().lower()
            lane_override = str(item.get("lane_override", "keep") or "keep").strip()
            if decision not in VALID_DECISIONS:
                break
            if lane_override != "keep" and lane_override not in VALID_LANES:
                lane_override = "keep"
            try:
                priority_score = int(item.get("priority_score", 0) or 0)
            except (TypeError, ValueError):
                priority_score = 0
            resolved[article.get("url", article_id) or article_id] = {
                "decision": decision,
                "priority_score": max(0, min(100, priority_score)),
                "lane_override": lane_override,
                "rationale": _compact_text(item.get("rationale", ""), 220),
            }
            break
    return resolved


def rerank_final_digest_articles(
    articles: list[dict[str, Any]],
    *,
    feedback_summary_text: str = "",
) -> dict[str, dict[str, Any]]:
    indexed_articles = [(f"a{index + 1}", article) for index, article in enumerate(articles)]
    payload_articles = [_article_payload(article, article_id=article_id) for article_id, article in indexed_articles]
    parsed = _call_xai_json(
        system_prompt=GROK_FINAL_EDITOR_SYSTEM,
        user_prompt=_user_prompt(
            (
                "Sắp thứ tự các bài đã được chọn cho main brief. "
                "Không thay đổi selection; chỉ chấm rank_score để bài mạnh hơn đứng sớm hơn."
            ),
            payload_articles,
            feedback_summary_text=feedback_summary_text,
        ),
        schema_name="grok_final_editor_rank",
        schema=GROK_FINAL_EDITOR_SCHEMA,
        max_tokens=900,
    )

    article_orders = parsed.get("article_orders", []) if isinstance(parsed, dict) else []
    resolved: dict[str, dict[str, Any]] = {}
    for article_id, article in indexed_articles:
        for item in article_orders:
            if not isinstance(item, dict):
                continue
            if str(item.get("article_id", "") or "").strip() != article_id:
                continue
            try:
                rank_score = int(item.get("rank_score", 0) or 0)
            except (TypeError, ValueError):
                rank_score = 0
            resolved[article.get("url", article_id) or article_id] = {
                "rank_score": max(0, min(100, rank_score)),
                "rationale": _compact_text(item.get("rationale", ""), 220),
            }
            break
    return resolved


def rewrite_news_blurbs(
    articles: list[dict[str, Any]],
    *,
    feedback_summary_text: str = "",
) -> dict[str, dict[str, Any]]:
    indexed_articles = [(f"a{index + 1}", article) for index, article in enumerate(articles)]
    payload_articles = [_article_payload(article, article_id=article_id) for article_id, article in indexed_articles]
    parsed = _call_xai_json(
        system_prompt=GROK_NEWS_COPY_SYSTEM,
        user_prompt=_user_prompt(
            (
                "Viết lại mỗi bài thành một đoạn bản tin ngắn, trung tính, chuyên nghiệp cho Telegram. "
                "Chỉ nêu diễn biến chính và ý nghĩa ngắn gọn; không đưa lời khuyên hay chấm độ tin cậy."
            ),
            payload_articles,
            feedback_summary_text=feedback_summary_text,
        ),
        schema_name="grok_news_copy_blurbs",
        schema=GROK_NEWS_COPY_SCHEMA,
        max_tokens=1800,
    )

    article_blurbs = parsed.get("article_blurbs", []) if isinstance(parsed, dict) else []
    resolved: dict[str, dict[str, Any]] = {}
    for article_id, article in indexed_articles:
        for item in article_blurbs:
            if not isinstance(item, dict):
                continue
            if str(item.get("article_id", "") or "").strip() != article_id:
                continue
            resolved[article.get("url", article_id) or article_id] = {
                "blurb": _compact_text(item.get("blurb", ""), 320),
            }
            break
    return resolved


def rerank_facebook_topic_articles(
    articles: list[dict[str, Any]],
    *,
    feedback_summary_text: str = "",
) -> dict[str, dict[str, Any]]:
    indexed_articles = [(f"a{index + 1}", article) for index, article in enumerate(articles)]
    payload_articles = [_article_payload(article, article_id=article_id) for article_id, article in indexed_articles]
    parsed = _call_xai_json(
        system_prompt=GROK_FACEBOOK_SCORE_SYSTEM,
        user_prompt=_user_prompt(
            (
                "Chấm shortlist cho topic Facebook News. "
                "Ưu tiên post Facebook mới, có chiều sâu, đáng đọc, và thật sự hữu ích cho team AI. "
                "Cho include/review/skip và viết 1 blurb ngắn giọng news chuyên nghiệp."
            ),
            payload_articles,
            feedback_summary_text=feedback_summary_text,
        ),
        schema_name="grok_facebook_topic_rerank",
        schema=GROK_FACEBOOK_SCORE_SCHEMA,
        max_tokens=1800,
    )

    article_decisions = parsed.get("article_decisions", []) if isinstance(parsed, dict) else []
    resolved: dict[str, dict[str, Any]] = {}
    for article_id, article in indexed_articles:
        for item in article_decisions:
            if not isinstance(item, dict):
                continue
            if str(item.get("article_id", "") or "").strip() != article_id:
                continue
            decision = str(item.get("decision", "") or "").strip().lower()
            if decision not in VALID_DECISIONS:
                break
            try:
                priority_score = int(item.get("priority_score", 0) or 0)
                trust_score = int(item.get("trust_score", 0) or 0)
                usefulness_score = int(item.get("usefulness_score", 0) or 0)
                newsworthiness_score = int(item.get("newsworthiness_score", 0) or 0)
            except (TypeError, ValueError):
                priority_score = 0
                trust_score = 0
                usefulness_score = 0
                newsworthiness_score = 0
            resolved[article.get("url", article_id) or article_id] = {
                "decision": decision,
                "priority_score": max(0, min(100, priority_score)),
                "trust_score": max(0, min(100, trust_score)),
                "usefulness_score": max(0, min(100, usefulness_score)),
                "newsworthiness_score": max(0, min(100, newsworthiness_score)),
                "blurb": _compact_text(item.get("blurb", ""), 320),
                "rationale": _compact_text(item.get("rationale", ""), 220),
            }
            break
    return resolved


def suggest_source_gap_expansion(
    scored_articles: list[dict[str, Any]],
    raw_articles: list[dict[str, Any]],
    telegram_candidates: list[dict[str, Any]],
    *,
    feedback_summary_text: str = "",
) -> dict[str, Any]:
    candidate_payload = []
    selected_urls = {
        str(article.get("url", "") or "").strip()
        for article in telegram_candidates
        if isinstance(article, dict)
    }
    for article in scored_articles:
        if not isinstance(article, dict):
            continue
        candidate_payload.append(
            {
                "title": _compact_text(article.get("title", ""), 160),
                "type": str(article.get("primary_type", "") or "").strip(),
                "source_domain": _compact_text(article.get("source_domain", ""), 80),
                "source_kind": str(article.get("source_kind", "unknown") or "unknown"),
                "source_tier": str(article.get("source_tier", "unknown") or "unknown"),
                "prefilter_score": int(article.get("prefilter_score", 0) or 0),
                "total_score": int(article.get("total_score", 0) or 0),
                "selected_for_main": str(article.get("url", "") or "").strip() in selected_urls,
                "why_surfaced": [
                    _compact_text(reason, 70)
                    for reason in article.get("why_surfaced", [])[:3]
                    if str(reason or "").strip()
                ],
            }
        )
        if len(candidate_payload) >= max(4, DEFAULT_GROK_SOURCE_GAP_MAX_ARTICLES):
            break

    current_domains = sorted(
        {
            _compact_text(article.get("source_domain", ""), 80)
            for article in raw_articles
            if isinstance(article, dict) and str(article.get("source_domain", "") or "").strip()
        }
    )[:30]

    parsed = _call_xai_json(
        system_prompt=GROK_SOURCE_GAP_SYSTEM,
        user_prompt=json.dumps(
            {
                "task": (
                    "Từ source mix hiện tại và các bài mạnh/yếu trong batch, "
                    "gợi ý tối đa 4 source gap đáng mở rộng để bắt tin official tốt hơn. "
                    "Các gợi ý chỉ là hint, cần dễ verify thủ công."
                ),
                "editor_feedback": " ".join(str(feedback_summary_text or "").split())[:800],
                "current_source_domains": current_domains,
                "top_batch_articles": candidate_payload,
            },
            ensure_ascii=False,
            indent=2,
        ),
        schema_name="grok_source_gap_suggestions",
        schema=GROK_SOURCE_GAP_SCHEMA,
        max_tokens=1000,
    )

    suggestions: list[dict[str, Any]] = []
    for item in parsed.get("suggestions", []) if isinstance(parsed, dict) else []:
        if not isinstance(item, dict):
            continue
        priority = str(item.get("priority", "medium") or "medium").strip().lower()
        if priority not in {"high", "medium", "low"}:
            priority = "medium"
        suggestions.append(
            {
                "focus": _compact_text(item.get("focus", ""), 80),
                "priority": priority,
                "suggested_query": _compact_text(item.get("suggested_query", ""), 120),
                "suggested_feed_hint": _compact_text(item.get("suggested_feed_hint", ""), 120),
                "rationale": _compact_text(item.get("rationale", ""), 220),
            }
        )

    return {
        "suggestions": suggestions[:4],
        "batch_note": _compact_text(parsed.get("batch_note", ""), 220) if isinstance(parsed, dict) else "",
    }


def scout_web_search_articles(
    *,
    query: str,
    allowed_domains: list[str],
    existing_urls: list[str] | None = None,
    existing_titles: list[str] | None = None,
    max_articles: int = 3,
) -> dict[str, Any]:
    prompt = json.dumps(
        {
            "task": (
                "Tìm các bài AI mới, quan trọng trong 72 giờ gần nhất. "
                "Ưu tiên founder-grade signal và nguồn mạnh. "
                "Không lấy GitHub repo/release hay Facebook social post."
            ),
            "query": query,
            "avoid_urls": [url for url in (existing_urls or []) if str(url).strip()][:20],
            "avoid_titles": [title for title in (existing_titles or []) if str(title).strip()][:20],
            "max_articles": max(1, min(5, int(max_articles))),
            "output_rules": [
                "Trả về JSON object có key articles và batch_note.",
                "Mỗi article phải có title, url, published_at, source_domain, summary, why_it_matters.",
                "Nếu không tìm thấy bài đủ mạnh, trả mảng articles rỗng.",
            ],
        },
        ensure_ascii=False,
        indent=2,
    )
    response_json = _call_xai_responses_with_web_search(
        prompt=prompt,
        allowed_domains=allowed_domains,
        max_output_tokens=1400,
    )
    parsed = _extract_json_object(_extract_response_output_text(response_json))
    articles: list[dict[str, Any]] = []
    for item in parsed.get("articles", []) if isinstance(parsed, dict) else []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url", "") or "").strip()
        title = _compact_text(item.get("title", ""), 220)
        if not url or not title:
            continue
        articles.append(
            {
                "title": title,
                "url": url,
                "published_at": _compact_text(item.get("published_at", ""), 64),
                "source_domain": _compact_text(item.get("source_domain", ""), 80),
                "summary": _compact_text(item.get("summary", ""), 320),
                "why_it_matters": _compact_text(item.get("why_it_matters", ""), 220),
            }
        )
    return {
        "articles": articles[: max(1, min(5, int(max_articles)))],
        "batch_note": _compact_text(parsed.get("batch_note", ""), 220) if isinstance(parsed, dict) else "",
    }


def scout_x_posts(
    *,
    query: str,
    allowed_x_handles: list[str],
    excluded_x_handles: list[str] | None = None,
    existing_urls: list[str] | None = None,
    existing_titles: list[str] | None = None,
    max_posts: int = 3,
) -> dict[str, Any]:
    today = datetime.now(timezone.utc).date()
    from_date = (today - timedelta(days=3)).isoformat()
    to_date = today.isoformat()
    prompt = json.dumps(
        {
            "task": (
                "Tìm các post mới trên X trong 72 giờ gần nhất về AI / công nghệ / GitHub repo đáng chú ý cho founder và team AI. "
                "Ưu tiên post có link ra nguồn official, release note, benchmark, GitHub repo hoặc phân tích có chiều sâu."
            ),
            "query": query,
            "from_date": from_date,
            "to_date": to_date,
            "allowed_handles": [str(handle).strip().lstrip("@") for handle in allowed_x_handles if str(handle).strip()][:10],
            "excluded_handles": [str(handle).strip().lstrip("@") for handle in (excluded_x_handles or []) if str(handle).strip()][:20],
            "avoid_urls": [url for url in (existing_urls or []) if str(url).strip()][:20],
            "avoid_titles": [title for title in (existing_titles or []) if str(title).strip()][:20],
            "max_posts": max(1, min(5, int(max_posts))),
            "output_rules": [
                "Trả về JSON object có key posts và batch_note.",
                "Mỗi post phải có title, post_url, linked_url, published_at, author_handle, summary, why_it_matters.",
                "linked_url để trống nếu post không link ra nguồn ngoài.",
                "Nếu không tìm thấy post đủ mạnh, trả mảng posts rỗng.",
            ],
        },
        ensure_ascii=False,
        indent=2,
    )
    response_json = _call_xai_responses_with_x_search(
        prompt=prompt,
        allowed_x_handles=[str(handle).strip().lstrip("@") for handle in allowed_x_handles if str(handle).strip()],
        excluded_x_handles=excluded_x_handles,
        from_date=from_date,
        to_date=to_date,
        max_output_tokens=1600,
    )
    parsed = _extract_json_object(_extract_response_output_text(response_json))
    posts: list[dict[str, Any]] = []
    for item in parsed.get("posts", []) if isinstance(parsed, dict) else []:
        if not isinstance(item, dict):
            continue
        post_url = str(item.get("post_url", "") or "").strip()
        title = _compact_text(item.get("title", ""), 220)
        if not post_url or not title:
            continue
        posts.append(
            {
                "title": title,
                "post_url": post_url,
                "linked_url": str(item.get("linked_url", "") or "").strip(),
                "published_at": _compact_text(item.get("published_at", ""), 64),
                "author_handle": _compact_text(item.get("author_handle", ""), 80).lstrip("@"),
                "summary": _compact_text(item.get("summary", ""), 320),
                "why_it_matters": _compact_text(item.get("why_it_matters", ""), 220),
            }
        )
    return {
        "posts": posts[: max(1, min(5, int(max_posts)))],
        "batch_note": _compact_text(parsed.get("batch_note", ""), 220) if isinstance(parsed, dict) else "",
    }
