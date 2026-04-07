import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from digest.editorial.delivery_policy import apply_main_brief_routing
from digest.workflow.nodes.delivery_judge import (
    _deterministic_delivery_assessment,
    _select_main_brief_candidates,
    delivery_judge_node,
)


def _selection_article(**overrides):
    article = {
        "title": "Article",
        "source_kind": "strong_media",
        "source_domain": "techcrunch.com",
        "primary_type": "Product",
        "main_brief_score": 68,
        "delivery_score": 12,
        "total_score": 74,
        "main_brief_reason_codes": ["source_kind:strong_media", "source_advantage:strong_media"],
    }
    article.update(overrides)
    return article


class DeliveryJudgeTest(unittest.TestCase):
    def test_duplicate_event_returns_duplicate_event_skip_reason(self) -> None:
        article = {
            "title": "OpenAI launches new agent SDK for enterprise workflows",
            "source_kind": "official",
            "source_domain": "openai.com",
            "total_score": 62,
            "relevance_level": "High",
            "tags": ["api_platform", "ai_agents", "enterprise_ai"],
            "content_available": True,
            "source_verified": True,
            "freshness_bucket": "fresh",
            "is_ai_relevant": True,
            "event_is_primary": False,
            "event_cluster_size": 2,
        }
        apply_main_brief_routing(article)

        result = _deterministic_delivery_assessment(article)

        self.assertEqual(result["decision"], "skip")
        self.assertEqual(result["skip_reason"], "duplicate_event")

    def test_select_main_brief_prefers_official_source_when_available(self) -> None:
        official = {
            "title": "OpenAI launches enterprise admin APIs",
            "source_kind": "official",
            "source_domain": "openai.com",
            "primary_type": "Product",
            "main_brief_score": 64,
            "delivery_score": 13,
            "total_score": 70,
            "main_brief_reason_codes": ["source_kind:official"],
        }
        regional = {
            "title": "Genk recaps OpenAI enterprise admin APIs",
            "source_kind": "regional_media",
            "source_domain": "genk.vn",
            "primary_type": "Product",
            "main_brief_score": 66,
            "delivery_score": 11,
            "total_score": 72,
            "main_brief_reason_codes": ["source_kind:regional_media", "source_penalty:proxy"],
        }
        articles = [
            official,
            regional,
            {
                "title": "Strong media coverage",
                "source_kind": "strong_media",
                "source_domain": "techcrunch.com",
                "primary_type": "Product",
                "main_brief_score": 68,
                "delivery_score": 13,
                "total_score": 74,
                "main_brief_reason_codes": ["source_kind:strong_media"],
            },
            {
                "title": "Infra rollout",
                "source_kind": "strong_media",
                "source_domain": "theverge.com",
                "primary_type": "Product",
                "main_brief_score": 67,
                "delivery_score": 12,
                "total_score": 73,
                "main_brief_reason_codes": ["source_kind:strong_media"],
            },
        ]

        selected = _select_main_brief_candidates(articles)

        self.assertTrue(any(item["source_kind"] in {"official", "strong_media"} for item in selected))
        self.assertTrue(any(item["source_domain"] == "openai.com" for item in selected))

    def test_select_main_brief_caps_size_to_six_items(self) -> None:
        articles = []
        for index in range(8):
            articles.append(
                {
                    "title": f"Article {index}",
                    "source_kind": "strong_media" if index % 2 == 0 else "official",
                    "source_domain": f"example{index}.com",
                    "primary_type": "Product",
                    "main_brief_score": 75 - index,
                    "delivery_score": 13,
                    "total_score": 80 - index,
                    "main_brief_reason_codes": [f"source_kind:{'strong_media' if index % 2 == 0 else 'official'}"],
                }
            )

        selected = _select_main_brief_candidates(articles)

        self.assertLessEqual(len(selected), 6)

    def test_select_main_brief_keeps_high_consequence_society_item(self) -> None:
        selected = _select_main_brief_candidates(
            [
                _selection_article(
                    title="EU compute rules for frontier AI deployers",
                    primary_type="Society & Culture",
                    source_kind="strong_media",
                    source_domain="reuters.com",
                    main_brief_score=70,
                    delivery_score=13,
                    total_score=76,
                    main_brief_reason_codes=[
                        "source_kind:strong_media",
                        "source_advantage:strong_media",
                        "society_high_consequence",
                        "society_ecosystem_implication",
                    ],
                ),
                _selection_article(
                    title="OpenAI launches enterprise admin APIs",
                    source_kind="official",
                    source_domain="openai.com",
                    main_brief_score=72,
                    delivery_score=13,
                    total_score=79,
                    main_brief_reason_codes=["source_kind:official", "source_advantage:official"],
                ),
            ]
        )

        self.assertTrue(any(item["primary_type"] == "Society & Culture" for item in selected))

    def test_select_main_brief_is_smaller_when_no_official_and_pool_is_noisy(self) -> None:
        articles = [
            _selection_article(title="Strong media 1", main_brief_score=71, delivery_score=13, total_score=78),
            _selection_article(
                title="Strong media 2",
                source_domain="theverge.com",
                main_brief_score=69,
                delivery_score=12,
                total_score=75,
            ),
            _selection_article(
                title="Regional recap 1",
                source_kind="regional_media",
                source_domain="genk.vn",
                main_brief_score=72,
                delivery_score=12,
                total_score=77,
                main_brief_reason_codes=["source_kind:regional_media", "source_penalty:proxy"],
            ),
            _selection_article(
                title="Regional recap 2",
                source_kind="regional_media",
                source_domain="genk.vn",
                main_brief_score=68,
                delivery_score=11,
                total_score=73,
                main_brief_reason_codes=["source_kind:regional_media", "source_penalty:proxy"],
            ),
            _selection_article(
                title="Thin GitHub toolkit",
                source_kind="github",
                source_domain="github.com",
                main_brief_score=76,
                delivery_score=13,
                total_score=80,
                main_brief_reason_codes=["source_kind:github", "github_low_impact"],
            ),
            _selection_article(
                title="Generic framework chatter",
                source_kind="github",
                source_domain="github.com",
                main_brief_score=74,
                delivery_score=12,
                total_score=79,
                main_brief_reason_codes=["source_kind:github", "github_significant"],
            ),
        ]

        selected = _select_main_brief_candidates(articles, reviewed_articles=articles)

        self.assertLessEqual(len(selected), 4)
        self.assertTrue(all(item["source_kind"] in {"strong_media", "official"} for item in selected))

    def test_select_main_brief_does_not_auto_pass_generic_include_candidates(self) -> None:
        selected = _select_main_brief_candidates(
            [
                _selection_article(
                    title="OpenAI launches enterprise admin APIs",
                    source_kind="official",
                    source_domain="openai.com",
                    main_brief_score=73,
                    delivery_score=13,
                    total_score=80,
                    main_brief_reason_codes=["source_kind:official", "source_advantage:official"],
                ),
                _selection_article(
                    title="Generic market color piece",
                    source_kind="strong_media",
                    source_domain="techcrunch.com",
                    main_brief_score=58,
                    delivery_score=10,
                    total_score=63,
                    main_brief_reason_codes=["source_kind:strong_media", "source_advantage:strong_media"],
                ),
            ]
        )

        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["source_domain"], "openai.com")

    def test_delivery_judge_keeps_generic_github_noise_out_of_main_brief(self) -> None:
        result = delivery_judge_node(
            {
                "final_articles": [
                    {
                        "title": "Anthropic ships new enterprise admin controls",
                        "source_kind": "strong_media",
                        "source": "RSS: TechCrunch",
                        "source_domain": "techcrunch.com",
                        "primary_type": "Product",
                        "total_score": 78,
                        "relevance_level": "High",
                        "tags": ["product_update", "enterprise_ai", "api_platform"],
                        "content_available": True,
                        "source_verified": True,
                        "source_tier": "a",
                        "is_ai_relevant": True,
                        "event_is_primary": True,
                        "freshness_bucket": "fresh",
                        "freshness_unknown": False,
                        "is_stale_candidate": False,
                        "is_old_news": False,
                    },
                    {
                        "title": "someone/cool-agent-framework",
                        "source_kind": "github",
                        "source": "GitHub API Repo: someone/cool-agent-framework",
                        "source_domain": "github.com",
                        "primary_type": "Practical",
                        "total_score": 82,
                        "content_available": True,
                        "source_verified": True,
                        "source_tier": "a",
                        "is_ai_relevant": True,
                        "event_is_primary": True,
                        "freshness_bucket": "breaking",
                        "freshness_unknown": False,
                        "is_stale_candidate": False,
                        "is_old_news": False,
                        "github_signal_type": "repository",
                        "github_full_name": "someone/cool-agent-framework",
                        "github_stars": 900,
                        "tags": ["ai_agents"],
                    },
                ]
            }
        )

        self.assertEqual(len(result["telegram_candidates"]), 1)
        self.assertEqual(result["telegram_candidates"][0]["source_domain"], "techcrunch.com")


if __name__ == "__main__":
    unittest.main()
