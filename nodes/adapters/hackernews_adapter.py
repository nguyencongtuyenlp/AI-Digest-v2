"""
hackernews_adapter.py - Adapter lấy AI stories từ Hacker News Algolia API.

Trả về format chuẩn cho source API:
{
    "title": str,
    "url": str,
    "source": str,
    "source_kind": "api",
    "published_at": str,
    "content": str,
    "score": 0,
}
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
from typing import Any

import requests

logger = logging.getLogger(__name__)

HN_ALGOLIA_URL = "https://hn.algolia.com/api/v1/search"


def fetch_hackernews_top_stories(
    *,
    hits_per_page: int = 30,
    min_points: int = 50,
    limit: int = 10,
    timeout: int = 20,
) -> list[dict[str, Any]]:
    """
    Lấy HN stories 24h gần nhất có score đủ mạnh.

    Graceful fallback:
    - Nếu API lỗi hoặc parse lỗi -> return []
    """
    yesterday_unix = int((datetime.now(timezone.utc) - timedelta(days=1)).timestamp())
    try:
        response = requests.get(
            HN_ALGOLIA_URL,
            params={
                "tags": "story",
                "hitsPerPage": hits_per_page,
                "numericFilters": f"points>{min_points},created_at_i>{yesterday_unix}",
            },
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json() or {}
    except Exception as exc:
        logger.warning("Hacker News Algolia fetch failed: %s", exc)
        return []

    articles: list[dict[str, Any]] = []
    for hit in list(payload.get("hits", []) or []):
        try:
            title = str(hit.get("title", "") or hit.get("story_title", "") or "").strip()
            url = str(hit.get("url", "") or hit.get("story_url", "") or "").strip()
            published_at = str(hit.get("created_at", "") or "").strip()
            points = int(hit.get("points", 0) or 0)
            num_comments = int(hit.get("num_comments", 0) or 0)
            if not title:
                continue
            hn_url = f"https://news.ycombinator.com/item?id={hit.get('objectID', '')}"
            content = f"HN score={points} comments={num_comments}".strip()
            articles.append(
                {
                    "title": title,
                    "url": url or hn_url,
                    "source": "Hacker News Algolia",
                    "source_kind": "api",
                    "published_at": published_at,
                    "content": content,
                    "score": 0,
                    "community_hint": hn_url,
                    "hn_points": points,
                    "hn_num_comments": num_comments,
                    "community_signal_strength": 4 if points >= min_points else 0,
                }
            )
        except Exception as exc:
            logger.debug("Hacker News item parse failed: %s", exc)
            continue
        if len(articles) >= limit:
            break
    return articles
