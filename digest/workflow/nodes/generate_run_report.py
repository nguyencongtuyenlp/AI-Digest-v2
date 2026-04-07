"""
generate_run_report.py — Xuất báo cáo markdown sau mỗi run.

Mục tiêu của node này:
- giúp team nhìn rõ nguồn nào đang đóng góp nhiều
- giải thích vì sao tin nào được đưa lên Telegram
- tạo một artefact dễ review với sếp sau mỗi lần chạy
"""

from __future__ import annotations

import logging
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


from digest.runtime.artifact_retention import build_artifact_cleanup_markdown, cleanup_runtime_artifacts
from digest.storage.db import get_history
from digest.editorial.executive_intelligence import build_executive_intelligence_bundle
from digest.runtime.run_health import assess_run_health
from scripts.weekly_memo import build_weekly_memo, write_weekly_memo
from digest.sources.source_history import batch_source_history_rows, load_source_history
from digest.runtime.xai_grok import (
    grok_source_gap_enabled,
    grok_source_gap_max_articles,
    suggest_source_gap_expansion,
)

logger = logging.getLogger(__name__)


def _counter_markdown(title: str, items: list[tuple[str, int]], limit: int = 12) -> list[str]:
    lines = [f"## {title}", "", "| Nhóm | Số lượng |", "|---|---:|"]
    for key, value in items[:limit]:
        label = key or "(trống)"
        lines.append(f"| {label} | {value} |")
    lines.append("")
    return lines


def _count_by(articles: list[dict[str, Any]], field: str) -> list[tuple[str, int]]:
    counter = Counter(str(article.get(field, "") or "") for article in articles if isinstance(article, dict))
    return counter.most_common()


def _count_tags(articles: list[dict[str, Any]]) -> list[tuple[str, int]]:
    counter: Counter[str] = Counter()
    for article in articles:
        if not isinstance(article, dict):
            continue
        tags = article.get("tags", [])
        if not isinstance(tags, list):
            continue
        for tag in tags:
            label = str(tag or "").strip()
            if label:
                counter[label] += 1
    return counter.most_common()


def _is_facebook_article(article: dict[str, Any]) -> bool:
    source = str(article.get("source", "") or "").lower()
    domain = str(article.get("source_domain", "") or "").lower()
    platform = str(article.get("social_platform", "") or "").lower()
    lane_hint = str(article.get("delivery_lane_hint", "") or "").lower()
    return (
        platform == "facebook"
        or lane_hint == "facebook_topic"
        or "facebook" in source
        or domain in {"facebook.com", "www.facebook.com", "m.facebook.com", "fb.com", "mbasic.facebook.com"}
    )


def _count_facebook_sort_modes(articles: list[dict[str, Any]]) -> list[tuple[str, int]]:
    counter: Counter[str] = Counter()
    for article in articles:
        if not isinstance(article, dict) or not _is_facebook_article(article):
            continue
        mode = str(article.get("facebook_sort_mode", "") or "").strip() or "unknown"
        counter[mode] += 1
    return counter.most_common()


def _count_facebook_skip_reasons(articles: list[dict[str, Any]]) -> list[tuple[str, int]]:
    counter: Counter[str] = Counter()
    for article in articles:
        if not isinstance(article, dict) or not _is_facebook_article(article):
            continue
        reason = str(article.get("facebook_topic_skip_reason", "") or "").strip()
        decision = str(article.get("delivery_decision", "") or "").strip().lower()
        if decision != "skip" or not reason:
            continue
        counter[reason] += 1
    return counter.most_common()


def _count_delivery_skip_reasons(articles: list[dict[str, Any]]) -> list[tuple[str, int]]:
    counter: Counter[str] = Counter()
    for article in articles:
        if not isinstance(article, dict):
            continue
        if str(article.get("delivery_decision", "") or "").strip().lower() != "skip":
            continue
        reason = str(article.get("delivery_skip_reason", "") or "").strip()
        if not reason:
            continue
        counter[reason] += 1
    return counter.most_common()


def _compact_reason_value(value: Any, limit: int = 140) -> str:
    if isinstance(value, list):
        text = "; ".join(str(item or "") for item in value if str(item or "").strip())
    elif isinstance(value, dict):
        text = "; ".join(f"{key}={value[key]}" for key in sorted(value.keys()))
    else:
        text = str(value or "")
    text = " ".join(text.split()).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _build_run_report_markdown(state: dict[str, Any], generated_at: datetime) -> str:
    raw_articles = list(state.get("raw_articles", []))
    new_articles = list(state.get("new_articles", []))
    scored_articles = list(state.get("scored_articles", []))
    top_articles = list(state.get("top_articles", []))
    low_score_articles = list(state.get("low_score_articles", []))
    final_articles = list(state.get("final_articles", []))
    grok_scout_count = int(state.get("grok_scout_count", 0) or 0)
    telegram_candidates = list(state.get("telegram_candidates", []))
    notion_pages = list(state.get("notion_pages", []))
    summary_mode = str(state.get("summary_mode", "") or "")
    summary_warnings = list(state.get("summary_warnings", []) or [])
    telegram_sent = bool(state.get("telegram_sent", False))
    run_mode = str(state.get("run_mode", "") or "unknown")
    run_profile = str(state.get("run_profile", "") or run_mode)
    runtime_config = dict(state.get("runtime_config", {}) or {})
    feedback_summary_text = str(state.get("feedback_summary_text", "") or "")
    feedback_label_counts = dict(state.get("feedback_label_counts", {}) or {})
    feedback_preference_profile = dict(state.get("feedback_preference_profile", {}) or {})
    feedback_sync = dict(state.get("feedback_sync", {}) or {})
    weekly_memo_path = str(state.get("weekly_memo_path", "") or "")
    watchlist_report_path = str(state.get("watchlist_report_path", "") or "")
    topic_pages = list(state.get("topic_pages", []) or [])
    grok_source_gap_suggestions = list(state.get("grok_source_gap_suggestions", []) or [])
    grok_source_gap_batch_note = str(state.get("grok_source_gap_batch_note", "") or "")
    gather_snapshot_path = str(state.get("gather_snapshot_path", "") or "")
    scored_snapshot_path = str(state.get("scored_snapshot_path", "") or "")
    run_health = dict(state.get("run_health", {}) or assess_run_health(state))
    health_metrics = dict(run_health.get("metrics", {}) or {})
    source_history_map = load_source_history()
    source_history_leaders, source_history_risky = batch_source_history_rows(scored_articles or raw_articles, source_history_map)
    report_time = generated_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    lines = [
        "# Daily Digest Run Report",
        "",
        f"- Generated at: {report_time}",
        f"- Run mode: {run_mode}",
        f"- Run profile: {run_profile}",
        f"- Raw gathered: {len(raw_articles)}",
        f"- After dedup: {len(new_articles)}",
        f"- Scored: {len(scored_articles)}",
        f"- Deep analysis: {len(top_articles)}",
        f"- Final articles: {len(final_articles)}",
        f"- Grok scout articles: {grok_scout_count}",
        f"- Telegram candidates: {len(telegram_candidates)}",
        f"- Notion pages: {len(notion_pages)}",
        f"- Telegram sent: {'yes' if telegram_sent else 'no'}",
        f"- Summary mode: {summary_mode or 'unknown'}",
        f"- Health status: {run_health.get('status', 'unknown')}",
        f"- Publish ready: {'yes' if run_health.get('publish_ready') else 'no'}",
        "",
    ]

    lines.extend(["## Run Health", ""])
    lines.append(f"- Status: {run_health.get('status', 'unknown')}")
    lines.append(f"- Publish ready: {'yes' if run_health.get('publish_ready') else 'no'}")
    for key, value in sorted(health_metrics.items()):
        lines.append(f"- {key}: {value}")
    if run_health.get("issues"):
        lines.append("")
        lines.append("### Health issues")
        lines.append("")
        for issue in run_health.get("issues", []):
            lines.append(f"- {issue}")
    lines.append("")

    if summary_warnings:
        lines.extend(["## Summary warnings", ""])
        for warning in summary_warnings:
            lines.append(f"- {warning}")
        lines.append("")

    if runtime_config:
        lines.extend(["## Runtime config overrides", ""])
        for key, value in sorted(runtime_config.items()):
            lines.append(f"- {key}: {value}")
        lines.append("")

    if gather_snapshot_path or scored_snapshot_path:
        lines.extend(["## Temporal Snapshots", ""])
        if gather_snapshot_path:
            lines.append(f"- Gather snapshot: {gather_snapshot_path}")
        if scored_snapshot_path:
            lines.append(f"- Scored snapshot: {scored_snapshot_path}")
        lines.append("")

    lines.extend(["## Feedback Loop", ""])
    lines.append(
        f"- Sync result: synced={feedback_sync.get('synced', 0)} skipped={feedback_sync.get('skipped', 0)} error={feedback_sync.get('error', '') or 'none'}"
    )
    if feedback_label_counts:
        for key, value in sorted(feedback_label_counts.items(), key=lambda item: item[1], reverse=True):
            lines.append(f"- {key}: {value}")
    else:
        lines.append("- Chưa có feedback labels.")
    if feedback_preference_profile:
        lines.append(
            "- Preference profile: "
            + ", ".join(f"{key}={value}" for key, value in sorted(feedback_preference_profile.items()))
        )
    lines.append("")
    lines.append(feedback_summary_text or "Chưa có feedback mới từ team.")
    lines.append("")

    if weekly_memo_path or watchlist_report_path or topic_pages:
        lines.extend(["## Executive Intelligence", ""])
        if weekly_memo_path:
            lines.append(f"- Weekly memo: {weekly_memo_path}")
        if watchlist_report_path:
            lines.append(f"- Watchlist report: {watchlist_report_path}")
        if topic_pages:
            lines.append(f"- Topic pages/artifacts: {len(topic_pages)}")
            for page in topic_pages[:8]:
                label = str(page.get("topic", page.get("path", "")) or "").strip() or "(unknown)"
                ref = str(page.get("url", page.get("path", "")) or "").strip() or "(missing)"
                lines.append(f"- {label}: {ref}")
        lines.append("")

    lines.extend(_counter_markdown("Raw By Source", _count_by(raw_articles, "source")))
    lines.extend(_counter_markdown("Raw By Source Kind", _count_by(raw_articles, "source_kind")))
    lines.extend(_counter_markdown("Scored By Source Domain", _count_by(scored_articles, "source_domain")))
    lines.extend(_counter_markdown("Scored By Source Kind", _count_by(scored_articles, "source_kind")))
    lines.extend(_counter_markdown("Scored By Type", _count_by(scored_articles, "primary_type")))
    lines.extend(_counter_markdown("Scored By Tag", _count_tags(scored_articles)))
    lines.extend(_counter_markdown("Telegram Candidates By Type", _count_by(telegram_candidates, "primary_type")))
    lines.extend(_counter_markdown("Delivery Skip Reasons", _count_delivery_skip_reasons(final_articles)))
    lines.extend(_counter_markdown("Facebook Sort Mode Breakdown", _count_facebook_sort_modes(raw_articles)))
    lines.extend(_counter_markdown("Facebook Skip Reasons", _count_facebook_skip_reasons(final_articles)))

    lines.extend(["## Source History Signals", ""])
    if source_history_leaders:
        lines.append("### Strong sources in this batch")
        lines.append("")
        for source in source_history_leaders:
            lines.append(
                "- "
                f"{source.get('source_label', 'Unknown')} | "
                f"domain={source.get('source_domain', '') or '(none)'} | "
                f"quality={source.get('quality_score', 50)} | "
                f"status={source.get('status', 'neutral')} | "
                f"runs={source.get('runs_seen', 0)} | "
                f"selection_rate={source.get('selection_rate', 0.0)}"
            )
    else:
        lines.append("- Chưa có source history đủ dày để rút ra tín hiệu mạnh.")
    if source_history_risky:
        lines.append("")
        lines.append("### Sources currently penalized")
        lines.append("")
        for source in source_history_risky:
            lines.append(
                "- "
                f"{source.get('source_label', 'Unknown')} | "
                f"domain={source.get('source_domain', '') or '(none)'} | "
                f"quality={source.get('quality_score', 50)} | "
                f"status={source.get('status', 'neutral')} | "
                f"noise_rate={source.get('noise_rate', 0.0)} | "
                f"penalty={source.get('penalty', 0)}"
            )
    lines.append("")

    lines.extend(["## Grok Source Gap Suggestions", ""])
    if grok_source_gap_batch_note:
        lines.append(f"- Batch note: {_compact_reason_value(grok_source_gap_batch_note, limit=220)}")
    if grok_source_gap_suggestions:
        lines.append("- Các gợi ý dưới đây là hint chiến lược từ Grok, chưa phải nguồn đã verify.")
        for item in grok_source_gap_suggestions:
            if not isinstance(item, dict):
                continue
            lines.append(
                "- "
                f"[{str(item.get('priority', 'medium')).upper()}] "
                f"{item.get('focus', 'Unknown focus')} | "
                f"query={item.get('suggested_query', '') or '(trống)'} | "
                f"feed_hint={item.get('suggested_feed_hint', '') or '(trống)'} | "
                f"why={_compact_reason_value(item.get('rationale', ''))}"
            )
    else:
        lines.append("- Chưa có gợi ý source-gap từ Grok cho batch này.")
    lines.append("")

    lines.extend(["## Score Breakdown", ""])
    if scored_articles:
        for article in scored_articles[:10]:
            breakdown = dict(article.get("score_breakdown", {}) or {})
            lines.append(
                "- "
                f"[{article.get('primary_type', '?')}] "
                f"{article.get('title', 'N/A')} | "
                f"prefilter={breakdown.get('prefilter_score', article.get('prefilter_score', 0))} | "
                f"c1={article.get('c1_score', 0)} c2={article.get('c2_score', 0)} c3={article.get('c3_score', 0)} | "
                f"total={article.get('total_score', 0)} | "
                f"source_kind={breakdown.get('source_kind', article.get('source_kind', 'unknown'))} | "
                f"why={_compact_reason_value(breakdown.get('why_surfaced') or article.get('why_surfaced') or article.get('prefilter_reasons', []))}"
            )
    else:
        lines.append("- Chưa có bài để hiển thị breakdown.")
    lines.append("")

    lines.extend(["## Telegram Candidates", ""])
    if telegram_candidates:
        for article in telegram_candidates:
            delivery_breakdown = dict(article.get("delivery_score_breakdown", {}) or {})
            lines.append(
                "- "
                f"[{article.get('primary_type', '?')}] "
                f"{article.get('title', 'N/A')} | "
                f"score={article.get('total_score', 0)} | "
                f"delivery={article.get('delivery_score', 0)} | "
                f"source={article.get('source_domain', article.get('source', ''))} | "
                f"why={_compact_reason_value(article.get('delivery_rationale', delivery_breakdown.get('rationale', '')))}"
            )
    else:
        lines.append("- Không có bài nào được chọn cho Telegram.")
    lines.append("")

    lines.extend(["## Skipped Candidates", ""])
    skipped_articles = [
        article for article in final_articles
        if isinstance(article, dict) and str(article.get("delivery_decision", "") or "").strip().lower() == "skip"
    ]
    if skipped_articles:
        skipped_articles = sorted(
            skipped_articles,
            key=lambda article: (
                int(article.get("source_history_penalty", 0) or 0),
                int(article.get("total_score", 0) or 0),
            ),
            reverse=True,
        )
        for article in skipped_articles[:12]:
            lines.append(
                "- "
                f"[{article.get('primary_type', '?')}] "
                f"{article.get('title', 'N/A')} | "
                f"source={article.get('source_domain', article.get('source', ''))} | "
                f"score={article.get('total_score', 0)} | "
                f"skip_reason={article.get('delivery_skip_reason', '') or '-'} | "
                f"history_quality={article.get('source_history_quality_score', 50)} | "
                f"why={_compact_reason_value(article.get('delivery_rationale', ''))}"
            )
    else:
        lines.append("- Không có bài nào bị skip ở delivery judge.")
    lines.append("")

    lines.extend(["## Top Deep Analysis Articles", ""])
    if top_articles:
        for article in top_articles[:10]:
            lines.append(
                "- "
                f"[{article.get('primary_type', '?')}] "
                f"{article.get('title', 'N/A')} | "
                f"score={article.get('total_score', 0)} | "
                f"freshness={article.get('freshness_status', article.get('freshness_bucket', 'unknown'))}"
            )
    else:
        lines.append("- Không có bài deep analysis.")
    lines.append("")

    lines.extend(["## Why Skipped", ""])
    if low_score_articles:
        for article in low_score_articles[:10]:
            breakdown = dict(article.get("score_breakdown", {}) or {})
            why = breakdown.get("why_skipped") or article.get("why_skipped") or article.get("prefilter_reasons", [])
            lines.append(
                "- "
                f"[{article.get('primary_type', '?')}] "
                f"{article.get('title', 'N/A')} | "
                f"score={article.get('total_score', 0)} | "
                f"why={_compact_reason_value(why)}"
            )
    else:
        lines.append("- Không có bài bị skip rõ ràng để nêu ra.")
    lines.append("")

    lines.extend(
        [
            "## Notes",
            "",
            "- Báo cáo này dùng để giải thích run hiện tại, không thay thế eval suite.",
            "- Nếu summary rơi vào safe_fallback, nên xem lại source mix, raw quality, và output validator.",
            "",
        ]
    )

    return "\n".join(lines).strip() + "\n"


def generate_run_report_node(state: dict[str, Any]) -> dict[str, Any]:
    generated_at = datetime.now(timezone.utc)
    reports_dir = Path(os.getenv("DIGEST_REPORTS_DIR", "reports"))
    reports_dir.mkdir(parents=True, exist_ok=True)
    run_health = assess_run_health(state)

    filename = f"daily_digest_run_{generated_at.strftime('%Y%m%d_%H%M%S')}.md"
    report_path = reports_dir / filename
    enriched_state = dict(state)
    enriched_state["run_health"] = run_health
    enriched_state["publish_ready"] = bool(run_health.get("publish_ready", False))
    combined_history: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for article in list(state.get("final_articles", []) or []) + list(state.get("scored_articles", []) or []):
        if not isinstance(article, dict):
            continue
        url = str(article.get("url", "") or "").strip()
        if url and url in seen_urls:
            continue
        if url:
            seen_urls.add(url)
        combined_history.append(article)
    for article in get_history(days=14, limit=120):
        if not isinstance(article, dict):
            continue
        url = str(article.get("url", "") or "").strip()
        if url and url in seen_urls:
            continue
        if url:
            seen_urls.add(url)
        combined_history.append(article)

    try:
        weekly_memo_markdown = build_weekly_memo(combined_history, days=7)
        weekly_memo_path = write_weekly_memo(weekly_memo_markdown)
        enriched_state["weekly_memo_path"] = str(weekly_memo_path)
    except Exception as exc:
        logger.warning("Weekly memo generation skipped: %s", exc)
        enriched_state["weekly_memo_path"] = ""

    try:
        intelligence_bundle = build_executive_intelligence_bundle(combined_history, days=14)
        enriched_state["watchlist_report_path"] = str(intelligence_bundle.get("watchlist_path", "") or "")
        if not enriched_state.get("topic_pages"):
            enriched_state["topic_pages"] = list(intelligence_bundle.get("topic_page_artifacts", []) or [])
    except Exception as exc:
        logger.warning("Executive intelligence bundle skipped: %s", exc)
        enriched_state["watchlist_report_path"] = ""

    runtime_config = dict(state.get("runtime_config", {}) or {})
    if grok_source_gap_enabled(runtime_config):
        source_gap_result = suggest_source_gap_expansion(
            list(state.get("scored_articles", []) or [])[:grok_source_gap_max_articles(runtime_config)],
            list(state.get("raw_articles", []) or []),
            list(state.get("telegram_candidates", []) or []),
            feedback_summary_text=str(state.get("feedback_summary_text", "") or ""),
        )
        enriched_state["grok_source_gap_suggestions"] = list(source_gap_result.get("suggestions", []) or [])
        enriched_state["grok_source_gap_batch_note"] = str(source_gap_result.get("batch_note", "") or "")
    report_markdown = _build_run_report_markdown(enriched_state, generated_at)
    report_path.write_text(report_markdown, encoding="utf-8")

    artifact_cleanup = cleanup_runtime_artifacts(
        state=enriched_state,
        preserve_paths=[
            report_path,
            str(enriched_state.get("gather_snapshot_path", "") or ""),
            str(enriched_state.get("scored_snapshot_path", "") or ""),
            str(enriched_state.get("weekly_memo_path", "") or ""),
            str(enriched_state.get("watchlist_report_path", "") or ""),
        ],
    )
    cleanup_lines = build_artifact_cleanup_markdown(artifact_cleanup)
    if cleanup_lines:
        report_path.write_text(report_markdown.rstrip() + "\n\n" + "\n".join(cleanup_lines).rstrip() + "\n", encoding="utf-8")

    logger.info("🧾 Run report written: %s", report_path)
    return {
        "run_report_path": str(report_path),
        "weekly_memo_path": str(enriched_state.get("weekly_memo_path", "") or ""),
        "watchlist_report_path": str(enriched_state.get("watchlist_report_path", "") or ""),
        "topic_pages": list(enriched_state.get("topic_pages", []) or []),
        "run_health": run_health,
        "publish_ready": bool(run_health.get("publish_ready", False)),
        "grok_source_gap_suggestions": list(enriched_state.get("grok_source_gap_suggestions", []) or []),
        "grok_source_gap_batch_note": str(enriched_state.get("grok_source_gap_batch_note", "") or ""),
        "artifact_cleanup": artifact_cleanup,
    }
