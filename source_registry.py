"""
source_registry.py — Danh sách nguồn mặc định cho Daily Digest.

File này chỉ giữ "chúng ta đang theo dõi cái gì":
- RSS feeds
- search queries
- social/community seeds
- GitHub seeds
"""

from __future__ import annotations

import os

# ── RSS feeds ưu tiên: blog chính thức + media mạnh + Việt Nam ─────────────────
# Ghi theo thứ tự ưu tiên gần với nhu cầu founder/startup hơn.
CURATED_RSS_FEEDS: list[str] = [
    # Official / primary sources
    "https://openai.com/news/rss.xml",
    "https://openai.com/blog/rss.xml",
    "https://ai.meta.com/blog/rss.xml/",
    "https://huggingface.co/blog/feed.xml",
    "https://blog.google/technology/ai/rss/",
    "https://blogs.nvidia.com/blog/category/deep-learning/feed/",
    "https://www.theneuron.ai/rss",
    "https://simonwillison.net/atom/everything/",
    "https://blog.google/technology/ai/rss",
    "https://deepmind.google/discover/blog/rss.xml",
    "https://deepmind.google/blog/rss.xml",
    "https://about.fb.com/news/feed/",
    "https://ai.meta.com/blog/rss.xml/",
    "https://blogs.microsoft.com/ai/feed",
    "https://nvidianews.nvidia.com/releases.xml",
    "https://www.nvidia.com/en-us/research/ai/rss",
    "https://aws.amazon.com/blogs/machine-learning/feed/",
    "https://www.databricks.com/blog/category/ai/feed",
    "https://www.cloudflare.com/rss/tag/ai/",
    "https://cohere.com/blog/rss",
    # Strong media / ecosystem
    "https://techcrunch.com/category/artificial-intelligence/feed/",
    "https://www.theverge.com/rss/index.xml",
    "https://feeds.arstechnica.com/arstechnica/technology-lab",
    "https://news.mit.edu/rss/topic/artificial-intelligence2",
    # Vietnam sources
    "https://genk.vn/rss/ai.rss",
]


# ── Search queries chỉ giữ vai trò bổ sung, không còn là nguồn lõi ────────────
SEARCH_QUERIES_EN: list[str] = [
    "OpenAI product update API model release",
    "Anthropic Claude API Claude Code release notes",
    "Google AI Gemini DeepMind product update",
    "Meta AI model release open source product update",
    "Hugging Face product update open source AI release",
    "Mistral AI model release API update",
    "AI agents enterprise workflow launch",
    "MCP server release AI agents",
    "browser use agent workflow tutorial",
    "AI workflow practical guide enterprise team",
]


def build_search_queries_vn() -> list[str]:
    current_year = str(os.getenv("DIGEST_CURRENT_YEAR", "")).strip()
    year = current_year or "2026"
    return [
        "công cụ AI mới cho doanh nghiệp Việt Nam",
        "doanh nghiệp Việt Nam ứng dụng AI",
        "workflow AI thực tế cho team Việt Nam",
        f"sản phẩm AI Việt Nam {year}",
        f"ứng dụng AI trong giáo dục và công việc tại Việt Nam {year}",
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
    "MachineLearning",
    "artificial",
    "LocalLLaMA",
    "ChatGPT",
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
