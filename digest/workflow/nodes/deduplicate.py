"""
deduplicate.py — LangGraph node: Lọc bài trùng lặp.

Sử dụng 2 tầng dedup:
  1. SQLite (db.py): kiểm tra URL hash — nhanh, chính xác
  2. ChromaDB (memory.py): kiểm tra bài tương tự theo nội dung — thông minh

Quá trình:
  - Đọc raw_articles từ state
  - Loại bỏ bài đã có trong SQLite (theo URL)
  - Với mỗi bài mới, kiểm tra memory xem có bài cùng chủ đề không
  - Nếu có bài cũ cùng chủ đề → gắn thông tin "related_past" để Agent biết
  - Ghi new_articles vào state
"""

from __future__ import annotations

import logging
import re
from typing import Any

# Đảm bảo project root nằm trong sys.path

from digest.storage.db import is_duplicate
from digest.storage.memory import recall_similar

logger = logging.getLogger(__name__)

STRICT_OFFTOPIC_DOMAINS = {
    "jingyan.baidu.com",
    "tudientiengviet.org",
    "rung.vn",
}

SOFT_OFFTOPIC_DOMAINS = {
    "zhihu.com",
}

STRICT_OFFTOPIC_TITLE_RE = re.compile(
    r"\b(task manager|quan ly tac vu|quản lý tác vụ|mở task manager|mo task manager|"
    r"任务管理器|win10打开任务管理器|win11怎么打开任务管理器|"
    r"poco x8|điện thoại|dien thoai|smartphone|camera|"
    r"mặt trăng|mat trang|xăng dầu|xang dau|dầu mỏ|dau mo|"
    r"là gì|la gi|từ đồng nghĩa|trái nghĩa|từ điển|dictionary)\b",
    re.IGNORECASE,
)


def _editorial_skip_reason(article: dict[str, Any]) -> str:
    domain = str(article.get("source_domain", "") or "").lower()
    title = str(article.get("title", "") or "")
    url = str(article.get("url", "") or "")
    combined = f"{title} {url}".strip()
    is_ai_relevant = article.get("is_ai_relevant")

    if domain in STRICT_OFFTOPIC_DOMAINS:
        return f"strict_offtopic_domain:{domain}"

    if STRICT_OFFTOPIC_TITLE_RE.search(combined):
        return "strict_offtopic_title"

    if domain in SOFT_OFFTOPIC_DOMAINS and is_ai_relevant is not True:
        return f"soft_offtopic_domain:{domain}"

    if is_ai_relevant is False and not article.get("content_available", False):
        return "weak_ai_signal_thin_content"

    return ""


def deduplicate_node(state: dict[str, Any]) -> dict[str, Any]:
    """
    LangGraph node: lọc bài trùng lặp và gắn context lịch sử.

    Input (từ state):
        raw_articles: list bài từ gather_news

    Output (ghi vào state):
        new_articles: list bài chưa từng thấy, kèm related_past
    """
    raw = state.get("raw_articles", [])
    if not raw:
        logger.info("📭 Không có bài viết nào cần lọc.")
        return {"new_articles": []}

    new_articles = []
    dup_count = 0

    for article in raw:
        url = article.get("url", "")
        if not url:
            continue

        if not article.get("is_news_candidate", True):
            logger.info(
                "⏭️ Skip non-news candidate: %s (%s)",
                article.get("title", "N/A")[:60],
                ", ".join(article.get("acquisition_flags", [])) or "unknown",
            )
            continue

        editorial_skip_reason = _editorial_skip_reason(article)
        if editorial_skip_reason:
            logger.info(
                "⏭️ Skip editorial off-topic: %s (%s)",
                article.get("title", "N/A")[:60],
                editorial_skip_reason,
            )
            continue

        # ── Tầng 1: SQLite URL hash check ───────────────────────────
        if is_duplicate(url):
            dup_count += 1
            continue

        # ── Tầng 2: Memory recall — tìm bài cũ cùng chủ đề ────────
        title = article.get("title", "")
        summary = article.get("summary", article.get("snippet", ""))
        query_text = f"{title} {summary}"

        try:
            similar = recall_similar(query_text, n_results=3)
            # Chỉ giữ bài thực sự tương tự (cosine distance < 0.3)
            related = [
                s for s in similar
                if s.get("distance", 1.0) < 0.3
            ]
            if related:
                article["related_past"] = related
                logger.debug(
                    "🔗 Bài '%s' liên quan đến %d bài cũ",
                    title[:40], len(related)
                )
        except Exception as e:
            logger.debug("Memory recall skipped: %s", e)

        new_articles.append(article)

    logger.info(
        "🔍 Dedup: %d bài gốc → %d bài mới (%d trùng lặp bỏ qua)",
        len(raw), len(new_articles), dup_count
    )

    return {"new_articles": new_articles}
