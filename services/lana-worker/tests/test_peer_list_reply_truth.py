"""The reply over a neighbours list must be true to the cards under it.

Prod 2026-09-18, session ac89df01…, "find me neighbors nearby" answered with:

    "I've found 5 neighbors nearby who share some interests with you, like being
     parents and enjoying local parks. Let me know if you'd like me to introduce
     you to any of them!"

Under it, five cards — every one "✓ Sent" with no button. Two defects in one
sentence: it offered the single thing that could not be done for any of them, and
it named an overlap that exists nowhere. The rows shared "your gym", "Enjoys
Italian food", "Interested in sports" and "your sushi spot"; the user's own claims
were English/Urdu, gym, grandparent, gaming zone. Nothing about parents or parks.

The composer was told to "point to what they have in common" and handed only a
count, so it filled the gap — the facts it needed were never passed.
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.guest_capabilities import _shared_labels_across, format_peer_matches  # noqa: E402
from app.peer_discovery_surface import stamp_name_disambiguation  # noqa: E402

# The five rows exactly as prod stored them.
PROD_ROWS = [
    {"peer_user_id": "db98744e", "nickname": "Tommaso db", "connection": "intro_sent",
     "similarity_score": 0.892172,
     "shared_labels": ["your gym", "Enjoys Italian food", "Interested in sports"]},
    {"peer_user_id": "52b9d72d", "nickname": "Daniel", "connection": "intro_sent",
     "similarity_score": None, "shared_labels": ["your sushi spot"]},
    {"peer_user_id": "92ec6af9", "nickname": "Dom", "connection": "intro_sent",
     "similarity_score": None, "shared_labels": ["your sushi spot"]},
    {"peer_user_id": "4aaf0e42", "nickname": "Jake", "connection": "intro_sent",
     "similarity_score": None, "shared_labels": ["your sushi spot"]},
    {"peer_user_id": "984f0a98", "nickname": "Jake", "connection": "intro_sent",
     "similarity_score": None, "shared_labels": ["your sushi spot"]},
]


def _compose_args(rows, ctx=None):
    """The goal + facts handed to the composer for these rows."""
    captured = {}

    def fake(**kwargs):
        captured.update(kwargs)
        return kwargs.get("fallback", "")

    with mock.patch("app.guest_capabilities.compose_reply", side_effect=fake):
        reply = format_peer_matches([dict(r) for r in rows], ctx)
    return captured, reply


class NoIntroOfferWhenNoneCanBeIntroduced(unittest.TestCase):
    def test_all_sent_never_offers_an_intro(self) -> None:
        args, fallback = _compose_args(PROD_ROWS)
        goal = args["goal"]
        self.assertIn("NEVER offer an intro", goal)
        self.assertIn("look", goal.lower())
        facts = " ".join(args["facts"])
        self.assertIn("ALREADY introduced", facts)
        # The fallback ships when the model is unavailable — it must obey the same rule.
        self.assertNotIn("introduce you", fallback)

    def test_mixed_list_offers_only_the_reachable_one(self) -> None:
        rows = [dict(r) for r in PROD_ROWS]
        rows[2] = {**rows[2], "connection": None}  # Dom can still be introduced
        args, fallback = _compose_args(rows)
        facts = " ".join(args["facts"])
        self.assertIn("only these can still be introduced: Dom", facts)
        self.assertIn("ONLY to the ones named", args["goal"])
        self.assertIn("Dom", fallback)

    def test_a_row_the_send_would_refuse_is_not_offered(self) -> None:
        """can_nudge False is the same "no button" state as Sent — reach, not tier."""
        rows = [{**r, "connection": None, "can_nudge": False} for r in PROD_ROWS]
        args, _ = _compose_args(rows)
        self.assertIn("ALREADY introduced", " ".join(args["facts"]))

    def test_open_list_still_offers(self) -> None:
        rows = [{**r, "connection": None} for r in PROD_ROWS]
        args, fallback = _compose_args(rows)
        self.assertIn("Offer to introduce them to any of these neighbors", args["goal"])
        self.assertIn("introduce you to any of them", fallback)


class ShareClaimsMustBeReal(unittest.TestCase):
    def test_the_real_labels_are_passed_as_facts(self) -> None:
        args, _ = _compose_args(PROD_ROWS)
        facts = " ".join(args["facts"])
        for label in ("your gym", "Enjoys Italian food", "Interested in sports", "your sushi spot"):
            self.assertIn(label, facts)

    def test_the_composer_is_forbidden_to_invent(self) -> None:
        args, _ = _compose_args(PROD_ROWS)
        self.assertIn("ONLY from the facts", args["goal"])
        self.assertIn("it does not exist", args["goal"])

    def test_no_proven_overlap_says_so(self) -> None:
        rows = [{**r, "shared_labels": []} for r in PROD_ROWS]
        args, _ = _compose_args(rows)
        facts = " ".join(args["facts"])
        self.assertIn("No shared trait was proven", facts)
        self.assertIn("Do not claim shared interests", facts)

    def test_one_sided_labels_are_not_treated_as_shared(self) -> None:
        """matching_peer_label is the fuzzy, one-sided fallback — never "you both"."""
        rows = [{"peer_user_id": "x", "nickname": "Ann", "shared_labels": [],
                 "matching_peer_label": "Runs marathons", "similarity_score": 0.8}]
        self.assertEqual(_shared_labels_across(rows), [])

    def test_labels_carry_how_many_rows_hold_them(self) -> None:
        """The union alone reads as if everyone shares everything."""
        self.assertEqual(
            _shared_labels_across(PROD_ROWS),
            [("your sushi spot", 4), ("your gym", 1),
             ("Enjoys Italian food", 1), ("Interested in sports", 1)],
        )

    def test_a_partial_trait_is_flagged_as_partial(self) -> None:
        args, _ = _compose_args(PROD_ROWS)
        self.assertIn("No trait is held by all of them", " ".join(args["facts"]))

    def test_unscored_rows_are_neighbors_not_matches(self) -> None:
        rows = [{**r, "similarity_score": None} for r in PROD_ROWS]
        args, _ = _compose_args(rows)
        self.assertIn("not matches", " ".join(args["facts"]))


class SameNameRows(unittest.TestCase):
    def test_duplicate_names_get_a_hint_when_one_exists(self) -> None:
        rows = [
            {"peer_user_id": "4aaf0e42", "nickname": "Jake", "distance_text": "1 min walk"},
            {"peer_user_id": "984f0a98", "nickname": "Jake", "distance_text": "9.6 mi away"},
            {"peer_user_id": "92ec6af9", "nickname": "Dom", "distance_text": "1 min walk"},
        ]
        stamp_name_disambiguation(rows)
        self.assertEqual(rows[0]["name_hint"], "1 min walk")
        self.assertEqual(rows[1]["name_hint"], "9.6 mi away")
        self.assertNotIn("name_hint", rows[2], "a unique name needs no hint")

    def test_nothing_distinguishing_means_no_invented_hint(self) -> None:
        rows = [
            {"peer_user_id": "4aaf0e42", "nickname": "Jake"},
            {"peer_user_id": "984f0a98", "nickname": "Jake"},
        ]
        stamp_name_disambiguation(rows)
        self.assertNotIn("name_hint", rows[0])
        self.assertNotIn("name_hint", rows[1])

    def test_nickname_is_never_rewritten(self) -> None:
        """pick_peer_for_intro matches on nickname — decorating it breaks resolution."""
        rows = [
            {"peer_user_id": "a", "nickname": "Jake", "distance_text": "1 min walk"},
            {"peer_user_id": "b", "nickname": "Jake", "distance_text": "9.6 mi away"},
        ]
        stamp_name_disambiguation(rows)
        self.assertEqual([r["nickname"] for r in rows], ["Jake", "Jake"])


if __name__ == "__main__":
    unittest.main()
