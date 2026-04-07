from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable

from digest.sources.source_policy import classify_source_kind

WEB_SCOUT_PLANS: tuple[dict[str, Any], ...] = (
    {
        "name": "official-vendors",
        "domains": ["openai.com", "anthropic.com", "blog.google", "deepmind.google", "huggingface.co"],
        "query": (
            "Most important new AI model, API, enterprise, agent, or release-note announcements "
            "from official vendor sources in the last 72 hours"
        ),
        "per_query_limit": 3,
    },
    {
        "name": "official-platforms",
        "domains": ["nvidianews.nvidia.com", "blogs.microsoft.com", "aws.amazon.com", "databricks.com", "cloudflare.com"],
        "query": (
            "Most important new AI infrastructure, enterprise platform, or deployment announcements "
            "from official sources in the last 72 hours"
        ),
        "per_query_limit": 3,
    },
    {
        "name": "strong-media-backstop",
        "domains": ["reuters.com", "techcrunch.com", "cnbc.com", "theverge.com", "arstechnica.com"],
        "query": (
            "Most decision-useful AI product, business, or policy stories in the last 72 hours "
            "for startup founders and operators"
        ),
        "per_query_limit": 2,
    },
)

DEFAULT_X_SCOUT_HANDLES: tuple[str, ...] = (
    "openai",
    "OpenAIDevs",
    "AnthropicAI",
    "GoogleDeepMind",
    "huggingface",
    "LangChainAI",
    "Replit",
    "cursor_ai",
)

X_SCOUT_PLANS: tuple[dict[str, Any], ...] = (
    {
        "name": "vendor-posts",
        "query": (
            "Find the most important new X posts about AI model launches, API updates, release notes, "
            "enterprise announcements, benchmarks, or open-source releases."
        ),
        "per_query_limit": 3,
    },
    {
        "name": "builder-posts",
        "query": (
            "Find new X posts about agent workflows, coding tools, GitHub repo launches, MCP ecosystem updates, "
            "or practical AI tooling that is genuinely useful to builders."
        ),
        "per_query_limit": 3,
    },
)


def build_grok_scout_article(
    item: dict[str, Any],
    *,
    plan_name: str,
    is_blocked_url_fn: Callable[[str], bool],
    is_social_signal_url_fn: Callable[[str], bool],
    is_founder_grade_candidate_fn: Callable[[str, str, str, str], bool],
    domain_from_url_fn: Callable[[str], str],
    extract_full_text_fn: Callable[[str], str],
    truncate_text_fn: Callable[[str, int], str],
) -> dict[str, Any] | None:
    url = str(item.get("url", "") or "").strip()
    title = str(item.get("title", "") or "").strip()
    snippet = " ".join(
        part for part in [
            str(item.get("summary", "") or "").strip(),
            str(item.get("why_it_matters", "") or "").strip(),
        ]
        if part
    )
    if not url or not title or is_blocked_url_fn(url):
        return None

    domain = domain_from_url_fn(url)
    if domain == "github.com" or is_social_signal_url_fn(url):
        return None
    if not is_founder_grade_candidate_fn(title, snippet, url, domain):
        return None

    content = extract_full_text_fn(url)
    if content and not is_founder_grade_candidate_fn(title, snippet, url, content[:1800]):
        return None

    source_kind, source_priority = classify_source_kind(
        source=f"Grok Scout: {plan_name}",
        domain=domain,
        acquisition_quality="review",
    )
    return {
        "title": title,
        "url": url,
        "source": f"Grok Scout: {plan_name}",
        "snippet": truncate_text_fn(snippet or title, 500),
        "content": truncate_text_fn(content or snippet, 4000),
        "published": str(item.get("published_at", "") or "").strip(),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "acquisition_quality": "review",
        "source_kind": source_kind,
        "source_priority": source_priority,
        "community_signal_strength": 0,
        "watchlist_hit": False,
        "grok_scout": True,
        "grok_scout_plan": plan_name,
    }


def build_grok_x_scout_article(
    item: dict[str, Any],
    *,
    plan_name: str,
    is_blocked_url_fn: Callable[[str], bool],
    is_founder_grade_candidate_fn: Callable[[str, str, str, str], bool],
    domain_from_url_fn: Callable[[str], str],
    truncate_text_fn: Callable[[str, int], str],
) -> dict[str, Any] | None:
    post_url = str(item.get("post_url", "") or "").strip()
    linked_url = str(item.get("linked_url", "") or "").strip()
    title = str(item.get("title", "") or "").strip()
    summary = str(item.get("summary", "") or "").strip()
    why_it_matters = str(item.get("why_it_matters", "") or "").strip()
    author_handle = str(item.get("author_handle", "") or "").strip().lstrip("@")
    target_url = linked_url or post_url
    if not target_url or not title or is_blocked_url_fn(target_url):
        return None

    if linked_url and is_blocked_url_fn(linked_url):
        linked_url = ""
        target_url = post_url

    domain = domain_from_url_fn(target_url)
    source_text = " ".join(part for part in [title, summary, why_it_matters, author_handle, linked_url, post_url] if part)
    if not is_founder_grade_candidate_fn(title, summary, target_url, source_text):
        return None

    content = "\n\n".join(
        part
        for part in [
            f"X post by @{author_handle}" if author_handle else "",
            f"Summary: {summary}" if summary else "",
            f"Why it matters: {why_it_matters}" if why_it_matters else "",
            f"Original X post: {post_url}" if post_url and post_url != target_url else "",
        ]
        if part
    )

    source_kind, source_priority = classify_source_kind(
        source=f"Grok X Scout: {plan_name}",
        domain=domain,
        acquisition_quality="review" if linked_url else "manual",
        social_signal=not bool(linked_url),
    )
    return {
        "title": title,
        "url": target_url,
        "source": f"Grok X Scout: {plan_name}{f' | @{author_handle}' if author_handle else ''}",
        "snippet": truncate_text_fn(" ".join(part for part in [summary, why_it_matters] if part), 500),
        "content": truncate_text_fn(content, 4000),
        "published": str(item.get("published_at", "") or "").strip(),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "acquisition_quality": "review" if linked_url else "manual",
        "source_kind": source_kind,
        "source_priority": source_priority,
        "community_signal_strength": 4,
        "watchlist_hit": False,
        "grok_x_scout": True,
        "grok_x_scout_plan": plan_name,
        "social_signal": not bool(linked_url),
        "social_platform": "x",
        "x_post_url": post_url,
        "x_author_handle": author_handle,
        "community_hint": post_url if post_url and post_url != target_url else "",
    }


def runtime_x_scout_handles(*, configured_handles: list[str], default_handles: tuple[str, ...] = DEFAULT_X_SCOUT_HANDLES) -> list[str]:
    handles = configured_handles or list(default_handles)
    cleaned = []
    seen: set[str] = set()
    for handle in handles:
        normalized = str(handle or "").strip().lstrip("@")
        if not normalized or normalized.lower() in seen:
            continue
        seen.add(normalized.lower())
        cleaned.append(normalized)
    return cleaned[:10]


def run_grok_scout(
    *,
    should_run: bool,
    raw_articles: list[dict[str, Any]],
    max_queries: int,
    max_articles_total: int,
    scout_web_search_articles_fn: Callable[..., dict[str, Any]],
    is_blocked_url_fn: Callable[[str], bool],
    is_social_signal_url_fn: Callable[[str], bool],
    is_founder_grade_candidate_fn: Callable[[str, str, str, str], bool],
    domain_from_url_fn: Callable[[str], str],
    extract_full_text_fn: Callable[[str], str],
    truncate_text_fn: Callable[[str, int], str],
    logger: logging.Logger,
    plans: tuple[dict[str, Any], ...] = WEB_SCOUT_PLANS,
) -> list[dict[str, Any]]:
    if not should_run:
        logger.info("⏭️ Skip Grok scout vì source mix hiện tại đã đủ mạnh.")
        return []

    existing_urls = [str(article.get("url", "") or "").strip() for article in raw_articles if str(article.get("url", "") or "").strip()]
    existing_titles = [str(article.get("title", "") or "").strip() for article in raw_articles if str(article.get("title", "") or "").strip()]

    logger.info("🧠 Grok scout: source mix yếu, sẽ web search thêm tối đa %d query.", max_queries)
    collected: list[dict[str, Any]] = []
    for plan in plans[:max_queries]:
        remaining = max_articles_total - len(collected)
        if remaining <= 0:
            break
        result = scout_web_search_articles_fn(
            query=str(plan.get("query", "") or ""),
            allowed_domains=list(plan.get("domains", []) or []),
            existing_urls=existing_urls,
            existing_titles=existing_titles,
            max_articles=min(int(plan.get("per_query_limit", 2) or 2), remaining),
        )
        plan_name = str(plan.get("name", "search") or "search")
        for item in result.get("articles", []) or []:
            article = build_grok_scout_article(
                item,
                plan_name=plan_name,
                is_blocked_url_fn=is_blocked_url_fn,
                is_social_signal_url_fn=is_social_signal_url_fn,
                is_founder_grade_candidate_fn=is_founder_grade_candidate_fn,
                domain_from_url_fn=domain_from_url_fn,
                extract_full_text_fn=extract_full_text_fn,
                truncate_text_fn=truncate_text_fn,
            )
            if not article:
                continue
            existing_urls.append(str(article.get("url", "") or ""))
            existing_titles.append(str(article.get("title", "") or ""))
            collected.append(article)
        logger.info("   Grok scout[%s]: %d bài", plan_name, len(collected))

    return collected[:max_articles_total]


def run_grok_x_scout(
    *,
    enabled: bool,
    raw_articles: list[dict[str, Any]],
    max_queries: int,
    max_articles_total: int,
    allowed_handles: list[str],
    excluded_handles: list[str],
    scout_x_posts_fn: Callable[..., dict[str, Any]],
    is_blocked_url_fn: Callable[[str], bool],
    is_founder_grade_candidate_fn: Callable[[str, str, str, str], bool],
    domain_from_url_fn: Callable[[str], str],
    truncate_text_fn: Callable[[str, int], str],
    logger: logging.Logger,
    plans: tuple[dict[str, Any], ...] = X_SCOUT_PLANS,
) -> list[dict[str, Any]]:
    if not enabled:
        return []

    existing_urls = [str(article.get("url", "") or "").strip() for article in raw_articles if str(article.get("url", "") or "").strip()]
    existing_titles = [str(article.get("title", "") or "").strip() for article in raw_articles if str(article.get("title", "") or "").strip()]

    logger.info(
        "🧠 Grok X scout: searching X with up to %d query and %d handles.",
        max_queries,
        len(allowed_handles),
    )
    collected: list[dict[str, Any]] = []
    for plan in plans[:max_queries]:
        remaining = max_articles_total - len(collected)
        if remaining <= 0:
            break
        result = scout_x_posts_fn(
            query=str(plan.get("query", "") or ""),
            allowed_x_handles=allowed_handles,
            excluded_x_handles=excluded_handles,
            existing_urls=existing_urls,
            existing_titles=existing_titles,
            max_posts=min(int(plan.get("per_query_limit", 2) or 2), remaining),
        )
        plan_name = str(plan.get("name", "x-search") or "x-search")
        for item in result.get("posts", []) or []:
            article = build_grok_x_scout_article(
                item,
                plan_name=plan_name,
                is_blocked_url_fn=is_blocked_url_fn,
                is_founder_grade_candidate_fn=is_founder_grade_candidate_fn,
                domain_from_url_fn=domain_from_url_fn,
                truncate_text_fn=truncate_text_fn,
            )
            if not article:
                continue
            existing_urls.append(str(article.get("url", "") or ""))
            if article.get("x_post_url"):
                existing_urls.append(str(article.get("x_post_url", "") or ""))
            existing_titles.append(str(article.get("title", "") or ""))
            collected.append(article)
        logger.info("   Grok X scout[%s]: %d bài", plan_name, len(collected))

    return collected[:max_articles_total]
