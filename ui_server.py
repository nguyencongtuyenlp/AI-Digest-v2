#!/usr/bin/env python3
"""
ui_server.py — Local control panel cho Daily Digest Agent.

Phiên bản này ưu tiên 3 lớp UX quan trọng:
- Approve from preview
- xem trước đúng output Telegram theo 3 lane
- giữ review đơn giản, không biến UI thành nơi chỉnh threshold/preset
"""

from __future__ import annotations

import json
import logging
import os
import errno
import sys
import threading
import time
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

from pipeline_runner import publish_from_preview_state, publish_notion_only_from_preview_state, run_pipeline
from runtime_presets import apply_runtime_preset

env_path = PROJECT_ROOT / "config" / ".env"
if env_path.exists():
    load_dotenv(env_path)
else:
    load_dotenv(PROJECT_ROOT / ".env")


logger = logging.getLogger("digest-ui")


def _harden_http_logging() -> None:
    """Giảm log noise và tránh lộ token Telegram trong URL debug logs."""
    for lib in ("httpx", "httpcore", "urllib3", "requests", "requests.packages.urllib3"):
        logging.getLogger(lib).setLevel(logging.WARNING)


_harden_http_logging()


def _default_runtime_config() -> dict[str, Any]:
    """
    UI preview không nên tự override production.
    Mặc định trả về rỗng để pipeline đọc config thật từ env/runtime bên dưới.
    """
    return {}


class _RunLogHandler(logging.Handler):
    """Handler nhỏ để UI giữ lại log gần nhất cho người dùng xem nhanh."""

    def emit(self, record: logging.LogRecord) -> None:
        message = self.format(record)
        with APP_STATE["lock"]:
            logs = APP_STATE.setdefault("logs", [])
            logs.append(message)
            if len(logs) > 200:
                del logs[: len(logs) - 200]


APP_STATE: dict[str, Any] = {
    "lock": threading.Lock(),
    "running": False,
    "job_id": 0,
    "started_at": 0.0,
    "last_mode": "",
    "last_profile": "",
    "last_result": {},
    "last_error": "",
    "logs": [],
    "preview_state": None,
    "workspace_articles": [],
    "runtime_config": _default_runtime_config(),
}


def _read_report_content(report_path: str) -> str:
    if not report_path:
        return ""
    try:
        return Path(report_path).read_text(encoding="utf-8")
    except Exception:
        return ""


def _clean_preview_text(value: Any, max_len: int = 0) -> str:
    text = " ".join(str(value or "").replace("\ufeff", "").split())
    if max_len and len(text) > max_len:
        return text[: max_len - 1].rstrip() + "…"
    return text


def _format_preview_datetime(value: Any) -> str:
    raw = _clean_preview_text(value)
    if not raw:
        return "-"

    candidates = [raw, raw.replace(" ", "T")]
    for candidate in candidates:
        try:
            dt = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
            return dt.astimezone().strftime("%d/%m %H:%M")
        except ValueError:
            continue
    return raw[:16]


def _score_level(score: int) -> str:
    if score >= 75:
        return "High"
    if score >= 55:
        return "Medium"
    return "Low"


def _project_fit(article: dict[str, Any]) -> str:
    score = int(article.get("total_score", 0) or 0)
    decision = str(article.get("delivery_decision", "") or "").strip().lower()
    analysis_tier = str(article.get("analysis_tier", "") or "").strip().lower()
    tags = {
        str(tag).strip().lower()
        for tag in list(article.get("tags", []) or [])
        if str(tag).strip()
    }

    if decision == "include" or score >= 75:
        return "High"
    if {"api_platform", "model_release", "funding", "research", "vietnam"} & tags:
        return "High"
    if analysis_tier == "deep" or score >= 55:
        return "Medium"
    return "Low"


def _delivery_status(article: dict[str, Any]) -> str:
    decision = str(article.get("delivery_decision", "") or "").strip().lower()
    if decision == "include":
        return "Brief"
    if decision == "review":
        return "Review"
    if not article.get("event_is_primary", True):
        return "Cluster"
    if int(article.get("total_score", 0) or 0) >= 55:
        return "Watch"
    return "Hold"


def _build_workspace_articles(state: dict[str, Any] | None) -> list[dict[str, Any]]:
    current_state = dict(state or {})
    notion_pages = list(current_state.get("notion_pages", []) or [])
    notion_lookup = {
        str(page.get("source_url", "") or ""): str(page.get("url", "") or "")
        for page in notion_pages
        if isinstance(page, dict)
    }

    articles = (
        list(current_state.get("final_articles", []) or [])
        or list(current_state.get("scored_articles", []) or [])
        or list(current_state.get("new_articles", []) or [])
        or list(current_state.get("raw_articles", []) or [])
    )

    rows: list[dict[str, Any]] = []
    for index, article in enumerate(articles[:36], 1):
        if not isinstance(article, dict):
            continue

        score = int(article.get("total_score", 0) or 0)
        title = _clean_preview_text(article.get("title", "Untitled"), 120)
        if not title:
            continue

        url = str(article.get("url", "") or "")
        tags = [
            _clean_preview_text(tag, 22)
            for tag in list(article.get("tags", []) or [])
            if _clean_preview_text(tag)
        ][:4]
        summary = (
            _clean_preview_text(article.get("note_summary_vi", ""), 260)
            or _clean_preview_text(article.get("summary_vi", ""), 260)
            or _clean_preview_text(article.get("editorial_angle", ""), 260)
            or _clean_preview_text(article.get("snippet", ""), 260)
            or "Chưa có summary ngắn cho article này."
        )

        rows.append(
            {
                "id": index,
                "title": title,
                "emoji": _clean_preview_text(article.get("primary_emoji", "[]"), 4),
                "created_time": _format_preview_datetime(
                    article.get("published_at")
                    or article.get("discovered_at")
                    or article.get("fetched_at")
                    or ""
                ),
                "source": _clean_preview_text(article.get("source_domain") or article.get("source") or "unknown", 32),
                "source_name": _clean_preview_text(article.get("source") or article.get("source_domain") or "unknown", 60),
                "source_tier": _clean_preview_text(article.get("source_tier", "unknown"), 14),
                "url": url,
                "notion_url": notion_lookup.get(url, ""),
                "score": score,
                "summary": summary,
                "type": _clean_preview_text(article.get("primary_type", "Unknown"), 18),
                "tags": tags,
                "relevance_level": _clean_preview_text(article.get("relevance_level", _score_level(score)), 12),
                "project_fit": _project_fit(article),
                "analysis_tier": _clean_preview_text(article.get("analysis_tier", "basic"), 10),
                "delivery_status": _delivery_status(article),
                "editorial_angle": _clean_preview_text(article.get("editorial_angle", ""), 220),
                "grounding_note": _clean_preview_text(article.get("grounding_note", ""), 260),
                "confidence": _clean_preview_text(article.get("confidence_label", "unknown"), 12),
                "event_source_count": int(article.get("event_source_count", 1) or 1),
            }
        )

    return rows


def _status_payload() -> dict[str, Any]:
    with APP_STATE["lock"]:
        running = bool(APP_STATE.get("running"))
        started_at = float(APP_STATE.get("started_at", 0.0) or 0.0)
        result = dict(APP_STATE.get("last_result", {}) or {})
        preview_available = APP_STATE.get("preview_state") is not None
        payload = {
            "running": running,
            "job_id": int(APP_STATE.get("job_id", 0) or 0),
            "last_mode": APP_STATE.get("last_mode", ""),
            "last_profile": APP_STATE.get("last_profile", ""),
            "last_error": APP_STATE.get("last_error", ""),
            "logs": list(APP_STATE.get("logs", [])[-80:]),
            "result": result,
            "can_approve": bool(preview_available and not running),
            "can_publish_notion_only": bool(
                preview_available
                and not running
                and str((APP_STATE.get("preview_state") or {}).get("preview_publish_state", "") or "") != "notion_only_published"
            ),
            "runtime_config": dict(APP_STATE.get("runtime_config", {}) or {}),
            "preview_articles": list(APP_STATE.get("workspace_articles", []) or []),
            "preview_publish_state": str((APP_STATE.get("preview_state") or {}).get("preview_publish_state", "") or ""),
        }

    payload["elapsed_seconds"] = round(max(0.0, time.time() - started_at), 1) if running and started_at else 0.0
    payload["report_content"] = _read_report_content(str(result.get("run_report_path", "") or ""))
    return payload


def _normalize_runtime_config(raw: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(raw or {})
    if not merged:
        return {}

    int_defaults = {
        "min_deep_analysis_score": 60,
        "max_classify_articles": 8,
        "max_deep_analysis_articles": 10,
        "github_max_watchlist_repos": 6,
        "github_max_orgs": 4,
        "github_max_queries": 4,
        "github_max_org_repos": 4,
        "github_max_search_results": 4,
        "gather_rss_hours": 72,
        "classify_content_char_limit": 900,
        "classify_max_tokens": 320,
    }
    for key, default in int_defaults.items():
        if key not in merged:
            continue
        try:
            merged[key] = int(merged[key])
        except (TypeError, ValueError):
            merged[key] = default

    bool_defaults = {
        "enable_rss": True,
        "enable_github": True,
        "enable_social_signals": True,
        "enable_ddg": True,
        "enable_hn": True,
        "enable_reddit": True,
        "enable_watchlist": True,
        "enable_telegram_channels": True,
        "skip_feedback_sync": False,
    }
    for key, default in bool_defaults.items():
        if key not in merged:
            continue
        value = merged.get(key)
        if isinstance(value, bool):
            merged[key] = value
        else:
            merged[key] = str(value).strip().lower() not in {"0", "false", "no", "off"} if value is not None else default

    return merged


def _run_in_background(mode: str, runtime_config: dict[str, Any], run_profile: str) -> None:
    try:
        result, summary = run_pipeline(run_mode=mode, runtime_config=runtime_config, run_profile=run_profile)
        summary["summary_text"] = str(result.get("summary_vn", "") or "")
        summary["telegram_messages"] = list(result.get("telegram_messages", []) or [])
        summary["run_report_path"] = result.get("run_report_path", "")
        with APP_STATE["lock"]:
            APP_STATE["last_result"] = summary
            APP_STATE["last_error"] = ""
            APP_STATE["runtime_config"] = dict(runtime_config)
            APP_STATE["workspace_articles"] = _build_workspace_articles(result)
            if mode == "preview":
                APP_STATE["preview_state"] = result
    except Exception as exc:
        with APP_STATE["lock"]:
            APP_STATE["last_error"] = str(exc)
    finally:
        with APP_STATE["lock"]:
            APP_STATE["running"] = False


def _approve_preview_in_background() -> None:
    try:
        with APP_STATE["lock"]:
            preview_state = APP_STATE.get("preview_state")
        if not preview_state:
            raise RuntimeError("Chưa có preview nào để approve.")

        result, summary = publish_from_preview_state(preview_state)
        summary["summary_text"] = str(result.get("summary_vn", "") or "")
        summary["telegram_messages"] = list(result.get("telegram_messages", []) or [])
        summary["run_report_path"] = result.get("run_report_path", "")
        with APP_STATE["lock"]:
            APP_STATE["last_result"] = summary
            APP_STATE["last_error"] = ""
            APP_STATE["workspace_articles"] = _build_workspace_articles(result)
            APP_STATE["preview_state"] = None
    except Exception as exc:
        with APP_STATE["lock"]:
            APP_STATE["last_error"] = str(exc)
    finally:
        with APP_STATE["lock"]:
            APP_STATE["running"] = False


def _publish_notion_only_in_background() -> None:
    try:
        with APP_STATE["lock"]:
            preview_state = APP_STATE.get("preview_state")
        if not preview_state:
            raise RuntimeError("Chưa có preview nào để publish Notion.")

        result, summary = publish_notion_only_from_preview_state(preview_state)
        summary["summary_text"] = str(result.get("summary_vn", "") or "")
        summary["telegram_messages"] = list(result.get("telegram_messages", []) or [])
        summary["run_report_path"] = result.get("run_report_path", "")
        with APP_STATE["lock"]:
            APP_STATE["last_result"] = summary
            APP_STATE["last_error"] = ""
            APP_STATE["workspace_articles"] = _build_workspace_articles(result)
            APP_STATE["preview_state"] = result
    except Exception as exc:
        with APP_STATE["lock"]:
            APP_STATE["last_error"] = str(exc)
    finally:
        with APP_STATE["lock"]:
            APP_STATE["running"] = False


def start_job(mode: str, runtime_config: dict[str, Any] | None = None, run_profile: str = "") -> tuple[bool, str]:
    normalized_mode = str(mode or "").strip().lower()
    if normalized_mode not in {"preview", "publish"}:
        return False, "Mode không hợp lệ."

    normalized_profile = str(run_profile or normalized_mode).strip().lower()
    config = _normalize_runtime_config(runtime_config)
    config = apply_runtime_preset(normalized_profile, config)

    with APP_STATE["lock"]:
        if APP_STATE.get("running"):
            return False, "Đang có một run khác chưa xong."

        APP_STATE["running"] = True
        APP_STATE["job_id"] = int(APP_STATE.get("job_id", 0) or 0) + 1
        APP_STATE["started_at"] = time.time()
        APP_STATE["last_mode"] = normalized_mode
        APP_STATE["last_profile"] = normalized_profile
        APP_STATE["last_error"] = ""
        APP_STATE["logs"] = []
        APP_STATE["last_result"] = {}

    thread = threading.Thread(
        target=_run_in_background,
        args=(normalized_mode, config, normalized_profile),
        daemon=True,
    )
    thread.start()
    return True, "started"


def approve_preview_job() -> tuple[bool, str]:
    with APP_STATE["lock"]:
        if APP_STATE.get("running"):
            return False, "Đang có một run khác chưa xong."
        if APP_STATE.get("preview_state") is None:
            return False, "Chưa có preview nào để approve."

        APP_STATE["running"] = True
        APP_STATE["job_id"] = int(APP_STATE.get("job_id", 0) or 0) + 1
        APP_STATE["started_at"] = time.time()
        APP_STATE["last_mode"] = "publish"
        APP_STATE["last_profile"] = "approved_preview"
        APP_STATE["last_error"] = ""
        APP_STATE["logs"] = []
        APP_STATE["last_result"] = {}

    thread = threading.Thread(target=_approve_preview_in_background, daemon=True)
    thread.start()
    return True, "started"


def publish_notion_only_job() -> tuple[bool, str]:
    with APP_STATE["lock"]:
        if APP_STATE.get("running"):
            return False, "Đang có một run khác chưa xong."
        preview_state = APP_STATE.get("preview_state")
        if preview_state is None:
            return False, "Chưa có preview nào để publish Notion."
        if str((preview_state or {}).get("preview_publish_state", "") or "") == "notion_only_published":
            return False, "Batch preview này đã được đẩy lên Notion rồi."

        APP_STATE["running"] = True
        APP_STATE["job_id"] = int(APP_STATE.get("job_id", 0) or 0) + 1
        APP_STATE["started_at"] = time.time()
        APP_STATE["last_mode"] = "publish"
        APP_STATE["last_profile"] = "preview_notion_only"
        APP_STATE["last_error"] = ""
        APP_STATE["logs"] = []
        APP_STATE["last_result"] = {}

    thread = threading.Thread(target=_publish_notion_only_in_background, daemon=True)
    thread.start()
    return True, "started"


HTML_PAGE = """<!doctype html>
<html lang="vi">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Daily Digest Control Panel</title>
  <style>
    :root {
      --bg: #0f1723;
      --bg-soft: #141f2f;
      --bg-elev: #1b2737;
      --panel: rgba(244, 241, 234, 0.96);
      --panel-soft: rgba(252, 250, 246, 0.92);
      --ink: #20242b;
      --ink-soft: #5e6773;
      --line: rgba(28, 40, 56, 0.12);
      --line-strong: rgba(255, 255, 255, 0.08);
      --telegram: #162433;
      --telegram-elev: #1b2d40;
      --telegram-bubble: #23384d;
      --telegram-bubble-2: #203247;
      --telegram-text: #f4f7fb;
      --accent: #2b98f0;
      --accent-strong: #2381d2;
      --success: #2c8f62;
      --warning: #c78932;
      --danger: #b75c48;
      --radius-xl: 28px;
      --radius-lg: 22px;
      --radius-md: 16px;
      --shadow-dark: 0 30px 80px rgba(4, 12, 22, 0.38);
      --shadow-light: 0 18px 40px rgba(34, 40, 51, 0.1);
    }
    * { box-sizing: border-box; }
    html, body { min-height: 100%; }
    body {
      margin: 0;
      font-family: "Avenir Next", "SF Pro Display", "Helvetica Neue", sans-serif;
      color: var(--telegram-text);
      background:
        radial-gradient(circle at top left, rgba(43, 152, 240, 0.16), transparent 22%),
        radial-gradient(circle at top right, rgba(35, 129, 210, 0.12), transparent 18%),
        linear-gradient(180deg, #0f1723 0%, #111c2a 42%, #0c141f 100%);
    }
    button, input, select, textarea { font: inherit; }
    a { color: inherit; }
    .app-shell {
      display: grid;
      grid-template-columns: 92px minmax(0, 1fr);
      min-height: 100vh;
    }
    .rail {
      padding: 18px 12px;
      border-right: 1px solid var(--line-strong);
      background: linear-gradient(180deg, rgba(10, 17, 26, 0.92), rgba(13, 22, 34, 0.96));
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 16px;
    }
    .rail-badge,
    .rail-dot,
    .rail-avatar {
      width: 48px;
      height: 48px;
      border-radius: 18px;
      display: grid;
      place-items: center;
      font-weight: 800;
      letter-spacing: 0.04em;
    }
    .rail-badge {
      background: linear-gradient(180deg, #2d9cf4, #1e79c6);
      box-shadow: 0 12px 24px rgba(43, 152, 240, 0.28);
    }
    .rail-dot {
      background: rgba(255, 255, 255, 0.06);
      color: rgba(255, 255, 255, 0.86);
      border: 1px solid var(--line-strong);
      font-size: 12px;
    }
    .rail-avatar {
      margin-top: auto;
      background: linear-gradient(180deg, #fe8a63, #f46745);
    }
    .main-stage {
      padding: 24px;
      display: grid;
      gap: 18px;
    }
    .surface {
      border-radius: var(--radius-xl);
      overflow: hidden;
      box-shadow: var(--shadow-dark);
      border: 1px solid var(--line-strong);
      background: linear-gradient(180deg, rgba(20, 31, 47, 0.98), rgba(18, 27, 40, 0.98));
    }
    .hero {
      display: grid;
      grid-template-columns: minmax(0, 1.2fr) minmax(320px, 0.8fr);
    }
    .hero-main {
      padding: 24px 24px 22px;
      border-right: 1px solid var(--line-strong);
      background:
        radial-gradient(circle at top left, rgba(43, 152, 240, 0.14), transparent 30%),
        linear-gradient(180deg, rgba(20, 33, 49, 0.96), rgba(16, 26, 38, 0.96));
    }
    .hero-side {
      padding: 24px;
      background: linear-gradient(180deg, rgba(16, 27, 39, 0.98), rgba(15, 23, 35, 0.98));
    }
    .status-pill,
    .meta-chip,
    .workspace-chip,
    .tiny-chip {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 8px 12px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0.01em;
    }
    .status-pill {
      background: rgba(255, 255, 255, 0.08);
      color: rgba(255, 255, 255, 0.92);
    }
    .meta-chip {
      background: rgba(43, 152, 240, 0.13);
      color: #cce7ff;
    }
    .workspace-chip {
      background: rgba(33, 39, 46, 0.08);
      color: #526070;
    }
    .tiny-chip {
      background: rgba(255, 255, 255, 0.06);
      color: rgba(255, 255, 255, 0.76);
      padding: 6px 10px;
    }
    .dot {
      width: 10px;
      height: 10px;
      border-radius: 999px;
      background: rgba(255, 255, 255, 0.45);
    }
    .dot.running {
      background: #44d18d;
      box-shadow: 0 0 0 0 rgba(68, 209, 141, 0.48);
      animation: pulse 1.4s infinite;
    }
    @keyframes pulse {
      0% { box-shadow: 0 0 0 0 rgba(68, 209, 141, 0.45); }
      70% { box-shadow: 0 0 0 15px rgba(68, 209, 141, 0); }
      100% { box-shadow: 0 0 0 0 rgba(68, 209, 141, 0); }
    }
    h1, h2, h3, p { margin: 0; }
    h1 {
      margin-top: 16px;
      font-family: "Iowan Old Style", "Palatino Linotype", "Book Antiqua", serif;
      font-size: 42px;
      line-height: 1.04;
      max-width: 12ch;
    }
    .hero-copy {
      margin-top: 12px;
      max-width: 62ch;
      color: rgba(231, 238, 247, 0.78);
      line-height: 1.65;
      font-size: 14px;
    }
    .toolbar,
    .toggle-grid,
    .meta-row,
    .hero-metrics,
    .control-grid,
    .workspace-summary {
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
    }
    .hero-metrics,
    .control-grid,
    .toggle-grid,
    .toolbar {
      margin-top: 18px;
    }
    .control-box {
      min-width: 170px;
      flex: 1 1 170px;
    }
    .label {
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.12em;
      color: rgba(235, 241, 248, 0.5);
      margin-bottom: 8px;
    }
    .label.light { color: #8b96a5; }
    .metric-card {
      min-width: 120px;
      padding: 12px 14px;
      border-radius: 18px;
      background: rgba(255, 255, 255, 0.06);
      border: 1px solid var(--line-strong);
    }
    .metric-value {
      font-size: 28px;
      font-weight: 800;
      line-height: 1;
      margin-bottom: 6px;
    }
    .metric-copy {
      font-size: 12px;
      line-height: 1.5;
      color: rgba(231, 238, 247, 0.66);
    }
    .side-title {
      font-size: 13px;
      color: rgba(231, 238, 247, 0.62);
      margin-bottom: 12px;
      text-transform: uppercase;
      letter-spacing: 0.12em;
    }
    .side-number {
      font-size: 44px;
      font-weight: 800;
      line-height: 1;
      margin-bottom: 10px;
    }
    .side-copy,
    .hero-note {
      font-size: 13px;
      line-height: 1.65;
      color: rgba(231, 238, 247, 0.7);
    }
    select,
    input[type="number"] {
      width: 100%;
      padding: 12px 14px;
      border-radius: 16px;
      border: 1px solid var(--line-strong);
      background: rgba(255, 255, 255, 0.08);
      color: var(--telegram-text);
      outline: none;
    }
    input[type="checkbox"] { accent-color: var(--accent); }
    .toggle {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 10px 12px;
      border-radius: 16px;
      background: rgba(255, 255, 255, 0.06);
      border: 1px solid var(--line-strong);
      color: rgba(245, 248, 251, 0.88);
      font-size: 13px;
    }
    button {
      border: 0;
      border-radius: 16px;
      padding: 12px 16px;
      font-weight: 800;
      letter-spacing: 0.01em;
      cursor: pointer;
      transition: transform 0.14s ease, opacity 0.14s ease, filter 0.14s ease;
    }
    button:hover { transform: translateY(-1px); filter: brightness(1.04); }
    button:disabled { opacity: 0.45; cursor: not-allowed; transform: none; filter: none; }
    .btn-preview { background: linear-gradient(180deg, #2b98f0, #2381d2); color: white; }
    .btn-smart { background: linear-gradient(180deg, #7a66f2, #5e4fe0); color: white; }
    .btn-approve { background: linear-gradient(180deg, #39a872, #2c8f62); color: white; }
    .btn-publish { background: linear-gradient(180deg, #db7a5d, #c76549); color: white; }
    .btn-notion { background: linear-gradient(180deg, #86715e, #6f5d4e); color: white; }
    .btn-refresh, .btn-open { background: rgba(255, 255, 255, 0.1); color: #eef4fb; }
    .error {
      min-height: 20px;
      margin-top: 12px;
      color: #ffb6a2;
      font-weight: 700;
      font-size: 13px;
    }
    .stats-grid,
    .bottom-grid {
      display: grid;
      gap: 18px;
    }
    .stats-grid { grid-template-columns: repeat(4, minmax(0, 1fr)); }
    .stats-card {
      padding: 18px 20px;
      border-radius: var(--radius-lg);
      background: rgba(255, 255, 255, 0.04);
      border: 1px solid var(--line-strong);
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.04);
    }
    .stats-card .metric-value { font-size: 34px; }
    .preview-grid {
      display: grid;
      grid-template-columns: minmax(0, 0.95fr) minmax(0, 1.05fr);
      gap: 18px;
      align-items: start;
    }
    .telegram-window {
      min-height: 760px;
      border-radius: var(--radius-xl);
      overflow: hidden;
      background:
        linear-gradient(180deg, rgba(18, 30, 43, 0.98), rgba(15, 24, 36, 0.98)),
        linear-gradient(135deg, rgba(34, 57, 80, 0.55), transparent 55%);
      border: 1px solid var(--line-strong);
      box-shadow: var(--shadow-dark);
    }
    .telegram-header {
      padding: 16px 18px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      border-bottom: 1px solid var(--line-strong);
      background: rgba(21, 34, 49, 0.92);
    }
    .telegram-title {
      display: flex;
      align-items: center;
      gap: 12px;
    }
    .telegram-avatar {
      width: 42px;
      height: 42px;
      border-radius: 14px;
      display: grid;
      place-items: center;
      font-weight: 800;
      background: linear-gradient(180deg, #2b98f0, #1d6eb3);
      box-shadow: 0 10px 22px rgba(43, 152, 240, 0.28);
    }
    .telegram-title h2 {
      font-size: 20px;
      line-height: 1.1;
    }
    .telegram-subtitle {
      margin-top: 4px;
      color: rgba(227, 236, 246, 0.62);
      font-size: 13px;
    }
    .telegram-actions {
      display: flex;
      gap: 10px;
      color: rgba(227, 236, 246, 0.72);
      font-size: 13px;
    }
    .telegram-thread {
      padding: 18px 16px 22px;
      min-height: 620px;
      display: grid;
      align-content: start;
      gap: 14px;
      background-image:
        radial-gradient(circle at 12px 12px, rgba(255, 255, 255, 0.03) 2px, transparent 0),
        linear-gradient(180deg, rgba(255, 255, 255, 0.01), rgba(255, 255, 255, 0));
      background-size: 26px 26px, 100% 100%;
    }
    .lane-stack {
      padding: 16px;
      display: grid;
      gap: 14px;
      background-image:
        radial-gradient(circle at 12px 12px, rgba(255, 255, 255, 0.03) 2px, transparent 0),
        linear-gradient(180deg, rgba(255, 255, 255, 0.01), rgba(255, 255, 255, 0));
      background-size: 26px 26px, 100% 100%;
    }
    .topic-card {
      border-radius: 20px;
      overflow: hidden;
      border: 1px solid rgba(255, 255, 255, 0.06);
      background: linear-gradient(180deg, rgba(24, 38, 54, 0.98), rgba(19, 31, 46, 0.98));
      box-shadow: 0 14px 30px rgba(4, 12, 22, 0.18);
    }
    .topic-head {
      padding: 12px 14px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      border-bottom: 1px solid rgba(255, 255, 255, 0.06);
      background: rgba(255, 255, 255, 0.03);
    }
    .topic-title {
      font-size: 14px;
      font-weight: 800;
      color: #f1f6fc;
    }
    .topic-copy {
      margin-top: 3px;
      font-size: 12px;
      color: rgba(227, 236, 246, 0.6);
    }
    .topic-meta {
      padding: 6px 10px;
      border-radius: 999px;
      background: rgba(43, 152, 240, 0.13);
      color: #cce7ff;
      font-size: 11px;
      font-weight: 700;
      white-space: nowrap;
    }
    .topic-thread {
      padding: 12px 12px 14px;
      display: grid;
      gap: 10px;
      min-height: 170px;
    }
    .tg-bubble,
    .tg-empty {
      max-width: 92%;
      border-radius: 20px 20px 20px 8px;
      background: linear-gradient(180deg, rgba(35, 56, 77, 0.98), rgba(31, 50, 71, 0.98));
      border: 1px solid rgba(255, 255, 255, 0.06);
      padding: 14px 16px 12px;
      box-shadow: 0 14px 30px rgba(4, 12, 22, 0.2);
      white-space: pre-wrap;
      line-height: 1.62;
      font-size: 14px;
      color: #eef4fb;
    }
    .tg-empty { color: rgba(237, 244, 252, 0.72); }
    .tg-bubble-head {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 16px;
      margin-bottom: 10px;
      font-size: 12px;
      font-weight: 700;
      color: #8ec7ff;
    }
    .tg-bubble-meta {
      color: rgba(227, 236, 246, 0.52);
      font-weight: 600;
    }
    .notion-window {
      min-height: 760px;
      border-radius: var(--radius-xl);
      overflow: hidden;
      background: linear-gradient(180deg, rgba(251, 248, 243, 0.98), rgba(244, 241, 234, 0.98));
      color: var(--ink);
      border: 1px solid rgba(201, 191, 177, 0.46);
      box-shadow: var(--shadow-light);
    }
    .notion-topbar {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 14px 18px;
      border-bottom: 1px solid var(--line);
      background: rgba(252, 250, 247, 0.92);
    }
    .breadcrumbs {
      display: flex;
      gap: 8px;
      align-items: center;
      color: #7c8571;
      font-size: 13px;
    }
    .breadcrumbs strong { color: #40464d; }
    .notion-content { padding: 22px 22px 18px; }
    .notion-header {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 18px;
      margin-bottom: 16px;
    }
    .notion-title {
      font-family: "Iowan Old Style", "Palatino Linotype", "Book Antiqua", serif;
      font-size: 34px;
      line-height: 1.08;
      margin-bottom: 10px;
      color: #252a31;
    }
    .notion-copy {
      max-width: 56ch;
      line-height: 1.65;
      color: var(--ink-soft);
      font-size: 14px;
    }
    .notion-actions {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }
    .notion-btn {
      padding: 9px 12px;
      border-radius: 12px;
      border: 1px solid rgba(72, 86, 101, 0.12);
      background: white;
      font-size: 13px;
      font-weight: 700;
      color: #414a55;
    }
    .workspace-summary { margin-bottom: 14px; }
    .workspace-stat {
      min-width: 124px;
      padding: 12px 14px;
      border-radius: 16px;
      border: 1px solid rgba(72, 86, 101, 0.1);
      background: rgba(255, 255, 255, 0.75);
    }
    .workspace-stat .label { color: #8b96a5; margin-bottom: 6px; }
    .workspace-stat strong {
      display: block;
      font-size: 24px;
      color: #2b3037;
      line-height: 1;
      margin-bottom: 4px;
    }
    .workspace-stat span {
      font-size: 12px;
      color: var(--ink-soft);
      line-height: 1.45;
    }
    .database-shell {
      border: 1px solid rgba(72, 86, 101, 0.12);
      border-radius: 18px;
      overflow: hidden;
      background: rgba(255, 255, 255, 0.72);
    }
    .database-head {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      padding: 12px 14px;
      border-bottom: 1px solid rgba(72, 86, 101, 0.08);
      background: rgba(248, 245, 240, 0.88);
    }
    .database-title {
      display: flex;
      align-items: center;
      gap: 10px;
      font-weight: 700;
      color: #3d444c;
    }
    .database-table-wrap {
      overflow: auto;
      max-height: 420px;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }
    thead th {
      position: sticky;
      top: 0;
      z-index: 1;
      text-align: left;
      padding: 12px 10px;
      background: rgba(250, 248, 244, 0.98);
      color: #7a8290;
      border-bottom: 1px solid rgba(72, 86, 101, 0.08);
      font-weight: 700;
    }
    tbody td {
      padding: 12px 10px;
      border-bottom: 1px solid rgba(72, 86, 101, 0.08);
      vertical-align: top;
      color: #353c43;
    }
    tbody tr {
      cursor: pointer;
      transition: background 0.14s ease;
    }
    tbody tr:hover { background: rgba(240, 236, 230, 0.62); }
    tbody tr.is-active { background: rgba(224, 237, 252, 0.68); }
    .name-cell {
      min-width: 250px;
    }
    .name-title {
      font-weight: 700;
      color: #2f363d;
      line-height: 1.45;
      margin-bottom: 6px;
    }
    .name-meta {
      font-size: 12px;
      color: #7a8290;
      line-height: 1.5;
    }
    .summary-cell {
      min-width: 320px;
      line-height: 1.55;
      color: #505864;
    }
    .empty-workspace {
      padding: 26px 18px;
      color: #7b8490;
      line-height: 1.65;
      text-align: center;
    }
    .badge-row {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
    }
    .badge {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 4px 8px;
      border-radius: 8px;
      font-size: 11px;
      font-weight: 700;
    }
    .badge.type { background: #edf3ff; color: #406a9c; }
    .badge.fit-high, .badge.rel-high { background: #e8f5ec; color: #2b7a52; }
    .badge.fit-medium, .badge.rel-medium { background: #f8efd9; color: #93672a; }
    .badge.fit-low, .badge.rel-low { background: #f6e4e1; color: #98554a; }
    .badge.tag { background: #f2ecff; color: #7e61a8; }
    .badge.delivery { background: #eceff3; color: #5c6774; }
    .workspace-detail {
      margin-top: 14px;
      display: grid;
      gap: 12px;
      padding: 16px;
      border-top: 1px solid rgba(72, 86, 101, 0.08);
      background: rgba(248, 246, 241, 0.86);
    }
    .workspace-detail h3 {
      font-size: 22px;
      line-height: 1.3;
      color: #252a31;
    }
    .workspace-detail p,
    .workspace-detail a {
      line-height: 1.65;
      color: #525b66;
      font-size: 14px;
    }
    .detail-links {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
    }
    .detail-link {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 10px 12px;
      border-radius: 12px;
      background: white;
      border: 1px solid rgba(72, 86, 101, 0.12);
      text-decoration: none;
      font-weight: 700;
    }
    .bottom-grid { grid-template-columns: minmax(0, 0.9fr) minmax(0, 1.1fr); }
    .light-panel {
      border-radius: var(--radius-xl);
      background: linear-gradient(180deg, rgba(251, 248, 243, 0.96), rgba(244, 241, 234, 0.96));
      color: var(--ink);
      border: 1px solid rgba(201, 191, 177, 0.46);
      box-shadow: var(--shadow-light);
      padding: 22px;
    }
    .light-panel h2 {
      font-family: "Iowan Old Style", "Palatino Linotype", "Book Antiqua", serif;
      font-size: 28px;
      line-height: 1.12;
      margin-bottom: 14px;
      color: #242a31;
    }
    .guide-list {
      display: grid;
      gap: 12px;
    }
    .guide-item {
      padding: 14px 16px;
      border-radius: 16px;
      border: 1px solid rgba(72, 86, 101, 0.1);
      background: rgba(255, 255, 255, 0.76);
      line-height: 1.65;
      color: #4f5864;
      font-size: 14px;
    }
    .guide-item strong {
      display: block;
      margin-bottom: 6px;
      color: #30363d;
    }
    .pre {
      border-radius: 18px;
      border: 1px solid rgba(72, 86, 101, 0.12);
      background: rgba(255, 255, 255, 0.75);
      padding: 16px;
      font-family: "SF Mono", "Menlo", monospace;
      font-size: 12px;
      line-height: 1.62;
      white-space: pre-wrap;
      color: #45505d;
      max-height: 280px;
      overflow: auto;
    }
    .hint-light {
      color: #6d7580;
      line-height: 1.65;
      font-size: 14px;
      margin-bottom: 12px;
    }
    @media (max-width: 1220px) {
      .hero,
      .preview-grid,
      .bottom-grid,
      .stats-grid {
        grid-template-columns: 1fr;
      }
      h1 { max-width: none; }
    }
    @media (max-width: 900px) {
      .app-shell { grid-template-columns: 1fr; }
      .rail {
        flex-direction: row;
        justify-content: center;
        border-right: 0;
        border-bottom: 1px solid var(--line-strong);
      }
      .rail-avatar { margin-top: 0; }
      .main-stage { padding: 16px; }
      .hero-main, .hero-side, .notion-content, .light-panel { padding: 18px; }
      .telegram-thread { min-height: 360px; }
      .database-table-wrap { max-height: none; }
    }
  </style>
</head>
<body>
  <div class="app-shell">
    <aside class="rail">
      <div class="rail-badge">DD</div>
      <div class="rail-dot">AI</div>
      <div class="rail-dot">X</div>
      <div class="rail-dot">FB</div>
      <div class="rail-dot">DB</div>
      <div class="rail-avatar">AB</div>
    </aside>

    <main class="main-stage">
      <section class="surface hero">
        <div class="hero-main">
          <div class="status-pill"><span id="status-dot" class="dot"></span><span id="status-text">Idle</span></div>
          <h1>Founder-grade digest preview workspace</h1>
          <p class="hero-copy">
            Màn này chỉ làm một việc: cho bạn xem trước đầu ra sẽ lên Telegram ở các topic khác nhau.
            `Run Preview` là baseline bám production; `Grok Smart` là nhánh thử nghiệm mở rộng Grok để so chất lượng.
            UI không còn là nơi chỉnh tham số thủ công.
          </p>

          <div class="hero-metrics">
            <span class="meta-chip">Mode <span id="run-mode">-</span></span>
            <span class="meta-chip">Profile <span id="run-profile">-</span></span>
            <span class="meta-chip">Summary <span id="summary-mode">-</span></span>
            <span class="meta-chip">Health <span id="health-status">-</span></span>
            <span class="meta-chip">Publish ready <span id="publish-ready">-</span></span>
            <span class="meta-chip">Main ready <span id="main-candidate-chip">0</span></span>
            <span class="meta-chip">GitHub ready <span id="github-candidate-chip">0</span></span>
            <span class="meta-chip">Facebook ready <span id="facebook-candidate-chip">0</span></span>
            <span class="meta-chip">Notion pages <span id="notion-count">0</span></span>
            <span class="meta-chip">Telegram sent <span id="telegram-sent">no</span></span>
            <span id="preview-state-pill" class="tiny-chip">preview state: idle</span>
          </div>

          <div class="toolbar">
            <button id="btn-preview" class="btn-preview">Run Preview (Production)</button>
            <button id="btn-smart-preview" class="btn-smart">Run Preview (Grok Smart)</button>
            <button id="btn-notion-only" class="btn-notion">Publish Notion only</button>
            <button id="btn-approve" class="btn-approve">Approve Preview</button>
            <button id="btn-publish" class="btn-publish">Publish thật</button>
            <button id="btn-open-report" class="btn-open">Open Report</button>
            <button id="btn-refresh" class="btn-refresh">Refresh</button>
          </div>
          <div id="last-error" class="error"></div>
        </div>

        <div class="hero-side">
          <div class="side-title">Run timer</div>
          <div id="elapsed" class="side-number">0.0s</div>
          <p class="side-copy">
            Baseline preview dùng cùng backbone production và chưa tạo side effect ra ngoài.
            Nếu muốn thử batch “gắt” hơn, dùng Grok Smart rồi so trực tiếp 3 lane Telegram ở bên dưới.
          </p>
          <div class="guide-list" style="margin-top: 18px;">
            <div class="guide-item" style="background: rgba(255,255,255,0.06); border-color: rgba(255,255,255,0.08); color: rgba(231, 238, 247, 0.74);">
              <strong style="color: white;">Production-aligned preview</strong>
              UI không còn là nơi vặn threshold hay source toggle. Nó chỉ phản chiếu batch thật theo đúng logic pipeline hiện tại.
            </div>
            <div class="guide-item" style="background: rgba(255,255,255,0.06); border-color: rgba(255,255,255,0.08); color: rgba(231, 238, 247, 0.74);">
              <strong style="color: white;">Realtime ops vẫn giữ nguyên</strong>
              Chạy publish bằng terminal vẫn có log realtime như cũ. UI này chỉ phục vụ review output trước khi bạn bắn ra Telegram.
            </div>
          </div>
        </div>
      </section>

      <section class="stats-grid">
        <div class="stats-card"><div class="label">Raw gathered</div><div class="metric-value" id="raw-count">0</div><div class="metric-copy">Tổng số tín hiệu kéo về từ mọi nguồn.</div></div>
        <div class="stats-card"><div class="label">Scored</div><div class="metric-value" id="scored-count">0</div><div class="metric-copy">Số bài đã được classify và chấm điểm.</div></div>
        <div class="stats-card"><div class="label">Main topic</div><div class="metric-value" id="tg-candidate-count">0</div><div class="metric-copy">Số bài đủ chuẩn cho bản tin chính.</div></div>
        <div class="stats-card"><div class="label">GitHub topic</div><div class="metric-value" id="github-candidate-count">0</div><div class="metric-copy">Repo/release đủ chuẩn cho topic GitHub.</div></div>
        <div class="stats-card"><div class="label">Facebook topic</div><div class="metric-value" id="facebook-candidate-count">0</div><div class="metric-copy">Post community đủ chuẩn cho Facebook News.</div></div>
        <div class="stats-card"><div class="label">Notion pages</div><div class="metric-value" id="notion-count-card">0</div><div class="metric-copy">Số page được tạo hoặc reuse trong batch này.</div></div>
      </section>

      <section class="preview-grid">
        <section class="telegram-window">
          <div class="telegram-header">
            <div class="telegram-title">
              <div class="telegram-avatar">DD</div>
              <div>
                <h2>Telegram Output Preview</h2>
                <div class="telegram-subtitle">Xem trước đúng 3 lane: main brief, GitHub repo, Facebook News</div>
              </div>
            </div>
            <div class="telegram-actions">
              <span>Main</span>
              <span>GitHub</span>
              <span>Facebook</span>
            </div>
          </div>
          <div class="lane-stack">
            <section class="topic-card">
              <div class="topic-head">
                <div>
                  <div class="topic-title">Main Brief</div>
                  <div class="topic-copy">Topic bản tin chính như trên Telegram thread hiện tại</div>
                </div>
                <span id="main-topic-meta" class="topic-meta">0 messages</span>
              </div>
              <div id="messages-main" class="topic-thread"></div>
            </section>

            <section class="topic-card">
              <div class="topic-head">
                <div>
                  <div class="topic-title">GitHub Repo Digest</div>
                  <div class="topic-copy">Lane repo/release, không chen vào bản tin chính</div>
                </div>
                <span id="github-topic-meta" class="topic-meta">0 messages</span>
              </div>
              <div id="messages-github" class="topic-thread"></div>
            </section>

            <section class="topic-card">
              <div class="topic-head">
                <div>
                  <div class="topic-title">Facebook News</div>
                  <div class="topic-copy">Lane community riêng cho group/page/profile Facebook</div>
                </div>
                <span id="facebook-topic-meta" class="topic-meta">0 messages</span>
              </div>
              <div id="messages-facebook" class="topic-thread"></div>
            </section>
          </div>
        </section>

        <section class="notion-window">
          <div class="notion-topbar">
            <div class="breadcrumbs">
              <span>Avalook</span>
              <span>/</span>
              <span>Database</span>
              <span>/</span>
              <strong>News Database</strong>
            </div>
            <div class="tiny-chip" style="background: rgba(72, 86, 101, 0.08); color: #596675;">workspace mirror</div>
          </div>

          <div class="notion-content">
            <div class="notion-header">
              <div>
                <div class="notion-title">News Database</div>
                <p class="notion-copy">
                  Bảng này giả lập cách bạn sẽ review bài trong Notion: nhìn score, type, fit với dự án, tag và summary ngắn.
                  Hãy click từng dòng để xem note chi tiết và link nguồn.
                </p>
              </div>
              <div class="notion-actions">
                <button class="notion-btn" type="button">Filter</button>
                <button class="notion-btn" type="button">Sort</button>
                <button class="notion-btn" type="button">Search</button>
                <button class="notion-btn" type="button">New</button>
              </div>
            </div>

            <div class="workspace-summary">
              <div class="workspace-stat">
                <div class="label light">Rows</div>
                <strong id="workspace-count">0</strong>
                <span>Articles đang có trong batch preview.</span>
              </div>
              <div class="workspace-stat">
                <div class="label light">High fit</div>
                <strong id="workspace-high-fit">0</strong>
                <span>Bài có founder/project fit cao.</span>
              </div>
              <div class="workspace-stat">
                <div class="label light">Brief ready</div>
                <strong id="workspace-brief-ready">0</strong>
                <span>Bài đang đủ sức để cân nhắc lên Telegram.</span>
              </div>
            </div>

            <div class="database-shell">
              <div class="database-head">
                <div class="database-title">
                  <span>Bảng dữ liệu</span>
                  <span id="workspace-state" class="workspace-chip">idle</span>
                </div>
                <div class="workspace-chip">Click row để xem detail</div>
              </div>
              <div class="database-table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Name</th>
                      <th>Created time</th>
                      <th>Source</th>
                      <th>Score</th>
                      <th>Type</th>
                      <th>Project fit</th>
                      <th>Summary</th>
                    </tr>
                  </thead>
                  <tbody id="workspace-table-body"></tbody>
                </table>
                <div id="workspace-empty" class="empty-workspace">Chưa có article nào trong workspace preview.</div>
              </div>
              <div class="workspace-detail">
                <div class="badge-row" id="workspace-detail-badges"></div>
                <h3 id="workspace-detail-title">Chọn một article để xem detail</h3>
                <p id="workspace-detail-summary">Khi có preview, phần này sẽ hiện note ngắn, editorial angle, grounding và link nguồn tương ứng.</p>
                <p id="workspace-detail-grounding"></p>
                <div class="detail-links">
                  <a id="workspace-detail-source" class="detail-link" href="#" target="_blank" rel="noreferrer" style="display:none;">Open source</a>
                  <a id="workspace-detail-notion" class="detail-link" href="#" target="_blank" rel="noreferrer" style="display:none;">Open Notion page</a>
                </div>
              </div>
            </div>
          </div>
        </section>
      </section>

      <section class="bottom-grid">
        <section class="light-panel">
          <h2>Preview Principles</h2>
          <p class="hint-light">
            Màn preview bây giờ không còn là nơi “vọc thông số”. Nó chỉ giúp bạn nhìn đúng đầu ra của pipeline hiện tại trước khi publish.
          </p>
          <div class="guide-list">
            <div class="guide-item">
              <strong>Preview = production without sending</strong>
              Run Preview dùng cùng source mix, scoring, routing và editorial logic như publish thật; khác biệt chính là chưa tạo side effect ra ngoài.
            </div>
            <div class="guide-item">
              <strong>3 lane tách biệt</strong>
              Main brief, GitHub Repo Digest và Facebook News được xem riêng, để bạn nhìn đúng việc bài nào đang rơi vào lane nào thay vì bị trộn chung.
            </div>
            <div class="guide-item">
              <strong>Terminal realtime vẫn là nơi chạy ops</strong>
              Khi bạn muốn xem log runtime từng bước hoặc chạy publish theo thói quen cũ, terminal vẫn là kênh realtime quan trọng nhất.
            </div>
            <div class="guide-item">
              <strong>Database mirror vẫn giữ</strong>
              Bảng bên phải vẫn hữu ích để soi score, fit và source của từng bài, nhưng quyết định cuối nên dựa vào output Telegram preview ở bên trái.
            </div>
          </div>
        </section>

        <section class="light-panel">
          <h2>Run Report & Logs</h2>
          <div class="label light">Run health</div>
          <div id="run-health" class="hint-light">-</div>
          <div class="label light" style="margin-top:10px;">Health issues</div>
          <div id="health-issues" class="pre">(chưa có health issues)</div>
          <div class="label light">Report path</div>
          <div id="report-path" class="hint-light">-</div>
          <div id="report-content" class="pre">(chưa có report)</div>
          <div class="label light" style="margin-top:16px;">Feedback summary</div>
          <div id="feedback-summary" class="pre">(chưa có feedback)</div>
          <div class="label light" style="margin-top:16px;">Recent logs</div>
          <div id="logs" class="pre">(chưa có log)</div>
        </section>
      </section>
    </main>
  </div>

  <script>
    const workspaceState = {
      articles: [],
      selectedIndex: 0,
    };

    function escapeHtml(value) {
      return String(value || '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#39;');
    }

    function badgeClass(prefix, value) {
      const normalized = String(value || '').trim().toLowerCase();
      return `${prefix}-${normalized || 'low'}`;
    }

    async function callApi(path, payload) {
      const res = await fetch(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload || {})
      });
      const data = await res.json();
      if (!res.ok) {
        alert(data.error || 'Không thể thực hiện thao tác');
      }
      return data;
    }

    async function runPreview() {
      await callApi('/api/run', { mode: 'preview', run_profile: 'preview' });
      await refreshStatus();
    }

    async function runSmartPreview() {
      await callApi('/api/run', { mode: 'preview', run_profile: 'grok_smart' });
      await refreshStatus();
    }

    async function runPublish() {
      const ok = confirm('Chạy publish thật? Việc này có thể gửi Telegram và tạo Notion page.');
      if (!ok) return;
      await callApi('/api/run', { mode: 'publish', run_profile: 'publish' });
      await refreshStatus();
    }

    async function approvePreview() {
      const notionAlreadyPublished = document.getElementById('run-profile').textContent === 'preview_notion_only';
      const ok = confirm(
        notionAlreadyPublished
          ? 'Batch này đã lên Notion rồi. Tiếp tục gửi Telegram từ đúng preview này?'
          : 'Approve preview này và publish đúng batch vừa duyệt?'
      );
      if (!ok) return;
      await callApi('/api/approve', {});
      await refreshStatus();
    }

    async function publishNotionOnly() {
      const ok = confirm('Đẩy đúng batch preview hiện tại lên Notion, chưa gửi Telegram?');
      if (!ok) return;
      await callApi('/api/publish-notion-only', {});
      await refreshStatus();
    }

    function renderLaneMessages(containerId, metaId, messages, emptyText, laneLabel) {
      const container = document.getElementById(containerId);
      const meta = document.getElementById(metaId);
      container.innerHTML = '';
      meta.textContent = `${(messages || []).length} messages`;
      if (!messages || !messages.length) {
        container.innerHTML = `<div class="tg-empty">${escapeHtml(emptyText)}</div>`;
        return;
      }
      const decodeEntities = (input) => {
        const text = document.createElement('textarea');
        text.innerHTML = input || '';
        return text.value;
      };
      messages.forEach((message, index) => {
        const box = document.createElement('article');
        box.className = 'tg-bubble';
        const header = document.createElement('div');
        header.className = 'tg-bubble-head';
        header.innerHTML = `<span>${escapeHtml(laneLabel)} · chunk ${index + 1}</span><span class="tg-bubble-meta">preview</span>`;
        const body = document.createElement('div');
        body.textContent = decodeEntities((message || '').replace(/<[^>]+>/g, ''));
        box.appendChild(header);
        box.appendChild(body);
        container.appendChild(box);
      });
    }

    function renderWorkspaceDetail() {
      const article = workspaceState.articles[workspaceState.selectedIndex];
      const badges = document.getElementById('workspace-detail-badges');
      const title = document.getElementById('workspace-detail-title');
      const summary = document.getElementById('workspace-detail-summary');
      const grounding = document.getElementById('workspace-detail-grounding');
      const sourceLink = document.getElementById('workspace-detail-source');
      const notionLink = document.getElementById('workspace-detail-notion');

      badges.innerHTML = '';
      if (!article) {
        title.textContent = 'Chọn một article để xem detail';
        summary.textContent = 'Khi có preview, phần này sẽ hiện note ngắn, editorial angle, grounding và link nguồn tương ứng.';
        grounding.textContent = '';
        sourceLink.style.display = 'none';
        notionLink.style.display = 'none';
        return;
      }

      title.textContent = article.title || 'Untitled';
      summary.textContent = article.summary || 'Chưa có summary.';
      grounding.textContent = article.editorial_angle
        ? `Editorial angle: ${article.editorial_angle}${article.grounding_note ? ' | Grounding: ' + article.grounding_note : ''}`
        : (article.grounding_note || 'Chưa có grounding note cụ thể.');

      const badgeSpecs = [
        { label: article.type, className: 'type' },
        { label: `score ${article.score}`, className: 'delivery' },
        { label: `fit ${article.project_fit}`, className: badgeClass('fit', article.project_fit) },
        { label: `rel ${article.relevance_level}`, className: badgeClass('rel', article.relevance_level) },
        { label: article.delivery_status, className: 'delivery' },
        { label: `${article.source_tier} source`, className: 'delivery' },
        { label: `${article.analysis_tier} analysis`, className: 'delivery' },
        { label: `${article.event_source_count} sources`, className: 'delivery' },
      ];
      (article.tags || []).forEach((tag) => badgeSpecs.push({ label: tag, className: 'tag' }));

      badgeSpecs.forEach((item) => {
        if (!item.label) return;
        const span = document.createElement('span');
        span.className = `badge ${item.className}`;
        span.textContent = item.label;
        badges.appendChild(span);
      });

      if (article.url) {
        sourceLink.href = article.url;
        sourceLink.style.display = 'inline-flex';
      } else {
        sourceLink.style.display = 'none';
      }
      if (article.notion_url) {
        notionLink.href = article.notion_url;
        notionLink.style.display = 'inline-flex';
      } else {
        notionLink.style.display = 'none';
      }
    }

    function renderWorkspace(articles, publishState) {
      const rows = Array.isArray(articles) ? articles : [];
      workspaceState.articles = rows;
      if (workspaceState.selectedIndex >= rows.length) {
        workspaceState.selectedIndex = 0;
      }

      document.getElementById('workspace-state').textContent = publishState || 'preview only';
      document.getElementById('workspace-count').textContent = rows.length;
      document.getElementById('workspace-high-fit').textContent = rows.filter((row) => row.project_fit === 'High').length;
      document.getElementById('workspace-brief-ready').textContent = rows.filter((row) => row.delivery_status === 'Brief').length;

      const body = document.getElementById('workspace-table-body');
      const empty = document.getElementById('workspace-empty');
      body.innerHTML = '';

      if (!rows.length) {
        empty.style.display = 'block';
        renderWorkspaceDetail();
        return;
      }

      empty.style.display = 'none';
      rows.forEach((article, index) => {
        const tr = document.createElement('tr');
        if (index === workspaceState.selectedIndex) {
          tr.classList.add('is-active');
        }
        tr.innerHTML = `
          <td class="name-cell">
            <div class="name-title">${escapeHtml(article.emoji || '[]')} ${escapeHtml(article.title || 'Untitled')}</div>
            <div class="name-meta">${escapeHtml(article.source_name || article.source || 'unknown')}</div>
          </td>
          <td>${escapeHtml(article.created_time || '-')}</td>
          <td>${escapeHtml(article.source || '-')}</td>
          <td>${escapeHtml(article.score ?? 0)}</td>
          <td><span class="badge type">${escapeHtml(article.type || 'Unknown')}</span></td>
          <td><span class="badge ${badgeClass('fit', article.project_fit)}">${escapeHtml(article.project_fit || 'Low')}</span></td>
          <td class="summary-cell">${escapeHtml(article.summary || '-')}</td>
        `;
        tr.addEventListener('click', () => {
          workspaceState.selectedIndex = index;
          renderWorkspace(rows, publishState);
        });
        body.appendChild(tr);
      });
      renderWorkspaceDetail();
    }

    async function refreshStatus() {
      const res = await fetch('/api/status');
      const data = await res.json();
      const result = data.result || {};

      document.getElementById('status-text').textContent = data.running ? 'Running' : 'Idle';
      document.getElementById('status-dot').className = data.running ? 'dot running' : 'dot';
      document.getElementById('elapsed').textContent = `${data.elapsed_seconds || result.elapsed_seconds || 0}s`;
      document.getElementById('raw-count').textContent = result.raw_count || 0;
      document.getElementById('scored-count').textContent = result.scored_count || 0;
      document.getElementById('tg-candidate-count').textContent = result.telegram_candidate_count || 0;
      document.getElementById('github-candidate-count').textContent = result.github_topic_candidate_count || 0;
      document.getElementById('facebook-candidate-count').textContent = result.facebook_topic_candidate_count || 0;
      document.getElementById('notion-count-card').textContent = result.notion_count || 0;
      document.getElementById('run-mode').textContent = result.run_mode || data.last_mode || '-';
      document.getElementById('run-profile').textContent = result.run_profile || data.last_profile || '-';
      document.getElementById('summary-mode').textContent = result.summary_mode || '-';
      document.getElementById('health-status').textContent = (result.run_health || {}).status || '-';
      document.getElementById('publish-ready').textContent = result.publish_ready ? 'yes' : 'no';
      document.getElementById('main-candidate-chip').textContent = result.telegram_candidate_count || 0;
      document.getElementById('github-candidate-chip').textContent = result.github_topic_candidate_count || 0;
      document.getElementById('facebook-candidate-chip').textContent = result.facebook_topic_candidate_count || 0;
      document.getElementById('notion-count').textContent = result.notion_count || 0;
      document.getElementById('telegram-sent').textContent = result.telegram_sent ? 'yes' : 'no';
      document.getElementById('run-health').textContent = JSON.stringify(result.run_health || {}, null, 2);
      document.getElementById('health-issues').textContent = ((result.run_health || {}).issues || []).join('\\n') || '(không có issue nổi bật)';
      document.getElementById('report-path').textContent = result.run_report_path || '-';
      document.getElementById('report-content').textContent = data.report_content || '(chưa có report)';
      document.getElementById('feedback-summary').textContent = result.feedback_summary_text || '(chưa có feedback)';
      document.getElementById('logs').textContent = (data.logs || []).join('\\n') || '(chưa có log)';
      document.getElementById('last-error').textContent = data.last_error || '';
      document.getElementById('preview-state-pill').textContent = `preview state: ${data.preview_publish_state || 'preview only'}`;

      renderLaneMessages(
        'messages-main',
        'main-topic-meta',
        result.telegram_messages || [],
        'Chưa có bản tin chính nào. Hãy chạy preview để xem batch hiện tại.',
        'Main'
      );
      renderLaneMessages(
        'messages-github',
        'github-topic-meta',
        result.github_topic_messages || [],
        'Chưa có message nào cho GitHub Repo Digest ở batch này.',
        'GitHub'
      );
      renderLaneMessages(
        'messages-facebook',
        'facebook-topic-meta',
        result.facebook_topic_messages || [],
        'Chưa có message nào cho Facebook News ở batch này.',
        'Facebook'
      );
      renderWorkspace(data.preview_articles || [], data.preview_publish_state || 'preview only');

      document.getElementById('btn-preview').disabled = data.running;
      document.getElementById('btn-smart-preview').disabled = data.running;
      document.getElementById('btn-notion-only').disabled = data.running || !data.can_publish_notion_only;
      document.getElementById('btn-publish').disabled = data.running;
      document.getElementById('btn-refresh').disabled = data.running;
      document.getElementById('btn-approve').disabled = data.running || !data.can_approve;
      document.getElementById('btn-open-report').disabled = !result.run_report_path;
    }

    document.getElementById('btn-preview').addEventListener('click', runPreview);
    document.getElementById('btn-smart-preview').addEventListener('click', runSmartPreview);
    document.getElementById('btn-notion-only').addEventListener('click', publishNotionOnly);
    document.getElementById('btn-publish').addEventListener('click', runPublish);
    document.getElementById('btn-approve').addEventListener('click', approvePreview);
    document.getElementById('btn-refresh').addEventListener('click', refreshStatus);
    document.getElementById('btn-open-report').addEventListener('click', () => {
      window.open('/api/report/latest', '_blank');
    });

    refreshStatus();
    setInterval(refreshStatus, 1500);
  </script>
</body>
</html>
"""


class DigestUIHandler(BaseHTTPRequestHandler):
    server_version = "DigestUI/1.1"

    def _json_response(self, payload: dict[str, Any], status: int = HTTPStatus.OK) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _html_response(self, html: str) -> None:
        encoded = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _text_response(self, text: str, status: int = HTTPStatus.OK) -> None:
        encoded = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802
        if self.path in {"/", "/index.html"}:
            self._html_response(HTML_PAGE)
            return
        if self.path == "/api/status":
            self._json_response(_status_payload())
            return
        if self.path == "/api/report/latest":
            with APP_STATE["lock"]:
                report_path = str((APP_STATE.get("last_result") or {}).get("run_report_path", "") or "")
            content = _read_report_content(report_path)
            if not content:
                self._text_response("Chưa có report nào để mở.", status=HTTPStatus.NOT_FOUND)
                return
            self._text_response(content)
            return

        self._json_response({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw_body = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError:
            self._json_response({"error": "JSON không hợp lệ."}, status=HTTPStatus.BAD_REQUEST)
            return

        if self.path == "/api/run":
            ok, message = start_job(
                mode=str(payload.get("mode", "")),
                runtime_config=payload.get("runtime_config", {}),
                run_profile=str(payload.get("run_profile", "")),
            )
            if not ok:
                self._json_response({"error": message}, status=HTTPStatus.CONFLICT)
                return
            self._json_response({"ok": True, "message": message})
            return

        if self.path == "/api/approve":
            ok, message = approve_preview_job()
            if not ok:
                self._json_response({"error": message}, status=HTTPStatus.CONFLICT)
                return
            self._json_response({"ok": True, "message": message})
            return

        if self.path == "/api/publish-notion-only":
            ok, message = publish_notion_only_job()
            if not ok:
                self._json_response({"error": message}, status=HTTPStatus.CONFLICT)
                return
            self._json_response({"ok": True, "message": message})
            return

        self._json_response({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        logger.debug("HTTP: " + format, *args)


def _configure_logging() -> None:
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s │ %(levelname)-7s │ %(name)s │ %(message)s",
            datefmt="%H:%M:%S",
        )

    handler = _RunLogHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s │ %(levelname)-7s │ %(name)s │ %(message)s", "%H:%M:%S"))
    logging.getLogger().addHandler(handler)


def _ui_port_candidates(requested_port: int) -> list[int]:
    raw_span = os.getenv("DIGEST_UI_PORT_FALLBACK_SPAN", "12")
    try:
        span = max(0, min(50, int(raw_span)))
    except (TypeError, ValueError):
        span = 12
    return [requested_port + offset for offset in range(span + 1)]


def _bind_ui_server(host: str, requested_port: int) -> tuple[ThreadingHTTPServer, int]:
    last_error: OSError | None = None
    for candidate_port in _ui_port_candidates(requested_port):
        try:
            httpd = ThreadingHTTPServer((host, candidate_port), DigestUIHandler)
            if candidate_port != requested_port:
                logger.warning(
                    "⚠️ Port %d đang bị chiếm, Digest UI tự chuyển sang port %d.",
                    requested_port,
                    candidate_port,
                )
            return httpd, candidate_port
        except OSError as exc:
            last_error = exc
            if exc.errno not in {errno.EADDRINUSE, 48, 98}:
                raise
            logger.warning("Port %d đang bị chiếm, thử port khác…", candidate_port)

    raise RuntimeError(
        f"Không bind được Digest UI từ port {requested_port} tới {requested_port + len(_ui_port_candidates(requested_port)) - 1}."
    ) from last_error


def main() -> None:
    _configure_logging()
    port = int(os.getenv("DIGEST_UI_PORT", "8787"))
    host = os.getenv("DIGEST_UI_HOST", "127.0.0.1")
    httpd, bound_port = _bind_ui_server(host, port)
    logger.info("🌐 Digest UI running at http://%s:%d", host, bound_port)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
