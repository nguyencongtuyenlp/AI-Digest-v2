"""
feedback_loop.py — Thu feedback từ Telegram và biến nó thành context nhẹ cho agent.

Phiên bản đầu tiên ưu tiên tính thực dụng:
- lấy feedback bằng Telegram Bot API getUpdates
- chấp nhận feedback qua reply vào bot hoặc @bot hoặc #feedback
- gán nhãn heuristic đơn giản để downstream dễ dùng
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any

import requests

from db import get_meta, get_recent_feedback, save_feedback, set_meta

logger = logging.getLogger(__name__)

FEEDBACK_META_KEY = "telegram_feedback_last_update_id"
FEEDBACK_COMMAND_GUIDE = [
    "@bot cũ",
    "@bot nguồn yếu",
    "@bot không liên quan",
    "@bot đáng đọc",
    "@bot đào sâu hơn",
    "@bot ưu tiên founder",
    "@bot không nên lên brief",
    "@bot nên lên brief",
    "@bot sai loại business",
]


def _derive_feedback_preferences(
    entries: list[dict[str, Any]],
    label_counter: Counter[str],
) -> dict[str, Any]:
    expected_type_counter: Counter[str] = Counter()
    for entry in entries:
        for label in entry.get("labels", []) or []:
            if str(label).startswith("expected_type:"):
                expected_type_counter[str(label).split(":", 1)[1].strip()] += 1

    promote_count = int(label_counter.get("promote_delivery", 0) or 0)
    skip_count = int(label_counter.get("skip_delivery", 0) or 0)
    preference_profile = {
        "strict_source_review": int(label_counter.get("weak_source", 0) or 0) >= 2,
        "prefer_founder_angle": int(label_counter.get("founder_lens", 0) or 0) >= 1,
        "prefer_depth": int(label_counter.get("want_more_depth", 0) or 0) >= 1,
        "prefer_freshness": int(label_counter.get("stale", 0) or 0) >= 1,
        "delivery_bias": "promote" if promote_count > skip_count else "suppress" if skip_count > promote_count else "neutral",
        "preferred_types": [key for key, _count in expected_type_counter.most_common(3)],
    }
    return preference_profile


def _bot_username(bot_token: str) -> str:
    configured = str(os.getenv("TELEGRAM_BOT_USERNAME", "") or "").strip().lstrip("@")
    if configured:
        return configured

    try:
        response = requests.get(
            f"https://api.telegram.org/bot{bot_token}/getMe",
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        return str(payload.get("result", {}).get("username", "") or "").strip().lstrip("@")
    except Exception as exc:
        logger.debug("Không lấy được bot username từ Telegram: %s", exc)
        return ""


def _clean_feedback_text(text: str, bot_username: str = "") -> str:
    cleaned = str(text or "").strip()
    if not cleaned:
        return ""

    if bot_username:
        cleaned = re.sub(rf"@{re.escape(bot_username)}\b", "", cleaned, flags=re.IGNORECASE)

    cleaned = cleaned.strip()
    cleaned = re.sub(r"#feedback\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^(feedback|phan hoi|phản hồi)\s*[:\-]?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" :-")
    return cleaned


def _structured_feedback_labels(cleaned_text: str) -> list[str]:
    lowered = cleaned_text.lower()
    labels: list[str] = []

    exact_map = {
        "cũ": "stale",
        "tin cũ": "stale",
        "nguồn yếu": "weak_source",
        "không liên quan": "not_relevant",
        "dang doc": "good_pick",
        "đáng đọc": "good_pick",
        "đào sâu hơn": "want_more_depth",
        "uu tien founder": "founder_lens",
        "ưu tiên founder": "founder_lens",
        "không nên lên brief": "skip_delivery",
        "khong nen len brief": "skip_delivery",
        "nên lên brief": "promote_delivery",
        "nen len brief": "promote_delivery",
    }
    if lowered in exact_map:
        labels.append(exact_map[lowered])

    if "sai loại" in lowered or "sai loai" in lowered:
        labels.append("wrong_type")
        for type_name in ("research", "product", "business", "policy", "society", "practical"):
            if type_name in lowered:
                labels.append(f"expected_type:{type_name}")
                break

    return labels


def _feedback_labels(text: str, bot_username: str = "") -> list[str]:
    cleaned = _clean_feedback_text(text, bot_username=bot_username)
    lowered = cleaned.lower()
    labels: list[str] = []

    labels.extend(_structured_feedback_labels(cleaned))

    if any(token in lowered for token in ("cũ", "tin cu", "stale", "old news", "không mới", "khong moi")):
        labels.append("stale")
    if any(token in lowered for token in ("nguồn yếu", "nguon yeu", "source yếu", "source yeu", "không tin", "khong tin")):
        labels.append("weak_source")
    if any(token in lowered for token in ("không liên quan", "khong lien quan", "not relevant", "irrelevant")):
        labels.append("not_relevant")
    if any(token in lowered for token in ("đào sâu", "dao sau", "deeper", "chi tiết hơn", "chi tiet hon")):
        labels.append("want_more_depth")
    if any(token in lowered for token in ("hay", "tốt", "tot", "good pick", "ổn", "on")):
        labels.append("good_pick")
    if any(token in lowered for token in ("founder", "sếp", "sep", "operator", "startup")):
        labels.append("founder_lens")
    if any(token in lowered for token in ("không nên lên brief", "khong nen len brief", "đừng lên brief", "dung len brief")):
        labels.append("skip_delivery")
    if any(token in lowered for token in ("nên lên brief", "nen len brief", "đưa lên brief", "dua len brief")):
        labels.append("promote_delivery")

    if not labels:
        labels.append("general_feedback")
    return list(dict.fromkeys(labels))


def _is_feedback_message(message: dict[str, Any], bot_username: str) -> bool:
    text = str(message.get("text", "") or message.get("caption", "") or "").strip()
    if not text:
        return False

    if "#feedback" in text.lower():
        return True

    if re.match(r"^(feedback|phan hoi|phản hồi)\b", text, flags=re.IGNORECASE):
        return True

    reply = message.get("reply_to_message", {}) or {}
    if reply.get("from", {}).get("is_bot"):
        return True

    if bot_username and re.search(rf"@{re.escape(bot_username)}\b", text, flags=re.IGNORECASE):
        return True

    return False


def sync_feedback_from_telegram(limit: int = 50) -> dict[str, Any]:
    """
    Đồng bộ feedback từ Telegram Bot API.
    Nếu có lỗi mạng hoặc chưa cấu hình bot token thì chỉ skip mềm.
    """
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        return {"synced": 0, "skipped": 0, "error": "missing_bot_token"}

    bot_username = _bot_username(bot_token)
    offset_raw = get_meta(FEEDBACK_META_KEY, "0")
    try:
        offset = int(offset_raw or 0)
    except ValueError:
        offset = 0

    try:
        response = requests.get(
            f"https://api.telegram.org/bot{bot_token}/getUpdates",
            params={"offset": offset + 1, "limit": limit, "timeout": 1},
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        logger.debug("Telegram feedback sync skipped: %s", exc)
        return {"synced": 0, "skipped": 0, "error": str(exc)}

    updates = payload.get("result", []) or []
    synced = 0
    skipped = 0
    max_update_id = offset

    for update in updates:
        update_id = int(update.get("update_id", 0) or 0)
        max_update_id = max(max_update_id, update_id)
        message = update.get("message") or update.get("edited_message") or {}
        if not message:
            skipped += 1
            continue

        if not _is_feedback_message(message, bot_username):
            skipped += 1
            continue

        text = str(message.get("text", "") or message.get("caption", "") or "").strip()
        created_ts = float(message.get("date", 0) or 0)
        created_at = (
            datetime.fromtimestamp(created_ts, tz=timezone.utc).isoformat()
            if created_ts
            else datetime.now(timezone.utc).isoformat()
        )
        saved = save_feedback(
            update_id=update_id,
            chat_id=str(message.get("chat", {}).get("id", "") or ""),
            message_id=int(message.get("message_id", 0) or 0),
            user_name=str(
                message.get("from", {}).get("username")
                or message.get("from", {}).get("first_name")
                or "unknown"
            ),
            text=text,
            labels_json=json.dumps(_feedback_labels(text, bot_username=bot_username), ensure_ascii=False),
            created_at=created_at,
        )
        if saved:
            synced += 1

    if max_update_id > offset:
        set_meta(FEEDBACK_META_KEY, str(max_update_id))

    return {"synced": synced, "skipped": skipped, "error": ""}


def build_feedback_context(days: int = 14, limit: int = 20) -> dict[str, Any]:
    """
    Tạo summary gọn cho scoring/report.
    Không đẩy cả đống feedback thô vào prompt để tránh nhiễu.
    """
    entries = get_recent_feedback(days=days, limit=limit)
    if not entries:
        return {
            "recent_feedback": [],
            "feedback_summary_text": (
                "Chưa có feedback mới từ team.\n"
                "Lệnh gợi ý: " + " | ".join(FEEDBACK_COMMAND_GUIDE)
            ),
            "feedback_label_counts": {},
            "feedback_preference_profile": {
                "strict_source_review": False,
                "prefer_founder_angle": False,
                "prefer_depth": False,
                "prefer_freshness": False,
                "delivery_bias": "neutral",
                "preferred_types": [],
            },
        }

    label_counter: Counter[str] = Counter()
    lines: list[str] = []
    normalized_entries: list[dict[str, Any]] = []

    for entry in entries:
        try:
            labels = json.loads(entry.get("labels_json", "[]") or "[]")
        except json.JSONDecodeError:
            labels = []
        labels = [str(label) for label in labels]
        label_counter.update(labels)
        normalized_entries.append({**entry, "labels": labels})
        label_text = ", ".join(labels) if labels else "general_feedback"
        lines.append(f"- {entry.get('user_name', 'team')}: {entry.get('text', '')[:180]} [{label_text}]")

    top_labels = ", ".join(f"{label}={count}" for label, count in label_counter.most_common(5))
    summary_text = (
        f"Feedback gần đây từ team ({len(entries)} mục). "
        f"Nhãn nổi bật: {top_labels or 'chưa phân loại rõ'}.\n"
        + "\n".join(lines[:8])
        + "\n\nLệnh gợi ý: "
        + " | ".join(FEEDBACK_COMMAND_GUIDE)
    )
    preference_profile = _derive_feedback_preferences(normalized_entries, label_counter)

    return {
        "recent_feedback": normalized_entries,
        "feedback_summary_text": summary_text,
        "feedback_label_counts": dict(label_counter),
        "feedback_preference_profile": preference_profile,
    }
