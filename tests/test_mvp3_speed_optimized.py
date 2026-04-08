import os
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
                        "factual_summary_vi": "OpenAI công bố SDK agent mới cho workflow enterprise.",
                        "editorial_angle": "SDK này có thể giúp team tăng tốc triển khai agent.",
                        "why_it_matters_vi": "Đội build agent có thêm primitive để triển khai production nhanh hơn.",
                        "optional_editorial_angle": "Tín hiệu tích cực cho builder workflow.",
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
        self.assertEqual(result["top_articles"][0]["classify_json_status"], "valid_json")
        self.assertTrue(result["top_articles"][0]["factual_summary_vi"])
        self.assertTrue(result["top_articles"][0]["why_it_matters_vi"])
        self.assertTrue(result["top_articles"][0]["optional_editorial_angle"])
        self.assertEqual(result["scored_snapshot_path"], "reports/mock.json")

    @patch("digest.workflow.nodes.batch_classify_and_score_node.write_temporal_snapshot", return_value="reports/mock.json")
    @patch(
        "digest.workflow.nodes.batch_classify_and_score_node.run_json_inference_meta",
        return_value=(
            None,
            """```json
            {articles: [{item_id: 'article_0', primary_type: 'Product', primary_emoji: '🚀', c1_score: 20, c1_reason: 'fresh',
            c2_score: 18, c2_reason: 'useful', c3_score: 18, c3_reason: 'fit',
            summary_vi: 'OpenAI công bố SDK agent mới cho enterprise workflows.',
            factual_summary_vi: 'OpenAI công bố SDK agent mới cho workflow enterprise.',
            editorial_angle: 'SDK này có thể giúp team tăng tốc triển khai agent.',
            why_it_matters_vi: 'Đội build agent có thêm primitive để triển khai production nhanh hơn.',
            optional_editorial_angle: 'Tín hiệu tích cực cho builder workflow.',
            analysis_tier: 'deep', tags: ['api_platform'], relevance_level: 'High'}]}
            ```""",
            True,
        ),
    )
    def test_batch_classify_recovers_malformed_batch_json(self, _mock_infer, _mock_snapshot) -> None:
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
        self.assertEqual(result["scored_articles"][0]["classify_json_status"], "repaired_json")
        self.assertEqual(
            result["scored_articles"][0]["why_it_matters_vi"],
            "Đội build agent có thêm primitive để triển khai production nhanh hơn.",
        )

    @patch.dict(os.environ, {"MLX_LIGHT_MODEL": ""}, clear=False)
    @patch("digest.workflow.nodes.batch_classify_and_score_node.write_temporal_snapshot", return_value="reports/mock.json")
    @patch("digest.workflow.nodes.classify_and_score.call_xai_structured_json")
    @patch(
        "digest.workflow.nodes.classify_and_score.run_json_inference",
        side_effect=[(None, "", False)] * 6,
    )
    @patch("digest.workflow.nodes.batch_classify_and_score_node.single_article_json_inference")
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
                        "factual_summary_vi": "OpenAI công bố SDK agent mới cho workflow enterprise.",
                        "editorial_angle": "SDK này có thể giúp team tăng tốc triển khai agent.",
                        "why_it_matters_vi": "Đội build agent có thêm primitive để triển khai production nhanh hơn.",
                        "optional_editorial_angle": "Tín hiệu tích cực cho builder workflow.",
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
    def test_batch_classify_uses_grok_only_for_unstable_single_item_retry(
        self,
        _mock_batch_infer,
        mock_single_article_infer,
        _mock_classify_local_infer,
        mock_grok_json,
        _mock_snapshot,
    ) -> None:
        mock_single_article_infer.side_effect = [
            (None, "", False),
            (None, "", False),
            (None, "", False),
        ]
        mock_grok_json.return_value = {
            "primary_type": "Product",
            "primary_emoji": "🚀",
            "c1_score": 22,
            "c1_reason": "fresh",
            "c2_score": 20,
            "c2_reason": "useful",
            "c3_score": 18,
            "c3_reason": "fit",
            "summary_vi": "Anthropic cập nhật mới cho Claude Code enterprise.",
            "factual_summary_vi": "Anthropic cập nhật mới cho Claude Code enterprise.",
            "editorial_angle": "Đây là tín hiệu vận hành đáng theo dõi.",
            "why_it_matters_vi": "Nó giúp team triển khai agent enterprise có thêm bối cảnh vận hành.",
            "optional_editorial_angle": "Tín hiệu tích cực cho builder workflow.",
            "analysis_tier": "deep",
            "tags": ["api_platform"],
            "relevance_level": "High",
        }

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
                },
                {
                    "title": "Anthropic updates Claude Code enterprise controls",
                    "url": "https://example.com/b",
                    "source": "Anthropic",
                    "source_domain": "anthropic.com",
                    "source_tier": "a",
                    "source_kind": "official",
                    "source_verified": True,
                    "content_available": True,
                    "content": "Anthropic updated enterprise controls for Claude Code.",
                    "snippet": "Anthropic updated enterprise controls for Claude Code.",
                    "published_at": "2026-04-06T00:00:00+00:00",
                    "published_at_source": "source_metadata",
                    "freshness_bucket": "fresh",
                    "is_ai_relevant": True,
                    "event_is_primary": True,
                },
            ],
            "runtime_config": {
                "max_classify_articles": 5,
                "max_deep_analysis_articles": 3,
                "batch_classify_size": 2,
                "use_grok_for_classify": True,
            },
            "feedback_summary_text": "",
            "feedback_preference_profile": {},
        }

        result = batch_classify_and_score_node(state)

        rescued = next(article for article in result["scored_articles"] if article["url"] == "https://example.com/b")
        self.assertEqual(rescued["classify_provider_used"], "local_then_grok")
        self.assertEqual(result["grok_request_count"], 1)
        self.assertEqual(result["grok_stage_usage"]["classify"]["items_processed"], 1)
        self.assertEqual(mock_grok_json.call_count, 1)

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
