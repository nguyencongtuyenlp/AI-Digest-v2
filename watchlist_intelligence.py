"""
watchlist_intelligence.py - Dựng báo cáo watchlist intelligence từ local history.
"""

from __future__ import annotations

import argparse

from db import get_history
from executive_intelligence import build_executive_intelligence_bundle


def main() -> None:
    parser = argparse.ArgumentParser(description="Build watchlist intelligence report from digest history.")
    parser.add_argument("--days", type=int, default=14, help="Lookback window in days.")
    parser.add_argument("--limit", type=int, default=160, help="Maximum history rows to load.")
    parser.add_argument("--write", action="store_true", help="Write markdown/topic artifacts to reports/.")
    args = parser.parse_args()

    history = get_history(days=max(1, args.days), limit=max(20, args.limit))
    bundle = build_executive_intelligence_bundle(history, days=max(1, args.days))
    if args.write:
        print(bundle.get("watchlist_path", ""))
        for page in bundle.get("topic_page_artifacts", []) or []:
            print(page.get("path", ""))
    else:
        print(bundle.get("watchlist_markdown", ""))


if __name__ == "__main__":
    main()
