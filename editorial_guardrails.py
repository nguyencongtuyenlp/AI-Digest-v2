"""
editorial_guardrails.py - Shared helpers for grounding, safe summaries, and
Telegram sanity checks.

These helpers are deterministic on purpose. They give the pipeline a stable
fallback when the LLM over-infers or formats output poorly.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from html import escape, unescape
from typing import Any
from urllib.parse import urlparse, urlsplit, urlunsplit

from digest_formatter import TYPE_ORDER, canonical_type_name, type_emoji

MARKDOWN_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
HTML_LINK_RE = re.compile(r'<a\s+href="([^"]+)"[^>]*>', re.IGNORECASE)
ANCHOR_BLOCK_RE = re.compile(r'<a\s+href="[^"]+"[^>]*>.*?</a>', re.IGNORECASE | re.DOTALL)
RAW_URL_RE = re.compile(r"https?://\S+")
DELIVERY_PREFIX_RE = re.compile(r"^Ý chính của tin này là:\s*", re.IGNORECASE)
NON_VIETNAMESE_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
NON_VIETNAMESE_PUNCT_RE = re.compile(r"[。]+")
RISKY_PHRASE_RE = re.compile(
    r"\b(chắc chắn|hoàn toàn|đã chứng minh|không thể phủ nhận|rõ ràng sẽ|cam kết|bảo đảm)\b",
    re.IGNORECASE,
)
INTERNAL_COPY_PATTERNS = (
    (
        re.compile(r"Tin này đã có trong brief 8h sáng; mình nhắc lại để bạn tiện theo dõi\.?\s*", re.IGNORECASE),
        "",
    ),
    (
        re.compile(r"Mục này đang nhắc lại các tin đã có trong brief 8h sáng để bạn tiện theo dõi khi chạy thử\.?\s*", re.IGNORECASE),
        "",
    ),
    (
        re.compile(r"Bài này hiện được giữ ở lớp sàng lọc sơ bộ[^.]*\.", re.IGNORECASE),
        "",
    ),
    (
        re.compile(r"Tạm thời chỉ nên theo dõi, chưa đáng chiếm slot suy luận 32B ở vòng đầu\.?", re.IGNORECASE),
        "",
    ),
    (
        re.compile(r"Điểm đáng chú ý nhất là điểm đáng quan tâm là", re.IGNORECASE),
        "Điểm đáng chú ý là",
    ),
    (
        re.compile(r"\bĐây là tín hiệu yếu[^.]*\.?", re.IGNORECASE),
        "",
    ),
    (
        re.compile(r"\b(?:Tạm thời\s*)?chỉ nên theo dõi(?: thêm)?[^.]*\.?", re.IGNORECASE),
        "",
    ),
    (
        re.compile(r"\b(?:Team|Doanh nghiệp|Startup|Người làm sản phẩm|Người vận hành)[^.]*\bnên\b[^.]*\.?", re.IGNORECASE),
        "",
    ),
    (
        re.compile(
            r"(?:,?\s*(?:nhưng|tuy nhiên|song|đồng thời)?\s*)?"
            r"(?:do\s+)?(?:Nguồn tin|Nguồn bài|Dữ liệu|Phản ứng thị trường)[^.]*"
            r"\b(?:yếu|hạn chế|chưa rõ|chưa được xác thực|không đáng tin cậy)[^.]*\.?",
            re.IGNORECASE,
        ),
        "",
    ),
    (
        re.compile(r"\b(?:Cần đọc thận trọng|Cần thêm thời gian để đánh giá|Chưa nên hành động)[^.]*\.?", re.IGNORECASE),
        "",
    ),
    (
        re.compile(r"\bhiện phù hợp để theo dõi thêm\b\.?", re.IGNORECASE),
        "",
    ),
)
DANGLING_END_RE = re.compile(r"(,?\s*(nhưng|tuy nhiên|do|do đó|vì vậy|song|đồng thời|đặc biệt là))[\s,.!?:;…-]*$", re.IGNORECASE)
TITLE_NOISE_PATTERNS = (
    re.compile(r"\s*-\s*\.:.*?:\.\s*", re.IGNORECASE),
    re.compile(r"\s*\|\s*(ai daily brief|thinking partner|official site)\s*$", re.IGNORECASE),
)
ARCHIVE_BLOCKED_DOMAINS = {
    "stackoverflow.com",
    "support.google.com",
    "bing.com",
    "news.google.com",
    "chouseisan.com",
}
ARCHIVE_LOW_SIGNAL_TITLES = (
    "startup - báo vietnamnet",
    "startup - bao vietnamnet",
    "artificial intelligence | mit news | massachusetts institute of technology",
    "google deepmind - ai research & foundation models | tossom",
)
ARCHIVE_AI_SIGNAL_RE = re.compile(
    r"\b(ai|artificial intelligence|tri tue nhan tao|trí tuệ nhân tạo|llm|model|agent|agents|"
    r"openai|anthropic|claude|gpt|gemini|deepmind|hugging face|huggingface|xai|grok|"
    r"nvidia|inference|training|transcription|asr|benchmark|robotics|robot|chip|gpu)\b",
    re.IGNORECASE,
)
ARCHIVE_OFF_TOPIC_RE = re.compile(
    r"\b(mac pro|oil|tankers|hormuz|iran|middle east|zelenskyy|ukraine aid|troops|stocks|wall street|"
    r"private credit|saudi|football|showbiz|weather|celebrity|camera roundup)\b",
    re.IGNORECASE,
)
ARCHIVE_AI_FRIENDLY_DOMAINS = {
    "openai.com",
    "anthropic.com",
    "deepmind.google",
    "ai.googleblog.com",
    "blog.google",
    "research.google",
    "huggingface.co",
    "developer.nvidia.com",
    "techcrunch.com",
    "theverge.com",
    "arstechnica.com",
    "github.com",
}
ARCHIVE_MIN_REPLAY_SCORE = 35


def _format_archive_day(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    normalized = raw.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return ""
    return dt.strftime("%d/%m/%Y")


def _clean_text(text: Any, max_len: int = 400) -> str:
    cleaned = unescape(str(text or ""))
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[: max_len - 1].rstrip() + "…"


def _strip_note_prefix(text: str) -> str:
    cleaned = _clean_text(text, max_len=900)
    return DELIVERY_PREFIX_RE.sub("", cleaned).strip()


def _today_label(today: date | datetime | None = None) -> str:
    value = today or datetime.now()
    if isinstance(value, datetime):
        return value.strftime("%d/%m")
    return value.strftime("%d/%m")


def _type_label(article: dict[str, Any]) -> str:
    ptype = canonical_type_name(article.get("primary_type"))
    emoji = str(article.get("primary_emoji", type_emoji(ptype)) or type_emoji(ptype)).strip()
    return f"{emoji} {ptype}"


def _confidence_label_vi(confidence_label: str) -> str:
    mapping = {
        "high": "cao",
        "medium": "vừa",
        "low": "thăm dò",
    }
    return mapping.get(str(confidence_label or "").lower(), "thăm dò")


def _looks_like_missing_community(text: str) -> bool:
    lowered = text.lower()
    return (
        not lowered
        or "không tìm thấy phản ứng cộng đồng" in lowered
        or "chưa có dữ liệu cộng đồng" in lowered
        or "khong tim thay phan ung cong dong" in lowered
    )


def _qualified_articles(final_articles: list[dict[str, Any]], threshold: int = 30) -> list[dict[str, Any]]:
    return sorted(
        [article for article in final_articles if int(article.get("total_score", 0) or 0) >= threshold],
        key=lambda article: int(article.get("total_score", 0) or 0),
        reverse=True,
    )


def _decision_rank(article: dict[str, Any]) -> int:
    decision = str(article.get("delivery_decision", "") or "").lower()
    if decision == "include":
        return 2
    if decision == "review":
        return 1
    return 0


def _clean_archive_summary(summary: str) -> str:
    cleaned = _strip_note_prefix(summary)
    cleaned = re.sub(
        r"Tin này đã có trong brief 8h sáng; mình nhắc lại để bạn tiện theo dõi\.?\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\s*Đây là tín hiệu yếu.*$", "", cleaned, flags=re.IGNORECASE).strip()
    return cleaned


def _clean_title_text(title: Any) -> str:
    cleaned = _clean_text(title, max_len=220)
    for pattern in TITLE_NOISE_PATTERNS:
        cleaned = pattern.sub(" ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -|")
    return cleaned or "Untitled"


def _ensure_complete_sentence(text: str) -> str:
    cleaned = DANGLING_END_RE.sub("", text).strip(" ,;:-")
    if not cleaned:
        return "Đang cập nhật chi tiết trong bài viết."
    if cleaned[-1] not in ".!?…":
        cleaned += "."
    return cleaned


def sanitize_delivery_text(text: Any, max_len: int = 320) -> str:
    """
    Làm sạch copy trước khi lên Telegram:
    - giải mã HTML entities
    - bỏ jargon nội bộ
    - tránh câu bị cụt / kết thúc lơ lửng
    """
    cleaned = _clean_text(text, max_len=max_len * 2)
    cleaned = _strip_note_prefix(cleaned)
    cleaned = NON_VIETNAMESE_CJK_RE.sub(" ", cleaned)
    cleaned = NON_VIETNAMESE_PUNCT_RE.sub(" ", cleaned)
    for pattern, replacement in INTERNAL_COPY_PATTERNS:
        cleaned = pattern.sub(replacement, cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = _ensure_complete_sentence(cleaned)
    if len(cleaned) <= max_len:
        return cleaned
    shortened = cleaned[:max_len].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return _ensure_complete_sentence(shortened)


def _source_domain(article: dict[str, Any]) -> str:
    raw_domain = _clean_text(article.get("source_domain", ""), 120)
    if raw_domain:
        return raw_domain

    url = str(article.get("url", "") or "").strip()
    if not url:
        return ""

    try:
        return urlparse(url).netloc.replace("www.", "")
    except ValueError:
        return ""


def _normalize_link_url(value: Any) -> str:
    raw = unescape(str(value or "")).strip()
    if not raw:
        return ""

    try:
        parsed = urlsplit(raw)
    except ValueError:
        return raw

    if not parsed.scheme or not parsed.netloc:
        return raw

    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")

    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            path,
            parsed.query,
            "",
        )
    )


def _archive_is_ai_core(article: dict[str, Any], domain: str) -> bool:
    """Giữ archive replay bám AI-core để preview nhìn như một brief thật."""
    title = _clean_text(article.get("title", ""), 260)
    summary = _clean_text(article.get("summary", article.get("summary_vi", "")), 500)
    combined = f"{title} {summary}".strip()

    ai_hits = len(ARCHIVE_AI_SIGNAL_RE.findall(combined))
    off_topic_hits = len(ARCHIVE_OFF_TOPIC_RE.findall(combined))

    if domain in ARCHIVE_AI_FRIENDLY_DOMAINS:
        return True
    if off_topic_hits and ai_hits == 0:
        return False
    if ai_hits >= 1:
        return True
    return False


def _article_link(article: dict[str, Any], notion_map_by_title: dict[str, str], notion_map_by_source_url: dict[str, str]) -> str:
    title = str(article.get("title", "") or "")
    source_url = str(article.get("url", "") or "")
    return notion_map_by_source_url.get(source_url, "") or notion_map_by_title.get(title, "") or source_url


def _prepare_archive_articles(history_articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    for article in history_articles:
        if not isinstance(article, dict):
            continue

        domain = _source_domain(article)
        title = _clean_text(article.get("title", ""), 220).lower()
        score = int(article.get("relevance_score", 0) or 0)
        if domain in ARCHIVE_BLOCKED_DOMAINS:
            continue
        if title in ARCHIVE_LOW_SIGNAL_TITLES:
            continue
        if score < ARCHIVE_MIN_REPLAY_SCORE:
            continue
        if not _archive_is_ai_core(article, domain):
            continue

        canonical_type = canonical_type_name(article.get("primary_type"))
        prepared.append(
            {
                "title": article.get("title", ""),
                "url": article.get("url", ""),
                "source": article.get("source", ""),
                "source_domain": domain,
                "primary_type": canonical_type,
                "primary_emoji": type_emoji(canonical_type),
                "note_summary_vi": article.get("summary", ""),
                "summary_vi": article.get("summary", ""),
                "total_score": score,
                "created_at": article.get("created_at", ""),
                "is_repeat": True,
            }
        )
    return prepared


def _select_section_articles(
    current_articles: list[dict[str, Any]],
    archive_articles: list[dict[str, Any]],
    section_type: str,
    per_type: int,
    *,
    allow_archive_replay: bool = True,
    allow_high_priority_overflow: bool = False,
) -> list[dict[str, Any]]:
    current_bucket = []
    for article in current_articles:
        canonical_type = canonical_type_name(article.get("primary_type"))
        if canonical_type != section_type:
            continue
        if article.get("is_ai_relevant") is False:
            continue
        if str(article.get("delivery_decision", "")).lower() == "skip":
            continue

        normalized = dict(article)
        normalized["primary_type"] = canonical_type
        normalized["primary_emoji"] = type_emoji(canonical_type)
        normalized["is_repeat"] = False
        current_bucket.append(normalized)

    current_bucket.sort(
        key=lambda article: (
            int(article.get("grok_final_rank_score", -1) or -1),
            int(article.get("grok_priority_score", -1) or -1),
            _decision_rank(article),
            int(article.get("delivery_score", 0) or 0),
            int(article.get("total_score", 0) or 0),
            -float(article.get("age_hours", 9999) or 9999),
        ),
        reverse=True,
    )

    selected: list[dict[str, Any]] = current_bucket[:per_type]
    if allow_high_priority_overflow and current_bucket:
        selected_ids = {id(article) for article in selected}
        overflow_candidates = [
            article
            for article in current_bucket
            if id(article) not in selected_ids
            and str(article.get("delivery_decision", "") or "").lower() == "include"
            and (
                int(article.get("grok_final_rank_score", -1) or -1) >= 90
                or int(article.get("grok_priority_score", -1) or -1) >= 88
                or (
                    int(article.get("grok_priority_score", -1) or -1) >= 82
                    and int(article.get("total_score", 0) or 0) >= 80
                )
                or (
                    int(article.get("delivery_score", 0) or 0) >= 13
                    and int(article.get("total_score", 0) or 0) >= 78
                )
            )
        ]
        if overflow_candidates:
            selected.append(overflow_candidates[0])

    archive_limit = per_type if selected else min(per_type, 2)
    if len(selected) >= per_type:
        if not allow_archive_replay:
            return selected
        if len(selected) > per_type:
            return selected
    if not allow_archive_replay:
        return selected

    seen_urls = {str(article.get("url", "") or "") for article in selected}
    seen_titles = {str(article.get("title", "") or "") for article in selected}

    archive_bucket = []
    for article in archive_articles:
        canonical_type = canonical_type_name(article.get("primary_type"))
        if canonical_type != section_type:
            continue
        url = str(article.get("url", "") or "")
        title = str(article.get("title", "") or "")
        if not title or url in seen_urls or title in seen_titles:
            continue
        normalized = dict(article)
        normalized["primary_type"] = canonical_type
        normalized["primary_emoji"] = type_emoji(canonical_type)
        normalized["is_repeat"] = True
        archive_bucket.append(normalized)

    archive_bucket.sort(
        key=lambda article: (
            str(article.get("created_at", "") or ""),
            int(article.get("total_score", 0) or 0),
        ),
        reverse=True,
    )

    for article in archive_bucket[:archive_limit]:
        selected.append(article)
        if len(selected) >= archive_limit:
            break

    return selected


def build_safe_digest_messages(
    final_articles: list[dict[str, Any]],
    notion_pages: list[dict[str, Any]],
    history_articles: list[dict[str, Any]] | None = None,
    today: date | datetime | None = None,
    per_type: int = 3,
    *,
    allow_archive_replay: bool = True,
    include_empty_sections: bool = True,
    allow_high_priority_overflow: bool = False,
) -> list[str]:
    """Build six deterministic Telegram messages, one per type."""
    today_label = _today_label(today)
    notion_map_by_title = {
        str(page.get("title", "")): str(page.get("url", ""))
        for page in notion_pages
        if isinstance(page, dict)
    }
    notion_map_by_source_url = {
        str(page.get("source_url", "")): str(page.get("url", ""))
        for page in notion_pages
        if isinstance(page, dict)
    }
    archive = _prepare_archive_articles(history_articles or [])
    messages: list[str] = []

    for section_type, emoji in TYPE_ORDER:
        selected = _select_section_articles(
            final_articles,
            archive,
            section_type,
            per_type=per_type,
            allow_archive_replay=allow_archive_replay,
            allow_high_priority_overflow=allow_high_priority_overflow,
        )
        if not selected and not include_empty_sections:
            continue
        lines = [f"<b>{emoji} {escape(section_type)} | AI Daily Brief | {today_label}</b>"]

        if not selected:
            lines.extend(
                [
                    "",
                    "Chưa có tin nổi bật cho nhóm này ở lượt chạy hiện tại.",
                ]
            )
            messages.append("\n".join(lines).strip())
            continue

        for index, article in enumerate(selected, 1):
            title = escape(_clean_title_text(article.get("title", "Untitled")))
            base_summary = (
                article.get("telegram_blurb_vi")
                or article.get("telegram_news_blurb_vi")
                or article.get("note_summary_vi")
                or article.get("summary_vi")
                or article.get("editorial_angle")
                or article.get("title", "")
            )
            summary = sanitize_delivery_text(_clean_archive_summary(str(base_summary or "")))
            if not summary:
                summary = "Đang cập nhật chi tiết trong bài viết."
            if article.get("is_repeat"):
                replay_day = _format_archive_day(
                    article.get("published_at")
                    or article.get("published")
                    or article.get("created_at")
                )
                if replay_day:
                    summary = f"{summary} ({replay_day})"
                else:
                    summary = f"{summary} (Tin cu)"

            link = _article_link(article, notion_map_by_title, notion_map_by_source_url)
            block_lines = [
                "",
                f"{index}. <b>{title}</b>",
                escape(summary),
            ]
            if link:
                block_lines.append(f'<a href="{escape(link, quote=True)}">Đọc thêm</a>')
            lines.extend(block_lines)

        messages.append("\n".join(lines).strip())

    return messages


def build_article_grounding(article: dict[str, Any]) -> dict[str, Any]:
    """
    Build deterministic grounding metadata so downstream prompts can separate:
    - fact anchors from metadata and known evidence state
    - reasonable inferences
    - unknowns / missing verification
    """
    facts: list[str] = []
    inferences: list[str] = []
    unknowns: list[str] = []
    caution_flags: list[str] = []

    source = _clean_text(article.get("source", ""), 120)
    domain = _clean_text(article.get("source_domain", ""), 120)
    published_at = _clean_text(article.get("published_at", article.get("published", "")), 120)
    content_available = bool(article.get("content_available", False))
    source_verified = bool(article.get("source_verified", False))
    source_tier = str(article.get("source_tier", "unknown") or "unknown").lower()
    total_score = int(article.get("total_score", 0) or 0)
    age_hours = article.get("age_hours")
    community = _clean_text(article.get("community_reactions") or article.get("community_signal_summary"), 1200)
    related_past = article.get("related_past", []) or []

    if source or domain:
        source_bits = [bit for bit in [source, domain] if bit]
        facts.append(f"Nguồn đang dùng để đọc tin: {' | '.join(source_bits)}.")
    if published_at:
        facts.append(f"Có mốc thời gian nguồn: {published_at}.")
    else:
        unknowns.append("Chưa có published_at chuẩn, nên độ mới của tin chưa chắc chắn.")

    if age_hours is not None:
        facts.append(f"Bài được hệ thống ước tính khoảng {age_hours} giờ tuổi.")

    if content_available:
        facts.append("Có nội dung nguồn đủ dài để đối chiếu, không chỉ dựa vào tiêu đề.")
    else:
        unknowns.append("Không có toàn văn đủ dài, nên không nên khẳng định chi tiết tính năng hay kết quả.")

    if source_verified:
        facts.append("Nguồn được heuristic hệ thống xếp vào nhóm đã xác minh ở mức cơ bản.")
    else:
        caution_flags.append("Nguồn chưa được heuristic xác minh, cần giữ giọng điệu thận trọng.")

    if source_tier in {"a", "b"}:
        facts.append(f"Nguồn nằm ở tier {source_tier.upper()}, độ tin cậy hệ thống tương đối tốt.")
    else:
        caution_flags.append(f"Nguồn tier {source_tier.upper() if source_tier else 'UNKNOWN'}, không nên suy diễn mạnh.")

    if community and not _looks_like_missing_community(community):
        facts.append("Có thêm tín hiệu cộng đồng hoặc nguồn phụ để đối chiếu ngữ cảnh.")
    else:
        unknowns.append("Chưa có dữ liệu cộng đồng đáng tin để kiểm tra phản ứng thị trường.")

    if related_past:
        inferences.append(
            f"Chủ đề này đã xuất hiện trong memory ({len(related_past)} bài liên quan), có thể là diễn biến tiếp nối."
        )
    else:
        inferences.append("Chưa có nhiều ngữ cảnh lịch sử trong memory, nên chỉ nên kết luận trong phạm vi bài hiện tại.")

    if total_score >= 70 and content_available and source_tier in {"a", "b"}:
        confidence_label = "high"
        grounding_note = (
            "Nền tảng bằng chứng của bài này khá tốt: có nguồn tương đối mạnh và đủ dữ liệu để phân tích thực tế."
        )
    elif total_score >= 45 and content_available:
        confidence_label = "medium"
        grounding_note = (
            "Bài này có tín hiệu dùng được, nhưng vẫn cần phân biệt phần nguồn nói trực tiếp với phần suy luận vận hành."
        )
    else:
        confidence_label = "low"
        grounding_note = (
            "Bài này đang có nền tảng bằng chứng yếu hoặc thiếu dữ liệu, nên chỉ nên xem như tín hiệu theo dõi."
        )

    if not caution_flags and confidence_label == "high":
        caution_flags.append("Vẫn cần tách dữ kiện trong nguồn khỏi khuyến nghị nội bộ.")

    return {
        "confidence_label": confidence_label,
        "grounding_note": grounding_note,
        "fact_anchors": facts,
        "fact_anchors_text": "\n".join(f"- {item}" for item in facts) if facts else "- Chưa có fact anchor mạnh.",
        "reasonable_inferences": inferences,
        "reasonable_inferences_text": "\n".join(f"- {item}" for item in inferences)
        if inferences
        else "- Không có suy luận bổ sung.",
        "unknowns": unknowns,
        "unknowns_text": "\n".join(f"- {item}" for item in unknowns) if unknowns else "- Không có unknown lớn từ metadata.",
        "caution_flags": caution_flags,
        "caution_flags_text": "\n".join(f"- {item}" for item in caution_flags)
        if caution_flags
        else "- Không có cảnh báo lớn ngoài việc cần đọc thận trọng như thường lệ.",
    }


def build_safe_digest(
    final_articles: list[dict[str, Any]],
    notion_pages: list[dict[str, Any]],
    history_articles: list[dict[str, Any]] | None = None,
    today: date | datetime | None = None,
    max_articles: int = 6,
    *,
    allow_archive_replay: bool = True,
    include_empty_sections: bool = True,
    allow_high_priority_overflow: bool = False,
) -> str:
    """
    Deterministic HTML fallback digest for Telegram.
    """
    messages = build_safe_digest_messages(
        final_articles,
        notion_pages,
        history_articles=history_articles,
        today=today,
        per_type=min(max_articles, 3),
        allow_archive_replay=allow_archive_replay,
        include_empty_sections=include_empty_sections,
        allow_high_priority_overflow=allow_high_priority_overflow,
    )
    return "\n\n".join(messages).strip()


def validate_telegram_summary(
    summary: str,
    final_articles: list[dict[str, Any]],
    notion_pages: list[dict[str, Any]],
    today: date | datetime | None = None,
) -> list[str]:
    """
    Return deterministic warnings for risky or malformed Telegram output.
    """
    warnings: list[str] = []
    expected_header = f"<b>AI Daily Brief | {_today_label(today)}</b>"
    text = summary or ""
    notion_urls = {
        _normalize_link_url(page.get("url", ""))
        for page in notion_pages
        if isinstance(page, dict) and page.get("url")
    }
    source_urls = {
        _normalize_link_url(article.get("url", ""))
        for article in final_articles
        if isinstance(article, dict) and article.get("url")
    }
    qualified = _qualified_articles(final_articles)
    weak_articles = [
        article for article in qualified
        if build_article_grounding(article)["confidence_label"] == "low"
    ]

    if not text.strip().startswith("<b>"):
        warnings.append("missing_or_wrong_header")
    elif expected_header not in text and "AI Daily Brief |" not in text:
        warnings.append("missing_or_wrong_header")
    if MARKDOWN_LINK_RE.search(text):
        warnings.append("markdown_links_present")
    if "<br" in text.lower():
        warnings.append("br_tag_present")
    if len(text) > 4096:
        warnings.append("message_too_long")
    if "\n---" in text or text.strip().startswith("---"):
        warnings.append("separator_style_present")
    numbered_lines = re.findall(r"^\d+\..*$", text, re.MULTILINE)
    if "<b>" in text and "AI Daily Brief |" not in text:
        warnings.append("missing_type_label")
    if text.count("<b>") != text.count("</b>"):
        warnings.append("unbalanced_bold_tags")
    if text.count("<a href=") != text.count("</a>"):
        warnings.append("unbalanced_anchor_tags")

    hrefs = [_normalize_link_url(url) for url in HTML_LINK_RE.findall(text)]
    allowed_urls = notion_urls | source_urls
    if allowed_urls and numbered_lines and not hrefs:
        warnings.append("missing_notion_links")
    if allowed_urls and any(url and url not in allowed_urls for url in hrefs):
        warnings.append("unknown_links_present")
    text_without_anchors = ANCHOR_BLOCK_RE.sub("", text)
    if RAW_URL_RE.search(text_without_anchors):
        warnings.append("raw_urls_present")

    if weak_articles and RISKY_PHRASE_RE.search(text):
        warnings.append("overclaim_language_with_weak_sources")

    return warnings


def validate_telegram_messages(
    messages: list[str],
    final_articles: list[dict[str, Any]],
    notion_pages: list[dict[str, Any]],
    today: date | datetime | None = None,
) -> list[str]:
    """Validate each Telegram message independently."""
    warnings: list[str] = []
    if len(messages) > len(TYPE_ORDER):
        warnings.append("wrong_message_count")

    for index, message in enumerate(messages, 1):
        for warning in validate_telegram_summary(message, final_articles, notion_pages, today=today):
            warnings.append(f"msg{index}:{warning}")

    return warnings
