"""
collect_feedback.py — Đồng bộ feedback từ Telegram và đưa context vào state.

Nếu Telegram feedback sync lỗi thì pipeline vẫn tiếp tục bình thường.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from feedback_loop import build_feedback_context, sync_feedback_from_telegram

logger = logging.getLogger(__name__)


def collect_feedback_node(state: dict[str, Any]) -> dict[str, Any]:
    runtime_config = dict(state.get("runtime_config", {}) or {})
    skip_sync = bool(runtime_config.get("skip_feedback_sync", False))

    try:
        if skip_sync:
            # Fast preview vẫn đọc feedback cũ trong DB, chỉ bỏ network sync để chạy nhanh hơn.
            sync_result = {"synced": 0, "skipped": 0, "mode": "local_only"}
        else:
            sync_result = sync_feedback_from_telegram(limit=50)
        context = build_feedback_context(days=14, limit=20)
        logger.info(
            "🗣️ Feedback sync: synced=%d skipped=%d",
            int(sync_result.get("synced", 0) or 0),
            int(sync_result.get("skipped", 0) or 0),
        )
        return {
            "recent_feedback": context["recent_feedback"],
            "feedback_summary_text": context["feedback_summary_text"],
            "feedback_label_counts": context["feedback_label_counts"],
            "feedback_sync": sync_result,
        }
    except Exception as exc:
        logger.warning("Feedback sync skipped: %s", exc)
        return {
            "recent_feedback": [],
            "feedback_summary_text": "Feedback sync lỗi hoặc chưa có dữ liệu.",
            "feedback_label_counts": {},
            "feedback_sync": {"synced": 0, "skipped": 0, "error": str(exc)},
        }
