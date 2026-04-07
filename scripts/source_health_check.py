"""
source_health_check.py - Standalone script kiểm tra tình trạng nguồn.

Chạy:
    python source_health_check.py
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from digest.runtime.run_health import (
    _facebook_storage_state_path,
    _file_age_days,
    _telethon_session_path,
    collect_source_health,
    notify_source_health_if_needed,
)


def _status_icon(status: str) -> str:
    return {
        "ok": "OK",
        "stale": "WARN",
        "dead": "DEAD",
    }.get(str(status or "").strip().lower(), "UNK")


def _print_table(source_health: dict[str, str]) -> None:
    rows = [("source", "STATUS")]
    rows.extend((name, status) for name, status in source_health.items())
    left_width = max(len(left) for left, _right in rows)
    for left, right in rows:
        if right == "STATUS":
            print(f"{left.ljust(left_width)} | {right}")
            continue
        print(f"{left.ljust(left_width)} | {_status_icon(right)} ({right})")


def main() -> None:
    source_health = collect_source_health()
    print(f"Source health check @ {datetime.now(timezone.utc).isoformat()}")
    print()
    _print_table(source_health)
    print()

    facebook_state = _facebook_storage_state_path()
    facebook_age_days = _file_age_days(facebook_state)
    telethon_session = _telethon_session_path()

    if facebook_state.exists():
        age_text = f"{facebook_age_days:.1f} days" if facebook_age_days is not None else "unknown age"
        print(f"facebook_storage_state: {facebook_state} ({age_text})")
    else:
        print(f"facebook_storage_state: missing ({facebook_state})")

    if telethon_session.exists():
        print(f"telethon_session: found ({telethon_session})")
    else:
        print(f"telethon_session: missing ({telethon_session})")

    alerted = notify_source_health_if_needed(source_health, run_mode="publish")
    print(f"telegram_alert_sent: {alerted}")


if __name__ == "__main__":
    main()
