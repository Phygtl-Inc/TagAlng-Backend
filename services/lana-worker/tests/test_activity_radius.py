"""Browse reads events by RADIUS, not by home-ZIP equality (PR7 wiring).

The bug: fetch_preview_events_on_block filtered `.eq("block_id", <home block>)`,
so a neighbour one ZIP over saw nothing. It now pre-filters on
get_activities_near_point and only falls back to block equality when the block
cannot be placed (no centroid, or the RPC isn't deployed on this DB).
"""

import unittest
from unittest.mock import MagicMock, patch

from app.discovery_route import activity_radius_meters, event_ids_near_block


def _sb(rpc_rows=None, rpc_exc=None):
    sb = MagicMock()
    rpc = MagicMock()
    if rpc_exc is not None:
        rpc.execute.side_effect = rpc_exc
    else:
        rpc.execute.return_value = MagicMock(data=rpc_rows)
    sb.rpc.return_value = rpc
    return sb


class ActivityRadiusTests(unittest.TestCase):
    def test_default_radius_is_the_rpc_default(self):
        with patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("LANA_ACTIVITY_RADIUS_METERS", None)
            self.assertEqual(activity_radius_meters(), 40000.0)

    def test_env_knob_overrides_and_garbage_falls_back(self):
        with patch.dict("os.environ", {"LANA_ACTIVITY_RADIUS_METERS": "12000"}):
            self.assertEqual(activity_radius_meters(), 12000.0)
        with patch.dict("os.environ", {"LANA_ACTIVITY_RADIUS_METERS": "wide"}):
            self.assertEqual(activity_radius_meters(), 40000.0)

    def test_ids_come_from_the_radius_rpc_around_the_block_centroid(self):
        sb = _sb(rpc_rows=[{"id": "e1"}, {"id": "e2"}, {"no_id": 1}])
        with patch("app.places._centroid", return_value=(28.36, -81.25)), patch(
            "app.discovery_route.service_client", return_value=sb
        ), patch.dict("os.environ", {"LANA_ACTIVITY_RADIUS_METERS": "40000"}):
            self.assertEqual(event_ids_near_block("zip-32827", limit=40), ["e1", "e2"])
        name, args = sb.rpc.call_args[0]
        self.assertEqual(name, "get_activities_near_point")
        self.assertEqual(args["p_radius_meters"], 40000.0)
        self.assertEqual((args["p_lat"], args["p_lng"]), (28.36, -81.25))

    def test_empty_is_an_answer_but_unplaceable_is_not(self):
        # [] — the radius really holds nothing. Distinct from None.
        with patch("app.places._centroid", return_value=(28.36, -81.25)), patch(
            "app.discovery_route.service_client", return_value=_sb(rpc_rows=[])
        ):
            self.assertEqual(event_ids_near_block("zip-32827"), [])
        # None — no centroid for this block: caller must fall back, not show nothing.
        with patch("app.places._centroid", return_value=None):
            self.assertIsNone(event_ids_near_block("zip-99999"))
        # None — RPC missing on this DB (migration 20260920120000 not pushed).
        with patch("app.places._centroid", return_value=(28.36, -81.25)), patch(
            "app.discovery_route.service_client",
            return_value=_sb(rpc_exc=Exception("function does not exist")),
        ):
            self.assertIsNone(event_ids_near_block("zip-32827"))


class ActivityWindowTests(unittest.TestCase):
    """The horizon knob, and the rule that makes it a knob: EVERY activity read passes
    a window explicitly.

    The bug it replaces: only event_ids_near_block passed one ("90 days"). The far-supply
    probe and look_meet both took the RPC's 14-day default, so browse would list a local
    meet six weeks out while the probe whose whole job is finding far supply could not
    see its counterpart 40 miles away.
    """

    def test_default_and_env_override_and_garbage_falls_back(self):
        from app.discovery_route import activity_window

        import os

        with patch.dict("os.environ", {}, clear=False):
            os.environ.pop("LANA_ACTIVITY_WINDOW_DAYS", None)
            self.assertEqual(activity_window(), "90 days")
        with patch.dict("os.environ", {"LANA_ACTIVITY_WINDOW_DAYS": "30"}):
            self.assertEqual(activity_window(), "30 days")
        with patch.dict("os.environ", {"LANA_ACTIVITY_WINDOW_DAYS": "soon"}):
            self.assertEqual(activity_window(), "90 days")
        # A zero or negative horizon would read as "nothing is ever coming up".
        with patch.dict("os.environ", {"LANA_ACTIVITY_WINDOW_DAYS": "0"}):
            self.assertEqual(activity_window(), "1 days")

    def test_browse_local_read_passes_the_window(self):
        sb = _sb(rpc_rows=[{"id": "e1"}])
        with patch("app.places._centroid", return_value=(28.36, -81.25)), patch(
            "app.discovery_route.service_client", return_value=sb
        ), patch.dict("os.environ", {"LANA_ACTIVITY_WINDOW_DAYS": "45"}):
            event_ids_near_block("zip-32827")
        _name, args = sb.rpc.call_args[0]
        self.assertEqual(args["p_window"], "45 days")

    def test_far_probe_uses_the_same_window_as_the_local_read(self):
        """The two reads that answer one question must share one horizon."""
        from app.discovery_route import activities_beyond_radius

        rpc = MagicMock(return_value=[])
        with patch("app.places._centroid", return_value=(33.97, -118.24)), patch(
            "app.supabase_rpc.call_rpc", rpc
        ), patch.dict("os.environ", {"LANA_ACTIVITY_WINDOW_DAYS": "45"}):
            activities_beyond_radius("jwt", "zip-90001")
        _jwt, name, args = rpc.call_args[0]
        self.assertEqual(name, "get_nearby_activities")
        self.assertEqual(args["p_window"], "45 days")

    def test_look_meet_states_its_own_14_days_rather_than_inheriting_a_default(self):
        from app.look_meet import LOOK_MEET_WINDOW, _find_block_events

        self.assertEqual(LOOK_MEET_WINDOW, "14 days")
        rpc = MagicMock(return_value=[])
        with patch("app.places._centroid", return_value=(28.36, -81.25)), patch(
            "app.supabase_rpc.call_rpc", rpc
        ), patch.dict("os.environ", {"LANA_ACTIVITY_WINDOW_DAYS": "45"}):
            _find_block_events(
                user_jwt="jwt", kind="run", zip_code=None, block_id="zip-32827"
            )
        _jwt, name, args = rpc.call_args[0]
        self.assertEqual(name, "get_activities_near_point")
        # Deliberately NOT the browse knob — this surface promises 14 days.
        self.assertEqual(args["p_window"], "14 days")

    def test_look_meet_legacy_fallback_also_passes_it(self):
        """The fallback is the path that runs on a DB without 20260920120000 — the one
        place a forgotten window would go unnoticed longest."""
        from app.look_meet import _find_block_events

        def _rpc(_jwt, name, _args):
            if name == "get_activities_near_point":
                raise Exception("function does not exist")
            return []

        rpc = MagicMock(side_effect=_rpc)
        with patch("app.places._centroid", return_value=(28.36, -81.25)), patch(
            "app.supabase_rpc.call_rpc", rpc
        ):
            _find_block_events(
                user_jwt="jwt", kind="run", zip_code="32827", block_id="zip-32827"
            )
        _jwt, name, args = rpc.call_args[0]
        self.assertEqual(name, "get_nearby_activities")
        self.assertEqual(args["p_window"], "14 days")


class AreaStateWithheldTests(unittest.TestCase):
    """Gate off must also silence the POLICY's version of "your area is still new".

    Removing the mechanical blocks isn't enough: world["area"]["state"] is what the
    policy prompt reads as "you can't act yet — pivot to hosting", so it has to go
    quiet on the same switch.
    """

    def _world(self):
        from app.policy.world import world_state

        with patch("app.policy.world._user_row", return_value={"home_zip": "32827"}), patch(
            "app.policy.world._zip_snapshot",
            return_value={"state": "warming", "count": 3, "threshold": 10},
        ), patch("app.policy.world._circles", return_value=[]), patch(
            "app.policy.world._place_names", return_value={}
        ):
            return world_state("u1")

    def test_off_withholds_area_state(self):
        with patch.dict("os.environ", {"LANA_ZIP_UNLOCK_GATE": "off"}):
            w = self._world()
        self.assertIsNone(w["area"]["state"])
        self.assertNotIn("zip_open", w["states"])

    def test_soft_still_reports_it(self):
        with patch.dict("os.environ", {"LANA_ZIP_UNLOCK_GATE": "soft"}):
            w = self._world()
        self.assertEqual(w["area"], {"state": "warming", "count": 3, "threshold": 10})


class FarSupplyHonestyTests(unittest.TestCase):
    """QA 2026-08-31, ZIP 90001: "show me activities happening on other blocks" got
    "nothing in your area" + a "Widen the search" pill that only clears the TOPIC
    filter. Lake Nona is 3,555 km away — get_activities_near_point clamps at 200 km
    in SQL, so no radius could ever reach it. Name the far supply instead.
    """

    _FAR = [
        {"id": "e9", "title": "Morning Run Club", "venue_name": "Lake Nona Commons",
         "distance_meters": 3_555_000.0},
        {"id": "e8", "title": "Down the street", "distance_meters": 1000.0},
    ]

    def _probe(self, rows, *, block_row="Lake Nona (32827)"):
        from app.discovery_route import nearest_activity_beyond_radius

        sb = MagicMock()
        chain = MagicMock()
        for m in ("select", "eq", "limit"):
            getattr(chain, m).return_value = chain
        chain.execute.side_effect = [
            MagicMock(data=[{"block_id": "zip-32827"}]),
            MagicMock(data=[{"display_name": block_row}]),
        ]
        sb.table.return_value = chain
        with patch("app.places._centroid", return_value=(33.97, -118.24)), patch(
            "app.discovery_route.service_client", return_value=sb
        ), patch("app.supabase_rpc.call_rpc", return_value=rows):
            return nearest_activity_beyond_radius("jwt", "zip-90001")

    def test_names_the_nearest_thing_beyond_the_radius(self):
        far = self._probe(list(self._FAR))
        self.assertEqual(far["title"], "Morning Run Club")
        self.assertEqual(far["zip5"], "32827")
        self.assertEqual(far["miles"], 2209)  # 3,555 km — no radius reaches this

    def test_ignores_rows_inside_the_radius(self):
        self.assertIsNone(self._probe([self._FAR[1]]))

    def test_facts_replace_the_false_widen_promise(self):
        from app.activity_browse import _far_supply_facts, _far_where

        far = self._probe(list(self._FAR))
        facts = _far_supply_facts(far)
        blob = " ".join(facts)
        self.assertIn("Morning Run Club", blob)
        self.assertIn("2,209 miles", blob)
        self.assertIn("Lake Nona (32827)", blob)
        # Exactly one area was probed, so the copy may not generalise to a plural
        # ("events in places like Foster City (94404)" — QA 2026-08-31).
        self.assertIn("never", blob)
        self.assertIn("places like", blob)  # named as forbidden, in the facts
        self.assertIn("No other area was looked at", blob)
        # A ZIP-named area still shows its ZIP — it helps the user place it.
        self.assertIn("32827", _far_where(far))

    def test_an_area_without_a_zip_is_still_offered(self):
        """The seeded pilot areas are H3 cells (8a2a1072b59ffff) with no ZIP anywhere in
        them. Requiring a zip5 made the offer silently impossible in exactly the areas
        the pilot runs in — the block id is what re-anchors now, not a parsed ZIP."""
        from app.activity_browse import _far_supply_facts, _far_where

        far = self._probe(list(self._FAR), block_row="Lake Nona — Block A (placeholder)")
        far["zip5"] = None
        far["block_id"] = "8a2a1072b59ffff"
        facts = _far_supply_facts(far)
        self.assertTrue(facts)
        # clean_block_label strips "(placeholder)" and rewrites the banned word "Block".
        self.assertEqual(_far_where(far), "Lake Nona — Area A")
        self.assertIn("Lake Nona — Area A", " ".join(facts))

    def test_no_block_to_send_them_to_means_no_offer(self):
        """A name with nothing behind it is the one case that must still stay silent —
        the pill would re-anchor on an empty block and land them nowhere."""
        from app.activity_browse import _far_supply_facts

        far = self._probe(list(self._FAR))
        far["block_id"] = None
        self.assertEqual(_far_supply_facts(far), [])

    def test_no_name_means_no_offer(self):
        from app.activity_browse import _far_supply_facts

        far = self._probe(list(self._FAR), block_row=None)
        far["zip5"] = None
        self.assertEqual(_far_supply_facts(far), [])


class FarOfferPillTests(unittest.TestCase):
    """QA 2026-08-31 follow-up: the copy named Foster City (94404) but the pill said
    "Widen the search", and tapping it answered "share your 5-digit ZIP code".

    Two bugs: activity_browse_actions hardcoded both seek pills and ignored the
    lane's suggestions, and _far_offer armed _need_zip so ANY non-ZIP reply was
    read as a malformed ZIP.
    """

    def test_seek_pills_use_the_lanes_own_labels(self):
        from app.ui_actions import activity_browse_actions

        acts = activity_browse_actions(
            {"browse_draft": {"_seek_offer": True,
                              "suggestions": ["Yes, listen for me", "Look in Foster City (94404)"]}}
        )
        self.assertEqual([a["label"] for a in acts],
                         ["Yes, listen for me", "Look in Foster City (94404)"])
        # The pill's message IS the next user turn — the ZIP has to survive into it.
        self.assertIn("94404", acts[1]["message"])

    def test_seek_pills_fall_back_when_the_lane_set_none(self):
        from app.ui_actions import activity_browse_actions

        acts = activity_browse_actions({"browse_draft": {"_seek_offer": True}})
        self.assertEqual([a["label"] for a in acts], ["Yes, listen for me", "Widen the search"])

    _ROWS = [
        {"id": "e9", "title": "Kayak morning", "distance_meters": 20000.0, "host_id": "h1"},
        {"id": "e7", "title": "Kayak evening", "distance_meters": 90000.0, "host_id": "h1"},
    ]

    def _offer(self, *, matched, rows=None, jwt_uid="me"):
        from app.activity_browse import _far_offer

        draft: dict = {}
        with patch("app.discovery_route.activities_beyond_radius",
                   return_value=list(self._ROWS if rows is None else rows)), patch(
            "app.activity_browse._filter_events_by_query",
            side_effect=lambda ev, q: (matched, ""),
        ), patch("app.discovery_route.far_activity_details",
                 side_effect=lambda r: r and {"title": r["title"], "miles": 12,
                                              "zip5": "94404", "area_label": "Foster City",
                                              "block_id": "zip-94404",
                                              "venue": None}), patch(
            "app.auth.jwt_user_id", return_value=jwt_uid
        ):
            return _far_offer("jwt", "zip-90001", draft, interest="kayak") + (draft,)

    def test_far_offer_does_not_arm_the_zip_consumer(self):
        facts, chip, draft = self._offer(matched=list(self._ROWS))
        self.assertTrue(facts)
        self.assertEqual(chip, "Look in Foster City (94404)")
        # Arming this up front is what produced the "share your 5-digit ZIP" loop.
        self.assertNotIn("_need_zip", draft)

    def test_far_offer_remembers_the_area_it_offered(self):
        """The tap is matched against the exact label offered and re-anchors on the block
        id — no ZIP is parsed back out of the pill text."""
        _facts, chip, draft = self._offer(matched=list(self._ROWS))
        self.assertEqual(draft["_area_offer_chip"], chip)
        self.assertEqual(draft["_area_offer_block_id"], "zip-94404")
        self.assertEqual(draft["_area_offer_name"], "Foster City (94404)")

    def test_a_declined_offer_leaves_nothing_tappable_behind(self):
        """A stale label must not still re-anchor geography on a later empty state."""
        _facts, _chip, draft = self._offer(matched=[])
        self.assertIsNone(draft["_area_offer_chip"])
        self.assertIsNone(draft["_area_offer_block_id"])

    def test_no_offer_when_nothing_there_answers_the_search(self):
        """Offering an area and then landing on "nothing here" is worse than the empty
        state it replaced — so an area whose events fail THIS search is not offered."""
        facts, chip, _ = self._offer(matched=[])
        self.assertEqual((facts, chip), ([], ""))

    def test_offer_names_the_closest_match_not_the_first_row(self):
        # _filter_events_by_query may reorder; distance still decides.
        facts, _chip, _ = self._offer(matched=[self._ROWS[1], self._ROWS[0]])
        self.assertIn("Kayak morning", " ".join(facts))

    def test_own_hosted_far_meets_are_excluded(self):
        """browse drops the caller's own meets, so the probe must too — otherwise the
        offer rests on an event the re-anchored search will filter straight back out."""
        from app.discovery_route import activities_beyond_radius

        with patch("app.places._centroid", return_value=(33.9, -118.2)), patch(
            "app.supabase_rpc.call_rpc",
            return_value=[{"id": "e1", "title": "My own meet",
                           "distance_meters": 90000.0, "host_id": "me"}],
        ):
            self.assertEqual(
                activities_beyond_radius("jwt", "zip-90001", exclude_host_id="me"), []
            )


class FarAreaSwitchTests(unittest.TestCase):
    """QA 2026-08-31: tapping "Look in Foster City (94404)" re-offered Foster City,
    forever. discovery_route.resolve_block_id returns home_block_id FIRST and only
    falls back to preview_block_id, so for anyone with a home area the resolved
    preview was thrown away and the search re-ran at home.
    """

    def _turn(self, home_block_id):
        from app.activity_browse import run_activity_browse_turn

        seen: dict = {}

        def _fetch(jwt, block_id, *, weekend_only):
            seen["block_id"] = block_id
            return [{"id": "e1", "title": "Kayak morning", "starts_at": "2026-09-02T09:00:00",
                     "venue_name": "Lagoon", "cohort_tags": []}]

        ctx: dict = {
            "activity_browse_active": True,
            "browse_draft": {"_seek_offer": True, "interest": "", "_asked": True,
                             "suggestions": ["Yes, listen for me", "Look in Foster City (94404)"]},
        }
        with patch("app.activity_browse._fetch_block_events", side_effect=_fetch), patch(
            "app.discovery_route.resolve_zip_coverage",
            return_value=({"block_id": "zip-94404", "display_name": "Foster City (94404)"}, "covered"),
        ), patch("app.activity_browse._filter_events_by_query",
                 side_effect=lambda ev, q: (ev, None)):
            run_activity_browse_turn(
                user_message="Look in Foster City (94404)",
                session_ctx=ctx,
                history=[],
                user_jwt="jwt",
                home_block_id=home_block_id,
                slots={"zip": "94404"},
            )
        return seen.get("block_id"), ctx

    def test_switches_area_even_when_the_user_has_a_home_block(self):
        block_id, ctx = self._turn(home_block_id="zip-90001")
        self.assertEqual(block_id, "zip-94404")  # was "zip-90001" → the loop
        self.assertEqual(ctx["browse_draft"]["_area_block_id"], "zip-94404")

    def test_guest_without_a_home_block_still_switches(self):
        block_id, _ = self._turn(home_block_id=None)
        self.assertEqual(block_id, "zip-94404")


class AreaChipRoundTripTests(unittest.TestCase):
    """Tapping the remembered pill re-anchors on the block id we stored when we offered
    it — the path an area with no ZIP in its name depends on entirely."""

    def _tap(self, label, *, draft_extra=None):
        from app.activity_browse import run_activity_browse_turn

        seen: dict = {}

        def _fetch(jwt, block_id, *, weekend_only):
            seen["block_id"] = block_id
            return [{"id": "e1", "title": "Kayak morning", "starts_at": "2026-09-02T09:00:00",
                     "venue_name": "Lagoon", "cohort_tags": []}]

        draft = {
            "_seek_offer": True,
            "interest": "kayak",
            "_asked": True,
            "_area_offer_chip": "Look in Lake Nona — Area A",
            "_area_offer_block_id": "8a2a1072b59ffff",
            "_area_offer_name": "Lake Nona — Area A",
            "suggestions": ["Yes, listen for me", "Look in Lake Nona — Area A"],
        }
        draft.update(draft_extra or {})
        ctx: dict = {"activity_browse_active": True, "browse_draft": draft}
        with patch("app.activity_browse._fetch_block_events", side_effect=_fetch), patch(
            "app.activity_browse._filter_events_by_query",
            side_effect=lambda ev, q: (ev, None),
        ):
            run_activity_browse_turn(
                user_message=label,
                session_ctx=ctx,
                history=[],
                user_jwt="jwt",
                home_block_id="zip-90001",
                slots={},
            )
        return seen.get("block_id"), ctx

    def test_tapping_a_zipless_area_searches_that_area(self):
        block_id, ctx = self._tap("Look in Lake Nona — Area A")
        # The whole point: an H3 area has no ZIP to parse out of the label.
        self.assertEqual(block_id, "8a2a1072b59ffff")
        self.assertEqual(ctx["browse_draft"]["_area_block_id"], "8a2a1072b59ffff")

    def test_the_tap_is_not_swallowed_as_a_new_interest(self):
        _block_id, ctx = self._tap("Look in Lake Nona — Area A")
        self.assertEqual(ctx["browse_draft"]["interest"], "kayak")

    def test_the_tap_never_asks_for_a_zip(self):
        """Arming _need_zip on a label with no ZIP in it is the "share your 5-digit ZIP
        code" dead end, one namespace over."""
        _block_id, ctx = self._tap("Look in Lake Nona — Area A")
        self.assertFalse(ctx["browse_draft"].get("_need_zip"))

    def test_a_different_reply_is_not_mistaken_for_the_chip(self):
        _block_id, ctx = self._tap("actually, what about badminton?")
        self.assertNotEqual(ctx["browse_draft"].get("_area_block_id"), "8a2a1072b59ffff")


class EmptyStateSaysWhatThePillDoesTests(unittest.TestCase):
    """The bug this pins: the facts offered the far area, the system prompt hardcoded
    "or widen the search", and the static fallback ended the same way — so with no LLM
    configured the message contradicted the pill beside it 100% of the time.
    """

    def _compose(self, **kw):
        from app.activity_browse import _compose_empty_seek_offer

        # llm_configured() false is the deploy where the static string is the ONLY output.
        with patch("app.orchestrator.llm.llm_configured", return_value=False):
            return _compose_empty_seek_offer("kayak", lang="en", **kw)

    def test_far_area_fallback_names_the_area_not_a_widen(self):
        msg = self._compose(far_facts=["x"], area="Foster City (94404)")
        self.assertIn("Foster City (94404)", msg)
        self.assertNotIn("widen", msg.lower())

    def test_generic_fallback_still_offers_the_widen_it_actually_shows(self):
        """No area found → the pill really is "Widen the search", so the copy should
        still say so. The fix is agreement, not deleting the word."""
        msg = self._compose()
        self.assertIn("widen", msg.lower())

    def test_community_fallback_is_unchanged(self):
        msg = self._compose(community="CF Fitness")
        self.assertIn("CF Fitness", msg)
        self.assertNotIn("widen", msg.lower())

    def test_the_system_prompt_names_no_options_of_its_own(self):
        """The options live in the facts, which are computed from what was armed. A menu
        hardcoded in the prompt is a fourth place to keep in sync, and it drifted twice."""
        from app.activity_browse import _compose_empty_seek_offer

        captured: dict = {}

        def _llm_json(**kw):
            captured.update(kw)
            return {"message": "ok"}

        with patch("app.orchestrator.llm.llm_configured", return_value=True), patch(
            "app.orchestrator.llm.llm_json", _llm_json
        ), patch("app.orchestrator.llm.synthesizer_model", return_value="m"):
            _compose_empty_seek_offer(
                "kayak", lang="en", far_facts=["Option B is to look in Foster City"],
                area="Foster City (94404)",
            )
        self.assertNotIn("widen the search", captured["system"].lower())
        # …and the far option it WAS given still reaches the model.
        self.assertIn("Foster City", captured["user_payload"])

    def _facts_for(self, **kw):
        from app.activity_browse import _compose_empty_seek_offer

        cap: dict = {}

        def _llm_json(**k):
            cap.update(k)
            return {"message": "ok"}

        with patch("app.orchestrator.llm.llm_configured", return_value=True), patch(
            "app.orchestrator.llm.llm_json", _llm_json
        ), patch("app.orchestrator.llm.synthesizer_model", return_value="m"):
            _compose_empty_seek_offer("kayak", lang="en", **kw)
        return cap["system"], cap["user_payload"]

    def test_facts_do_not_claim_nothing_outside_was_looked_at_when_it_was(self):
        """Found by dumping the real payload: the far-supply fact names an area that WAS
        checked, while the honest-state fact said nothing outside was looked at. The
        model was being handed both sentences at once."""
        _sys, payload = self._facts_for(
            far_facts=["Option B is to look in Lake Nona — Area A"],
            area="Lake Nona — Area A",
        )
        self.assertIn("Lake Nona — Area A", payload)
        self.assertNotIn("Nothing outside it was looked at", payload)

    def test_the_no_wider_look_claim_survives_when_no_wider_look_happened(self):
        """It is true — and load-bearing — in the plain empty state."""
        _sys, payload = self._facts_for()
        self.assertIn("Nothing outside it was looked at", payload)

    def test_copy_calls_it_an_area_not_a_block(self):
        """The read is a radius, and "block" is lingo-banned (clean_block_label strips it)."""
        system, payload = self._facts_for()
        self.assertNotIn("their block", system + payload)
        self.assertIn("their area", payload)

    def test_every_empty_string_exists_in_all_three_languages(self):
        from app.i18n import _STRINGS

        for key in (
            "browse.empty_interest_offer", "browse.empty_generic_offer",
            "browse.empty_interest_far", "browse.empty_generic_far",
            "browse.empty_community_interest", "browse.empty_community_generic",
        ):
            for lang in ("en", "es", "pt"):
                self.assertTrue(_STRINGS[key][lang].strip(), f"{key}/{lang}")

    def test_no_placeholder_survives_into_the_copy(self):
        for kw in ({"area": "Foster City (94404)", "far_facts": ["x"]},
                   {"community": "CF Fitness"},
                   {}):
            msg = self._compose(**kw)
            self.assertNotIn("{", msg, kw)


if __name__ == "__main__":
    unittest.main()
