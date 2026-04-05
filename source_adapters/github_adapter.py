from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import requests

GITHUB_AGENT_SIGNAL_KEYWORDS = (
    "agent",
    "agents",
    "agentic",
    "claude code",
    "codex",
    "mcp",
    "model context protocol",
    "browser-use",
    "browser use",
    "plugin",
    "tool use",
    "workflow",
    "orchestration",
    "memory",
    "multi-agent",
    "multi agent",
    "server",
    "sdk",
)


def has_github_agent_signal(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(keyword in lowered for keyword in GITHUB_AGENT_SIGNAL_KEYWORDS)


def github_headers(*, request_headers: dict[str, str], github_token: str = "") -> dict[str, str]:
    headers = dict(request_headers)
    headers["Accept"] = "application/vnd.github+json"
    headers["X-GitHub-Api-Version"] = "2022-11-28"
    if github_token.strip():
        headers["Authorization"] = f"Bearer {github_token.strip()}"
    return headers


def github_get_json(
    path: str,
    *,
    request_headers: dict[str, str],
    github_token: str = "",
    params: dict[str, Any] | None = None,
    logger: logging.Logger | None = None,
) -> Any:
    try:
        response = requests.get(
            f"https://api.github.com{path}",
            headers=github_headers(request_headers=request_headers, github_token=github_token),
            params=params or None,
            timeout=20,
        )
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        if logger:
            logger.warning("GitHub API request failed for %s: %s", path, exc)
        return None


def valid_github_repo_full_name(value: str) -> bool:
    parts = [segment.strip() for segment in str(value or "").split("/") if segment.strip()]
    return len(parts) == 2


def build_github_repo_article(
    repo: dict[str, Any],
    *,
    source: str,
    query_context: str = "",
    is_founder_grade_candidate_fn: Callable[[str, str, str, str], bool],
    truncate_text_fn: Callable[[str, int], str],
) -> dict[str, Any] | None:
    full_name = str(repo.get("full_name", "") or "").strip()
    html_url = str(repo.get("html_url", "") or "").strip()
    description = str(repo.get("description", "") or "").strip()
    topics = repo.get("topics", []) or []
    language = str(repo.get("language", "") or "").strip()
    owner = str((repo.get("owner") or {}).get("login", "") or "").strip()
    stars = int(repo.get("stargazers_count", 0) or 0)
    forks = int(repo.get("forks_count", 0) or 0)
    updated_at = str(repo.get("pushed_at") or repo.get("updated_at") or "")

    surface_text = " ".join(
        part for part in [full_name, description, " ".join(str(topic) for topic in topics), language, query_context] if part
    )
    if not is_founder_grade_candidate_fn(full_name, description, html_url, surface_text):
        return None
    if source in {"GitHub API Search"} or source.startswith("GitHub API Org:"):
        if not has_github_agent_signal(surface_text):
            return None

    summary_bits = [
        f"GitHub repo: {full_name}" if full_name else "",
        f"owner={owner}" if owner else "",
        f"language={language}" if language else "",
        f"stars={stars}",
        f"forks={forks}",
        f"topics={', '.join(str(topic) for topic in topics[:8])}" if topics else "",
        f"watchlist_query={query_context}" if query_context else "",
    ]
    content = " | ".join(bit for bit in summary_bits if bit)
    if description:
        content = f"{content}\n\n{description}" if content else description

    return {
        "title": full_name or html_url,
        "url": html_url,
        "source": source,
        "snippet": truncate_text_fn(description or content, 500),
        "content": truncate_text_fn(content, 4000),
        "published": updated_at,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "acquisition_quality": "high",
        "github_signal_type": "repository",
        "github_full_name": full_name,
        "github_owner": owner,
        "github_stars": stars,
        "source_kind": "github",
        "source_priority": 90,
        "community_signal_strength": 2,
    }


def build_github_release_article(
    release: dict[str, Any],
    *,
    repo_full_name: str,
    source: str,
    is_founder_grade_candidate_fn: Callable[[str, str, str, str], bool],
    truncate_text_fn: Callable[[str, int], str],
) -> dict[str, Any] | None:
    html_url = str(release.get("html_url", "") or "").strip()
    tag_name = str(release.get("tag_name", "") or "").strip()
    name = str(release.get("name", "") or "").strip()
    body = str(release.get("body", "") or "").strip()
    published = str(release.get("published_at") or release.get("created_at") or "")
    title = name or tag_name or f"{repo_full_name} release"
    combined = " ".join(part for part in [repo_full_name, title, body] if part)
    if not is_founder_grade_candidate_fn(title, body[:800], html_url, combined):
        return None

    content = " | ".join(
        bit
        for bit in [
            f"GitHub release: {repo_full_name}",
            f"tag={tag_name}" if tag_name else "",
            f"title={title}" if title else "",
        ]
        if bit
    )
    if body:
        content = f"{content}\n\n{body}" if content else body

    return {
        "title": f"{repo_full_name} — {title}",
        "url": html_url,
        "source": source,
        "snippet": truncate_text_fn(body or title, 500),
        "content": truncate_text_fn(content, 4000),
        "published": published,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "acquisition_quality": "high",
        "github_signal_type": "release",
        "github_full_name": repo_full_name,
        "source_kind": "github",
        "source_priority": 90,
        "community_signal_strength": 2,
    }


def fetch_github_articles(
    *,
    repo_watchlist: list[str],
    org_watchlist: list[str],
    query_watchlist: list[str],
    max_releases_per_repo: int,
    max_org_repos: int,
    max_search_results: int,
    request_headers: dict[str, str],
    github_token: str,
    logger: logging.Logger,
    is_founder_grade_candidate_fn: Callable[[str, str, str, str], bool],
    truncate_text_fn: Callable[[str, int], str],
) -> list[dict[str, Any]]:
    """
    Thu tín hiệu GitHub theo 3 lớp:
    - repo watchlist: repo metadata + release mới
    - org watchlist: repo update gần đây
    - query watchlist: search repo/topic mới nổi
    """
    articles: list[dict[str, Any]] = []

    for repo_full_name in repo_watchlist:
        if not valid_github_repo_full_name(repo_full_name):
            logger.warning("Skip invalid GitHub repo watchlist entry: %s", repo_full_name)
            continue

        repo = github_get_json(
            f"/repos/{repo_full_name}",
            request_headers=request_headers,
            github_token=github_token,
            logger=logger,
        )
        if isinstance(repo, dict):
            article = build_github_repo_article(
                repo,
                source=f"GitHub API Repo: {repo_full_name}",
                is_founder_grade_candidate_fn=is_founder_grade_candidate_fn,
                truncate_text_fn=truncate_text_fn,
            )
            if article:
                articles.append(article)

        if max_releases_per_repo <= 0:
            continue
        releases = github_get_json(
            f"/repos/{repo_full_name}/releases",
            request_headers=request_headers,
            github_token=github_token,
            params={"per_page": max_releases_per_repo},
            logger=logger,
        )
        if not isinstance(releases, list):
            continue
        for release in releases[:max_releases_per_repo]:
            article = build_github_release_article(
                release,
                repo_full_name=repo_full_name,
                source=f"GitHub API Release: {repo_full_name}",
                is_founder_grade_candidate_fn=is_founder_grade_candidate_fn,
                truncate_text_fn=truncate_text_fn,
            )
            if article:
                articles.append(article)

    for org in org_watchlist:
        org_name = str(org or "").strip()
        if not org_name:
            continue
        repos = github_get_json(
            f"/orgs/{org_name}/repos",
            request_headers=request_headers,
            github_token=github_token,
            params={"sort": "updated", "direction": "desc", "per_page": max_org_repos},
            logger=logger,
        )
        if not isinstance(repos, list):
            continue
        for repo in repos[:max_org_repos]:
            article = build_github_repo_article(
                repo,
                source=f"GitHub API Org: {org_name}",
                query_context=f"org:{org_name}",
                is_founder_grade_candidate_fn=is_founder_grade_candidate_fn,
                truncate_text_fn=truncate_text_fn,
            )
            if article:
                articles.append(article)

    search_cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).date().isoformat()
    for query in query_watchlist:
        search_query = str(query or "").strip()
        if not search_query:
            continue
        payload = github_get_json(
            "/search/repositories",
            request_headers=request_headers,
            github_token=github_token,
            params={
                "q": f"{search_query} pushed:>={search_cutoff}",
                "sort": "updated",
                "order": "desc",
                "per_page": max_search_results,
            },
            logger=logger,
        )
        items = payload.get("items", []) if isinstance(payload, dict) else []
        for repo in items[:max_search_results]:
            article = build_github_repo_article(
                repo,
                source="GitHub API Search",
                query_context=search_query,
                is_founder_grade_candidate_fn=is_founder_grade_candidate_fn,
                truncate_text_fn=truncate_text_fn,
            )
            if article:
                articles.append(article)

    return articles

