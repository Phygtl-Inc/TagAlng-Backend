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


if __name__ == "__main__":
    unittest.main()


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
        # The pill's text IS the next user message, and its ZIP is what re-anchors.
        self.assertIn("32827", _far_where(far))

    def test_no_zip_means_no_offer_we_cannot_keep(self):
        far = self._probe(list(self._FAR))
        far["zip5"] = None
        self.assertEqual(_far_supply_facts_or_empty(far), [])


def _far_supply_facts_or_empty(far):
    from app.activity_browse import _far_supply_facts

    return _far_supply_facts(far)


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
