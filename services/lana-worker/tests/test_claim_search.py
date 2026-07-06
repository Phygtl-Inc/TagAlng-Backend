import unittest

from app.claim_search import (
    filters_to_rpc_payload,
    parse_claim_filters,
    peer_matches_identity_snippet,
    peer_heritage_key,
)


def _format_attr_empty(filter_text: str, partial_summary: str | None) -> str:
    if partial_summary:
        return (
            f"No one on your block matches all of \"{filter_text}\" yet. "
            f"{partial_summary}"
        )
    return f"I don't see neighbors matching \"{filter_text}\" on your block yet."


class TestClaimSearch(unittest.TestCase):
    def test_parse_pakistani_mom(self) -> None:
        filters = parse_claim_filters("find pakistani mom")
        buckets = {f.bucket for f in filters}
        self.assertIn("heritage", buckets)
        self.assertIn("stage", buckets)
        payload = filters_to_rpc_payload(filters)
        self.assertEqual(len(payload), 2)
        heritage = next(p for p in payload if p["bucket"] == "heritage")
        self.assertIn("pakistani", heritage["terms"])

    def test_parse_brazilian_mom(self) -> None:
        filters = parse_claim_filters("brazilian mom")
        payload = filters_to_rpc_payload(filters)
        heritage = next(p for p in payload if p["bucket"] == "heritage")
        self.assertIn("brazilian", heritage["terms"])
        stage = next(p for p in payload if p["bucket"] == "stage")
        self.assertIn("mom", stage["terms"])

    def test_heritage_mismatch_blocks_intro(self) -> None:
        peer = {"matching_peer_label": "Pakistani Heritage", "matching_peer_concept": "pakistani"}
        self.assertFalse(peer_matches_identity_snippet(peer, "brazilian mom"))
        self.assertEqual(peer_heritage_key(peer), "pakistani")

    def test_heritage_match_allows_intro(self) -> None:
        peer = {"matching_peer_label": "Brazilian mom", "matching_peer_concept": "brazilian"}
        self.assertTrue(peer_matches_identity_snippet(peer, "brazilian mom"))

    def test_bare_name_reply_is_not_a_match(self) -> None:
        # Regression: user answered "what should I call you?" with their name, which was
        # captured as the identity snippet. It must NOT auto-match an unrelated peer label.
        peer = {"matching_peer_label": "Married 10 years"}
        self.assertFalse(peer_matches_identity_snippet(peer, "tony"))

    def test_unrelated_interest_is_not_a_match(self) -> None:
        peer = {"matching_peer_label": "Married 10 years"}
        self.assertFalse(peer_matches_identity_snippet(peer, "running Saturday mornings"))

    def test_overlapping_interest_still_matches(self) -> None:
        peer = {"matching_peer_label": "Morning runners"}
        self.assertTrue(peer_matches_identity_snippet(peer, "running Saturday mornings"))

    def test_format_attr_partial_summary(self) -> None:
        reply = _format_attr_empty(
            "pakistani mom",
            "I see 1 neighbor matching \"pakistani\", 3 neighbors matching \"mom\", but no one with all of those yet.",
        )
        self.assertIn("No one on your block matches all", reply)
        self.assertIn("pakistani", reply.lower())


if __name__ == "__main__":
    unittest.main()
