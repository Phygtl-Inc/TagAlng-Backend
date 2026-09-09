"""A recommendation shared into a community lives ONLY there (product decision 2026-09-07).

Scope, not filter: with a community selected the read ignores distance; without one, a
community's tips are invisible to the area.
"""

import unittest
from unittest.mock import patch

from app import local_signals, tip_feed
from app.tip_share import COMMUNITY_FIELD, _community_step, resolve_community


class ReadScope(unittest.TestCase):
    def test_ask_passes_the_community_and_needs_no_block(self):
        with patch.object(local_signals, "call_rpc", return_value=[]) as rpc:
            local_signals.find_neighbor_tips(
                "jwt", block_id="", query="dentist", circle_place_id="place-1"
            )
        self.assertEqual(rpc.call_args[0][2]["p_circle_place_id"], "place-1")

    def test_area_read_sends_no_community(self):
        with patch.object(local_signals, "call_rpc", return_value=[]) as rpc:
            local_signals.find_neighbor_tips("jwt", block_id="b1", query="dentist")
        self.assertNotIn("p_circle_place_id", rpc.call_args[0][2])

    def test_old_db_answers_a_community_read_empty_not_area_wide(self):
        from fastapi import HTTPException

        boom = HTTPException(status_code=502, detail="PGRST202 no function")
        with patch.object(local_signals, "call_rpc", side_effect=boom) as rpc:
            rows = local_signals.find_neighbor_tips(
                "jwt", block_id="b1", query="dentist", circle_place_id="place-1"
            )
        self.assertEqual(rows, [])
        self.assertEqual(rpc.call_count, 1)  # no silent retry without the scope

    def test_feed_forwards_the_community(self):
        with patch.object(tip_feed, "call_rpc", return_value=[]) as rpc:
            tip_feed.recent_tips("jwt", tab="recent", circle_place_id="place-1")
        self.assertEqual(rpc.call_args[0][2]["p_circle_place_id"], "place-1")


class CommunityPick(unittest.TestCase):
    def test_step_is_optional_and_offers_an_opt_out(self):
        step = _community_step([{"place_id": "p1", "name": "CF Fitness"}])
        self.assertFalse(step["required"])
        self.assertIn("Everyone nearby", step["options"])

    def test_answering_the_step_by_name_resolves_to_the_place_id(self):
        ctx = {"tip_communities": [{"place_id": "p1", "name": "CF Fitness"}]}
        draft = {"answers": {COMMUNITY_FIELD: "CF Fitness"}}
        resolve_community(draft, user_jwt="jwt", session_ctx=ctx)
        self.assertEqual(draft["circle_place_id"], "p1")

    def test_everyone_nearby_beats_the_header_selection(self):
        ctx = {
            "tip_communities": [{"place_id": "p1", "name": "CF Fitness"}],
            "active_community": {"place_id": "p1", "name": "CF Fitness"},
        }
        draft = {"answers": {COMMUNITY_FIELD: "Everyone nearby"}}
        resolve_community(draft, user_jwt="jwt", session_ctx=ctx)
        self.assertIsNone(draft["circle_place_id"])

    def test_unanswered_falls_back_to_the_selected_community(self):
        ctx = {"active_community": {"place_id": "p2", "name": "Lagoinha"}}
        draft: dict = {"answers": {}}
        resolve_community(draft, user_jwt="jwt", session_ctx=ctx)
        self.assertEqual(draft["circle_place_id"], "p2")
        self.assertEqual(draft["circle_name"], "Lagoinha")


if __name__ == "__main__":
    unittest.main()


class WriteRecord(unittest.TestCase):
    """Where it was SENT is not something the card says about the place."""

    def _draft(self):
        return {
            "name": "Rosetta's",
            "category": "Restaurant",
            "step_set": [
                {"field": "why", "label": "Why", "question": "?"},
                {"field": COMMUNITY_FIELD, "label": "Community", "question": "?"},
            ],
            "answers": {"why": "great coffee", COMMUNITY_FIELD: "CF Fitness"},
        }

    def test_community_stays_out_of_the_stored_fields_and_text(self):
        from app.tip_share import _detail_text, _reco_fields

        self.assertNotIn("CF Fitness", _detail_text(self._draft()))
        self.assertEqual([f["field"] for f in _reco_fields(self._draft()) or []], ["why"])

    def test_the_ready_card_cta_publishes(self):
        from app.tip_share import _PASS_RE

        self.assertTrue(_PASS_RE.search("Share with the community"))
        self.assertTrue(_PASS_RE.search("pass the tip along"))


class PrefillIsNotSticky(unittest.TestCase):
    """The header pre-fill lands turns before the step is asked; it must not survive the
    user answering that step (caught by scripts/try_reco_carousel.py)."""

    def test_answer_overrides_an_already_stamped_prefill(self):
        ctx = {
            "tip_communities": [{"place_id": "p1", "name": "CF Fitness"}],
            "active_community": {"place_id": "p1", "name": "CF Fitness"},
        }
        draft = {"circle_place_id": "p1", "circle_name": "CF Fitness",
                 "answers": {COMMUNITY_FIELD: "Everyone nearby"}}
        resolve_community(draft, user_jwt="jwt", session_ctx=ctx)
        self.assertIsNone(draft["circle_place_id"])

    def test_an_explicit_everyone_nearby_pick_is_not_refilled(self):
        ctx = {"active_community": {"place_id": "p1", "name": "CF Fitness"}}
        draft = {"circle_place_id": None, "circle_picked": True, "answers": {}}
        resolve_community(draft, user_jwt="jwt", session_ctx=ctx)
        self.assertIsNone(draft["circle_place_id"])


class SameNamedCommunities(unittest.TestCase):
    """Two gyms both called "Life Time" — the name cannot tell them apart, the id can."""

    def test_the_step_carries_an_id_per_option(self):
        step = _community_step(
            [{"place_id": "p1", "name": "Life Time"}, {"place_id": "p2", "name": "Life Time"}]
        )
        self.assertEqual(step["option_ids"], ["p1", "p2", ""])
        self.assertEqual(len(step["option_ids"]), len(step["options"]))

    def test_a_posted_id_outranks_the_ambiguous_name(self):
        ctx = {"tip_communities": [
            {"place_id": "p1", "name": "Life Time"},
            {"place_id": "p2", "name": "Life Time"},
        ]}
        # What /tip-setup wrote: the SECOND Life Time, which name-matching can never reach.
        draft = {"circle_place_id": "p2", "circle_name": "Life Time",
                 "circle_picked": True, "answers": {COMMUNITY_FIELD: "Life Time"}}
        resolve_community(draft, user_jwt="jwt", session_ctx=ctx)
        self.assertEqual(draft["circle_place_id"], "p2")

    def test_a_different_name_later_still_corrects_a_pick(self):
        ctx = {"tip_communities": [
            {"place_id": "p1", "name": "Life Time"},
            {"place_id": "p9", "name": "Fitness CF"},
        ]}
        draft = {"circle_place_id": "p1", "circle_name": "Life Time",
                 "circle_picked": True, "answers": {COMMUNITY_FIELD: "Fitness CF"}}
        resolve_community(draft, user_jwt="jwt", session_ctx=ctx)
        self.assertEqual(draft["circle_place_id"], "p9")
