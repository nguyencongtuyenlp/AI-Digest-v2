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
            if lower.startswith("query:"):
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

    return {
        "urls": list(dict.fromkeys(urls)),
        "queries": list(dict.fromkeys(queries)),
        "github_repos": list(dict.fromkeys(github_repos)),
        "github_orgs": list(dict.fromkeys(github_orgs)),
        "github_queries": list(dict.fromkeys(github_queries)),
    }


def social_signal_inbox_path(project_root: Path) -> Path:
    return Path(
        os.getenv("SOCIAL_SIGNAL_INBOX_FILE", "") or (project_root / SOCIAL_SIGNAL_INBOX_DEFAULT_FILE)
    )
