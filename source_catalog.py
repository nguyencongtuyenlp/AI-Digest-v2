"""
source_catalog.py — Compatibility facade cho source layer.

Repo hiện vẫn import từ module này ở nhiều nơi. Để refactor dần mà không làm
vỡ code cũ, module này re-export các phần đã được tách ra:
- source_registry.py: danh sách nguồn/queries mặc định
- source_policy.py: trust tiers, blocklists, classify_source_kind
- source_runtime.py: watchlist + social inbox runtime paths
"""

from source_policy import (
    BLOCKED_DOMAINS,
    EXTRACTION_BLOCKED_DOMAINS,
    OFFICIAL_SOURCE_DOMAINS,
    SOURCE_PRIORITY_BY_KIND,
    STRONG_MEDIA_DOMAINS,
    SUPPLEMENTAL_BLOCKED_DOMAINS,
    SUPPLEMENTAL_LOW_QUALITY_DOMAINS,
    SUPPLEMENTAL_REVIEW_DOMAINS,
    SUPPLEMENTAL_TRUSTED_DOMAINS,
    classify_source_kind,
)
from source_registry import (
    CURATED_RSS_FEEDS,
    DEFAULT_GITHUB_ORGS,
    DEFAULT_GITHUB_REPOS,
    DEFAULT_GITHUB_SEARCH_QUERIES,
    DEFAULT_HN_KEYWORDS,
    DEFAULT_REDDIT_SUBREDDITS,
    DEFAULT_TELEGRAM_CHANNELS,
    SEARCH_QUERIES_EN,
    build_search_queries_vn,
)
from source_runtime import (
    SOCIAL_SIGNAL_INBOX_DEFAULT_FILE,
    WATCHLIST_DEFAULT_FILE,
    load_watchlist_seeds,
    social_signal_inbox_path,
)

__all__ = [
    "BLOCKED_DOMAINS",
    "CURATED_RSS_FEEDS",
    "DEFAULT_GITHUB_ORGS",
    "DEFAULT_GITHUB_REPOS",
    "DEFAULT_GITHUB_SEARCH_QUERIES",
    "DEFAULT_HN_KEYWORDS",
    "DEFAULT_REDDIT_SUBREDDITS",
    "DEFAULT_TELEGRAM_CHANNELS",
    "EXTRACTION_BLOCKED_DOMAINS",
    "OFFICIAL_SOURCE_DOMAINS",
    "SEARCH_QUERIES_EN",
    "SOCIAL_SIGNAL_INBOX_DEFAULT_FILE",
    "SOURCE_PRIORITY_BY_KIND",
    "STRONG_MEDIA_DOMAINS",
    "SUPPLEMENTAL_BLOCKED_DOMAINS",
    "SUPPLEMENTAL_LOW_QUALITY_DOMAINS",
    "SUPPLEMENTAL_REVIEW_DOMAINS",
    "SUPPLEMENTAL_TRUSTED_DOMAINS",
    "WATCHLIST_DEFAULT_FILE",
    "build_search_queries_vn",
    "classify_source_kind",
    "load_watchlist_seeds",
    "social_signal_inbox_path",
]
