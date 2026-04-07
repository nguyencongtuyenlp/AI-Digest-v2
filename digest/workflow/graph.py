"""
Workflow graph implementation cho Daily Digest AI Agent.
"""

from __future__ import annotations

import operator
from time import perf_counter
from typing import Annotated, Any, TypedDict

from langgraph.graph import StateGraph, END
from langgraph.types import Send

from digest.runtime.stage_metrics import build_stage_timing_entry


class DigestState(TypedDict, total=False):
    # (giữ nguyên toàn bộ state của bạn, không thay đổi)
    run_mode: str
    run_profile: str
    publish_notion: bool
    publish_telegram: bool
    persist_local: bool
    started_at: str
    runtime_config: dict[str, Any]
    source_health: dict[str, str]
    source_health_alert_sent: bool

    raw_articles: list[dict[str, Any]]
    grok_scout_count: int
    gather_snapshot_path: str

    new_articles: list[dict[str, Any]]
    filtered_articles: list[dict[str, Any]]

    recent_feedback: list[dict[str, Any]]
    feedback_summary_text: str
    feedback_label_counts: dict[str, int]
    feedback_preference_profile: dict[str, Any]
    feedback_sync: dict[str, Any]

    scored_articles: list[dict[str, Any]]
    scored_snapshot_path: str

    top_articles: list[dict[str, Any]]

    analyzed_articles: Annotated[list[dict[str, Any]], operator.add]

    low_score_articles: list[dict[str, Any]]

    final_articles: list[dict[str, Any]]

    telegram_candidates: list[dict[str, Any]]

    notion_pages: list[dict[str, Any]]
    topic_pages: list[dict[str, Any]]

    summary_vn: str
    telegram_messages: list[str]

    summary_mode: str
    summary_warnings: list[str]

    telegram_sent: bool

    run_report_path: str
    weekly_memo_path: str
    watchlist_report_path: str
    run_health: dict[str, Any]
    publish_ready: bool
    grok_source_gap_suggestions: list[dict[str, Any]]
    grok_source_gap_batch_note: str
    artifact_cleanup: dict[str, Any]
    performance_report: dict[str, Any]
    stage_timings: Annotated[list[dict[str, Any]], operator.add]


# ── Node imports ─────────────────────────────────────────────────────
from digest.workflow.nodes.gather_news import gather_news_node
from digest.workflow.nodes.normalize_source import normalize_source_node
from digest.workflow.nodes.deduplicate import deduplicate_node
from digest.workflow.nodes.early_rule_filter_node import early_rule_filter_node
from digest.workflow.nodes.collect_feedback import collect_feedback_node
from digest.workflow.nodes.batch_classify_and_score_node import batch_classify_and_score_node
from digest.workflow.nodes.batch_deep_process_node import batch_deep_process_node
from digest.workflow.nodes.batch_quick_compose_node import batch_quick_compose_node
from digest.workflow.nodes.delivery_judge import delivery_judge_node
from digest.workflow.nodes.save_notion import save_notion_node
from digest.workflow.nodes.summarize_vn import summarize_vn_node
from digest.workflow.nodes.quality_gate import quality_gate_node
from digest.workflow.nodes.send_telegram import send_telegram_node
from digest.workflow.nodes.generate_run_report import generate_run_report_node


def _chunk_top_articles_for_parallel_send(state: DigestState) -> list[Send]:
    """Fan-out top_articles theo chunk"""
    top_articles = list(state.get("top_articles", []) or [])
    if not top_articles:
        return []
    runtime_config = dict(state.get("runtime_config", {}) or {})
    chunk_size = max(1, int(runtime_config.get("batch_deep_process_chunk_size", 3)))
    sends: list[Send] = []
    for i in range(0, len(top_articles), chunk_size):
        sends.append(Send("batch_deep_process", {
            "top_articles": top_articles[i:i+chunk_size],
            "runtime_config": runtime_config,
            "run_profile": state.get("run_profile", ""),
        }))
    return sends


def _route_batch_after_classify(state: DigestState) -> list[Send]:
    """MVP3 Speed Optimized - Batch + Parallel"""
    sends: list[Send] = _chunk_top_articles_for_parallel_send(state)
    low_articles = list(state.get("low_score_articles", []) or [])
    if low_articles:
        sends.append(Send("batch_quick_compose", {
            "low_score_articles": low_articles,
            "runtime_config": dict(state.get("runtime_config", {}) or {}),
        }))
    return sends


def _merge_processed_articles_node(state: DigestState) -> dict[str, Any]:
    """Merge sạch sẽ"""
    analyzed = list(state.get("analyzed_articles", []) or [])
    low = list(state.get("low_score_articles", []) or [])
    merged = []
    seen = set()
    for a in analyzed + low:
        key = str(a.get("url") or a.get("title") or id(a))
        if key in seen: continue
        seen.add(key)
        merged.append(a)
    if not merged:
        merged = list(state.get("scored_articles", []) or [])
    merged.sort(key=lambda x: int(x.get("total_score", 0)), reverse=True)
    return {"analyzed_articles": analyzed, "final_articles": merged}


def _instrument_stage_node(stage: str, node_fn):
    def _wrapped(state: DigestState) -> dict[str, Any]:
        started = perf_counter()
        result = dict(node_fn(state) or {})
        result.setdefault(
            "stage_timings",
            [build_stage_timing_entry(stage, state, result, (perf_counter() - started) * 1000.0)],
        )
        return result

    return _wrapped


def build_graph() -> StateGraph:
    graph = StateGraph(DigestState)

    graph.add_node("gather", _instrument_stage_node("gather", gather_news_node))
    graph.add_node("normalize_source", _instrument_stage_node("normalize_source", normalize_source_node))
    graph.add_node("deduplicate", _instrument_stage_node("deduplicate", deduplicate_node))
    graph.add_node("early_rule_filter", _instrument_stage_node("early_rule_filter", early_rule_filter_node))
    graph.add_node("collect_feedback", _instrument_stage_node("collect_feedback", collect_feedback_node))
    graph.add_node("batch_classify_and_score", _instrument_stage_node("batch_classify_and_score", batch_classify_and_score_node))
    graph.add_node("batch_deep_process", _instrument_stage_node("batch_deep_process", batch_deep_process_node))
    graph.add_node("batch_quick_compose", _instrument_stage_node("batch_quick_compose", batch_quick_compose_node))
    graph.add_node("merge_processed_articles", _instrument_stage_node("merge_processed_articles", _merge_processed_articles_node))
    graph.add_node("delivery_judge", _instrument_stage_node("delivery_judge", delivery_judge_node))
    graph.add_node("save_notion", _instrument_stage_node("save_notion", save_notion_node))
    graph.add_node("summarize_vn", _instrument_stage_node("summarize_vn", summarize_vn_node))
    graph.add_node("quality_gate", _instrument_stage_node("quality_gate", quality_gate_node))
    graph.add_node("send_telegram", _instrument_stage_node("send_telegram", send_telegram_node))
    graph.add_node("generate_run_report", generate_run_report_node)

    graph.set_entry_point("gather")
    graph.add_edge("gather", "normalize_source")
    graph.add_edge("normalize_source", "deduplicate")
    graph.add_edge("deduplicate", "collect_feedback")
    graph.add_edge("collect_feedback", "early_rule_filter")
    graph.add_edge("early_rule_filter", "batch_classify_and_score")

    graph.add_conditional_edges("batch_classify_and_score", _route_batch_after_classify)
    graph.add_edge("batch_deep_process", "merge_processed_articles")
    graph.add_edge("batch_quick_compose", "merge_processed_articles")
    graph.add_edge("merge_processed_articles", "delivery_judge")
    graph.add_edge("delivery_judge", "save_notion")
    graph.add_edge("save_notion", "summarize_vn")
    graph.add_edge("summarize_vn", "quality_gate")
    graph.add_edge("quality_gate", "send_telegram")
    graph.add_edge("send_telegram", "generate_run_report")
    graph.add_edge("generate_run_report", END)

    return graph.compile()
