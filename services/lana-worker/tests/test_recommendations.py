import unittest
from unittest.mock import patch

from app.orchestrator.tools import execute_tool
from app.recommendations import log_recommendation_impressions, rank_recommendation_candidates


class RecommendationRankingTests(unittest.TestCase):
    def test_ranks_heterogeneous_value_candidates(self):
        ranked = rank_recommendation_candidates(
            [
                {
                    "type": "event",
                    "event_id": "event-1",
                    "title": "Coffee walk",
                    "activity_affinity": 0.9,
                    "vicinity_score": 1.0,
                    "connector_score": 0.4,
                    "starts_at": "2026-06-20T10:00:00Z",
                },
                {
                    "type": "neighbor",
                    "candidate_user_id": "user-2",
                    "identity_affinity": 0.8,
                    "vicinity_score": 1.0,
                    "connector_score": 0.75,
                    "responsiveness_score": 0.6,
                    "matching_peer_label": "Brazilian coffee mom",
                },
                {
                    "type": "local_signal",
                    "signal_id": "sig-1",
                    "category": "swap_offer_bike",
                    "excerpt": "Toddler bike available",
                    "activity_affinity": 0.7,
                    "vicinity_score": 1.0,
                },
            ],
            query="coffee walk",
        )
        self.assertEqual(ranked[0]["type"], "event")
        self.assertIn("suggested_action", ranked[0])
        self.assertIn("safe_reason", ranked[0])
        self.assertTrue(all("reason_codes" in r for r in ranked))
        self.assertEqual({r["type"] for r in ranked}, {"neighbor", "event", "local_signal"})

    def test_filters_serious_safety_penalty(self):
        ranked = rank_recommendation_candidates(
            [
                {
                    "type": "neighbor",
                    "candidate_user_id": "unsafe",
                    "identity_affinity": 1.0,
                    "vicinity_score": 1.0,
                    "safety_penalty": 1.0,
                },
                {
                    "type": "event",
                    "event_id": "event-1",
                    "title": "Park hang",
                    "activity_affinity": 0.5,
                    "vicinity_score": 1.0,
                },
            ]
        )
        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0]["event_id"], "event-1")


class RecommendationToolTests(unittest.TestCase):
    @patch("app.recommendations.recommend_value_for_user")
    def test_tool_stamps_recommendations_on_context(self, mock_recommend):
        mock_recommend.return_value = [
            {
                "type": "event",
                "event_id": "event-1",
                "score": 0.7,
                "reason_codes": ["same_block", "open_activity"],
                "suggested_action": "suggest_activity_invite",
                "safe_reason": "an open nearby activity that may fit",
                "title": "Potluck",
            }
        ]
        ctx = {}
        result = execute_tool(
            tool_name="recommend_value",
            tool_args={"query": "potluck", "limit": 3},
            user_id="user-1",
            user_jwt="jwt",
            session_id="session-1",
            block_id="block-1",
            purpose="lana",
            session_ctx=ctx,
            source_module="test",
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["count"], 1)
        self.assertEqual(ctx["recommendations"][0]["event_id"], "event-1")


class RecommendationImpressionTests(unittest.TestCase):
    @patch("app.recommendations.service_client")
    def test_logs_impressions_best_effort(self, mock_service_client):
        mock_table = mock_service_client.return_value.table.return_value
        log_recommendation_impressions(
            user_id="user-1",
            session_id="session-1",
            block_id="block-1",
            query="bike",
            recommendations=[
                {
                    "type": "local_signal",
                    "signal_id": "sig-1",
                    "score": 0.67,
                    "reason_codes": ["same_block", "swap_offer"],
                    "suggested_action": "suggest_swap_followup",
                    "safe_reason": "a nearby bike signal may fit",
                    "signal_kind": "swap_offer",
                    "category": "swap_offer_bike",
                }
            ],
        )
        mock_service_client.return_value.table.assert_called_once_with("recommendation_impressions")
        inserted = mock_table.insert.call_args.args[0]
        self.assertEqual(inserted[0]["recommendation_type"], "local_signal")
        self.assertEqual(inserted[0]["status"], "shown")
        self.assertEqual(inserted[0]["reason_codes"], ["same_block", "swap_offer"])

    @patch("app.recommendations.service_client")
    def test_logging_failure_does_not_raise(self, mock_service_client):
        mock_service_client.return_value.table.return_value.insert.side_effect = RuntimeError("missing table")
        log_recommendation_impressions(
            user_id="user-1",
            session_id="session-1",
            block_id="block-1",
            recommendations=[
                {
                    "type": "event",
                    "event_id": "event-1",
                    "score": 0.5,
                    "reason_codes": ["open_activity"],
                    "suggested_action": "suggest_activity_invite",
                    "safe_reason": "nearby activity",
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
