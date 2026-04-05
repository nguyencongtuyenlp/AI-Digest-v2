"""
source_history.py - Lightweight memory for source quality over time.

The goal is pragmatic:
- remember which sources repeatedly surface strong stories
- remember which sources keep producing stale/promo/speculation noise
- feed that memory back into gather, scoring, delivery, and reporting

This stays intentionally simple and deterministic so the pipeline can keep
working even if no history exists yet.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from db import DB_PATH

SOURCE_HISTORY_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS source_history (
    source_key         TEXT PRIMARY KEY,
    source_label       TEXT,
    source_domain      TEXT,
    source_kind        TEXT,
    runs_seen          INTEGER NOT NULL DEFAULT 0,
    raw_articles       INTEGER NOT NULL DEFAULT 0,
    scored_articles    INTEGER NOT NULL DEFAULT 0,
    selected_main      INTEGER NOT NULL DEFAULT 0,
    selected_github    INTEGER NOT NULL DEFAULT 0,
    selected_facebook  INTEGER NOT NULL DEFAULT 0,
    skipped_old        INTEGER NOT NULL DEFAULT 0,
    skipped_speculation INTEGER NOT NULL DEFAULT 0,
    skipped_promo      INTEGER NOT NULL DEFAULT 0,
    skipped_weak       INTEGER NOT NULL DEFAULT 0,
    last_seen_at       TEXT,
    last_selected_at   TEXT,
    updated_at         TEXT NOT NULL
)
"""

NOISE_REASON_HINTS = {
    "old": ("old", "aging", "stale", "pinned"),
    "speculation": ("speculation", "rumor", "đồn", "tin đồn", "leak", "community"),
    "promo": ("promo", "event", "register", "ticket", "webinar", "sự kiện", "khóa học"),
}


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(SOURCE_HISTORY_TABLE_SQL)
    conn.commit()
    return conn


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _domain_from_url(url: Any) -> str:
    try:
        netloc = urlparse(str(url or "").strip()).netloc.lower()
    except Exception:
        return ""
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc


def build_source_history_key(article: dict[str, Any] | None) -> str:
    payload = dict(article or {})

    github_full_name = str(payload.get("github_full_name", "") or "").strip().lower()
    if github_full_name:
        return f"github:{github_full_name}"

    platform = str(payload.get("social_platform", "") or "").strip().lower()
    facebook_source_url = str(payload.get("facebook_source_url", "") or "").strip()
    if platform == "facebook" and facebook_source_url:
        return f"facebook:{_domain_from_url(facebook_source_url)}:{facebook_source_url.lower()}"

    x_handle = str(payload.get("x_author_handle", "") or "").strip().lstrip("@").lower()
    if platform == "x" and x_handle:
        return f"x:@{x_handle}"

    source_domain = str(payload.get("source_domain", "") or "").strip().lower() or _domain_from_url(payload.get("url", ""))
    if platform and source_domain:
        return f"{platform}:{source_domain}"
    if source_domain:
        return source_domain

    source = str(payload.get("source", "") or "").strip().lower()
    if source:
        return source

    url = str(payload.get("url", "") or "").strip().lower()
    if url:
        return url

    return "unknown"


def _source_label(article: dict[str, Any]) -> str:
    github_full_name = str(article.get("github_full_name", "") or "").strip()
    if github_full_name:
        return github_full_name

    facebook_label = str(article.get("facebook_source_label", "") or "").strip()
    if facebook_label:
        return facebook_label

    x_handle = str(article.get("x_author_handle", "") or "").strip().lstrip("@")
    if x_handle:
        return f"@{x_handle}"

    return str(article.get("source", "") or article.get("source_domain", "") or "unknown").strip() or "unknown"


def _source_domain(article: dict[str, Any]) -> str:
    return (
        str(article.get("source_domain", "") or "").strip().lower()
        or _domain_from_url(article.get("url", ""))
        or _domain_from_url(article.get("facebook_source_url", ""))
    )


def _source_kind(article: dict[str, Any]) -> str:
    return str(article.get("source_kind", "") or "unknown").strip().lower() or "unknown"


def _base_stats(article: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_key": build_source_history_key(article),
        "source_label": _source_label(article),
        "source_domain": _source_domain(article),
        "source_kind": _source_kind(article),
        "runs_seen": 0,
        "raw_articles": 0,
        "scored_articles": 0,
        "selected_main": 0,
        "selected_github": 0,
        "selected_facebook": 0,
        "skipped_old": 0,
        "skipped_speculation": 0,
        "skipped_promo": 0,
        "skipped_weak": 0,
        "last_seen_at": "",
        "last_selected_at": "",
        "updated_at": "",
    }


def _selection_total(entry: dict[str, Any]) -> int:
    return (
        int(entry.get("selected_main", 0) or 0)
        + int(entry.get("selected_github", 0) or 0)
        + int(entry.get("selected_facebook", 0) or 0)
    )


def _noise_total(entry: dict[str, Any]) -> int:
    return (
        int(entry.get("skipped_old", 0) or 0)
        + int(entry.get("skipped_speculation", 0) or 0)
        + int(entry.get("skipped_promo", 0) or 0)
        + int(entry.get("skipped_weak", 0) or 0)
    )


def compute_source_history_quality(entry: dict[str, Any] | None) -> dict[str, Any]:
    stats = dict(entry or {})
    runs_seen = int(stats.get("runs_seen", 0) or 0)
    raw_articles = max(1, int(stats.get("raw_articles", 0) or 0))
    scored_articles = max(1, int(stats.get("scored_articles", 0) or 0))
    selection_total = _selection_total(stats)
    noise_total = _noise_total(stats)
    selection_rate = selection_total / scored_articles
    noise_rate = noise_total / raw_articles

    if runs_seen <= 1:
        quality_score = 50
    else:
        quality_score = 50
        quality_score += min(20, int(selection_rate * 45))
        quality_score += min(
            22,
            (int(stats.get("selected_main", 0) or 0) * 7)
            + (int(stats.get("selected_github", 0) or 0) * 4)
            + (int(stats.get("selected_facebook", 0) or 0) * 4),
        )
        quality_score -= min(28, int(noise_rate * 55))
        quality_score -= min(
            18,
            (int(stats.get("skipped_old", 0) or 0) * 2)
            + (int(stats.get("skipped_speculation", 0) or 0) * 3)
            + (int(stats.get("skipped_promo", 0) or 0) * 3),
        )
        quality_score = max(0, min(100, quality_score))

    bonus = 0
    penalty = 0
    status = "neutral"
    if runs_seen >= 3:
        if quality_score >= 72 and selection_total >= 2:
            bonus = 4
            status = "trusted"
        elif quality_score >= 62 and selection_rate >= 0.25:
            bonus = 2
            status = "positive"
        elif quality_score <= 20:
            penalty = 14
            status = "muted"
        elif quality_score <= 35:
            penalty = 8
            status = "watch"
        elif quality_score <= 45 and noise_rate >= 0.35:
            penalty = 4
            status = "watch"

    return {
        "runs_seen": runs_seen,
        "raw_articles": raw_articles,
        "scored_articles": scored_articles,
        "selection_total": selection_total,
        "noise_total": noise_total,
        "selection_rate": round(selection_rate, 3),
        "noise_rate": round(noise_rate, 3),
        "quality_score": quality_score,
        "bonus": bonus,
        "penalty": penalty,
        "status": status,
    }


def load_source_history(limit: int = 800) -> dict[str, dict[str, Any]]:
    conn = _get_conn()
    try:
        rows = conn.execute(
            """
            SELECT *
            FROM source_history
            ORDER BY updated_at DESC, runs_seen DESC, selected_main DESC
            LIMIT ?
            """,
            (max(limit, 1),),
        ).fetchall()
    finally:
        conn.close()

    history_map: dict[str, dict[str, Any]] = {}
    for row in rows:
        payload = dict(row)
        payload.update(compute_source_history_quality(payload))
        history_map[str(payload.get("source_key", "") or "")] = payload
    return history_map


def annotate_article_with_source_history(article: dict[str, Any], history_map: dict[str, dict[str, Any]] | None) -> dict[str, Any]:
    history = dict((history_map or {}).get(build_source_history_key(article), {}) or {})
    quality = compute_source_history_quality(history)

    base_priority = int(article.get("source_priority", 0) or 0)
    bonus = int(quality.get("bonus", 0) or 0)
    penalty = int(quality.get("penalty", 0) or 0)
    adjusted_priority = max(0, min(100, base_priority + bonus - penalty))

    article["source_history_key"] = build_source_history_key(article)
    article["source_priority_base"] = base_priority
    article["source_priority"] = adjusted_priority
    article["source_history_runs"] = int(quality.get("runs_seen", 0) or 0)
    article["source_history_selection_rate"] = float(quality.get("selection_rate", 0.0) or 0.0)
    article["source_history_noise_rate"] = float(quality.get("noise_rate", 0.0) or 0.0)
    article["source_history_quality_score"] = int(quality.get("quality_score", 50) or 50)
    article["source_history_bonus"] = bonus
    article["source_history_penalty"] = penalty
    article["source_history_status"] = str(quality.get("status", "neutral") or "neutral")
    return article


def annotate_articles_with_source_history(
    articles: list[dict[str, Any]],
    history_map: dict[str, dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    annotated: list[dict[str, Any]] = []
    for article in articles:
        if not isinstance(article, dict):
            continue
        annotated.append(annotate_article_with_source_history(article, history_map))
    return annotated


def annotate_sources_with_history(
    sources: list[dict[str, Any]],
    history_map: dict[str, dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    annotated: list[dict[str, Any]] = []
    for source in sources:
        if not isinstance(source, dict):
            continue
        key = build_source_history_key(
            {
                "source_domain": _domain_from_url(source.get("url", "")),
                "url": source.get("url", ""),
                "facebook_source_url": source.get("url", ""),
                "social_platform": "facebook" if "facebook" in str(source.get("url", "") or "").lower() else "",
                "source": source.get("label", ""),
            }
        )
        history = dict((history_map or {}).get(key, {}) or {})
        quality = compute_source_history_quality(history)
        enriched = dict(source)
        enriched["source_history_key"] = key
        enriched["source_history_quality_score"] = int(quality.get("quality_score", 50) or 50)
        enriched["source_history_penalty"] = int(quality.get("penalty", 0) or 0)
        enriched["source_history_status"] = str(quality.get("status", "neutral") or "neutral")
        annotated.append(enriched)
    return annotated


def filter_discovered_sources_by_history(
    sources: list[dict[str, Any]],
    history_map: dict[str, dict[str, Any]] | None,
    *,
    max_active: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    annotated = annotate_sources_with_history(sources, history_map)
    active: list[dict[str, Any]] = []
    muted: list[dict[str, Any]] = []

    ordered = sorted(
        annotated,
        key=lambda source: (
            1 if str(source.get("discovery_origin", "") or "").lower() == "manual" else 0,
            int(source.get("ai_source_score", 0) or 0),
            int(source.get("source_history_quality_score", 50) or 50),
        ),
        reverse=True,
    )

    for source in ordered:
        origin = str(source.get("discovery_origin", "") or "").strip().lower()
        penalty = int(source.get("source_history_penalty", 0) or 0)
        quality_score = int(source.get("source_history_quality_score", 50) or 50)
        if origin != "manual" and penalty >= 8 and quality_score <= 35:
            muted_source = dict(source)
            muted_source["history_gate"] = "muted_low_quality"
            muted.append(muted_source)
            continue
        active.append(source)

    return active[:max_active], muted


def _aggregate_batch_counts(
    articles: list[dict[str, Any]],
    field_name: str,
    bucket: dict[str, dict[str, Any]],
) -> None:
    for article in articles:
        if not isinstance(article, dict):
            continue
        entry = bucket.setdefault(build_source_history_key(article), _base_stats(article))
        entry["source_label"] = _source_label(article)
        entry["source_domain"] = _source_domain(article)
        entry["source_kind"] = _source_kind(article)
        entry[field_name] += 1


def _classify_skip_reason(article: dict[str, Any]) -> str:
    explicit = str(article.get("facebook_topic_skip_reason", "") or "").strip().lower()
    if explicit:
        return explicit

    rationale = " ".join(
        part
        for part in [
            str(article.get("delivery_rationale", "") or "").strip(),
            str(article.get("editorial_angle", "") or "").strip(),
        ]
        if part
    ).lower()
    for reason, hints in NOISE_REASON_HINTS.items():
        if any(hint in rationale for hint in hints):
            return reason
    return "weak"


def record_source_history_run(state: dict[str, Any]) -> None:
    raw_articles = [item for item in state.get("raw_articles", []) if isinstance(item, dict)]
    if not raw_articles:
        return

    now = _utc_now()
    bucket: dict[str, dict[str, Any]] = {}
    raw_keys_seen: set[str] = set()

    for article in raw_articles:
        key = build_source_history_key(article)
        entry = bucket.setdefault(key, _base_stats(article))
        entry["source_label"] = _source_label(article)
        entry["source_domain"] = _source_domain(article)
        entry["source_kind"] = _source_kind(article)
        entry["raw_articles"] += 1
        entry["last_seen_at"] = now
        if key not in raw_keys_seen:
            entry["runs_seen"] += 1
            raw_keys_seen.add(key)

    _aggregate_batch_counts([item for item in state.get("scored_articles", []) if isinstance(item, dict)], "scored_articles", bucket)
    _aggregate_batch_counts([item for item in state.get("telegram_candidates", []) if isinstance(item, dict)], "selected_main", bucket)

    for article in state.get("telegram_candidates", []) or []:
        if not isinstance(article, dict):
            continue
        bucket.setdefault(build_source_history_key(article), _base_stats(article))["last_selected_at"] = now

    for article in state.get("final_articles", []) or []:
        if not isinstance(article, dict):
            continue
        if str(article.get("delivery_decision", "") or "").strip().lower() != "skip":
            continue
        entry = bucket.setdefault(build_source_history_key(article), _base_stats(article))
        reason = _classify_skip_reason(article)
        if reason in {"old", "aging", "stale"}:
            entry["skipped_old"] += 1
        elif reason == "speculation":
            entry["skipped_speculation"] += 1
        elif reason == "promo":
            entry["skipped_promo"] += 1
        else:
            entry["skipped_weak"] += 1

    conn = _get_conn()
    try:
        for entry in bucket.values():
            entry["updated_at"] = now
            conn.execute(
                """
                INSERT INTO source_history (
                    source_key,
                    source_label,
                    source_domain,
                    source_kind,
                    runs_seen,
                    raw_articles,
                    scored_articles,
                    selected_main,
                    selected_github,
                    selected_facebook,
                    skipped_old,
                    skipped_speculation,
                    skipped_promo,
                    skipped_weak,
                    last_seen_at,
                    last_selected_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_key) DO UPDATE SET
                    source_label = excluded.source_label,
                    source_domain = excluded.source_domain,
                    source_kind = excluded.source_kind,
                    runs_seen = source_history.runs_seen + excluded.runs_seen,
                    raw_articles = source_history.raw_articles + excluded.raw_articles,
                    scored_articles = source_history.scored_articles + excluded.scored_articles,
                    selected_main = source_history.selected_main + excluded.selected_main,
                    selected_github = source_history.selected_github + excluded.selected_github,
                    selected_facebook = source_history.selected_facebook + excluded.selected_facebook,
                    skipped_old = source_history.skipped_old + excluded.skipped_old,
                    skipped_speculation = source_history.skipped_speculation + excluded.skipped_speculation,
                    skipped_promo = source_history.skipped_promo + excluded.skipped_promo,
                    skipped_weak = source_history.skipped_weak + excluded.skipped_weak,
                    last_seen_at = excluded.last_seen_at,
                    last_selected_at = CASE
                        WHEN excluded.last_selected_at != '' THEN excluded.last_selected_at
                        ELSE source_history.last_selected_at
                    END,
                    updated_at = excluded.updated_at
                """,
                (
                    entry["source_key"],
                    entry["source_label"],
                    entry["source_domain"],
                    entry["source_kind"],
                    int(entry["runs_seen"] or 0),
                    int(entry["raw_articles"] or 0),
                    int(entry["scored_articles"] or 0),
                    int(entry["selected_main"] or 0),
                    int(entry["selected_github"] or 0),
                    int(entry["selected_facebook"] or 0),
                    int(entry["skipped_old"] or 0),
                    int(entry["skipped_speculation"] or 0),
                    int(entry["skipped_promo"] or 0),
                    int(entry["skipped_weak"] or 0),
                    str(entry.get("last_seen_at", "") or ""),
                    str(entry.get("last_selected_at", "") or ""),
                    now,
                ),
            )
        conn.commit()
    finally:
        conn.close()


def batch_source_history_rows(
    articles: list[dict[str, Any]],
    history_map: dict[str, dict[str, Any]] | None,
    *,
    limit: int = 12,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    seen: dict[str, dict[str, Any]] = {}
    for article in articles:
        if not isinstance(article, dict):
            continue
        key = build_source_history_key(article)
        if not key or key in seen:
            continue
        history = dict((history_map or {}).get(key, {}) or {})
        quality = compute_source_history_quality(history)
        seen[key] = {
            "source_key": key,
            "source_label": _source_label(article),
            "source_domain": _source_domain(article),
            "source_kind": _source_kind(article),
            "quality_score": int(quality.get("quality_score", 50) or 50),
            "status": str(quality.get("status", "neutral") or "neutral"),
            "runs_seen": int(quality.get("runs_seen", 0) or 0),
            "selection_rate": float(quality.get("selection_rate", 0.0) or 0.0),
            "noise_rate": float(quality.get("noise_rate", 0.0) or 0.0),
            "bonus": int(quality.get("bonus", 0) or 0),
            "penalty": int(quality.get("penalty", 0) or 0),
        }

    ordered = sorted(
        seen.values(),
        key=lambda item: (item["quality_score"], item["runs_seen"], item["selection_rate"]),
        reverse=True,
    )
    leaders = ordered[:limit]
    risky = sorted(
        [item for item in ordered if item["penalty"] > 0 or item["noise_rate"] >= 0.2],
        key=lambda item: (item["penalty"], item["noise_rate"], -item["quality_score"]),
        reverse=True,
    )[:limit]
    return leaders, risky
