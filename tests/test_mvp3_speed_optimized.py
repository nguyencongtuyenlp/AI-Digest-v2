import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from graph import build_graph
from digest.workflow.nodes.batch_classify_and_score_node import batch_classify_and_score_node
from digest.workflow.nodes.batch_deep_process_node import batch_deep_process_node
from digest.workflow.nodes.batch_quick_compose_node import batch_quick_compose_node
from digest.workflow.nodes.early_rule_filter_node import early_rule_filter_node


class MVP3SpeedOptimizedTest(unittest.TestCase):
    def test_early_rule_filter_drops_duplicate_titles(self) -> None:
        articles = [
            {
                "title": "OpenAI launches new agent SDK",
                "url": "https://example.com/a",
                "source_domain": "openai.com",
                "source_tier": "a",
                "is_ai_relevant": True,
                "content_available": True,
            },
            {
                "title": "OpenAI launches new agent SDK",
                "url": "https://example.com/b",
                "source_domain": "github.com",
                "source_tier": "c",
                "is_ai_relevant": True,
                "content_available": False,
            },
        ]
        result = early_rule_filter_node({"new_articles": articles, "runtime_config": {"early_rule_filter_min_keep": 1}})
        self.assertEqual(len(result["filtered_articles"]), 1)

    @patch("digest.workflow.nodes.batch_classify_and_score_node.write_temporal_snapshot", return_value="reports/mock.json")
    @patch(
        "digest.workflow.nodes.batch_classify_and_score_node.run_json_inference_meta",
        return_value=(
            {
                "articles": [
                    {
                        "item_id": "article_0",
                        "primary_type": "Product",
                        "primary_emoji": "🚀",
                        "c1_score": 20,
                        "c1_reason": "fresh",
                        "c2_score": 18,
                        "c2_reason": "useful",
                        "c3_score": 18,
                        "c3_reason": "fit",
                        "summary_vi": "OpenAI công bố SDK agent mới cho enterprise workflows.",
                        "editorial_angle": "SDK này có thể giúp team tăng tốc triển khai agent.",
                        "analysis_tier": "deep",
                        "tags": ["api_platform"],
                        "relevance_level": "High",
                    }
                ]
            },
            "{}",
            True,
        ),
    )
    def test_batch_classify_and_score_returns_top_and_low(self, _mock_infer, _mock_snapshot) -> None:
        state = {
            "filtered_articles": [
                {
                    "title": "OpenAI launches new agent SDK",
                    "url": "https://example.com/a",
                    "source": "OpenAI",
                    "source_domain": "openai.com",
                    "source_tier": "a",
                    "source_kind": "official",
                    "source_verified": True,
                    "content_available": True,
                    "content": "A new SDK for building production agents.",
                    "snippet": "A new SDK for building production agents.",
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
        result = batch_classify_and_score_node(state)
        self.assertEqual(len(result["scored_articles"]), 1)
        self.assertEqual(result["top_articles"][0]["primary_type"], "Product")
        self.assertEqual(result["top_articles"][0]["delivery_lane_candidate"], "main")
        self.assertEqual(result["top_articles"][0]["main_brief_eligibility"], "eligible")
        self.assertEqual(result["top_articles"][0]["base_total_score"], 56)
        self.assertGreaterEqual(result["top_articles"][0]["adjusted_total_score"], 56)
        self.assertIn("applied_adjustments", result["top_articles"][0]["score_breakdown"])
        self.assertEqual(result["scored_snapshot_path"], "reports/mock.json")

    @patch(
        "digest.workflow.nodes.batch_deep_process_node.run_json_inference_meta",
        return_value=(
            {
                "articles": [
                    {
                        "item_id": "top_1",
                        "deep_analysis": "## Executive Note\nSignal mạnh.\n\n## Source Snapshot\nNguồn tốt.",
                        "recommend_idea": "### 💡 Ý tưởng chính\nThử pilot nhỏ.",
                        "note_summary_vi": "OpenAI ra mắt SDK agent mới cho enterprise.",
                    }
                ]
            },
            "{}",
            True,
        ),
    )
    @patch("digest.workflow.nodes.batch_deep_process_node._search_community_reactions", return_value="Chưa có dữ liệu cộng đồng")
    def test_batch_deep_process_returns_analyzed_articles(self, _mock_search, _mock_infer) -> None:
        article = {
            "title": "OpenAI launches new agent SDK",
            "url": "https://example.com/a",
            "source": "OpenAI",
            "source_domain": "openai.com",
            "source_tier": "a",
            "source_verified": True,
            "content_available": True,
            "content": "A new SDK for building production agents.",
            "snippet": "A new SDK for building production agents.",
            "published_at": "2026-04-06T00:00:00+00:00",
            "primary_type": "Product",
            "editorial_angle": "Useful for enterprise builders.",
            "summary_vi": "OpenAI công bố SDK agent mới.",
            "total_score": 64,
            "is_ai_relevant": True,
        }
        result = batch_deep_process_node({"top_articles": [article]})
        self.assertEqual(len(result["analyzed_articles"]), 1)
        self.assertIn("deep_analysis", result["analyzed_articles"][0])
        self.assertIn("recommend_idea", result["analyzed_articles"][0])
        self.assertIn("note_summary_vi", result["analyzed_articles"][0])

    def test_batch_quick_compose_fast_mode_uses_fallback(self) -> None:
        article = {
            "title": "Small tooling update",
            "summary_vi": "Bản cập nhật nhỏ cho công cụ AI.",
            "primary_type": "Practical",
            "total_score": 32,
            "source": "Blog",
        }
        result = batch_quick_compose_node(
            {
                "low_score_articles": [article],
                "run_profile": "fast",
                "runtime_config": {},
            }
        )
        self.assertEqual(len(result["low_score_articles"]), 1)
        self.assertTrue(result["low_score_articles"][0]["note_summary_vi"])

    def test_build_graph_compiles_mvp3_flow(self) -> None:
        app = build_graph()
        self.assertIsNotNone(app)


if __name__ == "__main__":
    unittest.main()
