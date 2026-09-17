"""The Find-a-rec category chips are a filter, not a flavouring of the prose.

Tapping "Recipes" and asking must not answer with a plumber whose tip happens to mention
chicken: the pick is scoped on reco_type at BOTH readers (the ask and the browse), it is
sticky across the turns the user narrows on, and under an off-map chip the Google fallback
does not run at all.
"""

import unittest
from unittest.mock import patch

from app import local_signals, tip_feed
from app.reco_question_sets import (
    active_reco_types,
    apply_reco_type_filter,
    google_searchable,
    normalize_types,
)


class Vocabulary(unittest.TestCase):
    def test_known_keys_survive_and_unknown_ones_are_dropped(self):
        # Dropped, never guessed at: a key that fell through as "no filter" would show
        # every type under a chip that promised one.
        self.assertEqual(normalize_types(["Recipes", "kitchen", "diy"]), ["recipe", "diy"])
        self.assertEqual(normalize_types("professional"), ["professional"])
        self.assertEqual(normalize_types(None), [])

    def test_one_chip_can_stand_for_two_buckets(self):
        self.assertEqual(
            normalize_types(["professional", "service"]), ["professional", "service"]
        )


class Stickiness(unittest.TestCase):
    def test_the_pick_outlives_the_turn_it_arrived_on(self):
        ctx: dict = {}
        apply_reco_type_filter(ctx, ["recipes"])
        self.assertEqual(apply_reco_type_filter(ctx, None), ["recipe"])
        self.assertEqual(active_reco_types(ctx), ["recipe"])

    def test_an_empty_list_clears_it(self):
        ctx: dict = {}
        apply_reco_type_filter(ctx, ["recipe"])
        self.assertEqual(apply_reco_type_filter(ctx, []), [])
        self.assertEqual(active_reco_types(ctx), [])


class AskRead(unittest.TestCase):
    def test_the_filter_reaches_the_rpc(self):
        with patch.object(local_signals, "call_rpc", return_value=[]) as rpc:
            local_signals.find_neighbor_tips(
                "jwt", block_id="b1", query="biryani", reco_types=["recipes"]
            )
        self.assertEqual(rpc.call_args[0][2]["p_reco_types"], ["recipe"])

    def test_no_pick_sends_no_filter(self):
        with patch.object(local_signals, "call_rpc", return_value=[]) as rpc:
            local_signals.find_neighbor_tips("jwt", block_id="b1", query="dentist")
        self.assertNotIn("p_reco_types", rpc.call_args[0][2])

    def test_an_old_db_answers_a_filtered_ask_empty_not_out_of_the_wrong_bucket(self):
        from fastapi import HTTPException

        boom = HTTPException(status_code=502, detail="PGRST202 no function")
        with patch("app.layer1_handlers._embed_attr_filter", return_value=None), patch.object(
            local_signals, "call_rpc", side_effect=boom
        ) as rpc:
            rows = local_signals.find_neighbor_tips(
                "jwt", block_id="b1", query="biryani", reco_types=["recipe"]
            )
        self.assertEqual(rows, [])
        # No v1 retry: that signature has no filter, so it would answer out of every type.
        self.assertEqual(rpc.call_count, 1)


class BrowseRead(unittest.TestCase):
    def test_recent_recommendations_honours_the_same_pick(self):
        with patch.object(tip_feed, "call_rpc", return_value=[]) as rpc:
            tip_feed.recent_tips("jwt", tab="recent", reco_types=["Restaurants"])
        self.assertEqual(rpc.call_args[0][2]["p_reco_types"], ["restaurant"])


class OtherBucket(unittest.TestCase):
    """The escape hatch. A typeless recommendation is one nobody can find, so `other` is
    what the model answers when none of the seven fits — never null."""

    def test_other_is_part_of_the_closed_taxonomy(self):
        from app.reco_question_sets import RECO_TYPES, normalize_types

        self.assertIn("other", RECO_TYPES)
        self.assertEqual(len(RECO_TYPES), 8)  # 7 kinds + the hatch; not 9, not 40
        self.assertEqual(normalize_types(["others"]), ["other"])

    def test_it_has_a_question_set_and_a_floor(self):
        from app.reco_question_sets import missing_required, steps_for

        fields = [s["field"] for s in steps_for("other")]
        self.assertEqual(fields[0], "subject")
        # Without these the card cannot be written, so the flow cannot post either.
        self.assertEqual(missing_required("other", {}), ["subject", "helps_with", "where_to_look"])

    def test_its_where_step_is_a_text_box_not_a_map_search(self):
        from app.reco_question_sets import steps_for

        # A bus route and a Facebook group are not points on a map; a `where`-named field
        # would have carried the Places picker.
        kinds = {s["field"]: s["kind"] for s in steps_for("other")}
        self.assertEqual(kinds["where_to_look"], "text")

    def test_the_prompt_forbids_a_null_type(self):
        from app.tip_share import _EXTRACT_SYSTEM

        self.assertIn("NEVER null", _EXTRACT_SYSTEM)

    def test_a_typeless_draft_is_saved_as_other(self):
        from app import tip_share

        seen: dict = {}

        def _capture(_jwt, **kwargs):
            seen.update(kwargs)
            return {"signal_id": "s1"}

        with patch.object(tip_share, "_detail_text", return_value="x"), patch(
            "app.local_signals.save_local_signal", _capture
        ), patch("app.local_signals.tag_local_signal", return_value=True), patch(
            "app.tip_tags.tags_for_tip", return_value=[]
        ):
            tip_share._save_tip(draft={"name": "The 111 bus"}, user_jwt="jwt", block_id="b1", zip_code=None)
        self.assertEqual(seen.get("reco_type"), "other")


class TypeCensus(unittest.TestCase):
    """The chip row is built from what the reader can actually SEE."""

    def test_counts_come_back_keyed_by_type(self):
        rows = [{"reco_type": "professional", "n": 2}, {"reco_type": "product", "n": 1}]
        with patch.object(tip_feed, "call_rpc", return_value=rows) as rpc:
            counts = tip_feed.neighbor_tip_type_counts("jwt", block_id="b1")
        self.assertEqual(counts, {"professional": 2, "product": 1})
        self.assertEqual(rpc.call_args[0][2], {"p_block_id": "b1"})

    def test_a_community_scope_is_forwarded(self):
        with patch.object(tip_feed, "call_rpc", return_value=[]) as rpc:
            tip_feed.neighbor_tip_type_counts("jwt", circle_place_id="place-1")
        self.assertEqual(rpc.call_args[0][2]["p_circle_place_id"], "place-1")

    def test_no_scope_at_all_asks_nothing(self):
        with patch.object(tip_feed, "call_rpc") as rpc:
            self.assertEqual(tip_feed.neighbor_tip_type_counts("jwt"), {})
        rpc.assert_not_called()

    def test_an_old_db_leaves_the_row_alone(self):
        with patch.object(tip_feed, "call_rpc", side_effect=RuntimeError("boom")):
            self.assertEqual(tip_feed.neighbor_tip_type_counts("jwt", block_id="b1"), {})


class PolicyGate(unittest.TestCase):
    """A lit chip owns the turn. The conversational policy answers anything the classifier
    doesn't hand to an engine, and under a chip the words carry almost no meaning — so
    "show all" was read as discovery.find_peers and answered with people to meet."""

    def test_a_deterministic_entry_outranks_the_classifier(self):
        from app.lana_unified_pipeline import _turn_is_engine_action

        self.assertTrue(
            _turn_is_engine_action(
                {"tip_seek_hint": True},
                "show all",
                history=None,
                home_block_id="b1",
                phone_verified=True,
            )
        )

    def test_without_the_flag_the_gate_still_asks_the_classifier(self):
        from app.lana_unified_pipeline import _turn_is_engine_action

        # No slots available in the test env, so this is the fail-CLOSED path: the policy
        # keeps the turn rather than every message diverting to the engines.
        self.assertFalse(
            _turn_is_engine_action(
                {}, "show all", history=None, home_block_id="b1", phone_verified=True
            )
        )


class NudgeState(unittest.TestCase):
    """A recommender is a neighbour, and an intro to them may already be out. The rows
    used to arrive with no `connection`, so every card offered a Nudge that could only
    bounce off the 7-day pair cooldown — with nothing on screen to warn the reader."""

    def _tip(self, peer: str = "p-1"):
        return {
            "signal_id": "s-1",
            "peer_user_id": peer,
            "neighbor_label": "Natasha",
            "detail_text": "Jacas Barber · great beard trim",
            "match_strength": 0.8,
        }

    def test_an_outstanding_intro_shows_as_sent_not_as_a_button(self):
        from app import tip_rec_cascade

        ctx: dict = {}
        with patch("app.peer_discovery_surface.peer_tiers", return_value={"p-1": "nudge"}):
            shown = tip_rec_cascade.stamp_tip_peer_surface(
                ctx, [self._tip()], phone_verified=True, user_id="me"
            )
        self.assertEqual([r.get("connection") for r in shown], ["intro_sent"])

    def test_an_accepted_nudge_shows_as_connected(self):
        from app import tip_rec_cascade

        with patch("app.peer_discovery_surface.peer_tiers", return_value={"p-1": "direct"}):
            shown = tip_rec_cascade.stamp_tip_peer_surface(
                {}, [self._tip()], phone_verified=True, user_id="me"
            )
        self.assertEqual([r.get("connection") for r in shown], ["connected"])

    def test_a_stranger_keeps_the_button(self):
        from app import tip_rec_cascade

        with patch("app.peer_discovery_surface.peer_tiers", return_value={}):
            shown = tip_rec_cascade.stamp_tip_peer_surface(
                {}, [self._tip()], phone_verified=True, user_id="me"
            )
        self.assertIsNone(shown[0].get("connection"))

    def test_the_feed_row_carries_the_field(self):
        from app.tip_feed import _row

        row = _row({"signal_id": "s-1", "reco_name": "Jacas Barber", "connection": "intro_sent"})
        self.assertEqual(row and row["connection"], "intro_sent")


class GoogleFallback(unittest.TestCase):
    def test_off_map_types_get_no_places_search(self):
        self.assertFalse(google_searchable(["recipe"]))
        self.assertFalse(google_searchable(["recipe", "diy"]))

    def test_everything_findable_on_a_map_still_falls_back(self):
        self.assertTrue(google_searchable([]))
        self.assertTrue(google_searchable(["restaurant"]))
        # A mixed pick keeps the fallback — half the ask is a place.
        self.assertTrue(google_searchable(["recipe", "restaurant"]))

    def test_the_seek_fallback_returns_early_under_a_recipe_chip(self):
        from app import discovery_route

        ctx: dict = {}
        with patch.object(discovery_route, "_search_tip_places") as search:
            out = discovery_route._tip_seek_fallback_reply(
                ctx=ctx,
                msg="chicken biryani",
                detail="chicken biryani",
                category="recipe",
                block_id="b1",
                session_ctx={"reco_type_filter": ["recipe"]},
                user_id="u1",
            )
        self.assertEqual(out, "")
        search.assert_not_called()
        self.assertNotIn("google_place_suggestions", ctx)


if __name__ == "__main__":
    unittest.main()
