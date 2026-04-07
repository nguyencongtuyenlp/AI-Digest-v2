"""
temporal_snapshots.py — Ghi JSON artifacts nhỏ, ổn định để debug mỗi run.

Mục tiêu:
- có "source of truth" sau gather và sau score
- dễ so sánh giữa các run mà không cần đào log
- không lưu nội dung quá nặng như full article content
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TEMPORAL_SNAPSHOT_DIR = "reports/temporal_snapshots"
FALSE_VALUES = {"0", "false", "no", "off"}
EXCLUDED_ARTICLE_KEYS = {
    "content",
    "raw_content",
    "raw_html",
    "html",
    "document",
}


def temporal_snapshots_enabled(state: dict[str, Any] | None = None) -> bool:
    runtime_config = dict((state or {}).get("runtime_config", {}) or {})
    raw = runtime_config.get("enable_temporal_snapshots")
    if raw in (None, ""):
        raw = os.getenv("TEMPORAL_SNAPSHOTS_ENABLED", "1")
    return str(raw).strip().lower() not in FALSE_VALUES


def _snapshot_dir(state: dict[str, Any] | None = None) -> Path:
    runtime_config = dict((state or {}).get("runtime_config", {}) or {})
    configured = str(runtime_config.get("temporal_snapshot_dir", "") or os.getenv("TEMPORAL_SNAPSHOT_DIR", "")).strip()
    if configured:
        path = Path(configured).expanduser()
        return path if path.is_absolute() else PROJECT_ROOT / path
    return PROJECT_ROOT / DEFAULT_TEMPORAL_SNAPSHOT_DIR


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_stamp(state: dict[str, Any] | None = None) -> str:
    started_at = str((state or {}).get("started_at", "") or "").strip()
    if started_at:
        normalized = started_at.replace("Z", "+00:00")
        with_timezone = normalized if "T" not in normalized or "+" in normalized or normalized.endswith("+00:00") else f"{normalized}+00:00"
        try:
            parsed = datetime.fromisoformat(with_timezone)
            return parsed.astimezone(timezone.utc).strftime("%Y%m%d_%H%M%S")
        except ValueError:
            pass
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _compact(value: Any, *, limit: int = 320) -> Any:
    if isinstance(value, str):
        normalized = " ".join(value.split())
        if len(normalized) <= limit:
            return normalized
        return normalized[: limit - 1].rstrip() + "…"
    if isinstance(value, list):
        return [_compact(item, limit=180) for item in value[:12]]
    if isinstance(value, dict):
        return {str(key): _compact(item, limit=180) for key, item in value.items()}
    return value


def _article_snapshot(article: dict[str, Any]) -> dict[str, Any]:
    snapshot: dict[str, Any] = {}
    for key, value in article.items():
        if key in EXCLUDED_ARTICLE_KEYS:
            continue
        if value in (None, "", [], {}):
            continue
        snapshot[str(key)] = _compact(value)
    return snapshot


def write_temporal_snapshot(
    *,
    state: dict[str, Any] | None,
    stage: str,
    articles: list[dict[str, Any]],
    extra: dict[str, Any] | None = None,
) -> str:
    if not temporal_snapshots_enabled(state):
        return ""

    snapshot_dir = _snapshot_dir(state)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{_run_stamp(state)}_{stage}.json"
    path = snapshot_dir / filename
    payload = {
        "generated_at": _iso_now(),
        "started_at": str((state or {}).get("started_at", "") or ""),
        "run_mode": str((state or {}).get("run_mode", "") or "unknown"),
        "run_profile": str((state or {}).get("run_profile", "") or "unknown"),
        "stage": stage,
        "article_count": len(articles),
        "extra": dict(extra or {}),
        "articles": [_article_snapshot(article) for article in articles if isinstance(article, dict)],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)
