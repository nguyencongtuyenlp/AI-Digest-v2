#!/usr/bin/env python3
"""
main.py — Entry point cho Daily Digest AI Agent (MVP2).

Load environment, compile LangGraph, chạy pipeline, log kết quả.
Model: Qwen2.5-72B-4bit trên Apple Silicon (MLX).
"""

from __future__ import annotations

import logging
import sys
import os
from pathlib import Path

# Đảm bảo project root nằm trong sys.path
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

# ── Load .env ───────────────────────────────────────────────────────────
env_path = PROJECT_ROOT / "config" / ".env"
if env_path.exists():
    load_dotenv(env_path)
else:
    load_dotenv(PROJECT_ROOT / ".env")

# ── Logging ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-7s │ %(name)s │ %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("daily-digest")


def _harden_http_logging() -> None:
    """Giảm noise logs cho client libs và tránh rò rỉ token trong HTTP log URL."""
    for lib in ("httpx", "httpcore", "urllib3", "requests", "requests.packages.urllib3"):
        logging.getLogger(lib).setLevel(logging.WARNING)


_harden_http_logging()


def main() -> None:
    """Chạy toàn bộ pipeline Daily Digest Agent."""
    run_profile = str(os.getenv("DIGEST_RUN_PROFILE", "publish") or "publish").strip().lower()
    logger.info("=" * 60)
    logger.info("🚀 Daily Digest AI Agent (MVP2)")
    logger.info("   Model: %s", os.getenv("MLX_MODEL", "N/A"))
    logger.info("   Mode : publish")
    logger.info("   Profile: %s", run_profile)
    logger.info("=" * 60)
    from pipeline_runner import run_pipeline

    result, summary = run_pipeline(run_mode="publish", run_profile=run_profile)

    # ── Log kết quả ─────────────────────────────────────────────────
    elapsed = summary["elapsed_seconds"]
    raw_count = summary["raw_count"]
    grok_scout_count = summary.get("grok_scout_count", 0)
    new_count = summary["new_count"]
    scored_count = summary["scored_count"]
    top_count = summary["top_count"]
    notion_count = summary["notion_count"]
    tg_sent = summary["telegram_sent"]
    report_path = summary["run_report_path"]
    gather_snapshot_path = summary.get("gather_snapshot_path", "")
    scored_snapshot_path = summary.get("scored_snapshot_path", "")
    artifact_cleanup = dict(summary.get("artifact_cleanup", {}) or {})

    logger.info("─" * 60)
    logger.info("📊 Pipeline completed in %.1f seconds (%.1f minutes)", elapsed, elapsed / 60)
    logger.info("   Raw articles gathered  : %d", raw_count)
    logger.info("   Grok scout added       : %d", grok_scout_count)
    logger.info("   After dedup            : %d", new_count)
    logger.info("   Classified + scored    : %d", scored_count)
    logger.info("   Top (deep analyzed)    : %d", top_count)
    logger.info("   Deep analysis done     : %d", len(result.get("analyzed_articles", [])))
    logger.info("   Notion pages created   : %d", notion_count)
    logger.info("   Telegram sent          : %s", "✅" if tg_sent else "⏭️ skipped")
    if gather_snapshot_path:
        logger.info("   Gather snapshot       : %s", gather_snapshot_path)
    if scored_snapshot_path:
        logger.info("   Scored snapshot       : %s", scored_snapshot_path)
    if report_path:
        logger.info("   Run report            : %s", report_path)
    if artifact_cleanup.get("enabled"):
        logger.info(
            "   Artifact cleanup      : archived=%d kept=%d archive=%s",
            int(artifact_cleanup.get("archived_count", 0) or 0),
            int(artifact_cleanup.get("kept_count", 0) or 0),
            artifact_cleanup.get("archive_root", ""),
        )
    logger.info("=" * 60)

    # ── Print summary ra stdout ─────────────────────────────────────
    summary = result.get("summary_vn", "")
    if summary:
        # Strip HTML tags cho console output
        import re
        clean = re.sub(r"<[^>]+>", "", summary)
        print("\n" + clean)


if __name__ == "__main__":
    main()
