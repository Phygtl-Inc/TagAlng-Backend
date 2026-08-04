"""What they said this turn must outrank what we stored weeks ago.

QA 2026-08-03: the policy got the user's claims as an unordered bag with no
notion of what the turn was about, while the prompt asks any topic change to be
justified by "something THEY told you". So the model rummaged and produced "is
there a favorite blue thing that cheers you up?" — twice — while the person was
saying their stomach hurt and food was their only comfort. The relevant fact,
said one line earlier, had no more weight than a colour from a fortnight before.

Every failure here must fall back to the previous unranked read rather than
degrade the turn: no embedding, no RPC (pre-20260929 environment), or a DB error.
"""

import unittest
from unittest.mock import patch

from app.policy.decide import CLAIM_RELEVANCE_FLOOR, _claims


_RANKED_ROWS = [
    {"concept": "steak_lover", "label": "Loves steakhouses", "details": [],
     "bucket": "interest", "similarity": 0.81},
    {"concept": "favorite_color_blue", "label": "Favourite colour is blue", "details": [],
     "bucket": "interest", "similarity": 0.12},
]


class _Res:
    def __init__(self, rows):
        self.data = rows


class _Rpc:
    def __init__(self, rows):
        self._rows = rows

    def execute(self):
        return _Res(self._rows)


class _Client:
    def __init__(self, rows, seen=None):
        self._rows = rows
        self._seen = seen

    def rpc(self, name, params):
        if self._seen is not None:
            self._seen.update({"name": name, "params": params})
        return _Rpc(self._rows)


def _run(rows, *, message="my stomach hurts and food is my comfort", seen=None,
         embed=(0.1,) * 768):
    with patch("app.vertex_extract.vertex_embed", return_value=list(embed) if embed else None), \
         patch("app.auth.service_client", return_value=_Client(rows, seen)):
        return _claims("u1", message)


class TestRelevanceRanking(unittest.TestCase):
    def test_most_relevant_claim_comes_first(self) -> None:
        out = _run(_RANKED_ROWS)
        self.assertEqual(out[0]["concept"], "steak_lover")

    def test_relevance_is_flagged_per_claim(self) -> None:
        out = _run(_RANKED_ROWS)
        self.assertTrue(out[0]["relates_to_this_turn"])
        self.assertFalse(
            out[1]["relates_to_this_turn"],
            "a colour has nothing to do with a stomach ache — the model must be told",
        )

    def test_floor_is_the_boundary(self) -> None:
        rows = [
            {"concept": "a", "label": "A", "details": [], "similarity": CLAIM_RELEVANCE_FLOOR},
            {"concept": "b", "label": "B", "details": [],
             "similarity": CLAIM_RELEVANCE_FLOOR - 0.01},
        ]
        out = _run(rows)
        self.assertTrue(out[0]["relates_to_this_turn"])
        self.assertFalse(out[1]["relates_to_this_turn"])

    def test_message_is_what_gets_ranked_against(self) -> None:
        seen: dict = {}
        _run(_RANKED_ROWS, seen=seen)
        self.assertEqual(seen["name"], "rank_claims_by_relevance")
        self.assertEqual(seen["params"]["p_user_id"], "u1")
        self.assertIn("p_embedding", seen["params"])

    def test_missing_similarity_is_treated_as_irrelevant(self) -> None:
        out = _run([{"concept": "a", "label": "A", "details": [], "similarity": None}])
        self.assertFalse(out[0]["relates_to_this_turn"])


class TestFailsOpenToUnranked(unittest.TestCase):
    """Ranking is an improvement, never a dependency."""

    def _unranked(self):
        return [{"concept": "runner", "label": "Runner", "details": []}]

    def test_no_message_uses_the_plain_read(self) -> None:
        with patch(
            "app.claims_persist.fetch_active_claim_threads", return_value=self._unranked()
        ) as plain:
            out = _claims("u1", "")
        plain.assert_called_once()
        self.assertEqual(out[0]["concept"], "runner")

    def test_embedding_failure_uses_the_plain_read(self) -> None:
        with patch("app.vertex_extract.vertex_embed", return_value=None), \
             patch("app.claims_persist.fetch_active_claim_threads",
                   return_value=self._unranked()):
            out = _claims("u1", "hello there")
        self.assertEqual(out[0]["concept"], "runner")

    def test_missing_rpc_uses_the_plain_read(self) -> None:
        """A pre-20260929 environment has no rank_claims_by_relevance."""
        with patch("app.vertex_extract.vertex_embed", return_value=[0.1] * 768), \
             patch("app.auth.service_client", side_effect=RuntimeError("PGRST202")), \
             patch("app.claims_persist.fetch_active_claim_threads",
                   return_value=self._unranked()):
            out = _claims("u1", "hello there")
        self.assertEqual(out[0]["concept"], "runner")

    def test_no_claims_at_all_is_empty_not_an_error(self) -> None:
        with patch("app.vertex_extract.vertex_embed", return_value=[0.1] * 768), \
             patch("app.auth.service_client", return_value=_Client([])), \
             patch("app.claims_persist.fetch_active_claim_threads", return_value=[]):
            self.assertEqual(_claims("u1", "hello"), [])


if __name__ == "__main__":
    unittest.main()
