#!/usr/bin/env python3
"""
Publish a GitHub-first agent brief to Notion and a Telegram topic.

Use case:
- gather only GitHub repo / release signals
- pick the most agent-centric repos
- save them to Notion
- send a compact Telegram message with GitHub + Notion links
"""

from __future__ import annotations

import argparse
import html
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

from digest.workflow.nodes.gather_news import gather_news_node
from digest.workflow.nodes.generate_run_report import _build_run_report_markdown
from digest.workflow.nodes.save_notion import save_notion_node
from digest.workflow.nodes.send_telegram import _send_message


load_dotenv(PROJECT_ROOT / "config" / ".env")


AGENT_PRIORITY_KEYWORDS = (
    "claude code",
    "codex",
    "agent",
    "agentic",
    "multi-agent",
    "multi agent",
    "mcp",
    "model context protocol",
    "plugin",
    "workflow",
    "orchestration",
    "memory",
    "browser use",
    "browser-use",
    "sdk",
)


def _clean_text(value: Any, limit: int = 400) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _parse_dt(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None


def _keyword_hits(text: str) -> int:
    lowered = text.lower()
    return sum(1 for keyword in AGENT_PRIORITY_KEYWORDS if keyword in lowered)


def _repo_rank(repo_article: dict[str, Any], release_article: dict[str, Any] | None) -> int:
    stars = int(repo_article.get("github_stars", 0) or 0)
    combined = " ".join(
        part for part in [
            repo_article.get("title", ""),
            repo_article.get("snippet", ""),
            repo_article.get("content", ""),
            release_article.get("title", "") if release_article else "",
            release_article.get("snippet", "") if release_article else "",
        ] if part
    )

    score = 40
    score += min(18, int(math.log10(max(stars, 1)) * 6))
    score += min(20, _keyword_hits(combined) * 3)
    if release_article:
        score += 10

    published = _parse_dt(str((release_article or repo_article).get("published", "") or ""))
    if published:
        age_days = max(0, (datetime.now(timezone.utc) - published.astimezone(timezone.utc)).days)
        if age_days <= 7:
            score += 14
        elif age_days <= 30:
            score += 8
        elif age_days <= 90:
            score += 3

    return max(0, min(score, 100))


def _repo_focus(repo_article: dict[str, Any], release_article: dict[str, Any] | None) -> str:
    combined = " ".join(
        part for part in [
            repo_article.get("title", ""),
            repo_article.get("snippet", ""),
            release_article.get("title", "") if release_article else "",
            release_article.get("snippet", "") if release_article else "",
        ] if part
    ).lower()

    if "claude code" in combined:
        return "Claude Code"
    if "mcp" in combined or "model context protocol" in combined:
        return "MCP"
    if "plugin" in combined:
        return "plugin"
    if "memory" in combined:
        return "memory"
    if "browser use" in combined or "browser-use" in combined:
        return "browser automation"
    if "sdk" in combined:
        return "agent SDK"
    return "agent framework"


def _why_it_matters(focus: str) -> str:
    mapping = {
        "Claude Code": "mở rộng workflow coding agent và delegation trong terminal",
        "MCP": "mở đường nối agent với tool, memory và service ngoài codebase",
        "plugin": "giúp tái sử dụng capability theo mô-đun thay vì prompt chắp vá",
        "memory": "giúp agent giữ ngữ cảnh dài hơn và giảm lặp việc thủ công",
        "browser automation": "giúp agent chạm được workflow web thực tế thay vì chỉ chat",
        "agent SDK": "giúp team dựng agent có cấu trúc và dễ maintain hơn",
        "agent framework": "giúp team dựng workflow agent bền hơn, không phải nối tool theo kiểu ad-hoc",
    }
    return mapping.get(focus, mapping["agent framework"])


def _build_tags(repo_article: dict[str, Any], focus: str) -> list[str]:
    tags = ["github", "agents"]
    combined = " ".join(
        part for part in [repo_article.get("title", ""), repo_article.get("snippet", ""), focus] if part
    ).lower()
    if "claude" in combined:
        tags.append("claude-code")
    if "mcp" in combined or "model context protocol" in combined:
        tags.append("mcp")
    if "plugin" in combined:
        tags.append("plugins")
    if "memory" in combined:
        tags.append("memory")
    if "browser" in combined:
        tags.append("browser-use")
    if "sdk" in combined:
        tags.append("sdk")
    return list(dict.fromkeys(tags))


def _build_final_article(repo_article: dict[str, Any], release_article: dict[str, Any] | None) -> dict[str, Any]:
    repo_name = str(repo_article.get("github_full_name") or repo_article.get("title") or "").strip()
    repo_url = str(repo_article.get("url") or "").strip()
    repo_desc = _clean_text(repo_article.get("snippet", ""), 240)
    release_title = _clean_text(release_article.get("title", ""), 180) if release_article else ""
    release_url = str(release_article.get("url") or "").strip() if release_article else ""
    release_snippet = _clean_text(release_article.get("snippet", ""), 260) if release_article else ""
    stars = int(repo_article.get("github_stars", 0) or 0)
    focus = _repo_focus(repo_article, release_article)
    why = _why_it_matters(focus)
    total_score = _repo_rank(repo_article, release_article)
    tags = _build_tags(repo_article, focus)
    published = str((release_article or repo_article).get("published", "") or "")

    note_summary = (
        f"Ý chính của tin này là: {repo_name} là một repo nghiêng về {focus.lower()} với mô tả \"{repo_desc}\". "
        f"Giá trị thực tế là {why}. "
        f"{'Release gần nhất cho thấy repo còn được cập nhật đều, nên đáng theo dõi sát.' if release_article else 'Repo này đáng xem thêm nếu team đang tìm tool agent mới có thể dùng ngay.'}"
    )

    analysis_lines = [
        "### Repo overview",
        f"- Repo: {repo_name}",
        f"- GitHub: {repo_url}",
        f"- Focus: {focus}",
        f"- Stars: {stars}",
        f"- Mô tả ngắn: {repo_desc or 'Chưa có mô tả ngắn rõ ràng.'}",
        "",
        "### Why it matters",
        f"- Giá trị thực tế: {why}.",
        f"- Góc ứng dụng: phù hợp với team đang theo dõi AI agent, Claude Code, MCP và tool-use.",
    ]
    if release_article:
        analysis_lines.extend(
            [
                "",
                "### Latest release",
                f"- Release: {release_title}",
                f"- Link: {release_url}",
                f"- Điểm đáng chú ý: {release_snippet or 'Có release mới, nên đáng theo dõi changelog chi tiết.'}",
            ]
        )

    recommend = (
        f"### Hành động gợi ý\n"
        f"1. Theo dõi {repo_name} trong watchlist GitHub của team.\n"
        f"2. Nếu team đang làm {focus.lower()}, nên thử đọc README và changelog gần nhất.\n"
        f"3. Chỉ ưu tiên thử nhanh khi repo có ví dụ triển khai rõ và còn update đều."
    )

    c1_reason = (
        f"Repo này bám sát gu 'AI agent / công cụ mới / ứng dụng được ngay' nhờ focus vào {focus.lower()} "
        f"và có tín hiệu maintainer activity gần đây."
    )
    c2_reason = f"Startup AI có thể dùng repo này để rút ngắn thời gian dựng {focus.lower()} thay vì tự ghép tool từ đầu."
    c3_reason = "Phù hợp để theo dõi như một nguồn implementation pattern và công cụ cạnh tranh cho stack agent nội bộ."

    return {
        "title": repo_name,
        "url": repo_url,
        "source": "GitHub Agent Brief",
        "source_domain": "github.com",
        "published_at": published,
        "published": published,
        "primary_type": "Product",
        "primary_emoji": "🛠️",
        "total_score": total_score,
        "c1_score": min(35, total_score // 3),
        "c2_score": min(35, total_score // 3),
        "c3_score": min(30, total_score // 3),
        "c1_reason": c1_reason,
        "c2_reason": c2_reason,
        "c3_reason": c3_reason,
        "relevance_level": "High" if total_score >= 75 else "Medium",
        "summary_vi": note_summary,
        "note_summary_vi": note_summary,
        "content_page_md": "\n".join(analysis_lines),
        "recommend_idea": recommend,
        "delivery_decision": "include",
        "delivery_score": max(8, min(15, total_score // 6)),
        "source_verified": True,
        "source_tier": "a",
        "content_available": True,
        "is_ai_relevant": True,
        "is_news_candidate": True,
        "freshness_bucket": "fresh" if release_article else "recent",
        "tags": tags,
        "github_full_name": repo_name,
        "github_release_title": release_title,
        "github_release_url": release_url,
        "github_stars": stars,
    }


def _group_github_articles(raw_articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for article in raw_articles:
        repo_name = str(article.get("github_full_name") or article.get("title") or "").strip()
        if not repo_name:
            continue
        bucket = grouped.setdefault(repo_name, {"repo": None, "release": None})
        signal_type = str(article.get("github_signal_type", "") or "")
        if signal_type == "release":
            if bucket["release"] is None:
                bucket["release"] = article
        elif bucket["repo"] is None:
            bucket["repo"] = article

    final_articles: list[dict[str, Any]] = []
    for repo_name, bucket in grouped.items():
        repo_article = bucket["repo"] or bucket["release"]
        if not repo_article:
            continue
        final_articles.append(_build_final_article(repo_article, bucket["release"]))

    final_articles.sort(key=lambda item: int(item.get("total_score", 0) or 0), reverse=True)
    return final_articles


def _build_telegram_message(final_articles: list[dict[str, Any]], notion_pages: list[dict[str, Any]]) -> str:
    notion_by_title = {str(item.get("title", "") or ""): str(item.get("url", "") or "") for item in notion_pages}
    lines = [
        "<b>GitHub Agent Brief</b>",
        "Batch này ưu tiên repo mới và thực dụng cho <b>AI agent / Claude Code / MCP</b>.",
        "",
    ]

    for index, article in enumerate(final_articles, 1):
        title = str(article.get("title", "") or "")
        repo_url = str(article.get("url", "") or "")
        notion_url = notion_by_title.get(title, "")
        summary = html.escape(_clean_text(article.get("note_summary_vi", ""), 260))
        score = int(article.get("total_score", 0) or 0)
        release_url = str(article.get("github_release_url", "") or "")

        link_bits = [f'<a href="{repo_url}">GitHub</a>'] if repo_url else []
        if release_url:
            link_bits.append(f'<a href="{release_url}">Release</a>')
        if notion_url:
            link_bits.append(f'<a href="{notion_url}">Notion</a>')

        lines.extend(
            [
                f"<b>{index}. {html.escape(title)}</b> <i>({score}/100)</i>",
                " | ".join(link_bits) if link_bits else "",
                summary,
                "",
            ]
        )

    return "\n".join(line for line in lines if line is not None).strip()


def _runtime_config(max_org_repos: int, max_search_results: int, max_releases_per_repo: int) -> dict[str, Any]:
    return {
        "enable_rss": False,
        "enable_github": True,
        "enable_watchlist": False,
        "enable_hn": False,
        "enable_reddit": False,
        "enable_ddg": False,
        "enable_telegram_channels": False,
        "github_max_org_repos": max_org_repos,
        "github_max_releases_per_repo": max_releases_per_repo,
        "github_max_search_results": max_search_results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish GitHub-first AI agent brief to Notion and Telegram.")
    parser.add_argument("--thread-id", type=int, help="Telegram topic thread ID.")
    parser.add_argument("--top-n", type=int, default=8, help="How many repos to keep.")
    parser.add_argument("--max-org-repos", type=int, default=4)
    parser.add_argument("--max-search-results", type=int, default=4)
    parser.add_argument("--max-releases-per-repo", type=int, default=1)
    parser.add_argument("--no-telegram", action="store_true", help="Save to Notion only.")
    parser.add_argument("--no-notion", action="store_true", help="Send Telegram only, skip creating new Notion pages.")
    parser.add_argument("--dry-run", action="store_true", help="Do not publish to Notion or Telegram; just generate report and preview.")
    args = parser.parse_args()

    if not args.dry_run and not args.no_telegram and not args.thread_id:
        parser.error("--thread-id is required unless --no-telegram or --dry-run is used")

    raw_state = gather_news_node({"runtime_config": _runtime_config(args.max_org_repos, args.max_search_results, args.max_releases_per_repo)})
    raw_articles = list(raw_state.get("raw_articles", []) or [])
    grouped_articles = _group_github_articles(raw_articles)[: max(1, args.top_n)]

    notion_pages: list[dict[str, Any]] = []
    if not args.no_notion and not args.dry_run:
        notion_state = {
            "final_articles": grouped_articles,
            "publish_notion": True,
            "persist_local": True,
        }
        notion_result = save_notion_node(notion_state)
        notion_pages = list(notion_result.get("notion_pages", []) or [])

    telegram_sent = False
    message = _build_telegram_message(grouped_articles, notion_pages)
    if not args.no_telegram and not args.dry_run:
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        if not bot_token or not chat_id:
            raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID in config/.env")
        telegram_sent = _send_message(bot_token, chat_id, message, args.thread_id)

    report_state = {
        "raw_articles": raw_articles,
        "new_articles": grouped_articles,
        "scored_articles": grouped_articles,
        "top_articles": grouped_articles[: min(5, len(grouped_articles))],
        "final_articles": grouped_articles,
        "telegram_candidates": grouped_articles,
        "notion_pages": notion_pages,
        "telegram_sent": telegram_sent,
        "run_mode": "preview" if args.dry_run else "publish",
        "run_profile": "github_agent_brief_dry_run" if args.dry_run else "github_agent_brief",
        "runtime_config": _runtime_config(args.max_org_repos, args.max_search_results, args.max_releases_per_repo),
        "summary_mode": "github_topic_digest",
        "summary_warnings": [],
    }
    reports_dir = PROJECT_ROOT / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now(timezone.utc)
    report_path = reports_dir / f"github_agent_brief_{generated_at.strftime('%Y%m%d_%H%M%S')}.md"
    report_path.write_text(_build_run_report_markdown(report_state, generated_at), encoding="utf-8")

    print(f"selected={len(grouped_articles)}")
    print(f"notion_pages={len(notion_pages)}")
    print(f"telegram_sent={telegram_sent}")
    print(f"report_path={report_path}")
    print("preview_message=")
    print(message[:4000])
    for item in notion_pages:
        print(f"notion: {item.get('title')} -> {item.get('url')}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
