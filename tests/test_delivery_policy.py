import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from digest.editorial.delivery_policy import apply_main_brief_routing


def _base_article(**overrides):
    article = {
        "is_ai_relevant": True,
        "content_available": True,
        "source_verified": True,
        "event_is_primary": True,
        "event_cluster_size": 1,
        "freshness_bucket": "fresh",
        "tags": [],
        "relevance_level": "Medium",
        "total_score": 50,
    }
    article.update(overrides)
    return article


class DeliveryPolicyTest(unittest.TestCase):
    def test_official_fresh_actionable_article_routes_to_main_eligible(self) -> None:
        article = _base_article(
            title="OpenAI launches new agent SDK for enterprise workflows",
            source_kind="official",
            source_domain="openai.com",
            total_score=62,
            relevance_level="High",
            tags=["api_platform", "ai_agents", "enterprise_ai"],
        )

        apply_main_brief_routing(article)

        self.assertEqual(article["delivery_lane_candidate"], "main")
        self.assertEqual(article["main_brief_eligibility"], "eligible")
        self.assertEqual(article["main_brief_skip_reason"], "")

    def test_strong_media_fresh_article_routes_to_main_review_or_eligible(self) -> None:
        article = _base_article(
            title="Anthropic rolls out new Claude Code pricing for enterprise teams",
            source_kind="strong_media",
            source_domain="techcrunch.com",
            total_score=55,
            tags=["product_update", "developer_tools", "enterprise_ai"],
        )

        apply_main_brief_routing(article)

        self.assertEqual(article["delivery_lane_candidate"], "main")
        self.assertIn(article["main_brief_eligibility"], {"eligible", "review"})

    def test_generic_github_repo_routes_to_github_topic_only(self) -> None:
        article = _base_article(
            title="someone/cool-agent-framework",
            source_kind="github",
            source_domain="github.com",
            github_signal_type="repository",
            total_score=66,
            relevance_level="High",
            freshness_bucket="breaking",
            tags=["ai_agents"],
        )

        apply_main_brief_routing(article)

        self.assertEqual(article["delivery_lane_candidate"], "github")
        self.assertEqual(article["main_brief_skip_reason"], "github_topic_only")

    def test_significant_github_release_can_route_to_main(self) -> None:
        article = _base_article(
            title="Anthropic releases Claude Code SDK v2 for enterprise MCP workflows",
            source_kind="github",
            source_domain="github.com",
            github_signal_type="release",
            github_stars=6200,
            watchlist_hit=True,
            total_score=68,
            relevance_level="High",
            tags=["api_platform", "developer_tools", "enterprise_ai", "infrastructure"],
        )

        apply_main_brief_routing(article)

        self.assertEqual(article["delivery_lane_candidate"], "main")
        self.assertIn(article["main_brief_eligibility"], {"eligible", "review"})

    def test_thin_github_plugin_routes_out_of_main_brief(self) -> None:
        article = _base_article(
            title="someone/awesome-claude-plugin-template",
            source_kind="github",
            source_domain="github.com",
            github_signal_type="repository",
            github_full_name="someone/awesome-claude-plugin-template",
            github_stars=420,
            total_score=72,
            relevance_level="High",
            freshness_bucket="breaking",
            tags=["developer_tools"],
        )

        apply_main_brief_routing(article)

        self.assertEqual(article["delivery_lane_candidate"], "github")
        self.assertEqual(article["main_brief_skip_reason"], "github_topic_only")

    def test_weak_society_reflection_routes_out_of_main_brief(self) -> None:
        article = _base_article(
            title="My thoughts on AI culture after a week of reading blog posts",
            source_kind="strong_media",
            source_domain="example.com",
            primary_type="Society & Culture",
            total_score=64,
            relevance_level="Medium",
            tags=["education"],
            snippet="A reflective essay and opinion piece about how society is changing.",
        )

        apply_main_brief_routing(article)

        self.assertEqual(article["delivery_lane_candidate"], "archive_only")
        self.assertIn(article["main_brief_skip_reason"], {"weak_signal", "low_operator_value"})

    def test_high_consequence_society_item_can_still_route_to_main(self) -> None:
        article = _base_article(
            title="Reuters: EU proposes new compute reporting rules for frontier AI deployers",
            source_kind="strong_media",
            source_domain="reuters.com",
            primary_type="Society & Culture",
            total_score=69,
            relevance_level="High",
            tags=["regulation", "safety", "infrastructure"],
            snippet="New compliance and compute reporting rules would affect model deployment and AI infrastructure operators.",
        )

        apply_main_brief_routing(article)

        self.assertEqual(article["delivery_lane_candidate"], "main")
        self.assertIn(article["main_brief_eligibility"], {"eligible", "review"})

    def test_official_clinic_workflow_article_routes_to_main_eligible_from_operator_hints(self) -> None:
        article = _base_article(
            title="NVIDIA launches clinic workflow copilot with patient scheduling and human-in-the-loop review",
            source_kind="official",
            source_domain="nvidia.com",
            total_score=46,
            tags=[],
        )

        apply_main_brief_routing(article)

        self.assertEqual(article["delivery_lane_candidate"], "main")
        self.assertEqual(article["main_brief_eligibility"], "eligible")
        self.assertEqual(article["main_brief_skip_reason"], "")

    def test_strong_media_observability_and_local_deployment_article_routes_to_main(self) -> None:
        article = _base_article(
            title="TechCrunch covers AI observability and local deployment patterns for agent operations teams",
            source_kind="strong_media",
            source_domain="techcrunch.com",
            total_score=40,
            tags=[],
        )

        apply_main_brief_routing(article)

        self.assertEqual(article["delivery_lane_candidate"], "main")
        self.assertIn(article["main_brief_eligibility"], {"eligible", "review"})

    def test_stale_article_routes_to_stale(self) -> None:
        article = _base_article(
            title="OpenAI launches new agent SDK for enterprise workflows",
            source_kind="official",
            source_domain="openai.com",
            total_score=62,
            relevance_level="High",
            tags=["api_platform", "ai_agents", "enterprise_ai"],
            freshness_bucket="stale",
            is_old_news=True,
        )

        apply_main_brief_routing(article)

        self.assertEqual(article["delivery_lane_candidate"], "archive_only")
        self.assertEqual(article["main_brief_skip_reason"], "stale")

    def test_speculative_community_post_routes_to_speculation(self) -> None:
        article = _base_article(
            title="Rumor: OpenAI might acquire a robotics startup",
            source_kind="community",
            source_domain="reddit.com",
            total_score=58,
            freshness_bucket="breaking",
            tags=["market_competition"],
        )

        apply_main_brief_routing(article)

        self.assertEqual(article["delivery_lane_candidate"], "archive_only")
        self.assertEqual(article["main_brief_skip_reason"], "speculation")


if __name__ == "__main__":
    unittest.main()
