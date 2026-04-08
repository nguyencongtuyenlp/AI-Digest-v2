import json
import os
import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from digest.editorial.editorial_guardrails import (
    build_article_grounding,
    build_safe_digest,
    build_safe_digest_messages,
    build_telegram_copy_from_structured,
    sanitize_delivery_text,
    validate_telegram_messages,
    validate_telegram_summary,
)
from digest.editorial.executive_intelligence import build_executive_intelligence_bundle
from digest.editorial.feedback_loop import _clean_feedback_text, _feedback_labels, build_feedback_context
from pipeline_runner import build_initial_state, publish_from_preview_state, publish_notion_only_from_preview_state
from digest.runtime.run_health import assess_run_health, collect_source_health
from digest.runtime.runtime_presets import apply_runtime_preset
from digest.runtime.mlx_runner import run_json_inference
from digest.runtime.stage_metrics import summarize_stage_timings
from digest.runtime.artifact_retention import cleanup_runtime_artifacts
from scripts.github_agent_brief import _group_github_articles
from scripts.weekly_memo import build_weekly_memo
from digest.workflow.nodes.generate_run_report import _build_run_report_markdown, generate_run_report_node
from digest.sources.source_history import (
    annotate_article_with_source_history,
    build_source_history_key,
    compute_source_history_quality,
)
from digest.workflow.nodes.classify_and_score import (
    _annotate_event_clusters,
    _apply_freshness_penalty,
    _articles_same_event,
    _classify_inference_with_retry,
    classify_and_score_node,
    _finalize_scored_article,
    _held_out_article_fallback,
    _select_top_articles,
    _prefilter_primary_type,
    _infer_taxonomy_tags,
    _prepare_classify_candidates,
)
from digest.workflow.nodes.delivery_judge import _deterministic_delivery_assessment, delivery_judge_node
from digest.workflow.nodes.gather_news import (
    _build_facebook_auto_article,
    _fetch_hacker_news,
    _resolve_facebook_source_registry,
    _score_facebook_source,
    gather_news_node,
)
from digest.workflow.nodes.normalize_source import normalize_source_node
from digest.workflow.nodes.quality_gate import quality_gate_node
from digest.workflow.nodes.save_notion import (
    _find_existing_notion_page_url,
    _build_formatted_rich_text,
    _get_notion_parent_and_properties,
    _create_notion_page_with_fallback,
    _project_fit_level,
    _storage_primary_type,
    _storage_tags,
    _resolve_property_map,
    save_notion_node,
)
from digest.workflow.nodes.send_telegram import send_telegram_node
from digest.workflow.nodes.summarize_vn import summarize_vn_node
from digest.sources.source_catalog import load_watchlist_seeds
from digest.runtime.temporal_snapshots import write_temporal_snapshot


class EditorialGuardrailsTest(unittest.TestCase):
    def test_source_history_quality_penalizes_noisy_source(self) -> None:
        quality = compute_source_history_quality(
            {
                "runs_seen": 5,
                "raw_articles": 18,
                "scored_articles": 14,
                "selected_main": 0,
                "selected_github": 0,
                "selected_facebook": 1,
                "skipped_old": 5,
                "skipped_speculation": 3,
                "skipped_promo": 2,
                "skipped_weak": 4,
            }
        )

        self.assertGreaterEqual(quality["penalty"], 8)
        self.assertLessEqual(quality["quality_score"], 35)
        self.assertIn(quality["status"], {"watch", "muted"})

    def test_source_history_annotation_adjusts_source_priority(self) -> None:
        article = {
            "title": "AI community rumor",
            "url": "https://facebook.com/groups/example/posts/123",
            "source": "Social Signal: Facebook | Example Group",
            "source_domain": "facebook.com",
            "source_priority": 74,
            "social_platform": "facebook",
            "facebook_source_url": "https://facebook.com/groups/example",
        }
        source_key = build_source_history_key(article)
        annotated = annotate_article_with_source_history(
            dict(article),
            {
                source_key: {
                    "source_key": source_key,
                    "runs_seen": 6,
                    "raw_articles": 20,
                    "scored_articles": 14,
                    "selected_main": 0,
                    "selected_github": 0,
                    "selected_facebook": 1,
                    "skipped_old": 4,
                    "skipped_speculation": 3,
                    "skipped_promo": 2,
                    "skipped_weak": 5,
                }
            },
        )

        self.assertLess(annotated["source_priority"], article["source_priority"])
        self.assertGreaterEqual(annotated["source_history_penalty"], 8)
        self.assertIn(annotated["source_history_status"], {"watch", "muted"})

    def test_source_catalog_facade_reexports_split_modules(self) -> None:
        from digest.sources.source_catalog import CURATED_RSS_FEEDS as facade_feeds
        from digest.sources.source_catalog import classify_source_kind as facade_classify
        from digest.sources.source_policy import classify_source_kind as split_classify
        from digest.sources.source_registry import CURATED_RSS_FEEDS as split_feeds

        self.assertEqual(facade_feeds, split_feeds)
        self.assertEqual(
            facade_classify(source="RSS: OpenAI", domain="openai.com"),
            split_classify(source="RSS: OpenAI", domain="openai.com"),
        )

    def test_temporal_snapshot_writes_compact_json_without_full_content(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = write_temporal_snapshot(
                state={
                    "started_at": "2026-04-02T01:45:00+00:00",
                    "run_mode": "preview",
                    "run_profile": "fast",
                    "runtime_config": {
                        "temporal_snapshot_dir": tmpdir,
                    },
                },
                stage="gather",
                articles=[
                    {
                        "title": "OpenAI ships new agent workflow controls",
                        "url": "https://openai.com/index/example/",
                        "source": "RSS: OpenAI News",
                        "content": "x" * 5000,
                        "source_kind": "official",
                        "total_score": 77,
                    }
                ],
                extra={"raw_count": 1},
            )

            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            self.assertEqual(payload["stage"], "gather")
            self.assertEqual(payload["article_count"], 1)
            self.assertEqual(payload["extra"]["raw_count"], 1)
            self.assertEqual(payload["articles"][0]["title"], "OpenAI ships new agent workflow controls")
            self.assertNotIn("content", payload["articles"][0])

    def test_runtime_artifact_cleanup_archives_old_reports_snapshots_and_logs(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            reports_dir = root / "reports"
            snapshot_dir = reports_dir / "temporal_snapshots"
            checkpoints_dir = root / ".checkpoints"
            reports_dir.mkdir(parents=True, exist_ok=True)
            snapshot_dir.mkdir(parents=True, exist_ok=True)
            checkpoints_dir.mkdir(parents=True, exist_ok=True)

            current_report = reports_dir / "daily_digest_run_20260402_100000.md"
            old_report = reports_dir / "daily_digest_run_20260327_090000.md"
            current_snapshot = snapshot_dir / "20260402_100000_gather.json"
            old_snapshot = snapshot_dir / "20260327_090000_gather.json"
            old_checkpoint = checkpoints_dir / "pre_direction_b_20260327_151755.tar.gz"
            debug_output = root / "debug_output.txt"

            for path in (current_report, old_report, current_snapshot, old_snapshot, old_checkpoint, debug_output):
                path.write_text("artifact", encoding="utf-8")

            stale_ts = datetime(2026, 2, 20, tzinfo=timezone.utc).timestamp()
            os.utime(old_report, (stale_ts, stale_ts))
            os.utime(old_snapshot, (stale_ts, stale_ts))
            os.utime(old_checkpoint, (stale_ts, stale_ts))
            os.utime(debug_output, (stale_ts, stale_ts))

            summary = cleanup_runtime_artifacts(
                project_root=root,
                state={"runtime_config": {"temporal_snapshot_dir": str(snapshot_dir)}},
                preserve_paths=[current_report, current_snapshot],
            )

            archive_root = root / ".runtime_archive"
            self.assertGreaterEqual(summary["archived_count"], 4)
            self.assertTrue(current_report.exists())
            self.assertTrue(current_snapshot.exists())
            self.assertFalse(old_report.exists())
            self.assertFalse(old_snapshot.exists())
            self.assertFalse(old_checkpoint.exists())
            self.assertFalse(debug_output.exists())
            self.assertTrue((archive_root / "reports" / old_report.name).exists())
            self.assertTrue((archive_root / "reports" / "temporal_snapshots" / old_snapshot.name).exists())
            self.assertTrue((archive_root / ".checkpoints" / old_checkpoint.name).exists())
            self.assertTrue(any(path.name.startswith("debug_output_") for path in (archive_root / "logs").glob("debug_output_*.txt")))

    @patch("digest.workflow.nodes.generate_run_report.cleanup_runtime_artifacts")
    def test_generate_run_report_appends_artifact_cleanup_section(self, mock_cleanup) -> None:
        mock_cleanup.return_value = {
            "enabled": True,
            "archive_root": "/tmp/archive",
            "archived_count": 3,
            "kept_count": 4,
            "rules": [
                {"label": "daily_reports", "scanned": 5, "kept": 1, "archived": 4},
                {"label": "temporal_snapshots", "scanned": 2, "kept": 1, "archived": 1},
            ],
            "archived_files": ["/tmp/archive/reports/daily_digest_run_20260327_090000.md"],
        }

        with TemporaryDirectory() as tmpdir, patch.dict(os.environ, {"DIGEST_REPORTS_DIR": tmpdir}, clear=False):
            result = generate_run_report_node({"run_mode": "preview", "raw_articles": [], "scored_articles": []})
            content = Path(result["run_report_path"]).read_text(encoding="utf-8")

        self.assertIn("## Artifact Cleanup", content)
        self.assertIn("- Archived this run: 3", content)
        self.assertIn("daily_reports", content)
        self.assertIn("Sample archived", content)

    @patch("digest.workflow.nodes.generate_run_report.load_source_history")
    @patch("digest.workflow.nodes.generate_run_report.batch_source_history_rows")
    def test_run_report_mentions_source_history_sections(self, mock_rows, mock_load_history) -> None:
        mock_load_history.return_value = {}
        mock_rows.return_value = (
            [
                {
                    "source_label": "RSS: OpenAI News",
                    "source_domain": "openai.com",
                    "quality_score": 78,
                    "status": "trusted",
                    "runs_seen": 4,
                    "selection_rate": 0.4,
                }
            ],
            [
                {
                    "source_label": "Facebook Group Example",
                    "source_domain": "facebook.com",
                    "quality_score": 22,
                    "status": "muted",
                    "noise_rate": 0.5,
                    "penalty": 8,
                }
            ],
        )

        markdown = _build_run_report_markdown(
            {
                "run_mode": "preview",
                "run_profile": "preview",
                "raw_articles": [{"title": "x", "source": "RSS: OpenAI News", "source_domain": "openai.com"}],
                "new_articles": [],
                "scored_articles": [{"title": "x", "source": "RSS: OpenAI News", "source_domain": "openai.com"}],
                "top_articles": [],
                "low_score_articles": [],
                "final_articles": [],
                "telegram_candidates": [],
                "notion_pages": [],
            },
            datetime(2026, 4, 2, tzinfo=timezone.utc),
        )

        self.assertIn("## Source History Signals", markdown)
        self.assertIn("RSS: OpenAI News", markdown)
        self.assertIn("Facebook Group Example", markdown)

    def test_find_existing_notion_page_url_by_source_url(self) -> None:
        class _FakeDatabases:
            def query(self, database_id: str, **kwargs: dict) -> dict:
                self.database_id = database_id
                self.kwargs = kwargs
                return {
                    "results": [
                        {"url": "https://www.notion.so/existing-page"}
                    ]
                }

        class _FakeNotion:
            def __init__(self) -> None:
                self.databases = _FakeDatabases()

        notion = _FakeNotion()
        notion_url = _find_existing_notion_page_url(
            notion,
            {"database_id": "db_123"},
            {"url": "Link gốc"},
            "https://github.com/example/repo",
        )

        self.assertEqual(notion_url, "https://www.notion.so/existing-page")
        self.assertEqual(notion.databases.database_id, "db_123")
        self.assertEqual(notion.databases.kwargs["filter"]["property"], "Link gốc")

    def test_group_github_articles_merges_repo_and_release(self) -> None:
        raw_articles = [
            {
                "title": "example/agent-tool",
                "url": "https://github.com/example/agent-tool",
                "github_full_name": "example/agent-tool",
                "github_signal_type": "repository",
                "github_stars": 1200,
                "snippet": "Agent framework for Claude Code and MCP",
                "published": "2026-03-20T10:00:00Z",
            },
            {
                "title": "example/agent-tool — v1.2.0",
                "url": "https://github.com/example/agent-tool/releases/tag/v1.2.0",
                "github_full_name": "example/agent-tool",
                "github_signal_type": "release",
                "snippet": "Adds MCP memory plugin for Claude Code",
                "published": "2026-03-30T10:00:00Z",
            },
            {
                "title": "example/plain-repo",
                "url": "https://github.com/example/plain-repo",
                "github_full_name": "example/plain-repo",
                "github_signal_type": "repository",
                "github_stars": 100,
                "snippet": "Lightweight utility",
                "published": "2026-03-01T10:00:00Z",
            },
        ]

        grouped = _group_github_articles(raw_articles)

        self.assertEqual(grouped[0]["title"], "example/agent-tool")
        self.assertEqual(grouped[0]["github_release_title"], "example/agent-tool — v1.2.0")
        self.assertGreater(grouped[0]["total_score"], grouped[1]["total_score"])

    def test_load_watchlist_seeds_parses_github_entries(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            project_root = Path(tmp_dir)
            config_dir = project_root / "config"
            config_dir.mkdir(parents=True, exist_ok=True)
            watchlist_file = config_dir / "watchlist_seeds.txt"
            watchlist_file.write_text(
                "\n".join(
                    [
                        "query:OpenAI agents enterprise",
                        "github_repo:openai/openai-agents-python",
                        "github_org:huggingface",
                        "github_query:model context protocol",
                        "https://openai.com/news",
                    ]
                ),
                encoding="utf-8",
            )

            with patch.dict(
                "os.environ",
                {
                    "WATCHLIST_SEEDS_FILE": "",
                    "WATCHLIST_URLS": "",
                    "WATCHLIST_QUERIES": "",
                    "GITHUB_WATCHLIST_REPOS": "",
                    "GITHUB_WATCHLIST_ORGS": "",
                    "GITHUB_SEARCH_QUERIES": "",
                    "WATCHLIST_COMPANIES": "",
                    "WATCHLIST_PRODUCTS": "",
                    "WATCHLIST_TOOLS": "",
                    "WATCHLIST_POLICIES": "",
                    "WATCHLIST_TOPICS": "",
                },
                clear=False,
            ):
                seeds = load_watchlist_seeds(project_root)

        self.assertEqual(seeds["queries"], ["OpenAI agents enterprise"])
        self.assertEqual(seeds["github_repos"], ["openai/openai-agents-python"])
        self.assertEqual(seeds["github_orgs"], ["huggingface"])
        self.assertEqual(seeds["github_queries"], ["model context protocol"])
        self.assertEqual(seeds["urls"], ["https://openai.com/news"])

    def test_load_watchlist_seeds_parses_strategic_buckets(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            project_root = Path(tmp_dir)
            config_dir = project_root / "config"
            config_dir.mkdir(parents=True, exist_ok=True)
            watchlist_file = config_dir / "watchlist_seeds.txt"
            watchlist_file.write_text(
                "\n".join(
                    [
                        "company:OpenAI",
                        "product:GPT-4.1",
                        "tool:LangGraph",
                        "policy:EU AI Act",
                        "topic:Claude Code",
                    ]
                ),
                encoding="utf-8",
            )

            with patch.dict(
                "os.environ",
                {
                    "WATCHLIST_SEEDS_FILE": "",
                    "WATCHLIST_URLS": "",
                    "WATCHLIST_QUERIES": "",
                    "GITHUB_WATCHLIST_REPOS": "",
                    "GITHUB_WATCHLIST_ORGS": "",
                    "GITHUB_SEARCH_QUERIES": "",
                    "WATCHLIST_COMPANIES": "",
                    "WATCHLIST_PRODUCTS": "",
                    "WATCHLIST_TOOLS": "",
                    "WATCHLIST_POLICIES": "",
                    "WATCHLIST_TOPICS": "",
                },
                clear=False,
            ):
                seeds = load_watchlist_seeds(project_root)

        self.assertEqual(seeds["company_watchlist"], ["OpenAI"])
        self.assertEqual(seeds["product_watchlist"], ["GPT-4.1"])
        self.assertEqual(seeds["tool_watchlist"], ["LangGraph"])
        self.assertEqual(seeds["policy_watchlist"], ["EU AI Act"])
        self.assertEqual(seeds["topic_watchlist"], ["Claude Code"])

    @patch("digest.workflow.nodes.gather_news._fetch_github_articles")
    @patch("digest.workflow.nodes.gather_news.load_watchlist_seeds")
    def test_gather_news_can_collect_github_source(self, mock_watchlist, mock_fetch_github) -> None:
        mock_watchlist.return_value = {
            "urls": [],
            "queries": [],
            "github_repos": ["openai/openai-agents-python"],
            "github_orgs": ["openai"],
            "github_queries": ["ai agents framework"],
        }
        mock_fetch_github.return_value = [
            {
                "title": "openai/openai-agents-python",
                "url": "https://github.com/openai/openai-agents-python",
                "source": "GitHub API Repo: openai/openai-agents-python",
                "snippet": "GitHub repo signal",
                "content": "GitHub repo: openai/openai-agents-python | stars=123",
                "published": "2026-03-31T01:00:00Z",
            }
        ]

        result = gather_news_node(
            {
                "runtime_config": {
                    "enable_rss": False,
                    "enable_github": True,
                    "enable_watchlist": False,
                    "enable_hn": False,
                    "enable_reddit": False,
                    "enable_ddg": False,
                    "enable_telegram_channels": False,
                    "enable_grok_scout": False,
                    "enable_grok_x_scout": False,
                }
            }
        )

        self.assertEqual(len(result["raw_articles"]), 1)
        self.assertEqual(result["raw_articles"][0]["source"], "GitHub API Repo: openai/openai-agents-python")
        mock_fetch_github.assert_called_once()

    @patch("digest.workflow.nodes.gather_news.social_signal_inbox_path")
    @patch("digest.workflow.nodes.gather_news.load_watchlist_seeds")
    def test_gather_news_accepts_facebook_social_signal_for_topic_lane(self, mock_watchlist, mock_inbox_path) -> None:
        mock_watchlist.return_value = {
            "urls": [],
            "queries": [],
            "github_repos": [],
            "github_orgs": [],
            "github_queries": [],
        }

        with TemporaryDirectory() as tmp_dir:
            inbox_path = Path(tmp_dir) / "social_signal_inbox.txt"
            inbox_path.write_text(
                "\n".join(
                    [
                        "platform: facebook",
                        "author: AI Agent Vietnam",
                        "title: Founder chia sẻ workflow Claude Code + MCP",
                        "url: https://www.facebook.com/groups/example/posts/123456789/",
                        "note: Có demo thực chiến cho AI agent",
                        "content: Nhóm này đang dùng Claude Code, MCP và memory để chạy workflow nội bộ.",
                        "---",
                    ]
                ),
                encoding="utf-8",
            )
            mock_inbox_path.return_value = inbox_path

            result = gather_news_node(
                {
                    "runtime_config": {
                        "enable_rss": False,
                        "enable_github": False,
                        "enable_social_signals": True,
                        "enable_watchlist": False,
                        "enable_hn": False,
                        "enable_reddit": False,
                        "enable_ddg": False,
                        "enable_telegram_channels": False,
                        "enable_grok_scout": False,
                        "enable_grok_x_scout": False,
                    }
                }
            )

        self.assertEqual(len(result["raw_articles"]), 1)
        self.assertEqual(result["raw_articles"][0]["social_platform"], "facebook")
        self.assertEqual(result["raw_articles"][0]["delivery_lane_hint"], "")

    @patch("digest.workflow.nodes.gather_news._load_facebook_auto_targets")
    @patch("digest.workflow.nodes.gather_news._scrape_facebook_target_payloads")
    @patch("digest.workflow.nodes.gather_news.load_watchlist_seeds")
    @patch("digest.workflow.nodes.gather_news._facebook_session_needs_refresh", return_value=False)
    def test_gather_news_accepts_facebook_auto_articles_for_topic_lane(
        self,
        _mock_session_fresh,
        mock_watchlist,
        mock_scrape,
        mock_targets,
    ) -> None:
        mock_watchlist.return_value = {
            "urls": [],
            "queries": [],
            "github_repos": [],
            "github_orgs": [],
            "github_queries": [],
        }
        mock_targets.return_value = [
            {"label": "Nghiện AI", "url": "https://www.facebook.com/nghienai/"}
        ]
        mock_scrape.return_value = [
            {
                "text": (
                    "Nghiện AI\n14 giờ\nMiniMax 2.7 vs Claude Opus 4.6: Đánh đổi 10% chất lượng để tiết kiệm 90% chi phí?\n"
                    "Kilo Code đã làm benchmark giữa MiniMax M2.7 và Claude Opus 4.6 cho các bài test full-stack, bug fixing và phân tích codebase."
                ),
                "links": [
                    "https://www.facebook.com/nghienai/posts/123456789012345"
                ],
            }
        ]

        result = gather_news_node(
            {
                "runtime_config": {
                    "enable_rss": False,
                    "enable_github": False,
                    "enable_social_signals": False,
                    "enable_facebook_auto": True,
                    "enable_watchlist": False,
                    "enable_hn": False,
                    "enable_reddit": False,
                    "enable_ddg": False,
                    "enable_telegram_channels": False,
                    # Tránh gọi Grok scout/X khi máy có XAI_API_KEY trong .env (timeout/flaky).
                    "enable_grok_scout": False,
                    "enable_grok_x_scout": False,
                }
            }
        )

        self.assertEqual(len(result["raw_articles"]), 1)
        self.assertTrue(result["raw_articles"][0]["facebook_auto"])
        self.assertEqual(result["raw_articles"][0]["social_platform"], "facebook")
        self.assertEqual(result["raw_articles"][0]["delivery_lane_hint"], "facebook_topic")
        self.assertIn("MiniMax 2.7 vs Claude Opus 4.6", result["raw_articles"][0]["title"])

    def test_build_facebook_auto_article_keeps_content_when_time_is_after_text(self) -> None:
        payload = {
            "text": (
                "Hương Nguyễn\n"
                "Tác giả\n"
                "Người kiểm duyệt\n"
                "Anh em vào web aistudio . google . com, rồi chọn Gemini Flash 2.0 with Image Generation để test nhanh.\n"
                "1 năm\n"
                "Thích\n"
                "Trả lời\n"
                "Chia sẻ"
            ),
            "links": [
                "https://www.facebook.com/groups/ungdungaicongviec/posts/1023655233017187/?comment_id=1023655636350480"
            ],
        }

        article = _build_facebook_auto_article(
            payload,
            target={
                "label": "Nghiện AI - Ứng Dụng AI Vào Công Việc Hàng Ngày",
                "url": "https://www.facebook.com/groups/ungdungaicongviec/",
            },
        )

        self.assertIsNotNone(article)
        assert article is not None
        self.assertEqual(article["published_hint"], "1 năm")
        self.assertIn("Gemini Flash 2.0", article["title"])
        self.assertEqual(
            article["url"],
            "https://www.facebook.com/groups/ungdungaicongviec/posts/1023655233017187/",
        )
        self.assertEqual(article["facebook_source_type"], "group")
        self.assertTrue(article["facebook_content_style"])

    def test_score_facebook_source_promotes_ai_groups_and_profiles(self) -> None:
        self.assertGreaterEqual(
            _score_facebook_source(
                "Nghiện AI - Ứng Dụng AI Vào Công Việc Hàng Ngày",
                "https://www.facebook.com/groups/ungdungaicongviec/",
                source_type="group",
            ),
            70,
        )
        self.assertGreaterEqual(
            _score_facebook_source(
                "OpenClaw VN",
                "https://www.facebook.com/groups/2403721346759140/",
                source_type="group",
            ),
            70,
        )
        self.assertGreaterEqual(
            _score_facebook_source(
                "Việt Nguyễn AI",
                "https://www.facebook.com/vietnguyenai/",
                source_type="profile",
            ),
            50,
        )

    @patch("digest.workflow.nodes.gather_news._refresh_facebook_discovery_cache")
    @patch("digest.workflow.nodes.gather_news._load_facebook_auto_targets")
    def test_resolve_facebook_source_registry_merges_manual_and_discovered_sources(
        self,
        mock_targets,
        mock_refresh,
    ) -> None:
        mock_targets.return_value = [
            {
                "label": "Nghiện AI",
                "url": "https://www.facebook.com/groups/ungdungaicongviec/",
                "source_type": "group",
                "discovery_origin": "manual",
                "ai_source_score": 100,
                "status": "auto_active",
                "last_seen_at": "2026-04-02T00:00:00+00:00",
                "last_crawled_at": "",
            }
        ]
        mock_refresh.return_value = [
            {
                "label": "OpenClaw VN",
                "url": "https://www.facebook.com/groups/2403721346759140/",
                "source_type": "group",
                "discovery_origin": "joined",
                "ai_source_score": 76,
                "status": "auto_active",
                "last_seen_at": "2026-04-02T00:00:00+00:00",
                "last_crawled_at": "",
            },
            {
                "label": "Việt Nguyễn AI",
                "url": "https://www.facebook.com/vietnguyenai/",
                "source_type": "profile",
                "discovery_origin": "followed",
                "ai_source_score": 61,
                "status": "candidate",
                "last_seen_at": "2026-04-02T00:00:00+00:00",
                "last_crawled_at": "",
            },
        ]

        registry = _resolve_facebook_source_registry(
            {
                "runtime_config": {
                    "enable_facebook_discovery": True,
                    "facebook_discovery_refresh_hours": 0,
                    "facebook_discovery_max_active_sources": 12,
                }
            },
            headless=True,
        )

        self.assertEqual(len(registry["auto_active_sources"]), 2)
        self.assertEqual(registry["auto_active_sources"][0]["discovery_origin"], "manual")
        self.assertEqual(registry["auto_active_sources"][1]["label"], "OpenClaw VN")
        self.assertEqual(len(registry["candidate_sources"]), 1)
        self.assertEqual(registry["candidate_sources"][0]["label"], "Việt Nguyễn AI")

    @patch("digest.workflow.nodes.delivery_judge.grok_delivery_enabled", return_value=False)
    @patch("digest.workflow.nodes.delivery_judge.run_json_inference")
    def test_delivery_judge_keeps_generic_github_articles_out_of_main_brief(self, mock_judge, *_mocks) -> None:
        mock_judge.return_value = {
            "groundedness_score": 4,
            "freshness_score": 4,
            "operator_value_score": 4,
            "decision": "include",
            "rationale": "Đủ mạnh để đưa vào brief.",
        }

        result = delivery_judge_node(
            {
                "final_articles": [
                    {
                        "title": "Anthropic ships new enterprise admin controls",
                        "source": "RSS: TechCrunch",
                        "source_domain": "techcrunch.com",
                        "source_kind": "strong_media",
                        "primary_type": "Product",
                        "total_score": 78,
                        "relevance_level": "High",
                        "tags": ["product_update", "enterprise_ai", "api_platform"],
                        "content_available": True,
                        "source_verified": True,
                        "source_tier": "a",
                        "is_ai_relevant": True,
                        "event_is_primary": True,
                        "freshness_unknown": False,
                        "is_stale_candidate": False,
                        "is_old_news": False,
                    },
                    {
                        "title": "openai/openai-agents-python",
                        "source": "GitHub API Repo: openai/openai-agents-python",
                        "source_domain": "github.com",
                        "primary_type": "Product",
                        "total_score": 66,
                        "content_available": True,
                        "source_verified": True,
                        "source_tier": "a",
                        "is_ai_relevant": True,
                        "event_is_primary": True,
                        "freshness_unknown": False,
                        "is_stale_candidate": False,
                        "is_old_news": False,
                        "github_signal_type": "repository",
                        "github_full_name": "openai/openai-agents-python",
                        "github_stars": 1200,
                    },
                ]
            }
        )

        self.assertEqual(len(result["telegram_candidates"]), 1)
        self.assertEqual(result["telegram_candidates"][0]["source_domain"], "techcrunch.com")
        github_article = next(item for item in result["final_articles"] if item.get("source_domain") == "github.com")
        self.assertEqual(github_article["delivery_lane_candidate"], "github")

    @patch("digest.workflow.nodes.delivery_judge.grok_delivery_enabled", return_value=False)
    @patch("digest.workflow.nodes.delivery_judge.run_json_inference")
    def test_delivery_judge_keeps_x_discovered_github_repo_out_of_main_brief(self, mock_judge, *_mocks) -> None:
        mock_judge.return_value = {
            "groundedness_score": 4,
            "freshness_score": 4,
            "operator_value_score": 4,
            "decision": "include",
            "rationale": "Đủ mạnh để đưa vào brief.",
        }

        result = delivery_judge_node(
            {
                "final_articles": [
                    {
                        "title": "Anthropic ships new enterprise admin controls",
                        "source": "RSS: TechCrunch",
                        "source_domain": "techcrunch.com",
                        "source_kind": "strong_media",
                        "primary_type": "Product",
                        "total_score": 78,
                        "relevance_level": "High",
                        "tags": ["product_update", "enterprise_ai", "api_platform"],
                        "content_available": True,
                        "source_verified": True,
                        "source_tier": "a",
                        "is_ai_relevant": True,
                        "event_is_primary": True,
                        "freshness_unknown": False,
                        "is_stale_candidate": False,
                        "is_old_news": False,
                    },
                    {
                        "title": "New MCP repo just launched",
                        "url": "https://github.com/example/new-mcp-repo",
                        "source": "Grok X Scout: builder-posts | @OpenAIDevs",
                        "source_domain": "github.com",
                        "primary_type": "Practical",
                        "total_score": 63,
                        "content_available": True,
                        "source_verified": True,
                        "source_tier": "b",
                        "is_ai_relevant": True,
                        "event_is_primary": True,
                        "freshness_unknown": False,
                        "is_stale_candidate": False,
                        "is_old_news": False,
                        "social_signal": False,
                        "grok_x_scout": True,
                        "x_post_url": "https://x.com/OpenAIDevs/status/123",
                    },
                ]
            }
        )

        self.assertEqual(len(result["telegram_candidates"]), 1)
        self.assertEqual(result["telegram_candidates"][0]["source_domain"], "techcrunch.com")
        github_article = next(item for item in result["final_articles"] if item.get("source_domain") == "github.com")
        self.assertEqual(github_article["delivery_lane_candidate"], "github")
        self.assertEqual(github_article["source"], "Grok X Scout: builder-posts | @OpenAIDevs")

    @patch("digest.workflow.nodes.delivery_judge.grok_delivery_enabled", return_value=False)
    @patch("digest.workflow.nodes.delivery_judge.run_json_inference")
    def test_delivery_judge_treats_facebook_articles_as_main_candidates_when_strong_enough(self, mock_judge, *_mocks) -> None:
        mock_judge.return_value = {
            "groundedness_score": 4,
            "freshness_score": 4,
            "operator_value_score": 4,
            "decision": "include",
            "rationale": "Đủ mạnh để đưa vào brief.",
        }

        result = delivery_judge_node(
            {
                "final_articles": [
                    {
                        "title": "Founder chia sẻ workflow Claude Code + MCP trong group",
                        "url": "https://www.facebook.com/groups/example/posts/123456789/",
                        "source": "Social Signal: Facebook | AI Engineer Vietnam",
                        "source_domain": "facebook.com",
                        "primary_type": "Practical",
                        "total_score": 42,
                        "content_available": True,
                        "content": "Nhóm này đang dùng Claude Code, MCP và memory để chạy workflow nội bộ.",
                        "source_verified": False,
                        "source_tier": "c",
                        "is_ai_relevant": True,
                        "event_is_primary": True,
                        "freshness_unknown": False,
                        "is_stale_candidate": False,
                        "is_old_news": False,
                        "social_signal": True,
                        "social_platform": "facebook",
                        "community_reactions": "Nhiều comment xác nhận workflow hữu ích.",
                        "project_fit": "High",
                    },
                ]
            }
        )

        self.assertEqual(len(result["telegram_candidates"]), 1)
        self.assertEqual(result["telegram_candidates"][0]["source_domain"], "facebook.com")
        self.assertNotIn("facebook_topic_candidates", result)

    @patch("digest.workflow.nodes.delivery_judge.grok_final_editor_enabled", return_value=False)
    @patch("digest.workflow.nodes.delivery_judge.grok_delivery_enabled", return_value=False)
    @patch("digest.workflow.nodes.delivery_judge.run_json_inference")
    def test_delivery_judge_skips_facebook_promo_post(self, mock_judge, *_mocks) -> None:
        mock_judge.return_value = {}

        result = delivery_judge_node(
            {
                "runtime_config": {
                    "facebook_max_post_age_hours": 72,
                    "facebook_review_max_post_age_hours": 168,
                },
                "final_articles": [
                    {
                        "title": "Webinar miễn phí về AI agents cho người mới bắt đầu",
                        "url": "https://www.facebook.com/groups/example/posts/111/",
                        "source": "Facebook Auto | Example Group",
                        "source_domain": "facebook.com",
                        "primary_type": "Practical",
                        "total_score": 52,
                        "content_available": True,
                        "content": "Đăng ký webinar miễn phí để học dùng AI agents.",
                        "source_verified": False,
                        "source_tier": "c",
                        "is_ai_relevant": True,
                        "event_is_primary": True,
                        "freshness_unknown": False,
                        "is_stale_candidate": False,
                        "is_old_news": False,
                        "social_platform": "facebook",
                        "facebook_content_style": "promo",
                        "facebook_boss_style_score": 34,
                        "facebook_authority_score": 52,
                        "post_age_hours": 6,
                    },
                ]
            }
        )

        self.assertNotIn("facebook_topic_candidates", result)
        self.assertEqual(result["telegram_candidates"], [])
        self.assertEqual(result["final_articles"][0]["delivery_skip_reason"], "promo")

    @patch("digest.workflow.nodes.delivery_judge.grok_final_editor_enabled", return_value=False)
    @patch("digest.workflow.nodes.delivery_judge.grok_delivery_enabled", return_value=False)
    @patch("digest.workflow.nodes.delivery_judge.run_json_inference")
    def test_delivery_judge_includes_fresh_facebook_benchmark_post(self, mock_judge, *_mocks) -> None:
        mock_judge.return_value = {
            "groundedness_score": 4,
            "freshness_score": 4,
            "operator_value_score": 4,
            "decision": "include",
            "rationale": "Benchmark này đủ mạnh để đưa vào brief.",
        }

        result = delivery_judge_node(
            {
                "runtime_config": {
                    "facebook_max_post_age_hours": 72,
                    "facebook_review_max_post_age_hours": 168,
                },
                "final_articles": [
                    {
                        "title": "MiniMax 2.7 vs Claude Opus 4.6: tiết kiệm 90% chi phí?",
                        "url": "https://www.facebook.com/groups/example/posts/222/",
                        "source": "Facebook Auto | Nghiện AI | Trương Minh Toàn",
                        "source_domain": "facebook.com",
                        "primary_type": "Product",
                        "total_score": 46,
                        "content_available": True,
                        "content": (
                            "Kilo Code benchmark hai model qua 3 bài test TypeScript, bug fixing và phân tích codebase. "
                            "Claude Opus 4.6 đạt 33/35 còn MiniMax M2.7 đạt 29/35 với chi phí thấp hơn nhiều."
                        ),
                        "source_verified": False,
                        "source_tier": "c",
                        "is_ai_relevant": True,
                        "event_is_primary": True,
                        "freshness_unknown": False,
                        "is_stale_candidate": False,
                        "is_old_news": False,
                        "social_platform": "facebook",
                        "facebook_content_style": "benchmark",
                        "facebook_boss_style_score": 88,
                        "facebook_authority_score": 76,
                        "facebook_sort_mode": "newest",
                        "post_age_hours": 14,
                        "project_fit": "High",
                    },
                ]
            }
        )

        self.assertEqual(len(result["telegram_candidates"]), 1)
        self.assertEqual(result["telegram_candidates"][0]["delivery_decision"], "include")
        self.assertNotIn("facebook_topic_candidates", result)

    @patch("digest.workflow.nodes.delivery_judge.grok_delivery_enabled", return_value=False)
    @patch("digest.workflow.nodes.delivery_judge.run_json_inference")
    def test_delivery_judge_diversifies_main_candidates_by_type(self, mock_judge, *_mocks) -> None:
        mock_judge.return_value = {
            "groundedness_score": 4,
            "freshness_score": 4,
            "operator_value_score": 4,
            "decision": "include",
            "rationale": "Đủ mạnh để đưa vào brief.",
        }
        result = delivery_judge_node(
            {
                "final_articles": [
                    {
                        "title": "Product A",
                        "source": "RSS: Example",
                        "source_domain": "example.com",
                        "source_kind": "strong_media",
                        "primary_type": "Product",
                        "total_score": 82,
                        "relevance_level": "High",
                        "tags": ["api_platform", "enterprise_ai", "product_update"],
                        "content_available": True,
                        "source_verified": True,
                        "source_tier": "a",
                        "is_ai_relevant": True,
                        "event_is_primary": True,
                        "freshness_unknown": False,
                        "is_stale_candidate": False,
                        "is_old_news": False,
                        "delivery_decision": "include",
                    },
                    {
                        "title": "Product B",
                        "source": "RSS: Example",
                        "source_domain": "example.com",
                        "source_kind": "strong_media",
                        "primary_type": "Product",
                        "total_score": 79,
                        "relevance_level": "High",
                        "tags": ["api_platform", "enterprise_ai", "product_update"],
                        "content_available": True,
                        "source_verified": True,
                        "source_tier": "a",
                        "is_ai_relevant": True,
                        "event_is_primary": True,
                        "freshness_unknown": False,
                        "is_stale_candidate": False,
                        "is_old_news": False,
                        "delivery_decision": "include",
                    },
                    {
                        "title": "Business A",
                        "source": "RSS: Example",
                        "source_domain": "example.com",
                        "source_kind": "strong_media",
                        "primary_type": "Practical",
                        "total_score": 74,
                        "relevance_level": "High",
                        "tags": ["developer_tools", "ai_agents", "enterprise_ai"],
                        "content_available": True,
                        "source_verified": True,
                        "source_tier": "a",
                        "is_ai_relevant": True,
                        "event_is_primary": True,
                        "freshness_unknown": False,
                        "is_stale_candidate": False,
                        "is_old_news": False,
                        "delivery_decision": "include",
                    },
                ]
            }
        )

        candidate_types = [article["primary_type"] for article in result["telegram_candidates"]]
        self.assertEqual(candidate_types[:2], ["Product", "Product"])

    @patch("digest.workflow.nodes.delivery_judge.grok_delivery_max_articles", return_value=3)
    @patch("digest.workflow.nodes.delivery_judge.rerank_delivery_articles")
    @patch("digest.workflow.nodes.delivery_judge.grok_delivery_enabled", return_value=True)
    @patch("digest.workflow.nodes.delivery_judge.grok_final_editor_enabled", return_value=False)
    @patch("digest.workflow.nodes.delivery_judge.run_json_inference")
    def test_delivery_judge_grok_reranks_main_brief(
        self,
        mock_local_judge,
        _mock_final_editor_enabled,
        _mock_grok_enabled,
        mock_grok_rerank,
        _mock_grok_limit,
    ) -> None:
        mock_local_judge.return_value = {
            "groundedness_score": 4,
            "freshness_score": 4,
            "operator_value_score": 4,
            "decision": "include",
            "rationale": "Đủ mạnh để đưa vào brief.",
        }
        mock_grok_rerank.return_value = {
            "https://example.com/a": {
                "decision": "skip",
                "priority_score": 12,
                "lane_override": "keep",
                "rationale": "Không đủ founder-grade signal.",
            },
            "https://example.com/b": {
                "decision": "include",
                "priority_score": 91,
                "lane_override": "Business",
                "rationale": "Đáng lên main brief sáng nay.",
            },
        }

        result = delivery_judge_node(
            {
                "runtime_config": {},
                "final_articles": [
                    {
                        "title": "Article A",
                        "url": "https://example.com/a",
                        "source": "RSS: Example",
                        "source_domain": "example.com",
                        "primary_type": "Product",
                        "total_score": 80,
                        "content_available": True,
                        "source_verified": True,
                        "source_tier": "a",
                        "is_ai_relevant": True,
                        "event_is_primary": True,
                        "freshness_unknown": False,
                        "is_stale_candidate": False,
                        "is_old_news": False,
                    },
                    {
                        "title": "Startup B raises new funding for AI workflow platform",
                        "url": "https://example.com/b",
                        "source": "RSS: Example",
                        "source_domain": "example.com",
                        "primary_type": "Product",
                        "tags": ["funding", "enterprise_ai"],
                        "total_score": 75,
                        "content_available": True,
                        "source_verified": True,
                        "source_tier": "a",
                        "is_ai_relevant": True,
                        "event_is_primary": True,
                        "freshness_unknown": False,
                        "is_stale_candidate": False,
                        "is_old_news": False,
                    },
                ],
            }
        )

        article_a = next(article for article in result["final_articles"] if article["url"] == "https://example.com/a")
        article_b = next(article for article in result["final_articles"] if article["url"] == "https://example.com/b")
        self.assertEqual(article_b["primary_type"], "Product")
        self.assertEqual(article_b["grok_priority_score"], 91)
        self.assertTrue(article_a["grok_rerank_applied"])
        self.assertTrue(article_b["grok_rerank_applied"])
        self.assertEqual(article_a["grok_rerank_delta"]["decision_after"], "skip")
        self.assertEqual(article_b["grok_rerank_delta"]["decision_after"], "include")

    @patch("digest.workflow.nodes.delivery_judge.grok_delivery_max_articles", return_value=2)
    @patch("digest.workflow.nodes.delivery_judge.rerank_delivery_articles", return_value={})
    @patch("digest.workflow.nodes.delivery_judge.grok_delivery_enabled", return_value=True)
    @patch("digest.workflow.nodes.delivery_judge.grok_final_editor_enabled", return_value=False)
    @patch("digest.workflow.nodes.delivery_judge.run_json_inference")
    def test_delivery_judge_grok_rerank_only_sees_final_shortlist(
        self,
        mock_local_judge,
        _mock_final_editor_enabled,
        _mock_grok_enabled,
        mock_grok_rerank,
        _mock_grok_limit,
    ) -> None:
        mock_local_judge.return_value = {
            "groundedness_score": 4,
            "freshness_score": 4,
            "operator_value_score": 4,
            "decision": "include",
            "rationale": "Đủ mạnh để đưa vào brief.",
        }

        result = delivery_judge_node(
            {
                "runtime_config": {"use_grok_for_delivery_rerank": True},
                "final_articles": [
                    {
                        "title": f"Article {index}",
                        "url": f"https://example.com/{index}",
                        "source": "RSS: Example",
                        "source_domain": "example.com",
                        "primary_type": "Product",
                        "total_score": 85 - index,
                        "content_available": True,
                        "source_verified": True,
                        "source_tier": "a",
                        "is_ai_relevant": True,
                        "event_is_primary": True,
                        "freshness_unknown": False,
                        "is_stale_candidate": False,
                        "is_old_news": False,
                    }
                    for index in range(4)
                ],
            }
        )

        grok_shortlist = mock_grok_rerank.call_args.args[0]
        self.assertEqual(len(grok_shortlist), 2)
        self.assertEqual(result["grok_stage_usage"]["delivery_rerank"]["shortlist_size"], 2)
        self.assertTrue(all(not article["grok_rerank_applied"] for article in result["final_articles"]))

    @patch("digest.workflow.nodes.delivery_judge.grok_delivery_max_articles", return_value=2)
    @patch("digest.workflow.nodes.delivery_judge.rerank_delivery_articles", return_value={})
    @patch("digest.workflow.nodes.delivery_judge.grok_delivery_enabled", return_value=True)
    @patch("digest.workflow.nodes.delivery_judge.grok_final_editor_enabled", return_value=False)
    @patch("digest.workflow.nodes.delivery_judge.run_json_inference")
    def test_delivery_judge_falls_back_to_local_when_grok_rerank_returns_empty(
        self,
        mock_local_judge,
        _mock_final_editor_enabled,
        _mock_grok_enabled,
        _mock_grok_rerank,
        _mock_grok_limit,
    ) -> None:
        mock_local_judge.return_value = {
            "groundedness_score": 4,
            "freshness_score": 4,
            "operator_value_score": 4,
            "decision": "include",
            "rationale": "Đủ mạnh để đưa vào brief.",
        }

        result = delivery_judge_node(
            {
                "runtime_config": {"use_grok_for_delivery_rerank": True},
                "final_articles": [
                    {
                        "title": "Fallback article",
                        "url": "https://example.com/fallback",
                        "source": "RSS: Example",
                        "source_domain": "example.com",
                        "primary_type": "Product",
                        "total_score": 82,
                        "content_available": True,
                        "source_verified": True,
                        "source_tier": "a",
                        "is_ai_relevant": True,
                        "event_is_primary": True,
                        "freshness_unknown": False,
                        "is_stale_candidate": False,
                        "is_old_news": False,
                    }
                ],
            }
        )

        self.assertFalse(result["final_articles"][0]["grok_rerank_applied"])
        self.assertEqual(result["grok_stage_usage"]["delivery_rerank"]["fallback_count"], 1)

    @patch("digest.workflow.nodes.delivery_judge.grok_delivery_max_articles", return_value=3)
    @patch("digest.workflow.nodes.delivery_judge.rerank_delivery_articles")
    @patch("digest.workflow.nodes.delivery_judge.grok_delivery_enabled", return_value=True)
    @patch("digest.workflow.nodes.delivery_judge.grok_final_editor_enabled", return_value=False)
    @patch("digest.workflow.nodes.delivery_judge.run_json_inference")
    def test_delivery_judge_rejects_grok_lane_override_without_strong_support(
        self,
        mock_local_judge,
        _mock_final_editor_enabled,
        _mock_grok_enabled,
        mock_grok_rerank,
        _mock_grok_limit,
    ) -> None:
        mock_local_judge.return_value = {
            "groundedness_score": 4,
            "freshness_score": 4,
            "operator_value_score": 4,
            "decision": "include",
            "rationale": "Đủ mạnh để đưa vào brief.",
        }
        mock_grok_rerank.return_value = {
            "https://example.com/security-incident": {
                "decision": "include",
                "priority_score": 88,
                "lane_override": "Practical",
                "rationale": "Bài đáng theo dõi, nhưng lane override này nên bị chặn.",
            },
        }

        result = delivery_judge_node(
            {
                "runtime_config": {},
                "final_articles": [
                    {
                        "title": "Mercor says it was hit by cyberattack tied to compromise of open-source LiteLLM project",
                        "url": "https://example.com/security-incident",
                        "source": "RSS: TechCrunch",
                        "source_domain": "techcrunch.com",
                        "primary_type": "Business",
                        "tags": ["safety"],
                        "total_score": 78,
                        "content_available": True,
                        "source_verified": True,
                        "source_tier": "a",
                        "is_ai_relevant": True,
                        "event_is_primary": True,
                        "freshness_unknown": False,
                        "is_stale_candidate": False,
                        "is_old_news": False,
                    },
                ],
            }
        )

        self.assertEqual(len(result["telegram_candidates"]), 1)
        self.assertEqual(result["telegram_candidates"][0]["primary_type"], "Product")
        self.assertEqual(
            result["telegram_candidates"][0]["grok_primary_type_override_rejected"],
            "Practical",
        )

    @patch("digest.workflow.nodes.delivery_judge.grok_final_editor_max_articles", return_value=3)
    @patch("digest.workflow.nodes.delivery_judge.rerank_final_digest_articles")
    @patch("digest.workflow.nodes.delivery_judge.grok_final_editor_enabled", return_value=True)
    @patch("digest.workflow.nodes.delivery_judge.grok_delivery_enabled", return_value=False)
    @patch("digest.workflow.nodes.delivery_judge.run_json_inference")
    def test_delivery_judge_grok_final_editor_scores_selected_main_articles(
        self,
        mock_local_judge,
        _mock_delivery_enabled,
        _mock_final_enabled,
        mock_final_rerank,
        _mock_final_limit,
    ) -> None:
        mock_local_judge.return_value = {
            "groundedness_score": 4,
            "freshness_score": 4,
            "operator_value_score": 4,
            "decision": "include",
            "rationale": "Đủ mạnh để đưa vào brief.",
        }
        mock_final_rerank.return_value = {
            "https://example.com/b": {
                "rank_score": 95,
                "rationale": "Nên đứng trước vì founder-grade signal rõ hơn.",
            },
            "https://example.com/a": {
                "rank_score": 72,
                "rationale": "Vẫn đáng lên, nhưng nên sau bài B.",
            },
        }

        result = delivery_judge_node(
            {
                "runtime_config": {},
                "final_articles": [
                    {
                        "title": "Article A",
                        "url": "https://example.com/a",
                        "source": "RSS: Example",
                        "source_domain": "example.com",
                        "primary_type": "Product",
                        "total_score": 79,
                        "content_available": True,
                        "source_verified": True,
                        "source_tier": "a",
                        "is_ai_relevant": True,
                        "event_is_primary": True,
                        "freshness_unknown": False,
                        "is_stale_candidate": False,
                        "is_old_news": False,
                    },
                    {
                        "title": "Article B",
                        "url": "https://example.com/b",
                        "source": "RSS: Example",
                        "source_domain": "example.com",
                        "primary_type": "Product",
                        "total_score": 82,
                        "content_available": True,
                        "source_verified": True,
                        "source_tier": "a",
                        "is_ai_relevant": True,
                        "event_is_primary": True,
                        "freshness_unknown": False,
                        "is_stale_candidate": False,
                        "is_old_news": False,
                    },
                ],
            }
        )

        candidate_by_url = {
            article["url"]: article
            for article in result["telegram_candidates"]
        }
        self.assertEqual(candidate_by_url["https://example.com/b"]["grok_final_rank_score"], 95)
        self.assertEqual(candidate_by_url["https://example.com/a"]["grok_final_rank_score"], 72)

    @patch("digest.workflow.nodes.summarize_vn.get_history", return_value=[])
    def test_summarize_vn_publish_hides_empty_lanes_when_main_candidates_exist(self, _mock_history) -> None:
        result = summarize_vn_node(
            {
                "run_mode": "publish",
                "telegram_candidates": [
                    {
                        "title": "One product update",
                        "primary_type": "Product",
                        "primary_emoji": "🚀",
                        "note_summary_vi": "Ý chính của tin này là: có cập nhật sản phẩm mới cho team AI.",
                        "total_score": 71,
                        "source": "RSS: Example",
                        "source_domain": "example.com",
                        "content_available": True,
                        "source_verified": True,
                        "source_tier": "a",
                        "delivery_decision": "include",
                    }
                ],
                "notion_pages": [],
            }
        )

        self.assertEqual(len(result["telegram_messages"]), 1)
        self.assertTrue(any("🚀 Product" in msg for msg in result["telegram_messages"]))
        self.assertFalse(any("Lane này hôm nay hơi yên" in msg for msg in result["telegram_messages"]))

    @patch("digest.workflow.nodes.summarize_vn.rewrite_news_blurbs")
    @patch("digest.workflow.nodes.summarize_vn.grok_news_copy_enabled", return_value=True)
    @patch("digest.workflow.nodes.summarize_vn.get_history", return_value=[])
    def test_summarize_vn_prefers_grok_news_copy_for_selected_articles(
        self,
        _mock_history,
        _mock_enabled,
        mock_rewrite,
    ) -> None:
        mock_rewrite.return_value = {
            "https://example.com/product": {
                "blurb": "Công ty đã phát hành bản cập nhật sản phẩm mới, bổ sung khả năng tự động hóa tác vụ cho nhóm vận hành AI.",
            }
        }

        state = {
            "run_mode": "publish",
            "runtime_config": {"enable_grok_news_copy": True},
            "telegram_candidates": [
                {
                    "title": "One product update",
                    "url": "https://example.com/product",
                    "primary_type": "Product",
                    "primary_emoji": "🚀",
                    "note_summary_vi": "Có cập nhật sản phẩm mới cho team AI.",
                    "total_score": 71,
                    "source": "RSS: Example",
                    "source_domain": "example.com",
                    "content_available": True,
                    "source_verified": True,
                    "source_tier": "a",
                    "delivery_decision": "include",
                }
            ],
            "notion_pages": [],
        }
        result = summarize_vn_node(state)

        product_message = next(message for message in result["telegram_messages"] if "🚀 Product" in message)
        self.assertIn("bổ sung khả năng tự động hóa tác vụ", product_message)
        self.assertTrue(state["telegram_candidates"][0]["grok_polish_applied"])
        self.assertEqual(state["telegram_candidates"][0]["copy_source_used"], "grok_polish")
        self.assertEqual(result["grok_stage_usage"]["final_polish"]["polished_count"], 1)

    @patch("digest.workflow.nodes.summarize_vn.grok_news_copy_enabled", return_value=False)
    @patch("digest.workflow.nodes.summarize_vn.get_history", return_value=[])
    def test_summarize_vn_uses_structured_copy_when_grok_disabled(self, _mock_history, _mock_grok_enabled) -> None:
        state = {
            "run_mode": "publish",
            "telegram_candidates": [
                {
                    "title": "One product update",
                    "url": "https://example.com/product",
                    "primary_type": "Product",
                    "primary_emoji": "🚀",
                    "factual_summary_vi": "OpenAI vừa cập nhật sản phẩm kiểm soát vận hành AI.",
                    "why_it_matters_vi": "Điểm đáng chú ý là có thêm bước approval loop giúp đội vận hành giảm lỗi.",
                    "optional_editorial_angle": "Bài này giúp nhóm triển khai workflow AI.",
                    "total_score": 74,
                    "source": "RSS: Example",
                    "source_domain": "example.com",
                    "content_available": True,
                    "source_verified": True,
                    "source_tier": "a",
                    "delivery_decision": "include",
                }
            ],
            "notion_pages": [],
        }
        result = summarize_vn_node(state)

        product_message = next(message for message in result["telegram_messages"] if "🚀 Product" in message)
        self.assertIn("OpenAI vừa cập nhật sản phẩm kiểm soát vận hành AI", product_message)
        self.assertIn("approval loop", product_message)
        self.assertFalse(state["telegram_candidates"][0]["grok_polish_applied"])
        self.assertEqual(state["telegram_candidates"][0]["copy_source_used"], "structured_local")

    @patch("digest.workflow.nodes.summarize_vn.rewrite_news_blurbs", side_effect=RuntimeError("grok_down"))
    @patch("digest.workflow.nodes.summarize_vn.grok_news_copy_enabled", return_value=True)
    @patch("digest.workflow.nodes.summarize_vn.get_history", return_value=[])
    def test_summarize_vn_falls_back_to_structured_copy_when_grok_fails(
        self,
        _mock_history,
        _mock_enabled,
        _mock_rewrite,
    ) -> None:
        del _mock_rewrite
        del _mock_enabled
        state = {
            "run_mode": "publish",
            "runtime_config": {"enable_grok_news_copy": True},
            "telegram_candidates": [
                {
                    "title": "Local-first rollout update",
                    "url": "https://example.com/local-first",
                    "primary_type": "Product",
                    "primary_emoji": "🚀",
                    "factual_summary_vi": "OpenAI cập nhật mới cho triển khai on-device.",
                    "why_it_matters_vi": "Điểm đáng chú ý là bài viết cho thấy cách giảm chi phí vận hành.",
                    "total_score": 76,
                    "source": "RSS: Example",
                    "source_domain": "example.com",
                    "content_available": True,
                    "source_verified": True,
                    "source_tier": "a",
                    "delivery_decision": "include",
                }
            ],
            "notion_pages": [],
        }
        result = summarize_vn_node(state)

        product_message = next(message for message in result["telegram_messages"] if "🚀 Product" in message)
        self.assertIn("OpenAI cập nhật mới cho triển khai on-device", product_message)
        self.assertIn("giảm chi phí vận hành", product_message)
        self.assertFalse(state["telegram_candidates"][0]["grok_polish_applied"])
        self.assertEqual(state["telegram_candidates"][0]["copy_source_used"], "structured_local_fallback")
        self.assertEqual(result["grok_stage_usage"]["final_polish"]["fallback_count"], 1)

    def test_build_telegram_copy_from_structured_is_concise_and_coherent(self) -> None:
        message = build_telegram_copy_from_structured(
            {
                "factual_summary_vi": "Đây là tin cập nhật release cho tool automation mới cho pipeline operations.",
                "why_it_matters_vi": "Điểm đáng chú ý là có cơ chế orchestration giúp giảm thao tác thủ công và giảm rủi ro.",
                "optional_editorial_angle": "Rất phù hợp cho đội product đang vận hành workflow nhiều bước.",
            },
            max_len=260,
        )

        self.assertIn("release cho tool automation", message)
        self.assertIn("giảm thao tác thủ công", message)
        self.assertTrue(message.endswith("."))
        self.assertNotIn("\n", message)
        self.assertLessEqual(len(message), 260)

    def test_sanitize_delivery_text_removes_opinion_leakage(self) -> None:
        cleaned = sanitize_delivery_text(
            "Bài này có tín hiệu quan trọng cho đội vận hành. Tuy nhiên, bạn nên theo dõi thêm trước khi làm gì đó."
        )
        self.assertNotIn("theo dõi thêm", cleaned)
        self.assertNotIn("nên theo dõi", cleaned.lower())

    def test_build_safe_digest_messages_keeps_backward_compat_for_legacy_note_summary(self) -> None:
        messages = build_safe_digest_messages(
            [
                {
                    "title": "Legacy note summary",
                    "url": "https://example.com/legacy",
                    "primary_type": "Product",
                    "note_summary_vi": "Có tín hiệu mới về automation workflow trong sản phẩm.",
                    "source": "RSS: Example",
                    "source_domain": "example.com",
                    "total_score": 81,
                    "delivery_score": 15,
                    "delivery_decision": "include",
                }
            ],
            [],
            today=date(2026, 4, 2),
            allow_archive_replay=False,
        )

        product_message = next(message for message in messages if "🚀 Product" in message)
        self.assertIn("automation workflow", product_message)

    def test_get_notion_parent_and_properties_supports_data_source_schema(self) -> None:
        class _FakeDatabases:
            def retrieve(self, database_id: str) -> dict:
                return {
                    "object": "database",
                    "id": database_id,
                    "data_sources": [{"id": "ds_123", "name": "News Database"}],
                }

        class _FakeDataSources:
            def retrieve(self, data_source_id: str) -> dict:
                self.last_id = data_source_id
                return {
                    "object": "data_source",
                    "id": data_source_id,
                    "properties": {
                        "Name": {"type": "title"},
                        "Link gốc": {"type": "url"},
                        "Summarize": {"type": "rich_text"},
                    },
                }

        class _FakeNotion:
            def __init__(self) -> None:
                self.databases = _FakeDatabases()
                self.data_sources = _FakeDataSources()

        parent, properties = _get_notion_parent_and_properties(_FakeNotion(), "db_123")

        self.assertEqual(parent, {"data_source_id": "ds_123", "database_id": "db_123"})
        self.assertIn("Link gốc", properties)
        self.assertEqual(properties["Summarize"]["type"], "rich_text")

    def test_create_notion_page_with_fallback_retries_database_parent(self) -> None:
        class _FakeNotion:
            def __init__(self) -> None:
                self.calls = []

            class pages:
                pass

        fake = _FakeNotion()

        def _fake_create(notion, parent, article, database_properties):
            fake.calls.append(parent)
            if parent.get("data_source_id"):
                return None
            return "https://www.notion.so/page"

        with patch("digest.workflow.nodes.save_notion._create_notion_page", side_effect=_fake_create):
            url = _create_notion_page_with_fallback(
                fake,
                {"data_source_id": "ds_123", "database_id": "db_123"},
                "db_123",
                {"title": "Example", "primary_emoji": "🚀", "primary_type": "Product"},
                {"Name": {"type": "title"}},
            )

        self.assertEqual(url, "https://www.notion.so/page")
        self.assertEqual(fake.calls[0], {"data_source_id": "ds_123", "database_id": "db_123"})
        self.assertEqual(fake.calls[1], {"database_id": "db_123"})

    def test_build_formatted_rich_text_strips_markdown_headings(self) -> None:
        rich_text = _build_formatted_rich_text(
            "### 💡 Ý tưởng chính\nTạo hệ thống theo dõi cơ hội.\n\n### 📋 Bước thực hiện\n1. Thu thập nguồn\n2. Gửi thông báo"
        )

        plain = "".join(item["text"]["content"] for item in rich_text)

        self.assertIn("💡 Ý tưởng chính", plain)
        self.assertIn("📋 Bước thực hiện", plain)
        self.assertNotIn("###", plain)
        self.assertTrue(any(item.get("annotations", {}).get("bold") for item in rich_text))

    def test_build_article_grounding_high_confidence(self) -> None:
        article = {
            "title": "Reuters says model release expands enterprise usage",
            "source": "RSS: Reuters",
            "source_domain": "reuters.com",
            "published_at": "2026-03-26T01:00:00+00:00",
            "content_available": True,
            "source_verified": True,
            "source_tier": "a",
            "total_score": 78,
            "community_reactions": "[News] Enterprises discuss rollout impact",
            "related_past": [{"title": "Older launch"}],
        }

        grounding = build_article_grounding(article)

        self.assertEqual(grounding["confidence_label"], "high")
        self.assertIn("Nguồn đang dùng để đọc tin", grounding["fact_anchors_text"])
        self.assertIn("diễn biến tiếp nối", grounding["reasonable_inferences_text"])
        self.assertEqual(grounding["unknowns"], [])

    def test_build_article_grounding_low_confidence(self) -> None:
        article = {
            "title": "Category page only",
            "source": "DuckDuckGo (EN)",
            "source_domain": "random-blog.example",
            "content_available": False,
            "source_verified": False,
            "source_tier": "c",
            "total_score": 12,
            "community_reactions": "",
        }

        grounding = build_article_grounding(article)

        self.assertEqual(grounding["confidence_label"], "low")
        self.assertGreaterEqual(len(grounding["unknowns"]), 2)
        self.assertIn("tier C", grounding["caution_flags_text"])

    def test_build_safe_digest_contains_header_and_link(self) -> None:
        articles = [{
            "title": "Anthropic adds enterprise controls",
            "note_summary_vi": "Ý chính của tin này là: doanh nghiệp có thêm công cụ quản trị để triển khai AI an toàn hơn.",
            "total_score": 72,
            "primary_type": "Product",
            "primary_emoji": "🚀",
            "source": "RSS: TechCrunch",
            "source_domain": "techcrunch.com",
            "published_at": "2026-03-26T02:00:00+00:00",
            "content_available": True,
            "source_verified": True,
            "source_tier": "a",
            "community_reactions": "[News] buyers compare security posture",
        }]
        notion_pages = [{"title": "Anthropic adds enterprise controls", "url": "https://notion.so/example"}]

        digest = build_safe_digest(articles, notion_pages, today=date(2026, 3, 26))

        self.assertIn("| 26/03", digest)
        self.assertIn('<a href="https://notion.so/example">Đọc thêm</a>', digest)
        self.assertNotIn("[Đọc thêm](", digest)
        self.assertIn("🚀 Product | 26/03", digest)
        self.assertNotIn("Sáng nay có", digest)
        self.assertNotIn("Điểm 72/100", digest)
        self.assertNotIn("Độ chắc", digest)

    def test_build_safe_digest_messages_keeps_archive_replay_professional(self) -> None:
        history_articles = [{
            "title": "Gemini adds workspace import",
            "url": "https://example.com/gemini-import",
            "primary_type": "Product",
            "summary": "Ý chính của tin này là: Google giúp người dùng chuyển dữ liệu chatbot sang Gemini.",
            "source": "RSS: Example",
            "relevance_score": 78,
            "created_at": "2026-03-26T01:00:00+00:00",
        }]

        messages = build_safe_digest_messages([], [], history_articles=history_articles, today=date(2026, 3, 26))

        self.assertEqual(len(messages), 3)
        product_message = next(message for message in messages if "🚀 Product" in message)
        self.assertIn("Gemini adds workspace import", product_message)
        self.assertIn("(26/03/2026)", product_message)
        self.assertNotIn("brief 8h sáng", product_message)
        self.assertNotIn("nhắc lại", product_message.lower())

    def test_build_safe_digest_messages_archive_dedup_by_title(self) -> None:
        # Regression: archive replay trước đây có thể chèn 2 entry trùng title
        # (cùng section_type) nếu chúng đến từ history_articles khác nhau.
        history_articles = [
            {
                "title": "AI overly affirms users asking for personal advice",
                "url": "https://reddit.com/r/artificial/a",
                "primary_type": "Product",
                "summary": "Ý chính của tin này là: x",
                "source": "RSS: Example",
                "relevance_score": 80,
                "created_at": "2026-03-26T01:00:00+00:00",
            },
            {
                "title": "AI overly affirms users asking for personal advice",
                "url": "https://reddit.com/r/OpenAI/b",
                "primary_type": "Product",
                "summary": "Ý chính của tin này là: y",
                "source": "RSS: Example",
                "relevance_score": 79,
                "created_at": "2026-03-26T02:00:00+00:00",
            },
        ]

        messages = build_safe_digest_messages([], [], history_articles=history_articles, today=date(2026, 3, 26))

        product_message = next(message for message in messages if "🚀 Product" in message)
        title_occurrences = product_message.lower().count("ai overly affirms users asking for personal advice")
        self.assertEqual(title_occurrences, 1)

    def test_build_safe_digest_messages_dedup_same_story_different_headlines(self) -> None:
        # Cùng sự kiện, hai URL/title khác nhau (headline ngắn vs dài) — chỉ giữ một bullet Product.
        articles = [
            {
                "title": "OpenAI acquires TBPN",
                "url": "https://openai.com/index/openai-acquires-tbpn",
                "primary_type": "Product",
                "note_summary_vi": "OpenAI mua TBPN để mở rộng đối thoại AI.",
                "source": "RSS: OpenAI",
                "source_domain": "openai.com",
                "total_score": 72,
                "delivery_score": 12,
                "delivery_decision": "include",
                "grok_priority_score": 82,
                "is_ai_relevant": True,
            },
            {
                "title": "OpenAI acquires TBPN, the buzzy founder-led business talk show",
                "url": "https://example.com/tbpn-long-headline",
                "primary_type": "Product",
                "note_summary_vi": "Bản dài hơn về deal TBPN và show trên YouTube.",
                "source": "RSS: Example",
                "source_domain": "example.com",
                "total_score": 70,
                "delivery_score": 11,
                "delivery_decision": "include",
                "grok_priority_score": 78,
                "is_ai_relevant": True,
            },
        ]
        messages = build_safe_digest_messages(
            articles,
            [],
            today=date(2026, 4, 3),
            per_type=3,
            allow_archive_replay=False,
        )
        product_message = next(message for message in messages if "🚀 Product" in message)
        self.assertIn("OpenAI acquires TBPN", product_message)
        self.assertNotIn("founder-led", product_message.lower())

    def test_articles_same_event_matches_short_headline_inside_long(self) -> None:
        short_a = {"title": "OpenAI acquires TBPN"}
        long_b = {"title": "OpenAI acquires TBPN, the buzzy founder-led business talk show"}
        self.assertTrue(_articles_same_event(short_a, long_b))
        self.assertTrue(_articles_same_event(long_b, short_a))

    def test_build_safe_digest_messages_allows_high_priority_overflow(self) -> None:
        articles = [
            {
                "title": f"Product item {idx}",
                "url": f"https://example.com/p{idx}",
                "primary_type": "Product",
                "note_summary_vi": f"Tin product {idx} cho founder.",
                "source": "RSS: Example",
                "source_domain": "example.com",
                "total_score": 80 - idx,
                "delivery_score": 14,
                "delivery_decision": "include",
                "grok_priority_score": 70 if idx < 4 else 95,
            }
            for idx in range(1, 5)
        ]

        messages = build_safe_digest_messages(
            articles,
            [],
            today=date(2026, 4, 1),
            allow_archive_replay=False,
            allow_high_priority_overflow=True,
        )

        product_message = next(message for message in messages if "🚀 Product" in message)
        self.assertIn("Product item 4", product_message)

    def test_build_safe_digest_messages_prefers_grok_final_editor_order_within_section(self) -> None:
        articles = [
            {
                "title": "Anthropic adds admin controls",
                "url": "https://example.com/p1",
                "primary_type": "Product",
                "note_summary_vi": "Anthropic thêm admin controls cho enterprise.",
                "source": "RSS: Example",
                "source_domain": "example.com",
                "total_score": 88,
                "delivery_score": 15,
                "delivery_decision": "include",
                "grok_priority_score": 70,
                "grok_final_rank_score": 72,
            },
            {
                "title": "OpenAI ships agent memory tools",
                "url": "https://example.com/p2",
                "primary_type": "Product",
                "note_summary_vi": "OpenAI phát hành agent memory tools cho developer.",
                "source": "RSS: Example",
                "source_domain": "example.com",
                "total_score": 81,
                "delivery_score": 14,
                "delivery_decision": "include",
                "grok_priority_score": 65,
                "grok_final_rank_score": 96,
            },
        ]

        messages = build_safe_digest_messages(
            articles,
            [],
            today=date(2026, 4, 1),
            allow_archive_replay=False,
        )

        product_message = next(message for message in messages if "🚀 Product" in message)
        self.assertLess(product_message.find("OpenAI ships agent memory tools"), product_message.find("Anthropic adds admin controls"))

    def test_build_safe_digest_messages_prefers_telegram_blurb_over_note_summary(self) -> None:
        messages = build_safe_digest_messages(
            [
                {
                    "title": "Anthropic adds enterprise controls",
                    "url": "https://example.com/anthropic-controls",
                    "primary_type": "Product",
                    "primary_emoji": "🚀",
                    "telegram_blurb_vi": "Anthropic đã bổ sung lớp kiểm soát mới cho triển khai AI trong doanh nghiệp.",
                    "note_summary_vi": "Tin này hiện phù hợp để theo dõi thêm.",
                    "source": "RSS: Example",
                    "source_domain": "example.com",
                    "total_score": 82,
                    "delivery_score": 14,
                    "delivery_decision": "include",
                }
            ],
            [],
            today=date(2026, 4, 2),
            allow_archive_replay=False,
        )

        product_message = next(message for message in messages if "🚀 Product" in message)
        self.assertIn("Anthropic đã bổ sung lớp kiểm soát mới", product_message)
        self.assertNotIn("phù hợp để theo dõi thêm", product_message)

    def test_sanitize_delivery_text_removes_internal_copy_and_dangling_endings(self) -> None:
        cleaned = sanitize_delivery_text(
            "Ý chính của tin này là: Bài này hiện được giữ ở lớp sàng lọc sơ bộ để ưu tiên tài nguyên cho nhóm tin mới và mạnh hơn. "
            "Điểm đáng chú ý nhất là điểm đáng quan tâm là khả năng tự lưu trữ, nhưng"
        )

        self.assertNotIn("lớp sàng lọc sơ bộ", cleaned)
        self.assertNotIn("slot suy luận", cleaned)
        self.assertNotIn("điểm đáng quan tâm là", cleaned.lower())
        self.assertTrue(cleaned.endswith("."))

    def test_sanitize_delivery_text_removes_recommendation_boilerplate_and_cjk(self) -> None:
        cleaned = sanitize_delivery_text(
            "Ý chính của tin này là: StepFun 3.5 Flash đạt hiệu quả chi phí cao hơn trên bài test OpenClaw. "
            "Tuy nhiên, nguồn tin chưa được xác thực và dữ liệu yếu, chỉ nên theo dõi thêm. "
            "请继续关注后续信息。"
        )

        self.assertIn("StepFun 3.5 Flash", cleaned)
        self.assertNotIn("nguồn tin chưa được xác thực", cleaned.lower())
        self.assertNotIn("chỉ nên theo dõi", cleaned.lower())
        self.assertNotRegex(cleaned, r"[\u4e00-\u9fff]")

    def test_validate_telegram_summary_catches_markdown_and_bad_links(self) -> None:
        articles = [{
            "title": "Strong source article",
            "note_summary_vi": "Ý chính của tin này là: đây là tin quan trọng cho team.",
            "total_score": 65,
            "source": "RSS: Reuters",
            "source_domain": "reuters.com",
            "published_at": "2026-03-26T03:00:00+00:00",
            "content_available": True,
            "source_verified": True,
            "source_tier": "a",
        }]
        notion_pages = [{"title": "Strong source article", "url": "https://notion.so/good"}]
        summary = (
            "<b>🚀 Product | 26/03</b>\n\n"
            "[Đọc thêm](https://bad.example)\n\n"
            '<a href="https://bad.example">Đọc thêm</a>'
        )

        warnings = validate_telegram_summary(
            summary,
            articles,
            notion_pages,
            today=date(2026, 3, 26),
        )

        self.assertIn("markdown_links_present", warnings)
        self.assertIn("unknown_links_present", warnings)

    def test_validate_telegram_messages_accepts_six_type_messages(self) -> None:
        articles = [{
            "title": "Strong source article",
            "note_summary_vi": "Ý chính của tin này là: đây là tin quan trọng cho team.",
            "total_score": 65,
            "primary_type": "Business",
            "source": "RSS: Reuters",
            "source_domain": "reuters.com",
            "url": "https://example.com/strong-source",
            "published_at": "2026-03-26T03:00:00+00:00",
            "content_available": True,
            "source_verified": True,
            "source_tier": "a",
        }]
        messages = build_safe_digest_messages(articles, [], today=date(2026, 3, 26))

        warnings = validate_telegram_messages(messages, articles, [], today=date(2026, 3, 26))

        self.assertEqual(warnings, [])

    def test_validate_telegram_summary_accepts_html_escaped_source_link(self) -> None:
        articles = [{
            "title": "Strong source article",
            "note_summary_vi": "Ý chính của tin này là: đây là tin quan trọng cho team.",
            "total_score": 65,
            "primary_type": "Business",
            "source": "RSS: Reuters",
            "source_domain": "reuters.com",
            "url": "https://example.com/strong-source?ref=mail&day=1",
            "published_at": "2026-03-26T03:00:00+00:00",
            "content_available": True,
            "source_verified": True,
            "source_tier": "a",
        }]
        summary = (
            "<b>🚀 Product | 26/03</b>\n\n"
            "<b>Strong source article</b>\n"
            "Đây là tin quan trọng cho team.\n"
            '<a href="https://example.com/strong-source?ref=mail&amp;day=1">Đọc thêm</a>'
        )

        warnings = validate_telegram_summary(
            summary,
            articles,
            [],
            today=date(2026, 3, 26),
        )

        self.assertNotIn("unknown_links_present", warnings)

    def test_build_weekly_memo_summarizes_history_into_exec_view(self) -> None:
        memo = build_weekly_memo(
            [
                {
                    "title": "OpenAI ships enterprise admin controls",
                    "source": "RSS: OpenAI",
                    "primary_type": "Product",
                    "summary": "OpenAI thêm admin controls mới cho doanh nghiệp.",
                    "relevance_score": 88,
                },
                {
                    "title": "Claude Code workflow grows with MCP memory",
                    "source": "Reddit r/ChatGPT",
                    "primary_type": "Practical",
                    "summary": "Community chia sẻ workflow mới với Claude Code và MCP.",
                    "relevance_score": 79,
                },
            ],
            days=7,
            today=date(2026, 4, 5),
        )

        self.assertIn("Weekly AI Memo (30/03 - 05/04/2026)", memo)
        self.assertIn("## Top Signals", memo)
        self.assertIn("[Product] OpenAI ships enterprise admin controls", memo)
        self.assertIn("## Suggested Actions", memo)

    def test_prefilter_primary_type_routes_security_incident_to_society_lane(self) -> None:
        primary_type, primary_emoji = _prefilter_primary_type(
            "Mercor says it was hit by cyberattack tied to compromise of open-source LiteLLM project"
        )

        self.assertEqual(primary_type, "Society & Culture")
        self.assertEqual(primary_emoji, "🌍")

    def test_build_run_report_markdown_contains_source_breakdown(self) -> None:
        state = {
            "run_mode": "preview",
            "raw_articles": [
                {"source": "RSS: TechCrunch", "source_domain": "techcrunch.com"},
                {"source": "Hacker News API", "source_domain": "news.ycombinator.com"},
            ],
            "new_articles": [{"source": "RSS: TechCrunch"}],
            "scored_articles": [
                {
                    "source": "RSS: TechCrunch",
                    "source_domain": "techcrunch.com",
                    "primary_type": "Product",
                    "tags": ["product_update", "api_platform"],
                },
                {
                    "source": "Hacker News API",
                    "source_domain": "news.ycombinator.com",
                    "primary_type": "Research",
                    "tags": ["research"],
                },
            ],
            "top_articles": [{"title": "Top article", "primary_type": "Product", "total_score": 81, "freshness_status": "fresh_boost"}],
            "final_articles": [],
            "telegram_candidates": [{"title": "Telegram article", "primary_type": "Product", "total_score": 81, "delivery_score": 13, "source_domain": "techcrunch.com"}],
            "notion_pages": [{"title": "Telegram article", "url": "https://notion.so/example"}],
            "summary_mode": "deterministic_sections",
            "summary_warnings": [],
            "telegram_sent": True,
            "gather_snapshot_path": "reports/temporal_snapshots/20260402_084400_gather.json",
            "scored_snapshot_path": "reports/temporal_snapshots/20260402_084400_scored.json",
        }

        markdown = _build_run_report_markdown(state, datetime(2026, 3, 27, tzinfo=timezone.utc))

        self.assertIn("## Raw By Source", markdown)
        self.assertIn("RSS: TechCrunch", markdown)
        self.assertIn("## Telegram Candidates", markdown)
        self.assertIn("Telegram article", markdown)
        self.assertIn("## Scored By Tag", markdown)
        self.assertIn("product_update", markdown)
        self.assertIn("## Run Health", markdown)
        self.assertIn("## Temporal Snapshots", markdown)
        self.assertIn("20260402_084400_gather.json", markdown)
        self.assertIn("Publish ready", markdown)
        self.assertIn("- Run mode: preview", markdown)

    def test_build_run_report_markdown_includes_score_breakdown_and_skip_reasons(self) -> None:
        state = {
            "run_mode": "preview",
            "run_profile": "fast",
            "raw_articles": [{"title": "A", "source": "RSS: Example", "source_kind": "official"}],
            "new_articles": [{"title": "A"}],
            "scored_articles": [
                {
                    "title": "Strong article",
                    "primary_type": "Product",
                    "total_score": 71,
                    "prefilter_score": 42,
                    "source_kind": "official",
                    "prefilter_reasons": ["tier:a+10", "watchlist_hit+2"],
                    "score_breakdown": {
                        "prefilter_score": 42,
                        "source_kind": "official",
                        "why_surfaced": ["tier:a+10", "watchlist_hit+2"],
                        "base_total_score": 55,
                        "adjusted_total_score": 71,
                        "score_adjustment_total": 16,
                        "applied_adjustments": [
                            {"kind": "freshness", "reason": "fresh_boost", "delta": 5, "before": 55, "after": 60},
                            {
                                "kind": "event_consensus",
                                "reason": "event_consensus_bonus",
                                "delta": 6,
                                "before": 60,
                                "after": 66,
                            },
                            {
                                "kind": "source_history",
                                "reason": "source_history_adjustment",
                                "delta": 5,
                                "before": 66,
                                "after": 71,
                            },
                        ],
                    },
                    "c1_score": 20,
                    "c2_score": 18,
                    "c3_score": 17,
                    "delivery_rationale": "Đủ mạnh để đưa vào brief.",
                }
            ],
            "top_articles": [{"title": "Strong article", "total_score": 71}],
            "low_score_articles": [
                {
                    "title": "Skipped article",
                    "primary_type": "Society",
                    "total_score": 12,
                    "prefilter_reasons": ["blocked_domain-30"],
                    "score_breakdown": {"why_skipped": ["blocked_domain-30"]},
                }
            ],
            "final_articles": [],
            "telegram_candidates": [{"title": "Strong article", "primary_type": "Product", "total_score": 71, "delivery_score": 13, "delivery_rationale": "Đủ mạnh để đưa vào brief."}],
            "notion_pages": [],
        }

        markdown = _build_run_report_markdown(state, datetime(2026, 3, 27, tzinfo=timezone.utc))

        self.assertIn("## Score Breakdown", markdown)
        self.assertIn("## Why Skipped", markdown)
        self.assertIn("why=tier:a+10", markdown)
        self.assertIn("base=55 adjusted=71 delta=+16", markdown)
        self.assertIn("adjustments=fresh_boost+5", markdown)
        self.assertIn("Đủ mạnh để đưa vào brief", markdown)

    def test_finalize_scored_article_tracks_adjusted_score_debug_fields(self) -> None:
        article = {
            "title": "Fresh official article",
            "primary_type": "Product",
            "primary_emoji": "🚀",
            "source_kind": "official",
            "source_domain": "openai.com",
            "source_tier": "a",
            "source_verified": True,
            "content_available": True,
            "freshness_bucket": "fresh",
            "age_hours": 12,
            "is_ai_relevant": True,
            "analysis_tier": "basic",
            "c1_score": 20,
            "c1_reason": "Strong source.",
            "c2_score": 18,
            "c2_reason": "Useful for builders.",
            "c3_score": 17,
            "c3_reason": "Fits current workflow focus.",
            "total_score": 55,
        }

        _apply_freshness_penalty(article, min_score=60)
        _finalize_scored_article(article, min_score=60)

        self.assertEqual(article["base_total_score"], 55)
        self.assertEqual(article["adjusted_total_score"], 60)
        self.assertEqual(article["score_adjustment_total"], 5)
        self.assertTrue(article["applied_adjustments"])
        self.assertEqual(article["applied_adjustments"][0]["reason"], "fresh_boost")
        self.assertEqual(article["score_breakdown"]["base_total_score"], 55)
        self.assertEqual(article["score_breakdown"]["adjusted_total_score"], 60)

    def test_build_run_report_markdown_includes_grok_source_gap_section(self) -> None:
        state = {
            "run_mode": "preview",
            "raw_articles": [{"title": "A", "source": "RSS: Example", "source_domain": "example.com"}],
            "new_articles": [{"title": "A"}],
            "scored_articles": [],
            "top_articles": [],
            "low_score_articles": [],
            "final_articles": [],
            "telegram_candidates": [],
            "notion_pages": [],
            "grok_source_gap_batch_note": "Thiếu tín hiệu official từ các vendor model lớn.",
            "grok_source_gap_suggestions": [
                {
                    "focus": "OpenAI enterprise updates",
                    "priority": "high",
                    "suggested_query": "OpenAI enterprise agent launch",
                    "suggested_feed_hint": "OpenAI newsroom/blog",
                    "rationale": "Batch có tín hiệu mạnh từ nguồn thứ cấp nhưng thiếu nguồn official tương ứng.",
                }
            ],
        }

        markdown = _build_run_report_markdown(state, datetime(2026, 3, 27, tzinfo=timezone.utc))

        self.assertIn("## Grok Source Gap Suggestions", markdown)
        self.assertIn("OpenAI enterprise updates", markdown)
        self.assertIn("OpenAI newsroom/blog", markdown)

    def test_build_run_report_markdown_includes_performance_section(self) -> None:
        state = {
            "run_mode": "preview",
            "raw_articles": [{"title": "A", "source": "RSS: Example", "source_domain": "example.com"}],
            "new_articles": [{"title": "A"}],
            "filtered_articles": [{"title": "A", "content": "x" * 1400, "snippet": "short"}],
            "scored_articles": [{"title": "A"} for _ in range(12)],
            "top_articles": [
                {
                    "title": "Top article",
                    "content": "x" * 2400,
                    "factual_summary_vi": "Structured summary",
                    "why_it_matters_vi": "Structured why",
                }
            ],
            "telegram_candidates": [{"title": "Main 1"}, {"title": "Main 2"}],
            "stage_timings": [
                {"stage": "gather", "duration_ms": 120.0, "input_count": 0, "output_count": 24},
                {"stage": "batch_classify_and_score", "duration_ms": 1800.0, "input_count": 20, "output_count": 12},
                {"stage": "batch_deep_process", "duration_ms": 2200.0, "input_count": 3, "output_count": 3},
                {"stage": "batch_deep_process", "duration_ms": 2100.0, "input_count": 2, "output_count": 2},
                {"stage": "send_telegram", "duration_ms": 420.0, "input_count": 2, "output_count": 2},
            ],
        }
        state["performance_report"] = summarize_stage_timings(list(state["stage_timings"]), state)

        markdown = _build_run_report_markdown(state, datetime(2026, 3, 27, tzinfo=timezone.utc))

        self.assertIn("## Performance", markdown)
        self.assertIn("### Slowest stages", markdown)
        self.assertIn("batch_deep_process", markdown)
        self.assertIn("Token-waste hotspots", markdown)
        self.assertIn("Future parallelization opportunities", markdown)

    def test_build_run_report_markdown_includes_grok_usage_section(self) -> None:
        state = {
            "run_mode": "preview",
            "raw_articles": [{"title": "A", "source": "RSS: Example", "source_domain": "example.com"}],
            "new_articles": [{"title": "A"}],
            "scored_articles": [
                {
                    "title": "A",
                    "primary_type": "Product",
                    "classify_provider_used": "local_then_grok",
                    "prefilter_score": 70,
                    "c1_score": 20,
                    "c2_score": 18,
                    "c3_score": 17,
                    "base_total_score": 55,
                    "adjusted_total_score": 55,
                    "score_breakdown": {"why_surfaced": ["strong source"]},
                }
            ],
            "top_articles": [],
            "low_score_articles": [],
            "final_articles": [],
            "telegram_candidates": [],
            "notion_pages": [],
            "grok_stage_usage": {
                "classify": {
                    "enabled": True,
                    "request_count": 2,
                    "success_count": 1,
                    "fallback_count": 1,
                    "items_processed": 2,
                    "local_failure_count": 1,
                    "grok_rescue_count": 1,
                    "provider_local_then_grok_count": 1,
                },
                "delivery_rerank": {
                    "enabled": True,
                    "request_count": 1,
                    "success_count": 1,
                    "fallback_count": 0,
                    "items_processed": 6,
                    "shortlist_size": 6,
                    "applied": True,
                },
            },
            "grok_request_count": 3,
            "grok_success_count": 2,
            "grok_fallback_count": 1,
            "grok_items_processed": 8,
        }

        markdown = _build_run_report_markdown(state, datetime(2026, 3, 27, tzinfo=timezone.utc))

        self.assertIn("## Grok Usage", markdown)
        self.assertIn("classify: enabled=True requests=2 success=1 fallback=1 items=2", markdown)
        self.assertIn("delivery_rerank: enabled=True requests=1 success=1 fallback=0 items=6", markdown)
        self.assertIn("classify_provider=local_then_grok", markdown)

    def test_build_run_report_markdown_includes_facebook_discovery_and_skip_breakdown(self) -> None:
        state = {
            "run_mode": "preview",
            "raw_articles": [
                {
                    "title": "MiniMax benchmark",
                    "source": "Facebook Auto | Nghiện AI",
                    "source_domain": "facebook.com",
                    "social_platform": "facebook",
                    "facebook_sort_mode": "newest",
                }
            ],
            "new_articles": [],
            "scored_articles": [],
            "top_articles": [],
            "low_score_articles": [],
            "final_articles": [
                {
                    "title": "Promo webinar",
                    "source": "Facebook Auto | Example",
                    "source_domain": "facebook.com",
                    "social_platform": "facebook",
                    "delivery_decision": "skip",
                    "facebook_topic_skip_reason": "promo",
                }
            ],
            "telegram_candidates": [],
            "notion_pages": [],
        }

        markdown = _build_run_report_markdown(state, datetime(2026, 4, 2, tzinfo=timezone.utc))

        self.assertIn("Facebook Skip Reasons", markdown)
        self.assertIn("promo", markdown)

    def test_assess_run_health_flags_safe_fallback_batch(self) -> None:
        health = assess_run_health(
            {
                "raw_articles": [{"title": "x"}],
                "scored_articles": [{"title": "x", "source_domain": "github.com", "source_tier": "c"}],
                "telegram_candidates": [],
                "summary_mode": "safe_fallback",
                "summary_warnings": ["msg1:unknown_links_present"],
            }
        )

        self.assertEqual(health["status"], "red")
        self.assertFalse(health["publish_ready"])
        self.assertTrue(health["issues"])

    def test_build_initial_state_preview_disables_publish_flags(self) -> None:
        state = build_initial_state("preview")

        self.assertEqual(state["run_mode"], "preview")
        self.assertFalse(state["publish_notion"])
        self.assertFalse(state["publish_telegram"])
        self.assertFalse(state["persist_local"])

    @patch("digest.workflow.nodes.generate_run_report.generate_run_report_node", return_value={"run_report_path": "reports/mock.md"})
    @patch("digest.workflow.nodes.quality_gate.quality_gate_node", return_value={"summary_mode": "safe"})
    @patch("digest.workflow.nodes.summarize_vn.summarize_vn_node", return_value={"summary_vn": "summary", "telegram_messages": ["msg"]})
    @patch("digest.workflow.nodes.save_notion.save_notion_node", return_value={"notion_pages": [{"title": "Article", "url": "https://notion.so/page"}]})
    def test_publish_notion_only_from_preview_state_keeps_preview_for_later_approve(
        self,
        _mock_save_notion,
        _mock_summarize,
        _mock_quality_gate,
        _mock_report,
    ) -> None:
        preview_state = {
            "run_mode": "preview",
            "final_articles": [{"title": "Article"}],
            "telegram_messages": ["msg"],
            "notion_pages": [],
        }

        result, summary = publish_notion_only_from_preview_state(preview_state)

        self.assertEqual(result["preview_publish_state"], "notion_only_published")
        self.assertEqual(result["run_profile"], "preview_notion_only")
        self.assertEqual(summary["notion_count"], 1)
        self.assertFalse(summary["telegram_sent"])
        self.assertEqual(result["notion_pages"][0]["url"], "https://notion.so/page")

    @patch("digest.workflow.nodes.generate_run_report.generate_run_report_node", return_value={"run_report_path": "reports/mock.md"})
    @patch("digest.workflow.nodes.send_telegram.send_telegram_node", return_value={"telegram_sent": True})
    @patch("digest.workflow.nodes.quality_gate.quality_gate_node", return_value={"summary_mode": "safe"})
    @patch("digest.workflow.nodes.summarize_vn.summarize_vn_node", return_value={"summary_vn": "summary", "telegram_messages": ["msg"]})
    @patch("digest.workflow.nodes.save_notion.save_notion_node")
    def test_publish_from_preview_state_skips_duplicate_notion_after_notion_only(
        self,
        mock_save_notion,
        _mock_summarize,
        _mock_quality_gate,
        _mock_send_telegram,
        _mock_report,
    ) -> None:
        preview_state = {
            "run_mode": "publish",
            "preview_publish_state": "notion_only_published",
            "final_articles": [{"title": "Article"}],
            "telegram_messages": ["msg"],
            "notion_pages": [{"title": "Article", "url": "https://notion.so/page"}],
        }

        result, summary = publish_from_preview_state(preview_state)

        mock_save_notion.assert_not_called()
        self.assertEqual(result["run_profile"], "approved_preview_telegram_only")
        self.assertEqual(summary["notion_count"], 1)
        self.assertTrue(summary["telegram_sent"])

    def test_fast_preset_runtime_config_disables_slow_paths(self) -> None:
        config = apply_runtime_preset(
            "fast",
            {
                "min_deep_analysis_score": 60,
                "max_classify_articles": 8,
                "max_deep_analysis_articles": 10,
                "gather_rss_hours": 72,
                "github_max_org_repos": 4,
                "github_max_search_results": 4,
                "enable_rss": True,
                "enable_ddg": True,
                "enable_hn": True,
                "enable_reddit": True,
                "enable_watchlist": True,
                "enable_telegram_channels": True,
            },
        )

        self.assertGreaterEqual(config["max_deep_analysis_articles"], 1)
        self.assertLessEqual(config["max_deep_analysis_articles"], 2)
        self.assertGreaterEqual(config["max_classify_articles"], 6)
        self.assertLessEqual(config["max_classify_articles"], 8)
        self.assertLessEqual(config["github_max_watchlist_repos"], 6)
        self.assertLessEqual(config["github_max_orgs"], 4)
        self.assertLessEqual(config["github_max_queries"], 4)
        self.assertEqual(config["github_max_org_repos"], 1)
        self.assertEqual(config["github_max_search_results"], 1)
        self.assertFalse(config["enable_ddg"])
        self.assertFalse(config["enable_hn"])
        self.assertFalse(config["enable_reddit"])
        self.assertFalse(config["enable_telegram_channels"])
        self.assertTrue(config["skip_feedback_sync"])

    @patch(
        "digest.runtime.runtime_presets.os.getenv",
        side_effect=lambda key, default="": {"MLX_FAST_MODEL": "mlx-community/Qwen2.5-14B-Instruct-4bit-fast"}.get(
            key, default
        ),
    )
    def test_fast_preset_prefers_env_model(self, _mock_getenv) -> None:
        config = apply_runtime_preset(
            "fast",
            {"runtime_mlx_model": "mlx-community/Qwen2.5-32B-Instruct-4bit"},
        )

        self.assertEqual(config["runtime_mlx_model"], "mlx-community/Qwen2.5-14B-Instruct-4bit-fast")

    @patch(
        "digest.runtime.runtime_presets.os.getenv",
        side_effect=lambda key, default="": {"MLX_FAST_MODEL": ""}.get(key, default),
    )
    def test_fast_preset_preserves_user_runtime_model(self, _mock_getenv) -> None:
        config = apply_runtime_preset(
            "fast",
            {"runtime_mlx_model": "mlx-community/Qwen2.5-32B-Instruct-4bit"},
        )

        self.assertEqual(config["runtime_mlx_model"], "mlx-community/Qwen2.5-32B-Instruct-4bit")

    def test_grok_smart_preset_expands_grok_budget_without_replacing_core_pipeline(self) -> None:
        config = apply_runtime_preset(
            "grok_smart",
            {
                "max_classify_articles": 10,
                "max_deep_analysis_articles": 4,
                "gather_rss_hours": 72,
                "github_max_watchlist_repos": 6,
                "github_max_orgs": 4,
                "github_max_queries": 4,
                "github_max_org_repos": 4,
                "github_max_search_results": 4,
                "classify_content_char_limit": 900,
                "classify_max_tokens": 320,
            },
        )

        self.assertGreaterEqual(config["max_classify_articles"], 12)
        self.assertGreaterEqual(config["max_deep_analysis_articles"], 5)
        self.assertTrue(config["enable_rss"])
        self.assertTrue(config["enable_github"])
        self.assertTrue(config["enable_watchlist"])
        self.assertTrue(config["enable_ddg"])
        self.assertTrue(config["enable_hn"])
        self.assertTrue(config["enable_reddit"])
        self.assertTrue(config["enable_telegram_channels"])
        self.assertFalse(config["use_grok_for_classify"])
        self.assertEqual(config["grok_classify_mode"], "retry")
        self.assertTrue(config["use_grok_for_delivery_rerank"])
        self.assertTrue(config["enable_grok_delivery_judge"])
        self.assertTrue(config["enable_grok_prefilter"])
        self.assertTrue(config["enable_grok_final_editor"])
        self.assertTrue(config["use_grok_for_final_polish"])
        self.assertTrue(config["enable_grok_news_copy"])
        self.assertFalse(config["enable_grok_facebook_score"])
        self.assertTrue(config["use_grok_for_source_gap"])
        self.assertTrue(config["enable_grok_source_gap"])
        self.assertTrue(config["use_grok_for_scout"])
        self.assertTrue(config["enable_grok_scout"])
        self.assertTrue(config["enable_grok_x_scout"])
        self.assertLessEqual(config["grok_scout_max_queries"], 3)
        self.assertLessEqual(config["grok_x_scout_max_queries"], 3)
        self.assertIsInstance(config["grok_x_scout_allowed_handles"], list)
        self.assertGreaterEqual(len(config["grok_x_scout_allowed_handles"]), 8)

    def test_normalize_source_assigns_source_kind_for_official_and_community(self) -> None:
        normalized = normalize_source_node(
            {
                "raw_articles": [
                    {
                        "title": "Anthropic release notes",
                        "source": "RSS: Anthropic",
                        "url": "https://www.anthropic.com/news/claude-code-release-notes",
                    },
                    {
                        "title": "HN thread on agent workflows",
                        "source": "Hacker News API",
                        "url": "https://news.ycombinator.com/item?id=123",
                        "content": "AI agent workflows and practical usage",
                    },
                ]
            }
        )["raw_articles"]

        self.assertEqual(normalized[0]["source_kind"], "official")
        self.assertEqual(normalized[1]["source_kind"], "community")
        self.assertGreaterEqual(normalized[0]["source_priority"], 90)
        self.assertGreaterEqual(normalized[1]["community_signal_strength"], 3)

    @patch("digest.workflow.nodes.gather_news.requests.get")
    def test_fetch_hacker_news_uses_algolia_api_and_maps_hits(self, mock_get) -> None:
        class _Response:
            status_code = 200

            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, Any]:
                return {
                    "hits": [
                        {
                            "objectID": "12345",
                            "title": "OpenAI ships new agent workflow tools",
                            "url": "https://openai.com/index/new-agent-workflow-tools/",
                            "points": 180,
                            "num_comments": 41,
                            "created_at": "2026-04-04T02:00:00Z",
                        },
                        {
                            "objectID": "67890",
                            "title": "Weekend math puzzle",
                            "url": "https://example.com/puzzle",
                            "points": 99,
                            "num_comments": 12,
                            "created_at": "2026-04-04T03:00:00Z",
                        },
                    ]
                }

        mock_get.return_value = _Response()

        articles = _fetch_hacker_news(limit=5)

        self.assertEqual(len(articles), 1)
        self.assertEqual(mock_get.call_args.args[0], "https://hn.algolia.com/api/v1/search")
        self.assertEqual(mock_get.call_args.kwargs["params"]["tags"], "story")
        self.assertEqual(mock_get.call_args.kwargs["params"]["hitsPerPage"], 30)
        article = articles[0]
        self.assertEqual(article["source"], "Hacker News Algolia")
        self.assertEqual(article["url"], "https://openai.com/index/new-agent-workflow-tools/")
        self.assertEqual(article["community_hint"], "https://news.ycombinator.com/item?id=12345")
        self.assertEqual(article["hn_points"], 180)
        self.assertEqual(article["hn_num_comments"], 41)
        self.assertGreaterEqual(article["community_signal_strength"], 4)

    @patch("digest.runtime.run_health.requests.head")
    def test_collect_source_health_marks_dead_and_stale_sources(self, mock_head) -> None:
        class _Response:
            def __init__(self, status_code: int) -> None:
                self.status_code = status_code

        def _fake_head(url: str, **_kwargs: Any) -> _Response:
            return _Response(503 if "dead" in url else 200)

        mock_head.side_effect = _fake_head

        with TemporaryDirectory() as tmpdir:
            session_path = Path(tmpdir) / "digest_session.session"
            session_path.write_text("session", encoding="utf-8")
            old_session = datetime.now().timestamp() - (31 * 86400)
            os.utime(session_path, (old_session, old_session))

            facebook_path = Path(tmpdir) / "facebook_storage_state.json"
            facebook_path.write_text("{}", encoding="utf-8")
            old_facebook = datetime.now().timestamp() - (8 * 86400)
            os.utime(facebook_path, (old_facebook, old_facebook))

            with patch(
                "digest.runtime.run_health.CURATED_RSS_FEEDS",
                ["https://ok.example/rss", "https://dead.example/rss"],
            ):
                with patch.dict(
                    "os.environ",
                    {
                        "TELEGRAM_CHANNELS": "aivietnam",
                        "TELETHON_API_ID": "12345",
                        "TELETHON_API_HASH": "hash",
                        "TELETHON_SESSION_NAME": str(session_path),
                        "ENABLE_FACEBOOK_AUTO": "1",
                        "FACEBOOK_STORAGE_STATE_FILE": str(facebook_path),
                    },
                    clear=False,
                ):
                    health = collect_source_health()

        self.assertEqual(health["https://ok.example/rss"], "ok")
        self.assertEqual(health["https://dead.example/rss"], "dead")
        self.assertEqual(health["telethon_session"], "stale")
        self.assertEqual(health["facebook_storage_state"], "stale")

    @patch("digest.workflow.nodes.gather_news.requests.get")
    def test_fetch_reddit_posts_filters_to_hot_recent_high_score_threads(self, mock_get) -> None:
        now_ts = datetime.now(timezone.utc).timestamp()

        class _Response:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, Any]:
                return {
                    "data": {
                        "children": [
                            {
                                "data": {
                                    "title": "Claude Code workflow for AI teams",
                                    "selftext": "Detailed workflow using Claude Code and MCP.",
                                    "permalink": "/r/ChatGPT/comments/abc123/workflow/",
                                    "url": "https://www.reddit.com/r/ChatGPT/comments/abc123/workflow/",
                                    "created_utc": now_ts - 3600,
                                    "score": 180,
                                    "num_comments": 55,
                                }
                            },
                            {
                                "data": {
                                    "title": "Old AI thread",
                                    "selftext": "Still interesting",
                                    "permalink": "/r/ChatGPT/comments/old/old/",
                                    "url": "https://www.reddit.com/r/ChatGPT/comments/old/old/",
                                    "created_utc": now_ts - 90000,
                                    "score": 500,
                                    "num_comments": 90,
                                }
                            },
                            {
                                "data": {
                                    "title": "Low score AI thread",
                                    "selftext": "Not enough traction",
                                    "permalink": "/r/ChatGPT/comments/low/low/",
                                    "url": "https://www.reddit.com/r/ChatGPT/comments/low/low/",
                                    "created_utc": now_ts - 1800,
                                    "score": 45,
                                    "num_comments": 4,
                                }
                            },
                        ]
                    }
                }

        mock_get.return_value = _Response()

        with patch.dict(
            "os.environ",
            {
                "REDDIT_CLIENT_ID": "",
                "REDDIT_CLIENT_SECRET": "",
                "REDDIT_SUBREDDITS": "ChatGPT",
            },
            clear=False,
        ):
            from digest.workflow.nodes.gather_news import _fetch_reddit_posts

            articles = _fetch_reddit_posts(limit_per_subreddit=2)

        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0]["source"], "Reddit r/ChatGPT")
        self.assertGreaterEqual(articles[0]["reddit_score"], 180)

    @patch("digest.workflow.nodes.gather_news._fetch_github_articles")
    @patch("digest.workflow.nodes.gather_news.load_watchlist_seeds")
    @patch("digest.workflow.nodes.gather_news.DEFAULT_GITHUB_SEARCH_QUERIES", ["query-a", "query-b", "query-c"])
    @patch("digest.workflow.nodes.gather_news.DEFAULT_GITHUB_ORGS", ["org-a", "org-b", "org-c"])
    @patch("digest.workflow.nodes.gather_news.DEFAULT_GITHUB_REPOS", ["repo-a/a", "repo-b/b", "repo-c/c", "repo-d/d"])
    def test_gather_news_applies_github_breadth_caps(
        self,
        mock_watchlist,
        mock_fetch_github,
    ) -> None:
        mock_watchlist.return_value = {
            "urls": [],
            "queries": [],
            "github_repos": ["repo-extra/e"],
            "github_orgs": ["org-extra"],
            "github_queries": ["query-extra"],
        }
        mock_fetch_github.return_value = []

        gather_news_node(
            {
                "runtime_config": {
                    "enable_rss": False,
                    "enable_github": True,
                    "enable_watchlist": False,
                    "enable_hn": False,
                    "enable_reddit": False,
                    "enable_ddg": False,
                    "enable_telegram_channels": False,
                    "enable_grok_scout": False,
                    "enable_grok_x_scout": False,
                    "github_max_watchlist_repos": 2,
                    "github_max_orgs": 1,
                    "github_max_queries": 1,
                }
            }
        )

        mock_fetch_github.assert_called_once()
        kwargs = mock_fetch_github.call_args.kwargs
        self.assertEqual(kwargs["repo_watchlist"], ["repo-a/a", "repo-b/b"])
        self.assertEqual(kwargs["org_watchlist"], ["org-a"])

    @patch("digest.workflow.nodes.gather_news.requests.post")
    @patch("digest.workflow.nodes.gather_news._build_facebook_auto_articles")
    @patch("digest.workflow.nodes.gather_news._resolve_facebook_source_registry")
    @patch("digest.workflow.nodes.gather_news.load_watchlist_seeds", return_value={"urls": [], "queries": [], "github_repos": [], "github_orgs": [], "github_queries": []})
    def test_gather_news_skips_facebook_auto_and_alerts_when_session_is_stale(
        self,
        _mock_watchlist,
        mock_resolve_registry,
        mock_build_facebook,
        mock_post,
    ) -> None:
        class _Response:
            status_code = 200

        mock_post.return_value = _Response()

        with TemporaryDirectory() as tmpdir:
            facebook_path = Path(tmpdir) / "facebook_storage_state.json"
            facebook_path.write_text("{}", encoding="utf-8")
            old_timestamp = datetime.now().timestamp() - (8 * 86400)
            os.utime(facebook_path, (old_timestamp, old_timestamp))

            with patch.dict(
                "os.environ",
                {
                    "FACEBOOK_STORAGE_STATE_FILE": str(facebook_path),
                    "TELEGRAM_BOT_TOKEN": "token",
                    "TELEGRAM_CHAT_ID": "-100123",
                    "TELEGRAM_THREAD_ID": "111",
                },
                clear=False,
            ):
                result = gather_news_node(
                    {
                        "run_mode": "publish",
                        "runtime_config": {
                            "enable_rss": False,
                            "enable_github": False,
                            "enable_social_signals": False,
                            "enable_watchlist": False,
                            "enable_hn": False,
                            "enable_reddit": False,
                            "enable_ddg": False,
                            "enable_telegram_channels": False,
                            "enable_facebook_auto": True,
                            "enable_grok_scout": False,
                            "enable_grok_x_scout": False,
                        }
                    }
                )

        mock_resolve_registry.assert_not_called()
        mock_build_facebook.assert_not_called()
        mock_post.assert_called_once()
        self.assertEqual(
            mock_post.call_args.kwargs["json"]["text"],
            "⚠️ Facebook session cũ hơn 7 ngày. Cần chạy scripts/facebook_login_setup.py",
        )
        self.assertNotIn("facebook_auto_active_sources", result)

    @patch("digest.workflow.nodes.gather_news._extract_full_text", return_value="OpenAI ships stronger enterprise admin and agent controls.")
    @patch.dict("os.environ", {"XAI_API_KEY": "test-xai-key"}, clear=False)
    @patch("digest.workflow.nodes.gather_news.scout_web_search_articles")
    @patch("digest.workflow.nodes.gather_news._read_telegram_channels", return_value=[])
    @patch("digest.workflow.nodes.gather_news._fetch_reddit_posts", return_value=[])
    @patch("digest.workflow.nodes.gather_news._fetch_hacker_news", return_value=[])
    @patch("digest.workflow.nodes.gather_news._build_watchlist_articles", return_value=[])
    @patch("digest.workflow.nodes.gather_news._build_social_signal_articles", return_value=[])
    @patch("digest.workflow.nodes.gather_news._fetch_github_articles", return_value=[])
    @patch("digest.workflow.nodes.gather_news._fetch_rss")
    def test_gather_news_runs_grok_scout_when_official_mix_is_weak(
        self,
        mock_fetch_rss,
        _mock_fetch_github,
        _mock_social,
        _mock_watchlist,
        _mock_hn,
        _mock_reddit,
        _mock_telegram,
        mock_scout,
        _mock_extract,
    ) -> None:
        mock_fetch_rss.return_value = [
            {
                "title": "Small AI roundup",
                "url": "https://example.com/small-roundup",
                "source": "RSS: Example",
                "snippet": "AI roundup",
                "published": "2026-04-01T00:00:00+00:00",
                "fetched_at": "2026-04-01T00:05:00+00:00",
            }
        ]
        mock_scout.return_value = {
            "articles": [
                {
                    "title": "OpenAI adds enterprise agent controls",
                    "url": "https://openai.com/index/new-enterprise-agent-controls/",
                    "published_at": "2026-04-01T02:00:00+00:00",
                    "source_domain": "openai.com",
                    "summary": "OpenAI publishes new enterprise controls for agent deployments.",
                    "why_it_matters": "Useful for operators shipping AI into production.",
                }
            ],
            "batch_note": "Official coverage was weak, so one rescue article was added.",
        }

        result = gather_news_node(
            {
                "runtime_config": {
                    "enable_github": False,
                    "enable_social_signals": False,
                    "enable_watchlist": False,
                    "enable_hn": False,
                    "enable_reddit": False,
                    "enable_ddg": False,
                    "enable_telegram_channels": False,
                    "enable_facebook_auto": False,
                    "enable_grok_scout": True,
                    "grok_scout_max_queries": 1,
                    "grok_scout_max_articles": 2,
                }
            }
        )

        self.assertEqual(result["grok_scout_count"], 1)
        self.assertTrue(any(article.get("grok_scout") for article in result["raw_articles"]))
        self.assertTrue(any(article.get("url") == "https://openai.com/index/new-enterprise-agent-controls/" for article in result["raw_articles"]))

    @patch("digest.workflow.nodes.gather_news.scout_web_search_articles")
    @patch("digest.workflow.nodes.gather_news._read_telegram_channels", return_value=[])
    @patch("digest.workflow.nodes.gather_news._fetch_reddit_posts", return_value=[])
    @patch("digest.workflow.nodes.gather_news._fetch_hacker_news", return_value=[])
    @patch("digest.workflow.nodes.gather_news._build_watchlist_articles", return_value=[])
    @patch("digest.workflow.nodes.gather_news._build_social_signal_articles", return_value=[])
    @patch("digest.workflow.nodes.gather_news._fetch_github_articles", return_value=[])
    @patch("digest.workflow.nodes.gather_news._fetch_rss")
    def test_gather_news_skips_grok_scout_when_source_mix_is_already_strong(
        self,
        mock_fetch_rss,
        _mock_fetch_github,
        _mock_social,
        _mock_watchlist,
        _mock_hn,
        _mock_reddit,
        _mock_telegram,
        mock_scout,
    ) -> None:
        mock_fetch_rss.return_value = [
            {
                "title": f"Official article {idx}",
                "url": f"https://openai.com/index/article-{idx}/",
                "source": "RSS: OpenAI News",
                "snippet": "OpenAI enterprise update",
                "published": "2026-04-01T00:00:00+00:00",
                "fetched_at": "2026-04-01T00:05:00+00:00",
            }
            for idx in range(1, 9)
        ] + [
            {
                "title": f"Strong media article {idx}",
                "url": f"https://techcrunch.com/2026/04/01/story-{idx}/",
                "source": "RSS: TechCrunch",
                "snippet": "AI business update",
                "published": "2026-04-01T00:00:00+00:00",
                "fetched_at": "2026-04-01T00:05:00+00:00",
            }
            for idx in range(1, 3)
        ]

        result = gather_news_node(
            {
                "runtime_config": {
                    "enable_github": False,
                    "enable_social_signals": False,
                    "enable_watchlist": False,
                    "enable_hn": False,
                    "enable_reddit": False,
                    "enable_ddg": False,
                    "enable_telegram_channels": False,
                    "enable_facebook_auto": False,
                    "enable_grok_scout": True,
                    "enable_grok_x_scout": False,
                    "grok_scout_min_non_github_articles": 10,
                }
            }
        )

        self.assertEqual(result["grok_scout_count"], 0)
        mock_scout.assert_not_called()

    @patch.dict("os.environ", {"XAI_API_KEY": "test-xai-key"}, clear=False)
    @patch("digest.workflow.nodes.gather_news.scout_x_posts")
    @patch("digest.workflow.nodes.gather_news._read_telegram_channels", return_value=[])
    @patch("digest.workflow.nodes.gather_news._fetch_reddit_posts", return_value=[])
    @patch("digest.workflow.nodes.gather_news._fetch_hacker_news", return_value=[])
    @patch("digest.workflow.nodes.gather_news._build_watchlist_articles", return_value=[])
    @patch("digest.workflow.nodes.gather_news._build_social_signal_articles", return_value=[])
    @patch("digest.workflow.nodes.gather_news._fetch_github_articles", return_value=[])
    @patch("digest.workflow.nodes.gather_news._fetch_rss", return_value=[])
    def test_gather_news_runs_grok_x_scout_and_keeps_linked_source_url(
        self,
        _mock_fetch_rss,
        _mock_fetch_github,
        _mock_social,
        _mock_watchlist,
        _mock_hn,
        _mock_reddit,
        _mock_telegram,
        mock_x_scout,
    ) -> None:
        mock_x_scout.return_value = {
            "posts": [
                {
                    "title": "New MCP repo just launched",
                    "post_url": "https://x.com/OpenAIDevs/status/123",
                    "linked_url": "https://github.com/example/new-mcp-repo",
                    "published_at": "2026-04-02T01:00:00+00:00",
                    "author_handle": "OpenAIDevs",
                    "summary": "Thread introduces a new MCP repo for agent workflows.",
                    "why_it_matters": "Useful for teams building agent tooling.",
                }
            ],
            "batch_note": "One useful GitHub-linked post found on X.",
        }

        with TemporaryDirectory() as tmpdir:
            result = gather_news_node(
                {
                    "runtime_config": {
                        "enable_rss": False,
                        "enable_github": False,
                        "enable_social_signals": False,
                        "enable_watchlist": False,
                        "enable_hn": False,
                        "enable_reddit": False,
                        "enable_ddg": False,
                        "enable_telegram_channels": False,
                        "enable_facebook_auto": False,
                        "enable_grok_scout": False,
                        "enable_grok_x_scout": True,
                        "grok_x_scout_max_queries": 1,
                        "grok_x_scout_max_articles": 2,
                        "temporal_snapshot_dir": tmpdir,
                    }
                }
            )

            self.assertEqual(result["grok_scout_count"], 1)
            self.assertEqual(len(result["raw_articles"]), 1)
            article = result["raw_articles"][0]
            self.assertTrue(article["grok_x_scout"])
            self.assertEqual(article["url"], "https://github.com/example/new-mcp-repo")
            self.assertEqual(article["x_post_url"], "https://x.com/OpenAIDevs/status/123")
            self.assertIn("@OpenAIDevs", article["source"])
            snapshot_path = Path(result["gather_snapshot_path"])
            self.assertTrue(snapshot_path.exists())
            payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["stage"], "gather")
            self.assertEqual(payload["article_count"], 1)

    @patch("digest.workflow.nodes.gather_news.scout_x_posts")
    @patch("digest.workflow.nodes.gather_news._read_telegram_channels", return_value=[])
    @patch("digest.workflow.nodes.gather_news._fetch_reddit_posts", return_value=[])
    @patch("digest.workflow.nodes.gather_news._fetch_hacker_news", return_value=[])
    @patch("digest.workflow.nodes.gather_news._build_watchlist_articles", return_value=[])
    @patch("digest.workflow.nodes.gather_news._build_social_signal_articles", return_value=[])
    @patch("digest.workflow.nodes.gather_news._fetch_github_articles", return_value=[])
    @patch("digest.workflow.nodes.gather_news._fetch_rss", return_value=[])
    def test_gather_news_skips_grok_x_scout_when_disabled(
        self,
        _mock_fetch_rss,
        _mock_fetch_github,
        _mock_social,
        _mock_watchlist,
        _mock_hn,
        _mock_reddit,
        _mock_telegram,
        mock_x_scout,
    ) -> None:
        result = gather_news_node(
            {
                "runtime_config": {
                    "enable_rss": False,
                    "enable_github": False,
                    "enable_social_signals": False,
                    "enable_watchlist": False,
                    "enable_hn": False,
                    "enable_reddit": False,
                    "enable_ddg": False,
                    "enable_telegram_channels": False,
                    "enable_facebook_auto": False,
                    "enable_grok_scout": False,
                    "enable_grok_x_scout": False,
                }
            }
        )

        self.assertEqual(result["grok_scout_count"], 0)
        mock_x_scout.assert_not_called()

    @patch("pipeline_runner.clear_runtime_mlx_model_path")
    @patch("pipeline_runner.set_runtime_mlx_model_path")
    @patch("digest.workflow.nodes.generate_run_report.generate_run_report_node", return_value={"run_report_path": "reports/mock.md"})
    @patch("digest.workflow.nodes.quality_gate.quality_gate_node", return_value={"summary_mode": "safe"})
    @patch("digest.workflow.nodes.summarize_vn.summarize_vn_node", return_value={"summary_vn": "summary", "telegram_messages": ["msg"]})
    @patch("digest.workflow.nodes.save_notion.save_notion_node", return_value={"notion_pages": [{"title": "Article", "url": "https://notion.so/page"}]})
    def test_publish_notion_only_state_applies_runtime_model_override(
        self,
        mock_save_notion,
        mock_summarize,
        mock_quality_gate,
        mock_report,
        mock_set_model,
        mock_clear_model,
    ) -> None:
        publish_notion_only_from_preview_state(
            {
                "run_mode": "preview",
                "runtime_config": {"runtime_mlx_model": "mlx-community/fast-local-override"},
                "final_articles": [{"title": "Article"}],
                "telegram_messages": ["msg"],
                "notion_pages": [],
            }
        )

        mock_set_model.assert_called_once_with("mlx-community/fast-local-override")
        mock_clear_model.assert_called_once()
        mock_save_notion.assert_called_once()
        mock_report.assert_called_once()
        mock_quality_gate.assert_called_once()
        mock_summarize.assert_called_once()

    def test_send_telegram_preview_mode_skips_publish(self) -> None:
        result = send_telegram_node(
            {
                "publish_telegram": False,
                "telegram_messages": ["<b>Test</b>"],
            }
        )

        self.assertFalse(result["telegram_sent"])
        self.assertNotIn("facebook_topic_sent", result)

    @patch("digest.workflow.nodes.send_telegram._send_message", return_value=True)
    def test_send_telegram_sends_main_topic_only(self, mock_send) -> None:
        with patch.dict(
            "os.environ",
            {
                "TELEGRAM_BOT_TOKEN": "token",
                "TELEGRAM_CHAT_ID": "-100123",
                "TELEGRAM_THREAD_ID": "111",
            },
            clear=False,
        ):
            result = send_telegram_node(
                {
                    "publish_telegram": True,
                    "telegram_messages": ["<b>Main</b>"],
                }
            )

        self.assertTrue(result["telegram_sent"])
        self.assertNotIn("facebook_topic_sent", result)
        self.assertEqual(mock_send.call_count, 1)
        self.assertEqual(mock_send.call_args_list[0].args[3], 111)

    def test_save_notion_preview_mode_returns_no_fake_notion_pages(self) -> None:
        result = save_notion_node(
            {
                "publish_notion": False,
                "persist_local": False,
                "final_articles": [{
                    "title": "Relevant AI article",
                    "url": "https://example.com/ai",
                    "is_ai_relevant": True,
                    "is_news_candidate": True,
                }],
            }
        )

        self.assertEqual(result["notion_pages"], [])

    def test_feedback_labels_detects_common_team_signals(self) -> None:
        labels = _feedback_labels("Tin này cũ, nguồn yếu và cần đào sâu hơn cho founder.")

        self.assertIn("stale", labels)
        self.assertIn("weak_source", labels)
        self.assertIn("want_more_depth", labels)
        self.assertIn("founder_lens", labels)

    def test_feedback_labels_supports_vietnamese_command_style(self) -> None:
        cleaned = _clean_feedback_text("@avalookbot phản hồi: sai loại business", bot_username="avalookbot")
        labels = _feedback_labels("@avalookbot phản hồi: sai loại business", bot_username="avalookbot")

        self.assertEqual(cleaned, "sai loại business")
        self.assertIn("wrong_type", labels)
        self.assertIn("expected_type:business", labels)
    
    def test_feedback_labels_detects_delivery_commands(self) -> None:
        labels = _feedback_labels("@avalookbot không nên lên brief", bot_username="avalookbot")

        self.assertIn("skip_delivery", labels)

    @patch("digest.editorial.feedback_loop.get_recent_feedback")
    def test_build_feedback_context_summarizes_recent_feedback(self, mock_recent_feedback) -> None:
        mock_recent_feedback.return_value = [
            {
                "user_name": "alice",
                "text": "Tin này cũ và không liên quan.",
                "labels_json": '["stale", "not_relevant"]',
            },
            {
                "user_name": "bob",
                "text": "Bài này hay nhưng cần đào sâu hơn.",
                "labels_json": '["good_pick", "want_more_depth"]',
            },
        ]

        context = build_feedback_context(days=14, limit=20)

        self.assertIn("Feedback gần đây từ team", context["feedback_summary_text"])
        self.assertEqual(context["feedback_label_counts"]["stale"], 1)
        self.assertEqual(context["feedback_label_counts"]["good_pick"], 1)

    @patch("digest.editorial.feedback_loop.get_recent_feedback")
    def test_build_feedback_context_derives_preference_profile(self, mock_recent_feedback) -> None:
        mock_recent_feedback.return_value = [
            {
                "text": "@avalookbot ưu tiên founder",
                "user_name": "team",
                "labels_json": json.dumps(["founder_lens", "want_more_depth"]),
                "created_at": "2026-04-05T01:00:00+00:00",
            },
            {
                "text": "@avalookbot nguồn yếu",
                "user_name": "team",
                "labels_json": json.dumps(["weak_source", "weak_source", "expected_type:product"]),
                "created_at": "2026-04-05T02:00:00+00:00",
            },
            {
                "text": "@avalookbot nên lên brief",
                "user_name": "team",
                "labels_json": json.dumps(["promote_delivery"]),
                "created_at": "2026-04-05T03:00:00+00:00",
            },
        ]

        context = build_feedback_context(days=14, limit=20)

        profile = context["feedback_preference_profile"]
        self.assertTrue(profile["strict_source_review"])
        self.assertTrue(profile["prefer_founder_angle"])
        self.assertTrue(profile["prefer_depth"])
        self.assertEqual(profile["delivery_bias"], "promote")
        self.assertIn("product", profile["preferred_types"])

    def test_build_executive_intelligence_bundle_writes_watchlist_and_topic_artifacts(self) -> None:
        history = [
            {
                "title": "OpenAI ships new agent controls",
                "url": "https://example.com/openai-agent-controls",
                "source": "RSS: OpenAI",
                "source_domain": "openai.com",
                "primary_type": "Product",
                "summary": "OpenAI cập nhật thêm agent controls cho enterprise.",
                "relevance_score": 88,
                "created_at": "2026-04-05T01:00:00+00:00",
            },
            {
                "title": "Claude Code memory workflow spreads in teams",
                "url": "https://example.com/claude-code-memory",
                "source": "Reddit r/ChatGPT",
                "source_domain": "reddit.com",
                "primary_type": "Practical",
                "summary": "Workflow Claude Code + memory đang được nhiều team thử.",
                "relevance_score": 79,
                "created_at": "2026-04-05T02:00:00+00:00",
            },
        ]

        bundle = build_executive_intelligence_bundle(history, days=14)

        self.assertTrue(str(bundle["watchlist_path"]).endswith(".md"))
        self.assertIn("Watchlist Intelligence", bundle["watchlist_markdown"])
        self.assertIsInstance(bundle["topic_page_artifacts"], list)

    def test_quality_gate_falls_back_to_safe_digest(self) -> None:
        state = {
            "final_articles": [{
                "title": "Strong source article",
                "note_summary_vi": "Ý chính của tin này là: đây là tin quan trọng cho team.",
                "total_score": 65,
                "primary_type": "Product",
                "primary_emoji": "🚀",
                "source": "RSS: Reuters",
                "source_domain": "reuters.com",
                "published_at": "2026-03-26T03:00:00+00:00",
                "content_available": True,
                "source_verified": True,
                "source_tier": "a",
            }],
            "notion_pages": [{"title": "Strong source article", "url": "https://notion.so/good"}],
            "summary_vn": "Bản tóm tắt lỗi format",
        }

        result = quality_gate_node(state)

        self.assertEqual(result["summary_mode"], "safe_fallback")
        self.assertIn("🚀 Product |", result["summary_vn"])
        # build_safe_digest_messages(..., include_empty_sections=False) chỉ tạo message cho lane có bài.
        self.assertGreaterEqual(len(result["telegram_messages"]), 1)
        self.assertIn("🚀 Product |", result["telegram_messages"][0])
        self.assertTrue(result["summary_warnings"])

    @patch("digest.workflow.nodes.quality_gate.get_history", return_value=[])
    def test_quality_gate_preview_uses_same_history_window_as_summary(self, mock_get_history) -> None:
        state = {
            "run_mode": "preview",
            "telegram_candidates": [],
            "telegram_messages": ["<b>🌍 Society & Culture | 01/04</b>\n\nLane này hôm nay hơi yên, chưa có bài nào đủ chắc để đưa lên brief chính."],
            "summary_vn": "<b>🌍 Society & Culture | 01/04</b>\n\nLane này hôm nay hơi yên, chưa có bài nào đủ chắc để đưa lên brief chính.",
            "notion_pages": [],
            "summary_mode": "deterministic_sections",
        }

        result = quality_gate_node(state)

        mock_get_history.assert_called_once_with(days=7, limit=120)
        self.assertEqual(result["summary_mode"], "deterministic_sections")
        self.assertEqual(result["summary_warnings"], [])

    def test_resolve_property_map_supports_aliases(self) -> None:
        database_properties = {
            "Name": {"type": "title"},
            "URL": {"type": "url"},
            "Summary": {"type": "rich_text"},
            "Recommendation": {"type": "rich_text"},
            "Type": {"type": "select"},
            "Mức độ phù hợp": {"type": "select"},
            "Project fit": {"type": "select"},
            "Tags": {"type": "multi_select"},
            "Score": {"type": "number"},
        }

        property_map = _resolve_property_map(database_properties)

        self.assertEqual(property_map["url"], "URL")
        self.assertEqual(property_map["summary"], "Summary")
        self.assertEqual(property_map["recommend"], "Recommendation")
        self.assertEqual(property_map["project_fit"], "Project fit")
        self.assertEqual(property_map["tags"], "Tags")

    def test_project_fit_level_from_c3_score(self) -> None:
        self.assertEqual(_project_fit_level(28), "High")
        self.assertEqual(_project_fit_level(18), "Medium")
        self.assertEqual(_project_fit_level(8), "Low")

    def test_storage_primary_type_uses_conservative_title_heuristic_for_weak_thin_article(self) -> None:
        self.assertEqual(
            _storage_primary_type(
                {
                    "title": "AI startup raises new funding for expansion",
                    "primary_type": "Society",
                    "total_score": 28,
                    "content_available": False,
                    "source_tier": "c",
                },
                "low",
            ),
            "Product",
        )

    def test_storage_tags_drop_low_confidence_weak_source_noise(self) -> None:
        self.assertEqual(
            _storage_tags(
                {
                    "tags": ["funding", "enterprise_ai"],
                    "total_score": 48,
                    "content_available": False,
                    "source_tier": "c",
                    "is_ai_relevant": True,
                },
                "low",
            ),
            [],
        )

    def test_storage_tags_keep_grounded_strong_article(self) -> None:
        self.assertEqual(
            _storage_tags(
                {
                    "tags": ["funding", "infrastructure"],
                    "total_score": 74,
                    "content_available": True,
                    "source_tier": "a",
                    "is_ai_relevant": True,
                },
                "high",
            ),
            ["funding", "infrastructure"],
        )

    def test_normalize_source_does_not_promote_fetched_at_to_published_at(self) -> None:
        state = {
            "raw_articles": [{
                "title": "Old article discovered today",
                "url": "https://example.com/old-news",
                "source": "DuckDuckGo (EN)",
                "fetched_at": "2026-03-26T09:00:00+00:00",
                "content": "A" * 300,
            }]
        }

        result = normalize_source_node(state)
        article = result["raw_articles"][0]

        self.assertEqual(article["published_at"], "")
        self.assertEqual(article["published_at_source"], "unknown")
        self.assertEqual(article["discovered_at"], "2026-03-26T09:00:00+00:00")
        self.assertTrue(article["freshness_unknown"])

    def test_normalize_source_marks_old_article_as_stale_candidate(self) -> None:
        state = {
            "raw_articles": [{
                "title": "Actually old article",
                "url": "https://example.com/old-news",
                "source": "RSS: Example",
                "published": "2026-03-01T09:00:00+00:00",
                "content": "A" * 300,
            }]
        }

        result = normalize_source_node(state)
        article = result["raw_articles"][0]

        self.assertEqual(article["published_at_source"], "source_metadata")
        self.assertTrue(article["is_stale_candidate"])

    def test_normalize_source_can_use_published_hint_for_facebook_relative_time(self) -> None:
        state = {
            "raw_articles": [{
                "title": "Facebook post",
                "url": "https://www.facebook.com/groups/example/posts/123",
                "source": "Facebook Auto | Example",
                "published_hint": "1 năm",
                "content": "A" * 300,
            }]
        }

        result = normalize_source_node(state)
        article = result["raw_articles"][0]

        self.assertEqual(article["published_at_source"], "published_hint")
        self.assertTrue(article["is_old_news"])
        self.assertTrue(article["is_stale_candidate"])

    def test_normalize_source_marks_geopolitics_story_as_not_ai_relevant(self) -> None:
        state = {
            "raw_articles": [{
                "title": "Oil prices fall as Trump says Iran let 10 tankers through Hormuz",
                "url": "https://example.com/oil-prices",
                "source": "DuckDuckGo (EN)",
                "content": "Oil prices moved lower after comments about Iran and Hormuz shipping lanes.",
            }]
        }

        result = normalize_source_node(state)
        article = result["raw_articles"][0]

        self.assertFalse(article["is_ai_relevant"])

    def test_normalize_source_marks_consumer_ios_story_as_not_ai_relevant(self) -> None:
        state = {
            "raw_articles": [{
                "title": "Loạt tính năng mới trên iOS 26.4",
                "url": "https://vnexpress.net/loat-tinh-nang-moi-tren-ios-26-4-5055425.html",
                "source": "RSS: Khoa học công nghệ - VnExpress RSS",
                "content": (
                    "iOS 26.4 bổ sung chống trộm, tạo playlist bằng AI trên Apple Music "
                    "và theo dõi giấc ngủ chi tiết cho người dùng iPhone."
                ),
            }]
        }

        result = normalize_source_node(state)
        article = result["raw_articles"][0]

        self.assertFalse(article["is_ai_relevant"])

    def test_normalize_source_can_infer_date_from_url(self) -> None:
        state = {
            "raw_articles": [{
                "title": "Cafef style article with encoded date",
                "url": "https://cafef.vn/fpt-cung-nvidia-tro-luc-ai-startup-188250520171429754.chn",
                "source": "DuckDuckGo (VN)",
                "content": "A" * 300,
            }]
        }

        result = normalize_source_node(state)
        article = result["raw_articles"][0]

        self.assertEqual(article["published_at_source"], "url_pattern")
        self.assertTrue(article["published_at"].startswith("2025-05-20"))
        self.assertTrue(article["is_old_news"])

    def test_freshness_penalty_reduces_old_article_score(self) -> None:
        article = {
            "title": "Old but flashy article",
            "total_score": 72,
            "analysis_tier": "deep",
            "source_tier": "a",
            "freshness_unknown": False,
            "is_stale_candidate": True,
            "content_available": True,
        }

        _apply_freshness_penalty(article, min_score=60)

        self.assertLess(article["total_score"], 72)
        self.assertEqual(article["freshness_status"], "stale_candidate")
        self.assertIn(article["analysis_tier"], {"basic", "skip"})

    def test_event_clustering_marks_primary_article(self) -> None:
        articles = [
            {
                "title": "OpenAI launches new coding agent for enterprise teams",
                "total_score": 70,
                "source_tier": "a",
                "content_available": True,
                "freshness_unknown": False,
            },
            {
                "title": "OpenAI launches enterprise coding agent for software teams",
                "total_score": 62,
                "source_tier": "b",
                "content_available": True,
                "freshness_unknown": False,
            },
            {
                "title": "Vietnam AI startup raises seed round",
                "total_score": 55,
                "source_tier": "b",
                "content_available": True,
                "freshness_unknown": False,
            },
        ]

        primaries = _annotate_event_clusters(articles, min_score=60)

        self.assertEqual(len(primaries), 2)
        clustered = [article for article in articles if article.get("event_cluster_size") == 2]
        self.assertEqual(len(clustered), 2)
        self.assertEqual(sum(1 for article in clustered if article.get("event_is_primary")), 1)

    def test_prefilter_limits_candidates_before_llm_classify(self) -> None:
        articles = [
            {
                "title": "OpenAI ships new enterprise agent controls",
                "source_tier": "a",
                "freshness_bucket": "fresh",
                "content_available": True,
                "source_verified": True,
                "is_old_news": False,
                "is_stale_candidate": False,
            },
            {
                "title": "Anthropic launches workflow automation features",
                "source_tier": "a",
                "freshness_bucket": "recent",
                "content_available": True,
                "source_verified": True,
                "is_old_news": False,
                "is_stale_candidate": False,
            },
            {
                "title": "Celebrity smartphone camera roundup",
                "source_tier": "c",
                "freshness_bucket": "fresh",
                "content_available": False,
                "source_verified": False,
                "is_old_news": False,
                "is_stale_candidate": False,
            },
        ]

        selected, held_out = _prepare_classify_candidates(articles, max_candidates=2)

        self.assertEqual(len(selected), 2)
        self.assertEqual(len(held_out), 1)
        self.assertIn("OpenAI ships", selected[0]["title"])
        self.assertIn("Celebrity smartphone", held_out[0]["title"])

    @patch("digest.workflow.nodes.classify_and_score.grok_prefilter_max_articles", return_value=3)
    @patch("digest.workflow.nodes.classify_and_score.rerank_prefilter_articles")
    @patch("digest.workflow.nodes.classify_and_score.grok_prefilter_enabled", return_value=True)
    def test_prefilter_grok_can_rescue_article_into_local_classify(
        self,
        _mock_enabled,
        mock_rerank,
        _mock_limit,
    ) -> None:
        articles = [
            {
                "title": "Official product roundup",
                "url": "https://example.com/a",
                "source_tier": "a",
                "source_domain": "example.com",
                "freshness_bucket": "fresh",
                "content_available": True,
                "source_verified": True,
                "is_old_news": False,
                "is_stale_candidate": False,
            },
            {
                "title": "OpenAI quietly updates enterprise admin controls",
                "url": "https://example.com/b",
                "source_tier": "a",
                "source_domain": "example.com",
                "freshness_bucket": "recent",
                "content_available": True,
                "source_verified": True,
                "is_old_news": False,
                "is_stale_candidate": False,
            },
            {
                "title": "Low-value gadget rumor",
                "url": "https://example.com/c",
                "source_tier": "c",
                "source_domain": "rumor.example",
                "freshness_bucket": "fresh",
                "content_available": False,
                "source_verified": False,
                "is_old_news": False,
                "is_stale_candidate": False,
            },
        ]
        mock_rerank.return_value = {
            "https://example.com/b": {
                "keep_for_local": True,
                "priority_score": 97,
                "rationale": "Bài này có tín hiệu founder-grade dù heuristic headline chưa kéo đủ mạnh.",
            }
        }

        selected, held_out = _prepare_classify_candidates(
            articles,
            max_candidates=1,
            runtime_config={},
            feedback_summary_text="Ưu tiên founder-grade official updates.",
        )

        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["url"], "https://example.com/b")
        self.assertEqual(selected[0]["grok_prefilter_priority_score"], 97)
        self.assertEqual(len(held_out), 2)

    def test_infer_taxonomy_tags_normalizes_freeform_model_tags(self) -> None:
        article = {
            "title": "AI startup raises $1 billion to expand GPU data center capacity in Vietnam",
            "primary_type": "Business",
            "summary_vi": "Startup AI huy động vốn để mở rộng hạ tầng tính toán tại Việt Nam.",
            "total_score": 74,
        }

        tags = _infer_taxonomy_tags(article, raw_tags=["tech", "infrastructure", "startups", "1bn"])

        self.assertEqual(tags, ["infrastructure", "funding", "vietnam"])

    @patch("digest.workflow.nodes.classify_and_score.run_json_inference")
    def test_classify_inference_retries_once_after_json_parse_failure(self, mock_run_json) -> None:
        mock_run_json.side_effect = [
            None,
            {
                "primary_type": "Business",
                "primary_emoji": "💼",
                "c1_score": 24,
                "c2_score": 22,
                "c3_score": 20,
                "summary_vi": "Recovered on retry.",
                "editorial_angle": "Funding and infrastructure matter.",
                "analysis_tier": "deep",
                "tags": ["funding", "infrastructure"],
                "relevance_level": "High",
            },
        ]

        result = _classify_inference_with_retry("prompt body", max_tokens=320, temperature=0.1)

        self.assertIsNotNone(result)
        self.assertEqual(result["primary_type"], "Business")
        self.assertEqual(mock_run_json.call_count, 2)

    @patch("digest.workflow.nodes.classify_and_score.run_json_inference")
    def test_classify_inference_retries_for_clear_prose_before_giving_up(self, mock_run_json) -> None:
        mock_run_json.return_value = (
            None,
            "Đây là một bài cập nhật sản phẩm AI cho doanh nghiệp. Điểm đáng chú ý là nó phù hợp để theo dõi thêm trong brief sáng nay. Tuy nhiên model đang trả prose thay vì JSON.",
            True,
        )

        result = _classify_inference_with_retry("prompt body", max_tokens=320, temperature=0.1)

        self.assertIsNone(result)
        self.assertEqual(mock_run_json.call_count, 3)

    @patch("digest.workflow.nodes.classify_and_score.run_json_inference")
    def test_classify_inference_reuses_initial_failed_response_without_duplicate_first_call(self, mock_run_json) -> None:
        mock_run_json.return_value = {
            "primary_type": "Business",
            "primary_emoji": "💼",
            "c1_score": 23,
            "c2_score": 22,
            "c3_score": 20,
            "summary_vi": "Recovered on strict retry.",
            "editorial_angle": "Founder-grade move worth tracking.",
            "analysis_tier": "deep",
            "tags": ["funding"],
            "relevance_level": "High",
        }

        result = _classify_inference_with_retry(
            "prompt body",
            max_tokens=320,
            temperature=0.1,
            initial_response=(None, '{"primary_type": "Business",}', True),
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["primary_type"], "Business")
        self.assertEqual(mock_run_json.call_count, 0)

    @patch("digest.workflow.nodes.classify_and_score.write_temporal_snapshot", return_value="reports/mock.json")
    @patch("digest.workflow.nodes.classify_and_score.call_xai_structured_json")
    @patch("digest.workflow.nodes.classify_and_score.run_json_inference")
    def test_classify_node_uses_grok_retry_after_local_json_failure(
        self,
        mock_run_json,
        mock_grok_json,
        _mock_snapshot,
    ) -> None:
        mock_run_json.side_effect = [
            (None, "", False),
            (None, "", False),
            (None, "", False),
        ]
        mock_grok_json.return_value = {
            "primary_type": "Product",
            "primary_emoji": "🚀",
            "c1_score": 24,
            "c1_reason": "Nguồn mạnh và JSON ổn định.",
            "c2_score": 21,
            "c2_reason": "Hữu ích cho team vận hành AI.",
            "c3_score": 19,
            "c3_reason": "Fit với hướng agent workflow.",
            "summary_vi": "OpenAI công bố orchestration update cho agent workflow enterprise.",
            "factual_summary_vi": "OpenAI công bố orchestration update mới cho workflow enterprise.",
            "editorial_angle": "Đây là cập nhật platform đáng theo dõi cho builder.",
            "why_it_matters_vi": "Nó giúp đội vận hành agent kiểm soát flow ổn định hơn.",
            "optional_editorial_angle": "Tín hiệu tích cực cho team đang productize agent.",
            "analysis_tier": "deep",
            "tags": ["api_platform", "ai_agents"],
            "relevance_level": "High",
        }

        result = classify_and_score_node(
            {
                "new_articles": [
                    {
                        "title": "OpenAI ships orchestration controls for agents",
                        "url": "https://example.com/grok-classify",
                        "source": "OpenAI",
                        "source_domain": "openai.com",
                        "source_tier": "a",
                        "source_kind": "official",
                        "source_verified": True,
                        "content_available": True,
                        "content": "OpenAI shipped orchestration controls for enterprise agent workflows.",
                        "snippet": "OpenAI shipped orchestration controls for enterprise agent workflows.",
                        "published_at": "2026-04-06T00:00:00+00:00",
                        "published_at_source": "source_metadata",
                        "freshness_bucket": "fresh",
                        "is_ai_relevant": True,
                        "event_is_primary": True,
                    }
                ],
                "runtime_config": {
                    "max_classify_articles": 5,
                    "max_deep_analysis_articles": 3,
                    "use_grok_for_classify": True,
                },
                "feedback_summary_text": "",
                "feedback_preference_profile": {},
            }
        )

        article = result["scored_articles"][0]
        self.assertEqual(article["classify_provider_used"], "local_then_grok")
        self.assertEqual(article["classify_json_status"], "valid_json")
        self.assertEqual(result["grok_stage_usage"]["classify"]["grok_rescue_count"], 1)
        self.assertEqual(result["grok_stage_usage"]["classify"]["local_failure_count"], 1)
        self.assertEqual(result["grok_request_count"], 1)
        self.assertEqual(result["grok_success_count"], 1)
        self.assertEqual(mock_grok_json.call_count, 1)

    @patch("digest.workflow.nodes.classify_and_score.write_temporal_snapshot", return_value="reports/mock.json")
    @patch("digest.workflow.nodes.classify_and_score.call_xai_structured_json", return_value={})
    @patch("digest.workflow.nodes.classify_and_score.run_json_inference")
    def test_classify_node_falls_back_safely_when_grok_retry_returns_empty(
        self,
        mock_run_json,
        _mock_grok_json,
        _mock_snapshot,
    ) -> None:
        mock_run_json.side_effect = [
            (None, "", False),
            (None, "", False),
            (None, "", False),
        ]

        result = classify_and_score_node(
            {
                "new_articles": [
                    {
                        "title": "Unknown AI update",
                        "url": "https://example.com/grok-empty",
                        "source": "Blog",
                        "source_domain": "example.com",
                        "source_tier": "b",
                        "source_kind": "unknown",
                        "source_verified": False,
                        "content_available": True,
                        "content": "Some AI update.",
                        "snippet": "Some AI update.",
                        "published_at": "2026-04-06T00:00:00+00:00",
                        "published_at_source": "source_metadata",
                        "freshness_bucket": "fresh",
                        "is_ai_relevant": True,
                        "event_is_primary": True,
                    }
                ],
                "runtime_config": {
                    "max_classify_articles": 5,
                    "max_deep_analysis_articles": 3,
                    "use_grok_for_classify": True,
                },
                "feedback_summary_text": "",
                "feedback_preference_profile": {},
            }
        )

        article = result["scored_articles"][0]
        self.assertEqual(article["classify_provider_used"], "local_then_grok")
        self.assertIn(article["classify_json_status"], {"partial_recovery", "hard_fallback"})
        self.assertTrue(article["summary_vi"])
        self.assertEqual(result["grok_stage_usage"]["classify"]["fallback_count"], 1)
        self.assertEqual(result["grok_request_count"], 1)
        self.assertEqual(result["grok_fallback_count"], 1)

    @patch("digest.workflow.nodes.classify_and_score.write_temporal_snapshot", return_value="reports/mock.json")
    @patch("digest.workflow.nodes.classify_and_score.call_xai_structured_json")
    @patch("digest.workflow.nodes.classify_and_score.run_json_inference")
    def test_classify_node_keeps_valid_structured_json(self, mock_run_json, mock_grok_json, _mock_snapshot) -> None:
        mock_run_json.return_value = {
            "primary_type": "Product",
            "primary_emoji": "🚀",
            "c1_score": 24,
            "c1_reason": "Nguồn mạnh và dữ kiện rõ.",
            "c2_score": 22,
            "c2_reason": "Có ích cho team build sản phẩm AI.",
            "c3_score": 20,
            "c3_reason": "Fit với hướng agent workflow.",
            "summary_vi": "OpenAI công bố API orchestration mới cho agent workflows.",
            "factual_summary_vi": "OpenAI công bố API orchestration mới cho workflow nhiều bước.",
            "editorial_angle": "Đây là cập nhật platform đáng theo dõi cho builder.",
            "why_it_matters_vi": "Nó giúp đội vận hành agent có thêm primitive để kiểm soát flow.",
            "optional_editorial_angle": "Tín hiệu cho các team đang productize agent.",
            "analysis_tier": "deep",
            "tags": ["api_platform", "ai_agents"],
            "relevance_level": "High",
        }

        result = classify_and_score_node(
            {
                "new_articles": [
                    {
                        "title": "OpenAI launches orchestration API for agents",
                        "url": "https://example.com/a",
                        "source": "OpenAI",
                        "source_domain": "openai.com",
                        "source_tier": "a",
                        "source_kind": "official",
                        "source_verified": True,
                        "content_available": True,
                        "content": "OpenAI launched a new orchestration API for agent workflows.",
                        "snippet": "OpenAI launched a new orchestration API for agent workflows.",
                        "published_at": "2026-04-06T00:00:00+00:00",
                        "published_at_source": "source_metadata",
                        "freshness_bucket": "fresh",
                        "is_ai_relevant": True,
                        "event_is_primary": True,
                    }
                ],
                "runtime_config": {"max_classify_articles": 5, "max_deep_analysis_articles": 3},
                "feedback_summary_text": "",
                "feedback_preference_profile": {},
            }
        )

        article = result["scored_articles"][0]
        self.assertEqual(article["classify_json_status"], "valid_json")
        self.assertEqual(article["factual_summary_vi"], "OpenAI công bố API orchestration mới cho workflow nhiều bước.")
        self.assertEqual(article["why_it_matters_vi"], "Nó giúp đội vận hành agent có thêm primitive để kiểm soát flow.")
        self.assertEqual(article["optional_editorial_angle"], "Tín hiệu cho các team đang productize agent.")
        self.assertNotIn("classify hiện tại không trả JSON ổn định", article["summary_vi"])
        mock_grok_json.assert_not_called()

    @patch("digest.workflow.nodes.classify_and_score.write_temporal_snapshot", return_value="reports/mock.json")
    @patch("digest.workflow.nodes.classify_and_score.run_json_inference")
    def test_classify_node_repairs_malformed_json_output(self, mock_run_json, _mock_snapshot) -> None:
        mock_run_json.return_value = (
            None,
            """```json
            {primary_type: 'Product', primary_emoji: '🚀', c1_score: 21, c1_reason: 'fresh', c2_score: 20, c2_reason: 'useful',
            c3_score: 18, c3_reason: 'fit', summary_vi: 'OpenAI ra mắt API agent mới.', factual_summary_vi: 'OpenAI ra mắt API agent mới cho workflow agent.',
            editorial_angle: 'Đây là cập nhật platform.', why_it_matters_vi: 'Nó giúp team build agent thực chiến nhanh hơn.',
            optional_editorial_angle: 'Tín hiệu tốt cho builder.', analysis_tier: 'deep', tags: ['api_platform', 'ai_agents'], relevance_level: 'High',}
            ```""",
            True,
        )

        result = classify_and_score_node(
            {
                "new_articles": [
                    {
                        "title": "OpenAI launches agent API",
                        "url": "https://example.com/b",
                        "source": "OpenAI",
                        "source_domain": "openai.com",
                        "source_tier": "a",
                        "source_kind": "official",
                        "source_verified": True,
                        "content_available": True,
                        "content": "OpenAI launched a new API for agents.",
                        "snippet": "OpenAI launched a new API for agents.",
                        "published_at": "2026-04-06T00:00:00+00:00",
                        "published_at_source": "source_metadata",
                        "freshness_bucket": "fresh",
                        "is_ai_relevant": True,
                        "event_is_primary": True,
                    }
                ],
                "runtime_config": {"max_classify_articles": 5, "max_deep_analysis_articles": 3},
                "feedback_summary_text": "",
                "feedback_preference_profile": {},
            }
        )

        article = result["scored_articles"][0]
        self.assertEqual(article["classify_json_status"], "repaired_json")
        self.assertEqual(article["factual_summary_vi"], "OpenAI ra mắt API agent mới cho workflow agent.")
        self.assertEqual(article["why_it_matters_vi"], "Nó giúp team build agent thực chiến nhanh hơn.")

    @patch("digest.workflow.nodes.classify_and_score.write_temporal_snapshot", return_value="reports/mock.json")
    @patch("digest.workflow.nodes.classify_and_score.run_json_inference")
    def test_classify_node_recovers_missing_structured_fields(self, mock_run_json, _mock_snapshot) -> None:
        mock_run_json.return_value = {
            "primary_type": "Product",
            "primary_emoji": "🚀",
            "c1_score": 22,
            "c1_reason": "fresh",
            "c2_score": 21,
            "c2_reason": "useful",
            "c3_score": 19,
            "c3_reason": "fit",
            "summary_vi": "Anthropic cập nhật pricing cho Claude Code.",
            "editorial_angle": "Đây là thay đổi có tác động trực tiếp tới operator.",
            "analysis_tier": "deep",
            "tags": ["api_platform"],
            "relevance_level": "High",
        }

        result = classify_and_score_node(
            {
                "new_articles": [
                    {
                        "title": "Anthropic updates Claude Code pricing",
                        "url": "https://example.com/c",
                        "source": "Anthropic",
                        "source_domain": "anthropic.com",
                        "source_tier": "a",
                        "source_kind": "official",
                        "source_verified": True,
                        "content_available": True,
                        "content": "Anthropic updated Claude Code pricing for operators.",
                        "snippet": "Anthropic updated Claude Code pricing for operators.",
                        "published_at": "2026-04-06T00:00:00+00:00",
                        "published_at_source": "source_metadata",
                        "freshness_bucket": "fresh",
                        "is_ai_relevant": True,
                        "event_is_primary": True,
                    }
                ],
                "runtime_config": {"max_classify_articles": 5, "max_deep_analysis_articles": 3},
                "feedback_summary_text": "",
                "feedback_preference_profile": {},
            }
        )

        article = result["scored_articles"][0]
        self.assertEqual(article["classify_json_status"], "partial_recovery")
        self.assertEqual(article["factual_summary_vi"], "Anthropic cập nhật pricing cho Claude Code.")
        self.assertEqual(article["why_it_matters_vi"], "Đây là thay đổi có tác động trực tiếp tới operator.")
        self.assertEqual(article["optional_editorial_angle"], "Đây là thay đổi có tác động trực tiếp tới operator.")
        self.assertIn("factual_summary_vi", article["classify_json_missing_fields"])

    @patch("digest.workflow.nodes.classify_and_score.write_temporal_snapshot", return_value="reports/mock.json")
    @patch("digest.workflow.nodes.classify_and_score.run_json_inference")
    def test_classify_node_marks_hard_fallback_when_json_cannot_be_recovered(self, mock_run_json, _mock_snapshot) -> None:
        mock_run_json.side_effect = [
            (None, "", False),
            (None, "", False),
            (None, "", False),
        ]

        result = classify_and_score_node(
            {
                "new_articles": [
                    {
                        "title": "Unknown AI update",
                        "url": "https://example.com/d",
                        "source": "Blog",
                        "source_domain": "example.com",
                        "source_tier": "b",
                        "source_kind": "unknown",
                        "source_verified": False,
                        "content_available": True,
                        "content": "Some AI update.",
                        "snippet": "Some AI update.",
                        "published_at": "2026-04-06T00:00:00+00:00",
                        "published_at_source": "source_metadata",
                        "freshness_bucket": "fresh",
                        "is_ai_relevant": True,
                        "event_is_primary": True,
                    }
                ],
                "runtime_config": {"max_classify_articles": 5, "max_deep_analysis_articles": 3},
                "feedback_summary_text": "",
                "feedback_preference_profile": {},
            }
        )

        article = result["scored_articles"][0]
        self.assertEqual(article["classify_json_status"], "hard_fallback")
        self.assertIn("classify hiện tại không trả JSON ổn định", article["summary_vi"])

    def test_held_out_article_fallback_avoids_title_token_garbage_tags(self) -> None:
        article = {
            "title": "Pha dan giup luc cho doi startup",
            "prefilter_score": 8,
            "is_ai_relevant": True,
            "content_available": False,
        }

        _held_out_article_fallback(article)

        self.assertEqual(article["tags"], [])

    def test_prepare_classify_candidates_reserves_room_for_main_lane(self) -> None:
        articles = []
        for index in range(6):
            articles.append(
                {
                    "title": f"openai/repo-{index}",
                    "source_domain": "github.com",
                    "source_tier": "a",
                    "freshness_bucket": "fresh",
                    "content_available": True,
                    "source_verified": True,
                    "is_ai_relevant": True,
                    "is_news_candidate": True,
                    "github_full_name": f"openai/repo-{index}",
                }
            )
        for index in range(6):
            articles.append(
                {
                    "title": f"Enterprise AI funding round {index}",
                    "source_domain": "cnbc.com",
                    "source_tier": "a",
                    "freshness_bucket": "fresh",
                    "content_available": True,
                    "source_verified": True,
                    "is_ai_relevant": True,
                    "is_news_candidate": True,
                }
            )

        llm_articles, held_out_articles = _prepare_classify_candidates(articles, max_candidates=8)

        self.assertEqual(len(llm_articles), 8)
        self.assertTrue(any(article.get("source_domain") == "github.com" for article in llm_articles))
        self.assertTrue(any(article.get("source_domain") == "cnbc.com" for article in llm_articles))
        self.assertEqual(len(held_out_articles), 4)

    def test_held_out_article_fallback_can_keep_strong_main_signal_reviewable(self) -> None:
        article = {
            "title": "OpenAI enterprise rollout expands in Asia",
            "prefilter_score": 34,
            "prefilter_reasons": ["tier:a+10", "freshness:fresh+8", "content+4", "ai_relevant+4"],
            "is_ai_relevant": True,
            "content_available": True,
            "source_tier": "a",
            "freshness_bucket": "fresh",
            "source_domain": "cnbc.com",
        }

        _held_out_article_fallback(article)

        self.assertGreater(article["total_score"], 36)
        self.assertEqual(article["analysis_tier"], "basic")

    def test_select_top_articles_uses_dynamic_percentile_with_minimum_floor(self) -> None:
        articles = [
            {"title": "A", "total_score": 52, "analysis_tier": "basic"},
            {"title": "B", "total_score": 48, "analysis_tier": "basic"},
            {"title": "C", "total_score": 44, "analysis_tier": "basic"},
            {"title": "D", "total_score": 29, "analysis_tier": "skip"},
        ]

        top_articles, score_cutoff = _select_top_articles(articles, min_items=3, max_items=15)

        self.assertEqual(score_cutoff, 44)
        self.assertEqual([article["title"] for article in top_articles], ["A", "B", "C"])

    def test_delivery_judge_skips_duplicate_event_article(self) -> None:
        article = {
            "title": "Duplicate article",
            "total_score": 68,
            "source_tier": "a",
            "confidence_label": "high",
            "content_available": True,
            "freshness_unknown": False,
            "is_stale_candidate": False,
            "event_is_primary": False,
            "event_cluster_size": 2,
        }

        result = _deterministic_delivery_assessment(article)

        self.assertEqual(result["decision"], "skip")

    def test_delivery_judge_skips_old_news_even_if_not_stale_candidate(self) -> None:
        article = {
            "title": "Two-week old article",
            "total_score": 74,
            "source_tier": "b",
            "confidence_label": "medium",
            "content_available": True,
            "freshness_unknown": False,
            "freshness_bucket": "aging",
            "is_old_news": True,
            "is_stale_candidate": False,
            "event_is_primary": True,
            "event_cluster_size": 1,
        }

        result = _deterministic_delivery_assessment(article)

        self.assertEqual(result["decision"], "skip")

    def test_delivery_judge_skips_speculative_community_main_article(self) -> None:
        article = {
            "title": "Gemini 4 is coming ??",
            "total_score": 66,
            "source_tier": "c",
            "source_kind": "community",
            "confidence_label": "low",
            "content_available": True,
            "freshness_unknown": False,
            "freshness_bucket": "fresh",
            "is_old_news": False,
            "is_stale_candidate": False,
            "event_is_primary": True,
            "event_cluster_size": 1,
            "is_ai_relevant": True,
        }

        result = _deterministic_delivery_assessment(article)

        self.assertEqual(result["decision"], "skip")

    def test_delivery_judge_skips_event_promo_article(self) -> None:
        article = {
            "title": "Less than a month: StrictlyVC San Francisco brings leaders from TDK Ventures, Replit, and more together",
            "total_score": 71,
            "source_tier": "a",
            "source_kind": "media",
            "confidence_label": "medium",
            "content_available": True,
            "freshness_unknown": False,
            "freshness_bucket": "fresh",
            "is_old_news": False,
            "is_stale_candidate": False,
            "event_is_primary": True,
            "event_cluster_size": 1,
            "is_ai_relevant": True,
        }

        result = _deterministic_delivery_assessment(article)

        self.assertEqual(result["decision"], "skip")

    @patch("digest.workflow.nodes.summarize_vn.get_history", return_value=[])
    def test_summarize_vn_does_not_bypass_empty_delivery_judge(self, _mock_get_history) -> None:
        state = {
            "run_mode": "publish",
            "final_articles": [{
                "title": "Should not leak through",
                "note_summary_vi": "Ý chính của tin này là: đây là tin yếu.",
                "total_score": 80,
                "primary_type": "Business",
                "primary_emoji": "💼",
                "source": "RSS: Example",
                "source_domain": "example.com",
                "published_at": "2026-03-26T03:00:00+00:00",
                "content_available": True,
                "source_verified": True,
                "source_tier": "a",
            }],
            "telegram_candidates": [],
            "notion_pages": [],
            "scored_articles": [{
                "title": "Fallback article",
                "note_summary_vi": "Ý chính của tin này là: không nên xuất hiện.",
                "total_score": 90,
            }],
        }

        result = summarize_vn_node(state)

        self.assertEqual(result["telegram_messages"], [])
        self.assertEqual(result["summary_mode"], "no_candidates")
        self.assertNotIn("Fallback article", result["summary_vn"])
        self.assertNotIn("Should not leak through", result["summary_vn"])

    @patch("digest.workflow.nodes.summarize_vn.get_history", return_value=[{
        "title": "Anthropic adds enterprise controls",
        "url": "https://example.com/repeat",
        "primary_type": "Product",
        "summary": "Ý chính của tin này là: Anthropic bổ sung lớp kiểm soát enterprise cho AI deployment.",
        "source": "RSS: Example",
        "relevance_score": 78,
        "created_at": "2026-03-26T01:00:00+00:00",
    }])
    def test_summarize_vn_uses_archive_when_no_telegram_candidates(self, _mock_get_history) -> None:
        state = {
            "final_articles": [{
                "title": "Off-topic current article",
                "note_summary_vi": "Ý chính của tin này là: không nên lên brief.",
                "total_score": 80,
                "primary_type": "Business",
                "source": "RSS: Example",
            }],
            "telegram_candidates": [],
            "notion_pages": [],
        }

        result = summarize_vn_node(state)

        self.assertIn("Anthropic adds enterprise controls", result["summary_vn"])
        self.assertNotIn("Off-topic current article", result["summary_vn"])

    @patch("digest.workflow.nodes.summarize_vn.get_history", return_value=[{
        "title": "OpenAI launches new production controls",
        "url": "https://example.com/openai-production",
        "primary_type": "Product",
        "summary": "OpenAI ra mắt thêm lớp production control cho enterprise deployment.",
        "source": "RSS: Example",
        "relevance_score": 81,
        "created_at": "2026-03-31T01:00:00+00:00",
    }])
    def test_summarize_vn_publish_replays_recent_article_with_date_for_empty_type(self, _mock_get_history) -> None:
        state = {
            "run_mode": "publish",
            "telegram_candidates": [],
            "notion_pages": [],
        }

        result = summarize_vn_node(state)

        self.assertEqual(len(result["telegram_messages"]), 1)
        product_message = result["telegram_messages"][0]
        self.assertIn("OpenAI launches new production controls", product_message)
        self.assertIn("(31/03/2026)", product_message)
        self.assertNotEqual(result["summary_mode"], "no_candidates")

    @patch("digest.workflow.nodes.summarize_vn.get_history", return_value=[])
    def test_summarize_vn_keeps_all_quality_passed_articles_without_hard_cap(self, _mock_get_history) -> None:
        labels = ["admin controls", "agent memory", "workspace rollout", "voice mode", "MCP tooling"]
        candidates = [
            {
                "title": f"Product {labels[idx - 1]}",
                "url": f"https://example.com/product-{idx}",
                "primary_type": "Product",
                "note_summary_vi": f"OpenAI vừa đẩy tiếp {labels[idx - 1]} vào workflow doanh nghiệp.",
                "source": "RSS: Example",
                "source_domain": "example.com",
                "total_score": 85 - idx,
                "delivery_decision": "include",
            }
            for idx in range(1, 6)
        ]

        result = summarize_vn_node(
            {
                "run_mode": "publish",
                "telegram_candidates": candidates,
                "notion_pages": [],
            }
        )

        product_message = next(message for message in result["telegram_messages"] if "🚀 Product" in message)
        for label in labels:
            self.assertIn(f"Product {label}", product_message)

    def test_quality_gate_keeps_no_candidates_empty_for_publish(self) -> None:
        result = quality_gate_node(
            {
                "run_mode": "publish",
                "publish_telegram": True,
                "telegram_candidates": [],
                "telegram_messages": [],
                "summary_vn": "",
                "summary_mode": "no_candidates",
                "notion_pages": [],
            }
        )

        self.assertEqual(result["telegram_messages"], [])
        self.assertEqual(result["summary_mode"], "no_candidates")
        self.assertEqual(result["summary_warnings"], [])

    def test_delivery_judge_skips_unknown_weak_source(self) -> None:
        article = {
            "title": "Weak source article",
            "total_score": 39,
            "source_tier": "c",
            "confidence_label": "medium",
            "content_available": True,
            "freshness_unknown": True,
            "is_stale_candidate": False,
            "event_is_primary": True,
            "event_cluster_size": 1,
        }

        result = _deterministic_delivery_assessment(article)

        self.assertEqual(result["decision"], "skip")

    def test_delivery_judge_skips_non_ai_article(self) -> None:
        article = {
            "title": "Wall Street banks eye comeback in private credit",
            "total_score": 61,
            "source_tier": "a",
            "confidence_label": "high",
            "content_available": True,
            "freshness_unknown": False,
            "is_stale_candidate": False,
            "event_is_primary": True,
            "event_cluster_size": 1,
            "is_ai_relevant": False,
        }

        result = _deterministic_delivery_assessment(article)

        self.assertEqual(result["decision"], "skip")

    @patch("digest.workflow.nodes.delivery_judge.grok_final_editor_enabled", return_value=False)
    @patch("digest.workflow.nodes.delivery_judge.grok_delivery_enabled", return_value=False)
    @patch("digest.workflow.nodes.delivery_judge.run_json_inference", return_value=None)
    def test_delivery_judge_caps_main_brief_candidates_to_six_items(self, _mock_run_json, *_mocks) -> None:
        final_articles = [
            {
                "title": f"Main article {idx}",
                "url": f"https://example.com/main-{idx}",
                "primary_type": "Product",
                "note_summary_vi": (
                    f"OpenAI vừa mở thêm capability số {idx} cho API enterprise, hỗ trợ workflow agent "
                    "và rollout vận hành tốt hơn cho team AI."
                ),
                "total_score": 80,
                "source": "RSS: Example",
                "source_domain": "example.com",
                "source_kind": "strong_media",
                "source_tier": "a",
                "content_available": True,
                "source_verified": True,
                "tags": ["api_platform", "enterprise_ai", "ai_agents"],
                "freshness_unknown": False,
                "is_stale_candidate": False,
                "event_is_primary": True,
                "event_cluster_size": 1,
                "confidence_label": "high",
            }
            for idx in range(1, 15)
        ]

        result = delivery_judge_node({"final_articles": final_articles, "runtime_config": {}})

        self.assertEqual(len(result["telegram_candidates"]), 5)

    @patch(
        "digest.runtime.mlx_runner.run_inference",
        return_value="""```json
{'primary_type': 'Product', 'c1_score': 12, 'tags': ['ai',], 'analysis_tier': 'basic'}
```""",
    )
    def test_run_json_inference_can_rescue_pythonish_json(self, _mock_run_inference) -> None:
        parsed = run_json_inference("system", "user")

        self.assertIsInstance(parsed, dict)
        self.assertEqual(parsed["primary_type"], "Product")

    @patch(
        "digest.runtime.mlx_runner.run_inference",
        return_value="""
primary_type: Policy
c1_score: 21
c2_score: 18
c3_score: 16
analysis_tier: deep
summary_vi: Sự cố này tác động trực tiếp tới stack AI của nhiều team.
tags: [regulation, safety]
""",
    )
    def test_run_json_inference_rejects_line_based_non_json_output(self, _mock_run_inference) -> None:
        parsed = run_json_inference("system", "user")

        self.assertIsNone(parsed)


if __name__ == "__main__":
    unittest.main()
