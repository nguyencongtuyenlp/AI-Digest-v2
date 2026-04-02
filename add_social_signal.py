#!/usr/bin/env python3
"""
Append a manual social signal entry to config/social_signal_inbox.txt.

Useful for Facebook group/page/profile posts that the team wants to feed into
the digest pipeline without relying on a brittle API.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from source_catalog import social_signal_inbox_path


def _clean(value: str) -> str:
    return "\n".join(line.rstrip() for line in str(value or "").strip().splitlines()).strip()


def _field_block(name: str, value: str) -> str:
    cleaned = _clean(value)
    return f"{name}: {cleaned}" if cleaned else ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Append a social signal to the inbox file.")
    parser.add_argument("--platform", default="facebook")
    parser.add_argument("--group", default="")
    parser.add_argument("--author", default="")
    parser.add_argument("--title", required=True)
    parser.add_argument("--url", default="")
    parser.add_argument("--posted-at", default="")
    parser.add_argument("--note", default="")
    parser.add_argument("--content", default="")
    parser.add_argument("--comments", default="")
    args = parser.parse_args()

    inbox_path = social_signal_inbox_path(PROJECT_ROOT)
    inbox_path.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        _field_block("platform", args.platform),
        _field_block("group", args.group),
        _field_block("author", args.author),
        _field_block("title", args.title),
        _field_block("url", args.url),
        _field_block("posted_at", args.posted_at),
        _field_block("note", args.note),
        _field_block("content", args.content),
        _field_block("comments", args.comments),
        "---",
    ]
    block = "\n".join(line for line in lines if line).strip() + "\n"

    with inbox_path.open("a", encoding="utf-8") as handle:
        if handle.tell() > 0:
            handle.write("\n")
        handle.write(block)

    print(f"appended_to={inbox_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
