"""
graph.py — LangGraph StateGraph: Daily Digest AI Agent (MVP3 Speed Optimized - Fixed Routing).

Flow mới:
  gather → normalize_source → deduplicate → collect_feedback → early_rule_filter → batch_classify_and_score
    ├─ top_articles → (Send fan-out theo chunk) → batch_deep_process
    └─ low_score_articles → batch_quick_compose
  → merge_processed_articles → delivery_judge → save_notion → summarize_vn → quality_gate → send_telegram → END
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from langgraph.graph import StateGraph, END
from langgraph.types import Send


# ── State chia sẻ giữa tất cả nodes ─────────────────────────────────
class DigestState(TypedDict, total=False):
    """Typed state dict — mỗi node đọc/ghi vào đây."""

    # (giữ nguyên toàn bộ state của bạn, không thay đổi gì)
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


# ── Node imports ─────────────────────────────────────────────────────
from nodes.gather_news import gather_news_node
from nodes.normalize_source import normalize_source_node
from nodes.deduplicate import deduplicate_node
from nodes.early_rule_filter_node import early_rule_filter_node
from nodes.collect_feedback import collect_feedback_node
from nodes.batch_classify_and_score_node import batch_classify_and_score_node
from nodes.batch_deep_process_node import batch_deep_process_node
from nodes.batch_quick_compose_node import batch_quick_compose_node
from nodes.delivery_judge import delivery_judge_node
from nodes.save_notion import save_notion_node
from nodes.summarize_vn import summarize_vn_node
from nodes.quality_gate import quality_gate_node
from nodes.send_telegram import send_telegram_node
from nodes.generate_run_report import generate_run_report_node


# ── Router mới (sạch sẽ + ổn định) ─────────────────────────────────
def _chunk_top_articles_for_parallel_send(state: DigestState) -> list[Send]:
    """Fan-out top_articles theo chunk (parallel batch_deep_process)"""
    top_articles = list(state.get("top_articles", []) or [])
    if not top_articles:
        return []

    runtime_config = dict(state.get("runtime_config", {}) or {})
    chunk_size = max(1, int(runtime_config.get("batch_deep_process_chunk_size", 3) or 3))
    sends: list[Send] = []
    for index in range(0, len(top_articles), chunk_size):
        sends.append(
            Send(
                "batch_deep_process",
                {
                    "top_articles": top_articles[index : index + chunk_size],
                    "runtime_config": runtime_config,
                    "run_profile": state.get("run_profile", ""),
                },
            )
        )
    return sends


def _route_batch_after_classify(state: DigestState) -> list[Send]:
    """
    // MVP3 Speed Optimized - Batch + Parallel
    Router sạch sẽ: fan-out cả 2 nhánh song song
    - top_articles → batch_deep_process (parallel chunks)
    - low_score_articles → batch_quick_compose
    """
    sends: list[Send] = []

    # 1. Top articles → parallel deep process
    sends.extend(_chunk_top_articles_for_parallel_send(state))

    # 2. Low score articles → quick compose (luôn chạy song song)
    low_articles = list(state.get("low_score_articles", []) or [])
    if low_articles:
        sends.append(
            Send(
                "batch_quick_compose",
                {
                    "low_score_articles": low_articles,
                    "runtime_config": dict(state.get("runtime_config", {}) or {}),
                },
            )
        )
    return sends


def _merge_processed_articles_node(state: DigestState) -> dict[str, Any]:
    """// MVP3 Speed Optimized - Batch + Parallel"""
    analyzed = list(state.get("analyzed_articles", []) or [])
    low = list(state.get("low_score_articles", []) or [])

    merged = []
    seen = set()
    for article in analyzed + low:
        key = str(article.get("url") or article.get("title") or id(article))
        if key in seen:
            continue
        seen.add(key)
        merged.append(article)

    if not merged:
        merged = list(state.get("scored_articles", []) or [])

    merged.sort(key=lambda a: int(a.get("total_score", 0) or 0), reverse=True)

    return {
        "analyzed_articles": analyzed,
        "final_articles": merged,
    }


def build_graph() -> StateGraph:
    """MVP3 Speed Optimized - Fixed Routing"""
    graph = StateGraph(DigestState)

    # Nodes
    graph.add_node("gather", gather_news_node)
    graph.add_node("normalize_source", normalize_source_node)
    graph.add_node("deduplicate", deduplicate_node)
    graph.add_node("early_rule_filter", early_rule_filter_node)
    graph.add_node("collect_feedback", collect_feedback_node)
    graph.add_node("batch_classify_and_score", batch_classify_and_score_node)
    graph.add_node("batch_deep_process", batch_deep_process_node)
    graph.add_node("batch_quick_compose", batch_quick_compose_node)
    graph.add_node("merge_processed_articles", _merge_processed_articles_node)
    graph.add_node("delivery_judge", delivery_judge_node)
    graph.add_node("save_notion", save_notion_node)
    graph.add_node("summarize_vn", summarize_vn_node)
    graph.add_node("quality_gate", quality_gate_node)
    graph.add_node("send_telegram", send_telegram_node)
    graph.add_node("generate_run_report", generate_run_report_node)

    # Edges
    graph.set_entry_point("gather")
    graph.add_edge("gather", "normalize_source")
    graph.add_edge("normalize_source", "deduplicate")
    graph.add_edge("deduplicate", "collect_feedback")
    graph.add_edge("collect_feedback", "early_rule_filter")
    graph.add_edge("early_rule_filter", "batch_classify_and_score")

    # ← Routing chính (sạch nhất)
    graph.add_conditional_edges(
        "batch_classify_and_score",
        _route_batch_after_classify,
        # Không cần path map vì dùng Send
    )

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