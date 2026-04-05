"""
graph.py — LangGraph StateGraph: Daily Digest AI Agent (MVP2).

Pipeline mới với conditional edges (Structured Agent):
  gather → normalize_source → deduplicate → classify_and_score
    → [score >= 60?] → deep_analysis → recommend_idea → compose_note_summary
    → [score < 60?]  → compose_note_summary
  → delivery_judge → save_notion → summarize_vn → quality_gate → send_telegram → END

Khác biệt so với MVP1:
  - Conditional routing: chỉ phân tích sâu bài có score cao
  - Long-term memory: recall bài cũ cùng chủ đề
  - 13 fields Notion thay vì 2
  - Format Telegram mới (từng bài + link Notion)
"""

from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import StateGraph, END


# ── State chia sẻ giữa tất cả nodes ─────────────────────────────────
class DigestState(TypedDict, total=False):
    """Typed state dict — mỗi node đọc/ghi vào đây."""

    # Cờ điều khiển run cho CLI/UI
    run_mode: str
    run_profile: str
    publish_notion: bool
    publish_telegram: bool
    persist_local: bool
    started_at: str
    runtime_config: dict[str, Any]

    # Bước 1: Thu thập
    raw_articles: list[dict[str, Any]]
    grok_scout_count: int
    facebook_discovered_sources: list[dict[str, Any]]
    facebook_auto_active_sources: list[dict[str, Any]]
    facebook_candidate_sources: list[dict[str, Any]]
    gather_snapshot_path: str

    # Bước 2: Sau deduplicate
    new_articles: list[dict[str, Any]]

    # Context phản hồi từ team qua Telegram
    recent_feedback: list[dict[str, Any]]
    feedback_summary_text: str
    feedback_label_counts: dict[str, int]
    feedback_sync: dict[str, Any]

    # Bước 3: Sau classify + score
    scored_articles: list[dict[str, Any]]
    scored_snapshot_path: str

    # Bước 4: Bài đủ điểm để phân tích sâu
    top_articles: list[dict[str, Any]]

    # Bước 5: Sau deep analysis + recommend
    analyzed_articles: list[dict[str, Any]]

    # Bước 6: Bài không đủ điểm (lưu Notion nhưng không phân tích sâu)
    low_score_articles: list[dict[str, Any]]

    # Bước 7: Tập bài cuối cùng dùng cho Notion + Telegram
    final_articles: list[dict[str, Any]]

    # Bước 8: Tập bài được judge là đủ tốt để lên Telegram
    telegram_candidates: list[dict[str, Any]]
    github_topic_candidates: list[dict[str, Any]]
    facebook_topic_candidates: list[dict[str, Any]]

    # Bước 9: Notion URLs cho từng bài
    notion_pages: list[dict[str, Any]]

    # Bước 10: Bản tổng hợp tiếng Việt
    summary_vn: str
    telegram_messages: list[str]
    github_topic_messages: list[str]
    facebook_topic_messages: list[str]

    # Bước 11: Metadata chất lượng của bản brief
    summary_mode: str
    summary_warnings: list[str]

    # Bước 12: Trạng thái gửi Telegram
    telegram_sent: bool
    github_topic_sent: bool
    facebook_topic_sent: bool

    # Bước 13: Báo cáo run để review với team / sếp
    run_report_path: str
    run_health: dict[str, Any]
    publish_ready: bool
    grok_source_gap_suggestions: list[dict[str, Any]]
    grok_source_gap_batch_note: str
    artifact_cleanup: dict[str, Any]


# ── Node imports ─────────────────────────────────────────────────────
from nodes.gather_news import gather_news_node
from nodes.normalize_source import normalize_source_node
from nodes.deduplicate import deduplicate_node
from nodes.collect_feedback import collect_feedback_node
from nodes.classify_and_score import classify_and_score_node
from nodes.deep_analysis import deep_analysis_node
from nodes.recommend_idea import recommend_idea_node
from nodes.compose_note_summary import compose_note_summary_node
from nodes.delivery_judge import delivery_judge_node
from nodes.save_notion import save_notion_node
from nodes.summarize_vn import summarize_vn_node
from nodes.quality_gate import quality_gate_node
from nodes.send_telegram import send_telegram_node
from nodes.generate_run_report import generate_run_report_node


# ── Router: quyết định có chạy deep analysis hay không ──────────────
def _route_after_score(state: DigestState) -> str:
    """
    Conditional edge: sau classify_and_score, kiểm tra xem có bài nào
    đủ điểm (score >= 60) để phân tích sâu không.

    Returns:
        'deep_analysis' nếu có bài top, 'compose_note_summary' nếu không có
    """
    top = state.get("top_articles", [])
    if top:
        return "deep_analysis"
    return "compose_note_summary"


def build_graph() -> StateGraph:
    """
    Xây dựng và compile LangGraph cho Daily Digest Agent (MVP2).

    Flow:
      gather → normalize_source → deduplicate → classify_and_score
        → [conditional] → deep_analysis → recommend_idea → compose_note_summary
        → [conditional] → compose_note_summary (skip analysis)
      → delivery_judge → save_notion → summarize_vn → quality_gate → send_telegram → END
    """
    graph = StateGraph(DigestState)

    # ── Đăng ký tất cả nodes ────────────────────────────────────────
    graph.add_node("gather", gather_news_node)
    graph.add_node("normalize_source", normalize_source_node)
    graph.add_node("deduplicate", deduplicate_node)
    graph.add_node("collect_feedback", collect_feedback_node)
    graph.add_node("classify_and_score", classify_and_score_node)
    graph.add_node("deep_analysis", deep_analysis_node)
    graph.add_node("recommend_idea", recommend_idea_node)
    graph.add_node("compose_note_summary", compose_note_summary_node)
    graph.add_node("delivery_judge", delivery_judge_node)
    graph.add_node("save_notion", save_notion_node)
    graph.add_node("summarize_vn", summarize_vn_node)
    graph.add_node("quality_gate", quality_gate_node)
    graph.add_node("send_telegram", send_telegram_node)
    graph.add_node("generate_run_report", generate_run_report_node)

    # ── Edges cố định ───────────────────────────────────────────────
    graph.set_entry_point("gather")
    graph.add_edge("gather", "normalize_source")
    graph.add_edge("normalize_source", "deduplicate")
    graph.add_edge("deduplicate", "collect_feedback")
    graph.add_edge("collect_feedback", "classify_and_score")

    # ── Conditional edge: chỉ deep analysis nếu có bài top ─────────
    graph.add_conditional_edges(
        "classify_and_score",
        _route_after_score,
        {
            "deep_analysis": "deep_analysis",
            "compose_note_summary": "compose_note_summary",
        },
    )

    # ── Tiếp tục flow sau deep analysis ─────────────────────────────
    graph.add_edge("deep_analysis", "recommend_idea")
    graph.add_edge("recommend_idea", "compose_note_summary")

    # ── Output: lưu → tổng hợp → gửi ──────────────────────────────
    graph.add_edge("compose_note_summary", "delivery_judge")
    graph.add_edge("delivery_judge", "save_notion")
    graph.add_edge("save_notion", "summarize_vn")
    graph.add_edge("summarize_vn", "quality_gate")
    graph.add_edge("quality_gate", "send_telegram")
    graph.add_edge("send_telegram", "generate_run_report")
    graph.add_edge("generate_run_report", END)

    return graph.compile()
