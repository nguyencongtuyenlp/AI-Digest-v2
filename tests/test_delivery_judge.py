import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from digest.editorial.delivery_policy import apply_main_brief_routing
from digest.workflow.nodes.delivery_judge import _deterministic_delivery_assessment


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


if __name__ == "__main__":
    unittest.main()
