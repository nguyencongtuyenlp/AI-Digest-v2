"""
stage_metrics.py - Lightweight stage timing and performance summaries.

This module stays deliberately runtime-agnostic so the workflow can keep the
same reporting shape even if the inference backend changes later.
"""

from __future__ import annotations

import os
from typing import Any

STAGE_ORDER = (
    "gather",
    "normalize_source",
    "deduplicate",
    "collect_feedback",
    "early_rule_filter",
    "batch_classify_and_score",
    "batch_deep_process",
    "batch_quick_compose",
    "merge_processed_articles",
    "delivery_judge",
    "save_notion",
    "summarize_vn",
    "quality_gate",
    "send_telegram",
    "generate_run_report",
)

STAGE_IO_FIELDS: dict[str, dict[str, tuple[Any, ...]]] = {
    "gather": {"input": tuple(), "output": ("raw_articles",)},
    "normalize_source": {"input": ("raw_articles",), "output": ("raw_articles",)},
    "deduplicate": {"input": ("raw_articles",), "output": ("new_articles",)},
    "collect_feedback": {"input": ("new_articles",), "output": ("recent_feedback",)},
    "early_rule_filter": {"input": ("new_articles",), "output": ("filtered_articles",)},
    "batch_classify_and_score": {"input": ("filtered_articles", "new_articles"), "output": ("scored_articles",)},
    "batch_deep_process": {"input": ("top_articles",), "output": ("analyzed_articles",)},
    "batch_quick_compose": {"input": ("low_score_articles",), "output": ("low_score_articles",)},
    "merge_processed_articles": {"input": (("analyzed_articles", "low_score_articles"),), "output": ("final_articles",)},
    "delivery_judge": {"input": ("final_articles",), "output": ("telegram_candidates",)},
    "save_notion": {"input": ("final_articles",), "output": ("notion_pages",)},
    "summarize_vn": {"input": ("telegram_candidates",), "output": ("telegram_messages",)},
    "quality_gate": {"input": ("telegram_messages", "telegram_candidates"), "output": ("telegram_messages",)},
    "send_telegram": {"input": ("telegram_messages",), "output": ("telegram_sent",)},
    "generate_run_report": {"input": ("final_articles",), "output": ("run_report_path",)},
}


def runtime_backend_label() -> str:
    return str(os.getenv("DIGEST_INFERENCE_BACKEND", "mlx") or "mlx").strip().lower() or "mlx"


def _count_value(value: Any) -> int | None:
    if isinstance(value, list):
        return len(value)
    if isinstance(value, tuple):
        return len(value)
    if isinstance(value, dict):
        return len(value)
    if isinstance(value, str):
        return 1 if value.strip() else 0
    if isinstance(value, bool):
        return int(value)
    return None


def _resolve_count(container: dict[str, Any], specs: tuple[Any, ...]) -> int | None:
    for spec in specs:
        if isinstance(spec, tuple):
            total = 0
            found = False
            for key in spec:
                count = _count_value(container.get(key))
                if count is None:
                    continue
                found = True
                total += count
            if found:
                return total
            continue
        count = _count_value(container.get(spec))
        if count is not None:
            return count
    return None


def build_stage_timing_entry(
    stage: str,
    state: dict[str, Any],
    result: dict[str, Any] | None,
    duration_ms: float,
) -> dict[str, Any]:
    result_dict = dict(result or {})
    mapping = STAGE_IO_FIELDS.get(stage, {})
    input_count = _resolve_count(state, tuple(mapping.get("input", tuple())))
    output_count = _resolve_count(result_dict, tuple(mapping.get("output", tuple())))

    if stage == "send_telegram":
        telegram_messages = list(state.get("telegram_messages", []) or [])
        output_count = len(telegram_messages) if bool(result_dict.get("telegram_sent", False)) else 0
    elif stage == "generate_run_report":
        output_count = 1 if str(result_dict.get("run_report_path", "") or "").strip() else 0

    return {
        "stage": stage,
        "duration_ms": round(float(duration_ms), 2),
        "input_count": input_count,
        "output_count": output_count,
    }


def _article_text_len(article: dict[str, Any], fields: tuple[str, ...]) -> int:
    total = 0
    for field in fields:
        total += len(str(article.get(field, "") or ""))
    return total


def summarize_stage_timings(stage_timings: list[dict[str, Any]], state: dict[str, Any]) -> dict[str, Any]:
    aggregated: dict[str, dict[str, Any]] = {}
    for stage in STAGE_ORDER:
        aggregated[stage] = {
            "stage": stage,
            "invocations": 0,
            "total_duration_ms": 0.0,
            "max_duration_ms": 0.0,
            "input_count_total": 0,
            "output_count_total": 0,
            "last_input_count": None,
            "last_output_count": None,
        }

    for entry in stage_timings:
        if not isinstance(entry, dict):
            continue
        stage = str(entry.get("stage", "") or "").strip()
        if not stage:
            continue
        bucket = aggregated.setdefault(
            stage,
            {
                "stage": stage,
                "invocations": 0,
                "total_duration_ms": 0.0,
                "max_duration_ms": 0.0,
                "input_count_total": 0,
                "output_count_total": 0,
                "last_input_count": None,
                "last_output_count": None,
            },
        )
        duration = float(entry.get("duration_ms", 0.0) or 0.0)
        bucket["invocations"] += 1
        bucket["total_duration_ms"] += duration
        bucket["max_duration_ms"] = max(bucket["max_duration_ms"], duration)
        input_count = entry.get("input_count")
        output_count = entry.get("output_count")
        if isinstance(input_count, int):
            bucket["input_count_total"] += input_count
            bucket["last_input_count"] = input_count
        if isinstance(output_count, int):
            bucket["output_count_total"] += output_count
            bucket["last_output_count"] = output_count

    stage_summaries = []
    for stage in STAGE_ORDER:
        bucket = aggregated.get(stage)
        if not bucket or bucket["invocations"] == 0:
            continue
        bucket["total_duration_ms"] = round(bucket["total_duration_ms"], 2)
        bucket["max_duration_ms"] = round(bucket["max_duration_ms"], 2)
        stage_summaries.append(bucket)

    slowest_stages = sorted(stage_summaries, key=lambda item: item["total_duration_ms"], reverse=True)[:5]

    runtime_config = dict(state.get("runtime_config", {}) or {})
    classify_limit = int(runtime_config.get("classify_content_char_limit", 900) or 900)
    top_articles = list(state.get("top_articles", []) or [])
    scored_articles = list(state.get("scored_articles", []) or [])
    telegram_candidates = list(state.get("telegram_candidates", []) or [])

    token_waste_hotspots: list[str] = []
    long_top_articles = [
        article for article in top_articles
        if _article_text_len(article, ("content", "snippet")) > 1800
        and (
            str(article.get("factual_summary_vi", "") or "").strip()
            or str(article.get("why_it_matters_vi", "") or "").strip()
        )
    ]
    if long_top_articles:
        token_waste_hotspots.append(
            f"batch_deep_process vẫn phải nhìn context dài cho {len(long_top_articles)} top articles; đây là hotspot token lớn nhất còn lại."
        )

    oversized_classify_inputs = [
        article for article in list(state.get("filtered_articles", []) or state.get("new_articles", []) or [])
        if len(str(article.get("content", "") or article.get("snippet", "") or "")) > classify_limit
    ]
    if oversized_classify_inputs:
        token_waste_hotspots.append(
            f"batch_classify_and_score có {len(oversized_classify_inputs)} bài dài hơn classify limit {classify_limit} chars; truncation đang giúp nhưng đây vẫn là nguồn prompt volume đáng chú ý."
        )

    if len(scored_articles) > max(20, len(telegram_candidates) * 4) and telegram_candidates:
        token_waste_hotspots.append(
            "Tỷ lệ scored_articles so với telegram_candidates còn cao; classify và deep-process vẫn đang gánh nhiều item chỉ để loại ở các stage sau."
        )

    parallelization_opportunities: list[str] = []
    save_notion_stats = next((item for item in stage_summaries if item["stage"] == "save_notion"), None)
    if save_notion_stats and save_notion_stats["total_duration_ms"] >= 500 and save_notion_stats["input_count_total"] > 1:
        parallelization_opportunities.append(
            "save_notion là ứng viên tốt cho parallel publish writes sau này, nhất là ở publish runs có nhiều page."
        )
    send_stats = next((item for item in stage_summaries if item["stage"] == "send_telegram"), None)
    if send_stats and send_stats["total_duration_ms"] >= 300 and send_stats["input_count_total"] > 1:
        parallelization_opportunities.append(
            "send_telegram vẫn gửi tuần tự theo chunk; có thể song song hóa nhẹ sau nếu cần giảm publish latency."
        )
    deep_stats = next((item for item in stage_summaries if item["stage"] == "batch_deep_process"), None)
    if deep_stats and deep_stats["invocations"] > 1:
        parallelization_opportunities.append(
            "batch_deep_process đã fan-out theo chunk, nhưng community-search/cache vẫn còn dư địa để tách riêng và song song hóa sâu hơn."
        )
    gather_stats = next((item for item in stage_summaries if item["stage"] == "gather"), None)
    if gather_stats and gather_stats["total_duration_ms"] >= 1000:
        parallelization_opportunities.append(
            "gather còn là stage dễ hưởng lợi nhất từ parallel source fetch / source health prechecks trong tương lai."
        )

    return {
        "backend": runtime_backend_label(),
        "stage_summaries": stage_summaries,
        "slowest_stages": slowest_stages,
        "token_waste_hotspots": token_waste_hotspots,
        "future_parallelization_opportunities": parallelization_opportunities,
    }
