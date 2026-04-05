"""
source_policy.py — Chính sách phân loại và trust/source tiers cho Daily Digest.

File này chỉ giữ câu hỏi:
- nguồn nào được tin hơn
- domain nào bị chặn
- source kind nào được gán priority nào
"""

from __future__ import annotations

OFFICIAL_SOURCE_DOMAINS: list[str] = [
    "openai.com",
    "anthropic.com",
    "about.fb.com",
    "blog.google",
    "deepmind.google",
    "huggingface.co",
    "blogs.microsoft.com",
    "nvidianews.nvidia.com",
    "aws.amazon.com",
    "databricks.com",
    "cloudflare.com",
]

STRONG_MEDIA_DOMAINS: list[str] = [
    "techcrunch.com",
    "theverge.com",
    "arstechnica.com",
    "news.mit.edu",
    "cnbc.com",
    "reuters.com",
    "bloomberg.com",
]


# ── Chặn nguồn không nên lên digest ────────────────────────────────────────────
BLOCKED_DOMAINS: list[str] = [
    "wikipedia.org",
    "tripadvisor.com",
    "amazon.com",
    "ebay.com",
    "linkedin.com",
    "facebook.com",
    "twitter.com",
    "x.com",
    "instagram.com",
    "youtube.com",
    "pinterest.com",
    "yelp.com",
    "booking.com",
    "bing.com",
    "search.yahoo.com",
    "yahoo.com",
    "doubleclick.net",
    "googleadservices.com",
    "dichvucong.gov.vn",
    "support.google.com",
    "stackoverflow.com",
]


# ── Những domain hay 403 khi cố extract nội dung ──────────────────────────────
EXTRACTION_BLOCKED_DOMAINS: list[str] = [
    "ft.com",
    "chatgpt.com",
    "x.ai",
    "grok.x.ai",
]


# ── Nguồn search/query bổ sung nên lọc chặt hơn RSS/watchlist URL trực tiếp ───
SUPPLEMENTAL_BLOCKED_DOMAINS: list[str] = [
    "tudientiengviet.org",
    "rung.vn",
]

SUPPLEMENTAL_TRUSTED_DOMAINS: list[str] = [
    "openai.com",
    "anthropic.com",
    "about.fb.com",
    "blog.google",
    "googleblog.com",
    "deepmind.google",
    "huggingface.co",
    "aws.amazon.com",
    "techcrunch.com",
    "theverge.com",
    "arstechnica.com",
    "news.mit.edu",
    "genk.vn",
    "nvidianews.nvidia.com",
    "blogs.microsoft.com",
    "databricks.com",
    "cloudflare.com",
    "cnbc.com",
]

SUPPLEMENTAL_REVIEW_DOMAINS: list[str] = [
    "substack.com",
    "newatlas.com",
    "fortune.com",
    "marktechpost.com",
    "biopharmatrend.com",
    "erp.today",
    "perplexity.ai",
    "thenewstack.io",
    "vnptai.io",
    "vneconomy.vn",
]

SUPPLEMENTAL_LOW_QUALITY_DOMAINS: list[str] = [
    "analyticsinsight.net",
    "cometapi.com",
    "digitalapplied.com",
    "free-llm.com",
    "grokipedia.com",
    "intuitionlabs.ai",
    "iq.com",
    "mem0.ai",
    "newmarketpitch.com",
]

SOURCE_PRIORITY_BY_KIND: dict[str, int] = {
    "official": 95,
    "github": 90,
    "strong_media": 85,
    "regional_media": 80,
    "watchlist": 82,
    "community": 74,
    "search": 66,
    "manual": 70,
    "review": 58,
    "unknown": 45,
}


def classify_source_kind(
    *,
    source: str = "",
    domain: str = "",
    acquisition_quality: str = "",
    social_signal: bool = False,
    github_signal_type: str = "",
) -> tuple[str, int]:
    normalized_source = str(source or "").strip().lower()
    normalized_domain = str(domain or "").strip().lower()
    normalized_quality = str(acquisition_quality or "").strip().lower()
    normalized_github_signal_type = str(github_signal_type or "").strip().lower()

    if social_signal:
        return "community", SOURCE_PRIORITY_BY_KIND["community"]
    if normalized_domain == "github.com" or normalized_github_signal_type in {"repository", "release"}:
        return "github", SOURCE_PRIORITY_BY_KIND["github"]
    if normalized_domain in OFFICIAL_SOURCE_DOMAINS:
        return "official", SOURCE_PRIORITY_BY_KIND["official"]
    if normalized_domain in STRONG_MEDIA_DOMAINS:
        return "strong_media", SOURCE_PRIORITY_BY_KIND["strong_media"]
    if normalized_domain in {"vnexpress.net", "vietnamnet.vn", "vtv.vn", "genk.vn", "nhandan.vn"}:
        return "regional_media", SOURCE_PRIORITY_BY_KIND["regional_media"]
    if normalized_source.startswith("watchlist") or normalized_quality == "high":
        return "watchlist", SOURCE_PRIORITY_BY_KIND["watchlist"]
    if (
        normalized_source.startswith("reddit")
        or normalized_source.startswith("hacker news")
        or normalized_source.startswith("hn")
        or normalized_source.startswith("telegram")
    ):
        return "community", SOURCE_PRIORITY_BY_KIND["community"]
    if normalized_source.startswith("duckduckgo"):
        return "search", SOURCE_PRIORITY_BY_KIND["search"]
    if normalized_source.startswith("manual") or normalized_quality == "manual":
        return "manual", SOURCE_PRIORITY_BY_KIND["manual"]
    if normalized_quality == "review":
        return "review", SOURCE_PRIORITY_BY_KIND["review"]
    return "unknown", SOURCE_PRIORITY_BY_KIND["unknown"]

