"""
source_catalog.py — Danh mục nguồn và seed config cho Daily Digest.

Tách riêng file này để:
- dễ mở rộng nguồn mà không làm `gather_news.py` quá dài
- dễ review cùng sếp xem hệ đang theo dõi những nguồn nào
- dễ chỉnh watchlist thủ công theo gu nguồn của team
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

# ── RSS feeds ưu tiên: blog chính thức + media mạnh + Việt Nam ─────────────────
# Ghi theo thứ tự ưu tiên gần với nhu cầu founder/startup hơn.
CURATED_RSS_FEEDS: list[str] = [
    # Official / primary sources
    "https://openai.com/news/rss.xml",
    "https://www.anthropic.com/news/rss.xml",
    "https://blog.google/technology/ai/rss/",
    "https://deepmind.google/discover/blog/rss.xml",
    "https://about.fb.com/news/feed/",
    "https://huggingface.co/blog/feed.xml",
    "https://blogs.microsoft.com/ai/feed/",
    "https://nvidianews.nvidia.com/releases.xml",
    "https://aws.amazon.com/blogs/machine-learning/feed/",
    "https://www.databricks.com/blog/category/ai/feed",
    "https://www.cloudflare.com/rss/tag/ai/",
    # Strong media / ecosystem
    "https://techcrunch.com/category/artificial-intelligence/feed/",
    "https://www.theverge.com/ai-artificial-intelligence/rss/index.xml",
    "https://feeds.arstechnica.com/arstechnica/technology-lab",
    "https://news.mit.edu/rss/topic/artificial-intelligence2",
    # Vietnam sources
    "https://genk.vn/rss/ai.rss",
]


# ── Search queries chỉ giữ vai trò bổ sung, không còn là nguồn lõi ────────────
SEARCH_QUERIES_EN: list[str] = [
    "OpenAI API model release enterprise update",
    "Anthropic Claude API model release",
    "Anthropic Claude Code release enterprise",
    "Google DeepMind AI research benchmark",
    "Meta open source AI model release",
    "xAI Grok enterprise API update",
    "HuggingFace open source AI release",
    "AI infrastructure startup funding",
    "AI security incident model leak enterprise",
    "AI policy regulation update",
    "AI agents enterprise workflow launch",
    "MCP server release AI agents",
    "browser use agent enterprise workflow",
]


def build_search_queries_vn() -> list[str]:
    current_year = str(os.getenv("DIGEST_CURRENT_YEAR", "")).strip()
    year = current_year or "2026"
    return [
        "startup AI Việt Nam gọi vốn",
        "doanh nghiệp Việt Nam ứng dụng AI",
        "quy định luật AI Việt Nam",
        f"mô hình AI Việt Nam {year}",
        f"hạ tầng AI Việt Nam {year}",
    ]


# ── Social/community seeds ─────────────────────────────────────────────────────
DEFAULT_TELEGRAM_CHANNELS: list[str] = [
    "aivietnam",
    "MLVietnam",
    "binhdanhocai",
    "ai_mastering_vn",
    "nghienai",
]

DEFAULT_REDDIT_SUBREDDITS: list[str] = [
    "LocalLLaMA",
    "MachineLearning",
    "OpenAI",
    "Anthropic",
    "singularity",
    "artificial",
]

DEFAULT_HN_KEYWORDS: tuple[str, ...] = (
    "ai",
    "agent",
    "llm",
    "model",
    "openai",
    "anthropic",
    "claude",
    "gpt",
    "gemini",
    "deepmind",
    "nvidia",
    "hugging face",
    "robotics",
    "inference",
    "training",
)


# ── GitHub signals: repo/tool/framework discovery gần như free ─────────────────
DEFAULT_GITHUB_ORGS: list[str] = [
    "openai",
    "anthropics",
    "huggingface",
    "langchain-ai",
    "microsoft",
    "modelcontextprotocol",
]

DEFAULT_GITHUB_REPOS: list[str] = [
    "Yeachan-Heo/oh-my-claudecode",
    "anthropics/claude-code",
    "anthropics/claude-agent-sdk-python",
    "anthropics/skills",
    "anthropics/claude-plugins-official",
    "browser-use/browser-use",
    "crewAIInc/crewAI",
    "agno-agi/agno",
    "openai/codex-plugin-cc",
    "openai/codex",
    "openai/openai-agents-python",
    "langchain-ai/langgraph",
    "microsoft/autogen",
    "modelcontextprotocol/registry",
    "modelcontextprotocol/servers",
]

DEFAULT_GITHUB_SEARCH_QUERIES: list[str] = [
    "claude code",
    "ai agents framework",
    "model context protocol",
    "mcp server",
    "openclaw",
    "browser use agent",
    "computer use agent",
    "agent memory",
    "claude code plugin",
    "agent sdk",
    "llm evals tooling",
]

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
# Không phải là "cấm lấy tin", mà là "đừng cố scrape toàn văn", vì dễ tốn thời gian
# và làm log bẩn.
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


WATCHLIST_DEFAULT_FILE = "config/watchlist_seeds.txt"
SOCIAL_SIGNAL_INBOX_DEFAULT_FILE = "config/social_signal_inbox.txt"


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


def load_watchlist_seeds(project_root: Path) -> dict[str, list[str]]:
    """
    Đọc watchlist từ file + env.

    Format file:
    - URL trực tiếp: https://...
    - Query thủ công: query:Anthropic MCP
    - GitHub repo: github_repo:owner/repo
    - GitHub org: github_org:openai
    - GitHub search query: github_query:ai agents framework
    """
    urls: list[str] = []
    queries: list[str] = []
    github_repos: list[str] = []
    github_orgs: list[str] = []
    github_queries: list[str] = []

    file_path = Path(os.getenv("WATCHLIST_SEEDS_FILE", "") or (project_root / WATCHLIST_DEFAULT_FILE))
    if file_path.exists():
        for raw_line in file_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            lower = line.lower()
            if lower.startswith("github_repo:"):
                repo = line.split(":", 1)[1].strip()
                if repo:
                    github_repos.append(repo)
                continue
            if lower.startswith("github_org:"):
                org = line.split(":", 1)[1].strip()
                if org:
                    github_orgs.append(org)
                continue
            if lower.startswith("github_query:"):
                query = line.split(":", 1)[1].strip()
                if query:
                    github_queries.append(query)
                continue
            if line.lower().startswith("query:"):
                query = line.split(":", 1)[1].strip()
                if query:
                    queries.append(query)
                continue
            urls.append(line)

    env_urls = [item.strip() for item in os.getenv("WATCHLIST_URLS", "").split(",") if item.strip()]
    env_queries = [item.strip() for item in os.getenv("WATCHLIST_QUERIES", "").split("||") if item.strip()]
    env_github_repos = [item.strip() for item in os.getenv("GITHUB_WATCHLIST_REPOS", "").split(",") if item.strip()]
    env_github_orgs = [item.strip() for item in os.getenv("GITHUB_WATCHLIST_ORGS", "").split(",") if item.strip()]
    env_github_queries = [item.strip() for item in os.getenv("GITHUB_SEARCH_QUERIES", "").split("||") if item.strip()]

    urls.extend(env_urls)
    queries.extend(env_queries)
    github_repos.extend(env_github_repos)
    github_orgs.extend(env_github_orgs)
    github_queries.extend(env_github_queries)

    # Giữ thứ tự nhưng bỏ trùng.
    dedup_urls = list(dict.fromkeys(urls))
    dedup_queries = list(dict.fromkeys(queries))
    dedup_github_repos = list(dict.fromkeys(github_repos))
    dedup_github_orgs = list(dict.fromkeys(github_orgs))
    dedup_github_queries = list(dict.fromkeys(github_queries))
    return {
        "urls": dedup_urls,
        "queries": dedup_queries,
        "github_repos": dedup_github_repos,
        "github_orgs": dedup_github_orgs,
        "github_queries": dedup_github_queries,
    }


def social_signal_inbox_path(project_root: Path) -> Path:
    return Path(
        os.getenv("SOCIAL_SIGNAL_INBOX_FILE", "") or (project_root / SOCIAL_SIGNAL_INBOX_DEFAULT_FILE)
    )
