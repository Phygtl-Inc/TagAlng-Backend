"""Verified, claim-personalized tip_seek → Google Places fallback (hybrid loop).

Covers: place-type/attr whitelists, rec_personalize filter shaping, per-place verification,
the hybrid apply-vs-ask decision, 'only verified' honesty, and chip rendering.
"""

from __future__ import annotations

import unittest
from unittest import mock

from app import discovery_route as dr
from app.place_types import valid_attrs, valid_included_type
from app.ui_actions import derive_ui_actions, rec_chip_actions
from app.ui_intent import UI_INTENT_SIGNAL_SAVED


class TestPlaceTypeWhitelist(unittest.TestCase):
    def test_valid_type_passes_bad_type_dropped(self) -> None:
        self.assertEqual(valid_included_type("italian_restaurant"), "italian_restaurant")
        self.assertEqual(valid_included_type("VEGETARIAN_RESTAURANT"), "vegetarian_restaurant")
        self.assertIsNone(valid_included_type("healthy_restaurant"))  # not a real Google type
        self.assertIsNone(valid_included_type(None))

    def test_attr_whitelist_filters_unknown(self) -> None:
        self.assertEqual(
            valid_attrs(["goodForChildren", "servesVegetarianFood", "isHealthy"]),
            ["goodForChildren", "servesVegetarianFood"],
        )
        self.assertEqual(valid_attrs(None), [])
        # de-dupes
        self.assertEqual(valid_attrs(["dineIn", "dineIn"]), ["dineIn"])


class TestVerifyPlaces(unittest.TestCase):
    def test_only_true_attrs_survive(self) -> None:
        places = [
            {"name": "Kid Cafe", "attrs": {"goodForChildren": True}},
            {"name": "Bar X", "attrs": {"goodForChildren": False}},
            {"name": "Unknown", "attrs": {"goodForChildren": None}},
            {"name": "NoData", "attrs": {}},
        ]
        out = dr._verify_places(places, ["goodForChildren"])
        self.assertEqual([p["name"] for p in out], ["Kid Cafe"])

    def test_no_required_attrs_returns_all(self) -> None:
        places = [{"name": "A", "attrs": {}}, {"name": "B", "attrs": {}}]
        self.assertEqual(dr._verify_places(places, []), places)

    def test_all_attrs_must_hold(self) -> None:
        places = [
            {"name": "Both", "attrs": {"goodForChildren": True, "servesVegetarianFood": True}},
            {"name": "OnlyKids", "attrs": {"goodForChildren": True, "servesVegetarianFood": False}},
        ]
        out = dr._verify_places(places, ["goodForChildren", "servesVegetarianFood"])
        self.assertEqual([p["name"] for p in out], ["Both"])


def _ctx() -> dict:
    return {"signal_saved": {"intent": "tip_seek", "detail_text": "restaurants"}}


class TestTipFallbackHybrid(unittest.TestCase):
    def setUp(self) -> None:
        # Personalization on; a stable user + claims; the LLM/Google are stubbed per test.
        self._enabled = mock.patch("app.lana_paths.rec_personalize_enabled", return_value=True)
        self._claims = mock.patch(
            "app.context.load_user_context",
            return_value={"existing_claims": [{"label": "dad"}, {"label": "vegetarian"}]},
        )
        self._enabled.start()
        self._claims.start()
        self.addCleanup(self._enabled.stop)
        self.addCleanup(self._claims.stop)

    def _patch_personalize(self, filters):
        return mock.patch(
            "app.rec_personalize.personalize_tip_query",
            return_value={"relevant": True, "base_query": "restaurant", "filters": filters},
        )

    def _patch_search(self, fn):
        return mock.patch.object(dr, "_search_tip_places", side_effect=fn)

    def test_a_claim_angle_never_filters_it_becomes_a_chip(self) -> None:
        """Claims RANK, they never filter (2026-09-29 claims-rank spec): a vegetarian claim
        on a plain "restaurants" ask searches the plain ask — a great place Google has not
        tagged veg must not vanish — and offers the angle as a chip after the answer."""
        filters = [{
            "label": "Vegetarian", "query": "vegetarian restaurant",
            "included_type": "vegetarian_restaurant",
            "required_attrs": ["servesVegetarianFood"],
            "reframe": "Since you're vegetarian, I kept these veg-friendly.",
        }]
        seen: list[dict] = []

        def fake_search(*, query, block_id, zip_for_bias, user_id,
                        included_type=None, required_attrs=None, limit=3):
            seen.append({"query": query, "type": included_type, "attrs": required_attrs})
            return [{"name": "Steak & Co", "address": "1 St", "place_id": "p1", "attrs": {}}]

        ctx = _ctx()
        with self._patch_personalize(filters), self._patch_search(fake_search):
            reply = dr._tip_seek_fallback_reply(
                ctx=ctx, msg="find me restaurants", detail="restaurants", category="Food",
                block_id="zip-32827", session_ctx={"zip": "32827"}, user_id="u1",
            )
        self.assertEqual(seen, [{"query": "restaurant", "type": None, "attrs": None}])
        self.assertNotIn("veg-friendly", reply)
        self.assertEqual([p["name"] for p in ctx["google_place_suggestions"]], ["Steak & Co"])
        self.assertEqual([c["label"] for c in ctx["rec_chips"]], ["Vegetarian"])
        self.assertEqual(ctx["rec_chips"][0]["message"], "vegetarian restaurant")

    def test_two_claim_angles_answer_now_never_ask_first(self) -> None:
        filters = [
            {"label": "Kid-friendly", "query": "kid friendly restaurant",
             "included_type": None, "required_attrs": ["goodForChildren"], "reframe": "x"},
            {"label": "Vegetarian", "query": "vegetarian restaurant",
             "included_type": "vegetarian_restaurant",
             "required_attrs": ["servesVegetarianFood"], "reframe": "y"},
        ]
        called = {"n": 0}

        def fake_search(**kw):
            called["n"] += 1
            return [{"name": "Anything", "place_id": "p9", "attrs": {}}]

        ctx = _ctx()
        with self._patch_personalize(filters), self._patch_search(fake_search):
            reply = dr._tip_seek_fallback_reply(
                ctx=ctx, msg="find me restaurants", detail="restaurants", category="Food",
                block_id="zip-32827", session_ctx={"zip": "32827"}, user_id="u1",
            )
        self.assertEqual(called["n"], 1)  # searched straight away
        self.assertFalse(ctx.get("rec_filter_asked"))
        self.assertNotIn("tailor", reply)
        self.assertEqual([c["label"] for c in ctx["rec_chips"]], ["Kid-friendly", "Vegetarian"])

    def test_request_constraint_wins_over_claim(self) -> None:
        """Demo bug: 'kids friendly restaurant' must not be overridden by a Sicilian-heritage
        claim. The request-source filter is applied and searched; the claim angle is at most an
        optional refinement chip — never the primary pick, and we never ask-to-pick first."""
        filters = [
            {"label": "Kid-friendly", "query": "kids friendly restaurant",
             "included_type": None, "required_attrs": ["goodForChildren"],
             "reframe": "Kept these kid-friendly.", "source": "request"},
            {"label": "Italian", "query": "italian restaurant",
             "included_type": "italian_restaurant", "required_attrs": [],
             "reframe": "Since you have Sicilian heritage, some Italian spots.",
             "source": "claim"},
        ]
        seen_queries: list[str] = []

        def fake_search(*, query, block_id, zip_for_bias, user_id,
                        included_type=None, required_attrs=None, limit=3):
            seen_queries.append(query)
            return [{"name": "Playland Diner", "attrs": {"goodForChildren": True}}]

        ctx = _ctx()
        with self._patch_personalize(filters), self._patch_search(fake_search):
            reply = dr._tip_seek_fallback_reply(
                ctx=ctx, msg="kids friendly restaurant", detail="kids friendly restaurant",
                category="Food", block_id="zip-32827",
                session_ctx={"zip": "32827"}, user_id="u1",
            )
        # Applied the request angle immediately (no ask-to-pick), searched kid-friendly.
        self.assertNotIn("rec_filter_asked", ctx)
        self.assertIn("kids friendly restaurant", seen_queries)
        self.assertNotIn("Sicilian", reply)
        self.assertIn("kid-friendly", reply.lower())
        self.assertEqual([p["name"] for p in ctx["google_place_suggestions"]], ["Playland Diner"])
        # The claim angle survives only as an optional refinement chip.
        chip_labels = [c["label"] for c in ctx["rec_chips"]]
        self.assertEqual(chip_labels, ["Italian", "See all Food"])

    def test_unverified_is_honest_not_claimed(self) -> None:
        filters = [{
            "label": "Kid-friendly", "query": "kid friendly restaurant",
            "included_type": None, "required_attrs": ["goodForChildren"], "reframe": "kids!",
            # The ASK said kid-friendly — request constraints still filter, and are honest
            # when Google cannot confirm them.
            "source": "request",
        }]

        def fake_search(*, query, block_id, zip_for_bias, user_id,
                        included_type=None, required_attrs=None, limit=3):
            if required_attrs:
                # filtered call: Google can't confirm kid-friendliness
                return [{"name": "Mystery Grill", "attrs": {"goodForChildren": None}}]
            return [{"name": "Some Place", "address": "2 Rd", "place_id": "p2", "attrs": {}}]

        ctx = _ctx()
        with self._patch_personalize(filters), self._patch_search(fake_search):
            reply = dr._tip_seek_fallback_reply(
                ctx=ctx, msg="kid friendly restaurant", detail="restaurants", category="Food",
                block_id="zip-32827", session_ctx={"zip": "32827"}, user_id="u1",
            )
        self.assertIn("couldn't confirm", reply.lower())
        # falls back to the plain nearby list rather than claiming a match
        self.assertEqual([p["name"] for p in ctx["google_place_suggestions"]], ["Some Place"])

    def test_widen_skips_personalization(self) -> None:
        def fake_search(*, query, block_id, zip_for_bias, user_id,
                        included_type=None, required_attrs=None, limit=3):
            self.assertIsNone(included_type)  # plain search, no filter
            return [{"name": "Anything", "address": "", "place_id": "p3", "attrs": {}}]

        with mock.patch("app.rec_personalize.personalize_tip_query") as pz, \
                self._patch_search(fake_search):
            ctx = _ctx()
            reply = dr._tip_seek_fallback_reply(
                ctx=ctx, msg="show me all food", detail="restaurants", category="Food",
                block_id="zip-32827", session_ctx={"zip": "32827"}, user_id="u1",
            )
            pz.assert_not_called()
        self.assertIn("widening", reply.lower())


class TestRecChipRendering(unittest.TestCase):
    def test_rec_chip_actions_shape(self) -> None:
        chips = [
            {"label": "Vegetarian", "message": "vegetarian restaurant", "style": "primary"},
            {"label": "Just show all", "message": "show me all food"},
        ]
        actions = rec_chip_actions(chips)
        self.assertEqual(actions[0]["label"], "Vegetarian")
        self.assertEqual(actions[0]["message"], "vegetarian restaurant")
        self.assertEqual(actions[1]["style"], "secondary")  # default

    def test_derive_ui_actions_prefers_rec_chips(self) -> None:
        ctx = {
            "signal_saved": {"intent": "tip_seek", "detail_text": "restaurants"},
            "active_intent": "looking.tip",
            "rec_widen_noun": "Food",  # legacy chip present…
            "rec_chips": [{"label": "Vegetarian", "message": "vegetarian restaurant"}],
        }
        actions = derive_ui_actions(ctx, UI_INTENT_SIGNAL_SAVED)
        # …but rec_chips wins
        self.assertEqual(actions[0]["label"], "Vegetarian")


if __name__ == "__main__":
    unittest.main()
