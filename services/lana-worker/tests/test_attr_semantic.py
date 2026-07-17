"""Semantic fallback for discovery.find_by_attrs — meaning match, no word lists."""

import unittest
from unittest.mock import patch

from app.claim_search import (
    attr_display_filter,
    parse_claim_filters,
    peer_matches_identity_snippet,
)
from app.layer1_handlers import (
    fetch_peers_by_attr_filter,
    fetch_peers_semantic,
    format_attr_peers_reply,
    peers_to_match_rows,
)

_FAKE_EMBEDDING = [0.01] * 768

_SEMANTIC_ROW = {
    "peer_user_id": "p1",
    "nickname": "Daniel",
    "avatar_url": None,
    "similarity_score": 0.71,
    "matching_peer_label": "Gym enthusiast",
    "matching_peer_concept": "gym_enthusiast",
    "has_exact_concept_match": False,
}


class TestAttrSemanticFallback(unittest.TestCase):
    @patch("app.layer1_handlers._embed_attr_filter", return_value=_FAKE_EMBEDDING)
    @patch("app.layer1_handlers.call_rpc")
    def test_semantic_fallback_when_lexical_empty(self, mock_rpc, _mock_embed) -> None:
        def rpc_side_effect(_jwt, name, _payload):
            if name == "find_peers_by_claim_semantic":
                return [dict(_SEMANTIC_ROW)]
            return []

        mock_rpc.side_effect = rpc_side_effect
        peers = fetch_peers_by_attr_filter("jwt", "gymmer", limit=5)
        self.assertEqual(len(peers), 1)
        self.assertEqual(peers[0]["matching_peer_label"], "Gym enthusiast")
        self.assertTrue(peers[0]["semantic_match"])
        rpc_names = [c.args[1] for c in mock_rpc.call_args_list]
        self.assertIn("find_peers_by_claim_semantic", rpc_names)

    @patch("app.layer1_handlers._embed_attr_filter")
    @patch("app.layer1_handlers.call_rpc")
    def test_no_semantic_call_when_lexical_hits(self, mock_rpc, mock_embed) -> None:
        lexical_row = {
            "peer_user_id": "p2",
            "matching_peer_label": "Italian heritage",
        }

        def rpc_side_effect(_jwt, name, _payload):
            if name == "find_peers_by_claim_semantic":
                raise AssertionError("semantic RPC must not run when lexical matched")
            return [dict(lexical_row)]

        mock_rpc.side_effect = rpc_side_effect
        peers = fetch_peers_by_attr_filter("jwt", "italians", limit=5)
        self.assertTrue(peers)
        self.assertFalse(peers[0].get("semantic_match"))
        mock_embed.assert_not_called()

    @patch("app.layer1_handlers._embed_attr_filter", return_value=None)
    @patch("app.layer1_handlers.call_rpc", return_value=[])
    def test_embed_failure_degrades_to_empty(self, mock_rpc, _mock_embed) -> None:
        peers = fetch_peers_by_attr_filter("jwt", "gymmer", limit=5)
        self.assertEqual(peers, [])
        rpc_names = [c.args[1] for c in mock_rpc.call_args_list]
        self.assertNotIn("find_peers_by_claim_semantic", rpc_names)

    @patch("app.layer1_handlers._embed_attr_filter", return_value=_FAKE_EMBEDDING)
    @patch("app.layer1_handlers.call_rpc")
    def test_semantic_rpc_payload_uses_pgvector_literal(self, mock_rpc, _mock_embed) -> None:
        mock_rpc.return_value = []
        fetch_peers_semantic("jwt", "gymmer", limit=5)
        payload = mock_rpc.call_args.args[2]
        self.assertTrue(payload["p_query_embedding"].startswith("["))
        self.assertEqual(payload["p_limit"], 5)
        self.assertGreater(payload["p_min_similarity"], 0)


class TestAttrSemanticReply(unittest.TestCase):
    def test_semantic_reply_is_truthful(self) -> None:
        peers = [dict(_SEMANTIC_ROW, semantic_match=True)]
        reply = format_attr_peers_reply(peers, filter_text="gymmer")
        self.assertIn("word-for-word", reply)
        self.assertIn("gymmer", reply)
        self.assertNotIn("matching \"gymmer\"", reply)

    def test_lexical_reply_unchanged(self) -> None:
        peers = [{"peer_user_id": "p2", "matching_peer_label": "Italian heritage"}]
        reply = format_attr_peers_reply(peers, filter_text="italians")
        self.assertIn("matching \"italians\"", reply)

    def test_match_rows_carry_semantic_flag(self) -> None:
        rows = peers_to_match_rows(
            [dict(_SEMANTIC_ROW, semantic_match=True)], phone_verified=True
        )
        self.assertTrue(rows[0]["semantic_match"])


class TestSemanticQueryTexts(unittest.TestCase):
    @patch("app.layer1_handlers._embed_attr_filter", return_value=_FAKE_EMBEDDING)
    @patch("app.layer1_handlers.call_rpc", return_value=[])
    def test_embeds_ai_terms_not_raw_sentence(self, _mock_rpc, mock_embed) -> None:
        from app.layer1_handlers import fetch_peers_semantic

        fetch_peers_semantic(
            "jwt",
            "people in my block that likes paragliding",
            limit=5,
            slots={"attr_terms": [["paragliding", "paraglider"]]},
        )
        # The location/verb filler must never reach the embedder — it cosine-matches
        # boilerplate like "Block Resident".
        mock_embed.assert_called_once_with("paragliding paraglider")

    @patch("app.layer1_handlers._embed_attr_filter", return_value=_FAKE_EMBEDDING)
    @patch("app.layer1_handlers.call_rpc")
    def test_multi_group_intersects_and_takes_weakest_score(self, mock_rpc, _e) -> None:
        from app.layer1_handlers import fetch_peers_semantic

        rows_by_call = [
            [
                {"peer_user_id": "p1", "similarity_score": 0.8, "matching_peer_label": "Brazilian roots"},
                {"peer_user_id": "p2", "similarity_score": 0.7, "matching_peer_label": "From Brazil"},
            ],
            [
                {"peer_user_id": "p1", "similarity_score": 0.6, "matching_peer_label": "Swims Regularly"},
            ],
        ]
        mock_rpc.side_effect = lambda *_a, **_k: rows_by_call.pop(0)
        peers = fetch_peers_semantic(
            "jwt",
            "brazilians who swim",
            limit=5,
            slots={"attr_terms": [["brazilian"], ["swim"]]},
        )
        self.assertEqual(len(peers), 1)
        self.assertEqual(peers[0]["peer_user_id"], "p1")
        self.assertEqual(peers[0]["similarity_score"], 0.6)
        self.assertEqual(peers[0]["matching_peer_label"], "Brazilian roots · Swims Regularly")
        self.assertTrue(peers[0]["semantic_match"])

    @patch("app.layer1_handlers._embed_attr_filter", return_value=_FAKE_EMBEDDING)
    @patch("app.layer1_handlers.call_rpc", return_value=[])
    def test_falls_back_to_filter_text_without_ai_terms(self, _mock_rpc, mock_embed) -> None:
        from app.layer1_handlers import fetch_peers_semantic

        fetch_peers_semantic("jwt", "gymmer", limit=5)
        mock_embed.assert_called_once_with("gymmer")


class TestAttrDisplayFilter(unittest.TestCase):
    def test_display_uses_group_heads(self) -> None:
        slots = {"attr_terms": [["paragliding", "paraglider"]]}
        self.assertEqual(
            attr_display_filter("people in my block that likes paragliding", slots),
            "paragliding",
        )

    def test_display_joins_multiple_requirements(self) -> None:
        slots = {"attr_terms": [["brazilian", "brazil"], ["swim"]]}
        self.assertEqual(attr_display_filter("brazilians who swim", slots), "brazilian + swim")

    def test_display_falls_back_to_filter_text(self) -> None:
        self.assertEqual(attr_display_filter("gymmer", {}), "gymmer")


class TestAttrTermsFilters(unittest.TestCase):
    def test_ai_terms_win_over_tokenization(self) -> None:
        slots = {"attr_terms": [["swim", "swimming", "swimmer"]]}
        filters = parse_claim_filters("likes to swim", slots)
        self.assertEqual(len(filters), 1)
        self.assertIsNone(filters[0].bucket)
        self.assertEqual(filters[0].terms, ["swim", "swimming", "swimmer"])

    def test_ai_terms_multiple_groups_are_anded(self) -> None:
        slots = {"attr_terms": [["brazilian", "brazil"], ["swim", "swimming"]]}
        filters = parse_claim_filters("brazilians who swim", slots)
        self.assertEqual(len(filters), 2)

    def test_malformed_ai_terms_fall_back(self) -> None:
        slots = {"attr_terms": "swim"}
        filters = parse_claim_filters("pakistani mom", slots)
        buckets = {f.bucket for f in filters}
        self.assertIn("heritage", buckets)
        self.assertIn("stage", buckets)

    def test_fallback_general_tokens_are_anded_not_ored(self) -> None:
        filters = parse_claim_filters("likes to swim")
        # "like/likes" fold into one requirement, "swim" is another — AND across them,
        # so a peer with only "Likes Parks" no longer qualifies.
        self.assertEqual(len(filters), 2)
        self.assertEqual(filters[0].terms, ["like", "likes"])
        self.assertEqual(filters[1].terms, ["swim"])


class TestSemanticIntroGate(unittest.TestCase):
    def test_semantic_match_passes_snippet_gate(self) -> None:
        peer = {"matching_peer_label": "Gym enthusiast", "semantic_match": True}
        self.assertTrue(peer_matches_identity_snippet(peer, "gymmer"))

    def test_non_semantic_no_overlap_still_blocked(self) -> None:
        peer = {"matching_peer_label": "Married 10 years"}
        self.assertFalse(peer_matches_identity_snippet(peer, "gymmer"))

    def test_heritage_mismatch_still_blocks_semantic_match(self) -> None:
        peer = {
            "matching_peer_label": "Mexican Heritage",
            "matching_peer_concept": "mexican",
            "semantic_match": True,
        }
        self.assertFalse(peer_matches_identity_snippet(peer, "italians"))


if __name__ == "__main__":
    unittest.main()
