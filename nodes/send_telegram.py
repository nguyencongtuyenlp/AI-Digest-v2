"""
send_telegram.py — LangGraph node: Gửi bản tin qua Telegram.

Sử dụng python-telegram-bot, HTML parse mode.
Hỗ trợ gửi vào Topic (thread_id) trong group.
Tự động chia nhỏ nếu message > 4096 chars.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logger = logging.getLogger(__name__)


def _send_message(
    bot_token: str,
    chat_id: str,
    text: str,
    thread_id: int | None = None,
) -> bool:
    """
    Gửi 1 message qua Telegram Bot API (sync, dùng httpx).
    Tự động chia nhỏ nếu text > 4096 chars.

    Args:
        bot_token: Token của bot
        chat_id: Chat ID (group hoặc user)
        text: Nội dung HTML
        thread_id: ID của Topic trong group (None = General)

    Returns:
        True nếu gửi thành công
    """
    import httpx

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    # Chia nhỏ message nếu quá dài
    chunks = []
    if len(text) > 4096:
        # Chia theo dòng trống
        parts = text.split("\n\n")
        current = ""
        for part in parts:
            if len(current) + len(part) + 2 > 4000:
                chunks.append(current)
                current = part
            else:
                current = current + "\n\n" + part if current else part
        if current:
            chunks.append(current)
    else:
        chunks = [text]

    success = True
    for chunk in chunks:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if thread_id:
            payload["message_thread_id"] = thread_id

        try:
            resp = httpx.post(url, json=payload, timeout=30)
            if resp.status_code != 200:
                logger.error("❌ Telegram send failed: %s", resp.text[:200])
                success = False
        except Exception as e:
            logger.error("❌ Telegram send error: %s", e)
            success = False

    return success


def send_telegram_node(state: dict[str, Any]) -> dict[str, Any]:
    """
    LangGraph node: gửi các bản tin Telegram theo từng type.

    Input: telegram_messages hoặc summary_vn
    Output: telegram_sent (bool)
    """
    if not bool(state.get("publish_telegram", True)):
        logger.info("🧪 Preview mode: bỏ qua publish Telegram.")
        return {"telegram_sent": False}

    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not bot_token or not chat_id:
        logger.warning("⚠️ Telegram credentials chưa cấu hình — bỏ qua.")
        return {"telegram_sent": False}

    telegram_messages = [msg for msg in state.get("telegram_messages", []) if str(msg or "").strip()]
    if not telegram_messages:
        if str(state.get("summary_mode", "") or "") == "no_candidates":
            logger.info("📭 Không có bài nào đủ chuẩn để gửi Telegram trong run này.")
            return {"telegram_sent": False}
        summary = str(state.get("summary_vn", "") or "")
        if summary.strip():
            telegram_messages = [summary]

    if not telegram_messages:
        logger.warning("⚠️ Không có summary để gửi.")
        return {"telegram_sent": False}

    summary_mode = state.get("summary_mode", "unknown")
    summary_warnings = state.get("summary_warnings", [])
    logger.info(
        "📨 Telegram summary mode=%s warnings=%d",
        summary_mode,
        len(summary_warnings) if isinstance(summary_warnings, list) else 0,
    )

    # Thread ID cho Topic (nếu có)
    thread_id_str = os.getenv("TELEGRAM_THREAD_ID", "")
    thread_id = int(thread_id_str) if thread_id_str else None

    success = False
    if telegram_messages:
        success = True
        for index, message in enumerate(telegram_messages, 1):
            sent = _send_message(bot_token, chat_id, message, thread_id)
            success = success and sent
            if sent:
                logger.info(
                    "✅ Telegram chunk %d/%d sent to main thread=%s",
                    index,
                    len(telegram_messages),
                    thread_id,
                )
            else:
                logger.error(
                    "❌ Telegram chunk %d/%d failed for main thread=%s",
                    index,
                    len(telegram_messages),
                    thread_id,
                )

        if success:
            logger.info("✅ Telegram messages sent to chat=%s thread=%s", chat_id, thread_id)
        else:
            logger.error("❌ Telegram gửi thất bại cho main thread=%s!", thread_id)

    return {"telegram_sent": success}
