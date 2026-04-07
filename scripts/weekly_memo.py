"""
weekly_memo.py - Build a lightweight executive memo from recent digest history.

Phase 3 foundation:
- weekly memo tự động
- gom top signals theo lane
- rút ra themes và action items ngắn cho founder/operator
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import re
import sys
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from digest.storage.db import get_history
from digest.editorial.digest_formatter import TYPE_ORDER, canonical_type_name

REPORTS_DIR = PROJECT_ROOT / "reports"
TITLE_STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "this",
    "that",
    "from",
    "launch",
    "launches",
    "released",
    "release",
    "update",
    "updates",
    "new",
    "ai",
    "openai",
    "anthropic",
    "google",
    "meta",
    "microsoft",
    "news",
}


def _title_tokens(title: str) -> list[str]:
    return [
        token
        for token in re.findall(r"[a-z0-9]+", str(title or "").lower())
        if len(token) >= 4 and token not in TITLE_STOPWORDS
    ]


def _date_range_label(days: int, today: date | None = None) -> str:
    end_date = today or datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=max(1, days) - 1)
    return f"{start_date.strftime('%d/%m')} - {end_date.strftime('%d/%m/%Y')}"


def _group_by_lane(history: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for article in history:
        grouped[canonical_type_name(article.get("primary_type"))].append(article)
    for lane in grouped:
        grouped[lane].sort(key=lambda item: int(item.get("relevance_score", 0) or 0), reverse=True)
    return grouped


def _top_themes(history: list[dict[str, Any]], limit: int = 5) -> list[str]:
    counter: Counter[str] = Counter()
    for article in history:
        counter.update(_title_tokens(article.get("title", "")))
    return [token for token, _count in counter.most_common(limit)]


def _build_action_items(grouped: dict[str, list[dict[str, Any]]]) -> list[str]:
    actions: list[str] = []
    if grouped.get("Product"):
        actions.append("Review các product update mạnh nhất tuần này để xem có gì nên đưa ngay vào watchlist triển khai hoặc demo nội bộ.")
    if grouped.get("Practical"):
        actions.append("Chọn 1-2 workflow practical đáng tiền nhất và chuyển thành playbook ngắn cho team vận hành hoặc kỹ thuật.")
    if grouped.get("Society & Culture"):
        actions.append("Theo dõi các tín hiệu adoption, education hoặc policy đang tăng để tránh chậm nhịp với kỳ vọng của thị trường.")
    if not actions:
        actions.append("Tuần này tín hiệu còn mỏng; ưu tiên siết nguồn và watchlist trước khi đẩy thêm deep analysis.")
    return actions


def build_weekly_memo(
    history: list[dict[str, Any]],
    *,
    days: int = 7,
    today: date | None = None,
) -> str:
    if not history:
        return (
            f"# Weekly AI Memo ({_date_range_label(days, today)})\n\n"
            "Tuần này chưa có đủ dữ liệu trong history để dựng memo."
        )

    grouped = _group_by_lane(history)
    top_overall = sorted(history, key=lambda item: int(item.get("relevance_score", 0) or 0), reverse=True)[:6]
    themes = _top_themes(history)
    action_items = _build_action_items(grouped)

    lines = [
        f"# Weekly AI Memo ({_date_range_label(days, today)})",
        "",
        "## Top Signals",
    ]
    for article in top_overall:
        lane = canonical_type_name(article.get("primary_type"))
        score = int(article.get("relevance_score", 0) or 0)
        title = str(article.get("title", "") or "Untitled").strip()
        summary = str(article.get("summary", "") or "").strip()
        source = str(article.get("source", "") or "").strip()
        lines.append(f"- [{lane}] {title} ({score}/100) — {source}")
        if summary:
            lines.append(f"  {summary}")

    lines.extend(["", "## Lane Breakdown"])
    for lane, _emoji in TYPE_ORDER:
        bucket = grouped.get(lane, [])
        lines.append(f"- {lane}: {len(bucket)} signals")
        for article in bucket[:3]:
            title = str(article.get("title", "") or "Untitled").strip()
            score = int(article.get("relevance_score", 0) or 0)
            lines.append(f"  {title} ({score}/100)")

    lines.extend(["", "## Themes To Watch"])
    if themes:
        for theme in themes:
            lines.append(f"- {theme}")
    else:
        lines.append("- Chưa có theme lặp đủ mạnh trong tuần này.")

    lines.extend(["", "## Suggested Actions"])
    for action in action_items:
        lines.append(f"- {action}")

    return "\n".join(lines).strip() + "\n"


def write_weekly_memo(markdown: str, *, today: date | None = None) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = (today or datetime.now(timezone.utc).date()).strftime("%Y-%m-%d")
    path = REPORTS_DIR / f"weekly_memo_{stamp}.md"
    path.write_text(markdown, encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Build weekly executive memo from digest history.")
    parser.add_argument("--days", type=int, default=7, help="Lookback window in days.")
    parser.add_argument("--limit", type=int, default=120, help="Maximum history rows to load.")
    parser.add_argument("--write", action="store_true", help="Write markdown to reports/weekly_memo_YYYY-MM-DD.md.")
    args = parser.parse_args()

    history = get_history(days=max(1, args.days), limit=max(10, args.limit))
    markdown = build_weekly_memo(history, days=max(1, args.days))
    if args.write:
        output_path = write_weekly_memo(markdown)
        print(output_path)
    else:
        print(markdown)


if __name__ == "__main__":
    main()
