"""
pipeline_runner.py — Chạy pipeline theo nhiều chế độ nhưng dùng chung một lõi.

Ý tưởng chính:
- `publish`: hành vi production như cũ
- `preview`: chạy toàn bộ reasoning nhưng không publish ra ngoài

Nhờ vậy:
- CLI / launchd vẫn dùng được như trước
- UI chỉ là lớp điều khiển bên ngoài, không làm agent "ngu đi"
"""

from __future__ import annotations

import contextlib
import fcntl
from functools import lru_cache
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from runtime_presets import apply_runtime_preset
from mlx_runner import clear_runtime_mlx_model_path, set_runtime_mlx_model_path
from run_health import collect_source_health, notify_source_health_if_needed

logger = logging.getLogger(__name__)


@contextlib.contextmanager
def _pipeline_run_lock():
    """
    Khóa liên tiến trình để launchd, CLI và UI không chạy chồng pipeline lên nhau.
    Nếu đang có run khác, lời gọi sau sẽ đợi cho đến khi lock được nhả ra.
    """
    lock_path = Path(os.getenv("DIGEST_RUN_LOCK_PATH", "/tmp/daily-digest-agent.pipeline.lock"))
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as handle:
        logger.info("🔒 Waiting for pipeline run lock: %s", lock_path)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()} started_at={datetime.now(timezone.utc).isoformat()}\n")
        handle.flush()
        logger.info("🔓 Pipeline run lock acquired.")
        try:
            yield
        finally:
            handle.seek(0)
            handle.truncate()
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            logger.info("🔓 Pipeline run lock released.")


@contextlib.contextmanager
def _runtime_model_override(runtime_config: dict[str, Any] | None = None):
    config = dict(runtime_config or {})
    set_runtime_mlx_model_path(str(config.get("runtime_mlx_model", "")).strip() or None)
    try:
        yield
    finally:
        clear_runtime_mlx_model_path()


def build_initial_state(
    run_mode: str = "publish",
    runtime_config: dict[str, Any] | None = None,
    run_profile: str = "",
) -> dict[str, Any]:
    normalized_mode = str(run_mode or "publish").strip().lower()
    if normalized_mode not in {"publish", "preview"}:
        normalized_mode = "publish"
    normalized_profile = str(run_profile or normalized_mode).strip().lower()
    merged_runtime = dict(runtime_config or {})
    merged_runtime = apply_runtime_preset(normalized_profile, merged_runtime)

    is_publish = normalized_mode == "publish"
    return {
        # `run_mode` là cờ tổng để các node biết mình đang chạy kiểu nào.
        "run_mode": normalized_mode,
        # Hai cờ này giúp UI tắt publish riêng từng đầu ra nếu cần.
        "publish_notion": is_publish,
        "publish_telegram": is_publish,
        # Preview không ghi SQLite/Chroma để tránh pollute lịch sử khi test.
        "persist_local": is_publish,
        "run_profile": normalized_profile,
        "runtime_config": merged_runtime,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }


@lru_cache(maxsize=1)
def _get_pipeline_graph():
    from graph import build_graph

    return build_graph()


def summarize_result(result: dict[str, Any], elapsed_seconds: float) -> dict[str, Any]:
    telegram_messages = [msg for msg in result.get("telegram_messages", []) if str(msg or "").strip()]
    published_notion_pages = [
        page for page in result.get("notion_pages", [])
        if isinstance(page, dict) and str(page.get("url", "") or "").strip()
    ]
    return {
        "run_mode": result.get("run_mode", "unknown"),
        "elapsed_seconds": round(elapsed_seconds, 2),
        "raw_count": len(result.get("raw_articles", [])),
        "grok_scout_count": int(result.get("grok_scout_count", 0) or 0),
        "new_count": len(result.get("new_articles", [])),
        "scored_count": len(result.get("scored_articles", [])),
        "top_count": len(result.get("top_articles", [])),
        "final_count": len(result.get("final_articles", [])),
        "telegram_candidate_count": len(result.get("telegram_candidates", [])),
        "notion_count": len(published_notion_pages),
        "telegram_sent": bool(result.get("telegram_sent", False)),
        "summary_mode": result.get("summary_mode", "unknown"),
        "summary_warnings": list(result.get("summary_warnings", []) or []),
        "run_report_path": result.get("run_report_path", ""),
        "telegram_messages": telegram_messages,
        "run_profile": result.get("run_profile", ""),
        "runtime_config": dict(result.get("runtime_config", {}) or {}),
        "feedback_summary_text": str(result.get("feedback_summary_text", "") or ""),
        "feedback_label_counts": dict(result.get("feedback_label_counts", {}) or {}),
        "feedback_preference_profile": dict(result.get("feedback_preference_profile", {}) or {}),
        "feedback_sync": dict(result.get("feedback_sync", {}) or {}),
        "run_health": dict(result.get("run_health", {}) or {}),
        "publish_ready": bool(result.get("publish_ready", False)),
        "gather_snapshot_path": str(result.get("gather_snapshot_path", "") or ""),
        "scored_snapshot_path": str(result.get("scored_snapshot_path", "") or ""),
        "weekly_memo_path": str(result.get("weekly_memo_path", "") or ""),
        "watchlist_report_path": str(result.get("watchlist_report_path", "") or ""),
        "topic_pages": list(result.get("topic_pages", []) or []),
        "artifact_cleanup": dict(result.get("artifact_cleanup", {}) or {}),
    }


def run_pipeline(
    run_mode: str = "publish",
    runtime_config: dict[str, Any] | None = None,
    run_profile: str = "",
) -> tuple[dict[str, Any], dict[str, Any]]:
    initial_state = build_initial_state(
        run_mode=run_mode,
        runtime_config=runtime_config,
        run_profile=run_profile,
    )

    with _runtime_model_override(initial_state.get("runtime_config", {})):
        with _pipeline_run_lock():
            start = datetime.now(timezone.utc)
            source_health = collect_source_health()
            initial_state["source_health"] = source_health
            initial_state["source_health_alert_sent"] = notify_source_health_if_needed(
                source_health,
                run_mode=initial_state.get("run_mode", "publish"),
            )
            app = _get_pipeline_graph()
            result = app.invoke(initial_state)
            elapsed = (datetime.now(timezone.utc) - start).total_seconds()

            # Ghi ngược run_mode vào result để downstream/UI dễ hiển thị.
            result["run_mode"] = str(run_mode or "publish").strip().lower()
            result["run_profile"] = str(run_profile or run_mode or "publish").strip().lower()
            result["runtime_config"] = dict(initial_state.get("runtime_config", {}) or {})
            return result, summarize_result(result, elapsed)


def _publish_selected_outputs_from_preview(
    preview_state: dict[str, Any],
    *,
    publish_notion: bool,
    publish_telegram: bool,
    run_profile: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """
    Publish từ đúng preview state đã duyệt, không regather/rescore lại từ đầu.
    Có thể chọn publish riêng Notion hoặc Telegram.
    """
    with _pipeline_run_lock():
        start = datetime.now(timezone.utc)
        from nodes.generate_run_report import generate_run_report_node
        from nodes.quality_gate import quality_gate_node
        from nodes.save_notion import save_notion_node
        from nodes.send_telegram import send_telegram_node
        from nodes.summarize_vn import summarize_vn_node

        # Clone nông đủ dùng vì downstream chủ yếu update dict top-level / article fields.
        state = {
            key: (list(value) if isinstance(value, list) else dict(value) if isinstance(value, dict) else value)
            for key, value in dict(preview_state or {}).items()
        }
        state["run_mode"] = "publish"
        state["run_profile"] = str(run_profile or "approved_preview").strip().lower()
        state["publish_notion"] = bool(publish_notion)
        state["publish_telegram"] = bool(publish_telegram)
        state["persist_local"] = bool(publish_notion or publish_telegram)
        state["preview_publish_state"] = (
            "fully_published" if publish_notion and publish_telegram
            else "notion_only_published" if publish_notion
            else "telegram_only_published" if publish_telegram
            else "preview_only"
        )

        nodes = []
        if publish_notion:
            nodes.append(save_notion_node)
        nodes.extend([summarize_vn_node, quality_gate_node])
        if publish_telegram:
            nodes.append(send_telegram_node)
        nodes.append(generate_run_report_node)

        with _runtime_model_override(state.get("runtime_config", {})):
            for node in nodes:
                state.update(node(state))

        elapsed = (datetime.now(timezone.utc) - start).total_seconds()
        return state, summarize_result(state, elapsed)


def publish_from_preview_state(preview_state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """
    Publish từ đúng preview state đã duyệt, không regather/rescore lại từ đầu.
    Nếu preview đã đẩy Notion trước đó, lần approve tiếp theo chỉ gửi Telegram.
    """
    preview_publish_state = str((preview_state or {}).get("preview_publish_state", "") or "")
    notion_already_published = preview_publish_state == "notion_only_published"
    return _publish_selected_outputs_from_preview(
        preview_state,
        publish_notion=not notion_already_published,
        publish_telegram=True,
        run_profile="approved_preview_telegram_only" if notion_already_published else "approved_preview",
    )


def publish_notion_only_from_preview_state(preview_state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """
    Đẩy đúng batch preview lên Notion để review trước, chưa gửi Telegram.
    """
    return _publish_selected_outputs_from_preview(
        preview_state,
        publish_notion=True,
        publish_telegram=False,
        run_profile="preview_notion_only",
    )
