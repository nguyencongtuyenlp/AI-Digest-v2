"""
summarize.py — LangGraph node: Build the business-facing digest layout.

Output format:
- 6 fixed topics
- Up to 3 articles per topic
- Each article includes title, summary, and source link
"""

from __future__ import annotations

import logging
from typing import Any

from digest_formatter import build_digest_markdown

logger = logging.getLogger(__name__)


def summarize_node(state: dict[str, Any]) -> dict[str, Any]:
    classified = state.get("classified_articles", [])
    if not classified:
        logger.info("No articles to summarize.")
        return {"summary": "Không có bài viết nào để tổng hợp hôm nay."}

    summary = build_digest_markdown(classified, per_type=3)
    logger.info("✅ Digest layout prepared (%d chars).", len(summary))
    return {"summary": summary}
