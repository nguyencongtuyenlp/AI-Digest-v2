"""
source_runtime.py — Runtime paths và seed loading cho source layer.

File này giữ:
- watchlist file/env loading
- social inbox path
"""

from __future__ import annotations

import os
from pathlib import Path

WATCHLIST_DEFAULT_FILE = "config/watchlist_seeds.txt"
SOCIAL_SIGNAL_INBOX_DEFAULT_FILE = "config/social_signal_inbox.txt"


def load_watchlist_seeds(project_root: Path) -> dict[str, list[str]]:
    """
    Đọc watchlist từ file + env.

    Format file:
    - URL trực tiếp: https://...
    - Query thủ công: query:Anthropic MCP
    - GitHub repo: github_repo:owner/repo
    - GitHub org: github_org:openai
    - GitHub search query: github_query:ai agents framework
    - Strategic buckets:
      - company:OpenAI
      - product:GPT-4.1
      - tool:LangGraph
      - policy:EU AI Act
      - topic:Claude Code
    """
    urls: list[str] = []
    queries: list[str] = []
    github_repos: list[str] = []
    github_orgs: list[str] = []
    github_queries: list[str] = []
    company_watchlist: list[str] = []
    product_watchlist: list[str] = []
    tool_watchlist: list[str] = []
    policy_watchlist: list[str] = []
    topic_watchlist: list[str] = []

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
            if lower.startswith("query:"):
                query = line.split(":", 1)[1].strip()
                if query:
                    queries.append(query)
                continue
            if lower.startswith("company:"):
                company = line.split(":", 1)[1].strip()
                if company:
                    company_watchlist.append(company)
                continue
            if lower.startswith("product:"):
                product = line.split(":", 1)[1].strip()
                if product:
                    product_watchlist.append(product)
                continue
            if lower.startswith("tool:"):
                tool = line.split(":", 1)[1].strip()
                if tool:
                    tool_watchlist.append(tool)
                continue
            if lower.startswith("policy:"):
                policy = line.split(":", 1)[1].strip()
                if policy:
                    policy_watchlist.append(policy)
                continue
            if lower.startswith("topic:"):
                topic = line.split(":", 1)[1].strip()
                if topic:
                    topic_watchlist.append(topic)
                continue
            urls.append(line)

    env_urls = [item.strip() for item in os.getenv("WATCHLIST_URLS", "").split(",") if item.strip()]
    env_queries = [item.strip() for item in os.getenv("WATCHLIST_QUERIES", "").split("||") if item.strip()]
    env_github_repos = [item.strip() for item in os.getenv("GITHUB_WATCHLIST_REPOS", "").split(",") if item.strip()]
    env_github_orgs = [item.strip() for item in os.getenv("GITHUB_WATCHLIST_ORGS", "").split(",") if item.strip()]
    env_github_queries = [item.strip() for item in os.getenv("GITHUB_SEARCH_QUERIES", "").split("||") if item.strip()]
    env_companies = [item.strip() for item in os.getenv("WATCHLIST_COMPANIES", "").split("||") if item.strip()]
    env_products = [item.strip() for item in os.getenv("WATCHLIST_PRODUCTS", "").split("||") if item.strip()]
    env_tools = [item.strip() for item in os.getenv("WATCHLIST_TOOLS", "").split("||") if item.strip()]
    env_policies = [item.strip() for item in os.getenv("WATCHLIST_POLICIES", "").split("||") if item.strip()]
    env_topics = [item.strip() for item in os.getenv("WATCHLIST_TOPICS", "").split("||") if item.strip()]

    urls.extend(env_urls)
    queries.extend(env_queries)
    github_repos.extend(env_github_repos)
    github_orgs.extend(env_github_orgs)
    github_queries.extend(env_github_queries)
    company_watchlist.extend(env_companies)
    product_watchlist.extend(env_products)
    tool_watchlist.extend(env_tools)
    policy_watchlist.extend(env_policies)
    topic_watchlist.extend(env_topics)

    return {
        "urls": list(dict.fromkeys(urls)),
        "queries": list(dict.fromkeys(queries)),
        "github_repos": list(dict.fromkeys(github_repos)),
        "github_orgs": list(dict.fromkeys(github_orgs)),
        "github_queries": list(dict.fromkeys(github_queries)),
        "company_watchlist": list(dict.fromkeys(company_watchlist)),
        "product_watchlist": list(dict.fromkeys(product_watchlist)),
        "tool_watchlist": list(dict.fromkeys(tool_watchlist)),
        "policy_watchlist": list(dict.fromkeys(policy_watchlist)),
        "topic_watchlist": list(dict.fromkeys(topic_watchlist)),
    }


def social_signal_inbox_path(project_root: Path) -> Path:
    return Path(
        os.getenv("SOCIAL_SIGNAL_INBOX_FILE", "") or (project_root / SOCIAL_SIGNAL_INBOX_DEFAULT_FILE)
    )
