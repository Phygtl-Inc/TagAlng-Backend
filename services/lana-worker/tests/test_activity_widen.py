"""Distance in the browse copy, and the distance plumbing under it.

A meet is admitted by the distance rule (app.distance_admission), not by a radius, so a
match can legitimately be 90 miles out. The header must then say so: "nothing near you,
but here's what I found about 90 miles out" is honest only when the CLOSEST admitted
meet is far — one match close by makes the plain header the true one, however far the
rest are. The conversion is whole miles, from the nearest meet, and never invented.

The rest of this file pins the contracts the distance rides in on: event distances keyed
by id from the radius RPC, and the stamp onto every row of the block read.
"""

import unittest
from unittest.mock import MagicMock, patch

from app.activity_browse import _far_miles, _nearest_miles, run_activity_browse_turn


def _ev(eid, title, **kw):
    row = {
        "id": eid,
        "title": title,
        "starts_at": "2026-09-19T18:00:00",
        "has_time": True,
        "venue_name": "The Field",
        "cohort_tags": [],
    }
    row.update(kw)
    return row


class NearestMilesTests(unittest.TestCase):
    def test_it_takes_the_closest_and_rounds_to_whole_miles(self):
        self.assertEqual(
            _nearest_miles([
                _ev("a", "far", distance_meters=160934.0),   # 100 miles
                _ev("b", "near", distance_meters=64373.6),   # 40 miles
            ]),
            40,
        )

    def test_rows_without_a_distance_yield_none(self):
        self.assertIsNone(_nearest_miles([_ev("a", "no distance")]))
        self.assertIsNone(_nearest_miles([]))


class FarResultsAreLabelledHonestlyTests(unittest.TestCase):
    """Rendering a meet 90 miles out under "near you" would be the same lie as claiming
    supply we never measured. The distance is data on every admitted row, so the copy
    reads it — and only claims "far" when the nearest match actually is."""

    def _turn(self, admitted_rows):
        """One topical browse turn with the semantic fetch returning `admitted_rows` and
        the matcher passing them all through."""
        ctx = {"activity_browse_active": True, "browse_draft": {"_asked": True},
               "phone_verified": True}
        with patch(
            "app.activity_browse._fetch_admitted_events", return_value=(admitted_rows, False)
        ), patch(
            "app.activity_browse._filter_events_by_query",
            side_effect=lambda ev, q: (list(ev), "cricket"),
        ), patch("app.activity_browse._attach_host_names"):
            reply = run_activity_browse_turn(
                user_message="any cricket?",
                session_ctx=ctx,
                history=[],
                user_jwt="jwt",
                home_block_id="b1",
            )
        return reply, ctx

    def test_the_message_names_the_distance_and_does_not_say_near_you(self):
        reply, ctx = self._turn(
            [_ev("e1", "Cricket nets", distance_meters=144840.0)]  # 90 miles
        )
        self.assertIn("90 miles", reply)
        # The near-you header must not be used — "no cricket NEAR YOU, but ... 90 miles
        # out" is the honest form and does contain the phrase.
        self.assertNotIn("coming up", reply.lower())
        self.assertIn("out", reply.lower())
        # The cards still render — a far match is a result, not an empty state.
        self.assertEqual([p["title"] for p in ctx["activity_previews"]], ["Cricket nets"])
        self.assertIsNone((ctx.get("browse_draft") or {}).get("_seek_offer"))

    def test_the_closest_of_several_is_the_one_quoted(self):
        reply, _ctx = self._turn([
            _ev("e1", "Far nets", distance_meters=160934.0),   # 100 miles
            _ev("e2", "Closer nets", distance_meters=96560.4),  # 60 miles
        ])
        self.assertIn("60 miles", reply)
        self.assertNotIn("100 miles", reply)

    def test_a_far_match_without_a_distance_falls_back_to_the_plain_header(self):
        """Never invent a number. No distance measured means no distance claimed."""
        reply, _ctx = self._turn([_ev("e1", "Cricket nets")])
        self.assertNotIn("miles", reply.lower())
        self.assertIn("coming up", reply.lower())

    def test_nearest_beyond_the_threshold_renders_the_miles(self):
        # Just past far_copy_m (40 km): the honest header is the far one.
        reply, _ctx = self._turn([_ev("e1", "Cricket nets", distance_meters=48280.0)])  # 30 mi
        self.assertIn("30 miles", reply)
        self.assertNotIn("coming up", reply.lower())

    def test_nearest_inside_the_threshold_renders_the_plain_header(self):
        # A match five miles away means "nothing near you" would be false — however far
        # the other admitted match is, the plain header is the true one.
        reply, ctx = self._turn([
            _ev("e1", "Far nets", distance_meters=144840.0),   # 90 miles
            _ev("e2", "Local nets", distance_meters=8046.7),   # 5 miles
        ])
        self.assertNotIn("miles", reply.lower())
        self.assertIn("coming up", reply.lower())
        # Both still render; the threshold changes the copy, not the results.
        self.assertEqual(len(ctx["activity_previews"]), 2)

    def test_the_far_strings_exist_in_all_three_languages(self):
        from app.i18n import _STRINGS

        for key in ("browse.events_header_far", "browse.events_header_label_far"):
            for lang in ("en", "es", "pt"):
                self.assertTrue(_STRINGS[key][lang].strip(), f"{key}/{lang}")
                self.assertIn("{miles}", _STRINGS[key][lang])


class FarMilesThresholdTests(unittest.TestCase):
    """_far_miles on its own: the threshold is a keyword default and the value is the
    nearest meet's, via _nearest_miles."""

    def test_default_threshold_is_forty_km(self):
        self.assertIsNone(_far_miles([_ev("a", "x", distance_meters=40000.0)]))   # at, not beyond
        self.assertEqual(_far_miles([_ev("a", "x", distance_meters=40001.0)]), 25)

    def test_threshold_is_overridable(self):
        rows = [_ev("a", "x", distance_meters=20000.0)]
        self.assertIsNone(_far_miles(rows))
        self.assertEqual(_far_miles(rows, far_copy_m=10000.0), 12)

    def test_nearest_decides_and_is_what_is_quoted(self):
        rows = [
            _ev("far", "x", distance_meters=160934.0),
            _ev("near", "y", distance_meters=96560.4),
        ]
        self.assertEqual(_far_miles(rows), 60)
        rows.append(_ev("local", "z", distance_meters=1000.0))
        self.assertIsNone(_far_miles(rows))

    def test_no_distances_is_none(self):
        self.assertIsNone(_far_miles([_ev("a", "x")]))
        self.assertIsNone(_far_miles([]))


class RingIsCutOnDistanceNotOnSeenIdsTests(unittest.TestCase):
    """fetch_preview_events_on_block stamps the RPC's measured distance onto every row it
    returns — the fallback read's rows must carry a distance too, or the header would
    have nothing to quote when the semantic read could not run."""

    def _fetch(self, *, distances, min_distance_meters=None):
        from app.discovery_route import fetch_preview_events_on_block

        sb = MagicMock()
        chain = MagicMock()
        for m in ("select", "eq", "gte", "in_", "neq", "order", "limit"):
            getattr(chain, m).return_value = chain
        chain.execute.return_value = MagicMock(
            data=[{"id": eid, "title": eid} for eid in distances]
        )
        sb.table.return_value = chain
        with patch("app.discovery_route.service_client", return_value=sb), patch(
            "app.discovery_route.event_distances_near_block", return_value=dict(distances)
        ), patch("app.event_publish.roll_recurring_events"):
            rows = fetch_preview_events_on_block(
                "zip-90001", limit=40, pool=40, min_distance_meters=min_distance_meters
            )
        return rows, chain

    def test_distance_is_stamped_onto_every_row(self):
        rows, _chain = self._fetch(distances={"far": 90000.0})
        self.assertEqual(rows[0]["distance_meters"], 90000.0)


class DistanceLookupContractTests(unittest.TestCase):
    """event_ids_near_block's contract is load-bearing and unchanged: [] means nothing
    nearby, None means the block could not be placed."""

    def _sb(self, rows):
        sb = MagicMock()
        rpc = MagicMock()
        rpc.execute.return_value = MagicMock(data=rows)
        sb.rpc.return_value = rpc
        return sb

    def test_distances_come_back_keyed_by_id_nearest_first(self):
        from app.discovery_route import event_distances_near_block

        sb = self._sb([{"id": "a", "distance_meters": 100.0},
                       {"id": "b", "distance_meters": 900.0}])
        with patch("app.places._centroid", return_value=(28.36, -81.25)), patch(
            "app.discovery_route.service_client", return_value=sb
        ):
            self.assertEqual(
                event_distances_near_block("zip-32827"), {"a": 100.0, "b": 900.0}
            )

    def test_the_id_wrapper_still_returns_a_list_in_order(self):
        from app.discovery_route import event_ids_near_block

        sb = self._sb([{"id": "a", "distance_meters": 100.0},
                       {"id": "b", "distance_meters": 900.0}])
        with patch("app.places._centroid", return_value=(28.36, -81.25)), patch(
            "app.discovery_route.service_client", return_value=sb
        ):
            self.assertEqual(event_ids_near_block("zip-32827"), ["a", "b"])

    def test_an_unplaceable_block_is_none_not_empty(self):
        from app.discovery_route import event_distances_near_block, event_ids_near_block

        with patch("app.places._centroid", return_value=None):
            self.assertIsNone(event_distances_near_block("zip-99999"))
            self.assertIsNone(event_ids_near_block("zip-99999"))

    def test_a_missing_distance_reads_as_zero_rather_than_crashing(self):
        from app.discovery_route import event_distances_near_block

        sb = self._sb([{"id": "a"}, {"id": "b", "distance_meters": "junk"}])
        with patch("app.places._centroid", return_value=(28.36, -81.25)), patch(
            "app.discovery_route.service_client", return_value=sb
        ):
            self.assertEqual(event_distances_near_block("zip-32827"), {"a": 0.0, "b": 0.0})

    def test_the_radius_is_passed_per_call_and_defaults_to_the_env_knob(self):
        from app.discovery_route import event_distances_near_block

        sb = self._sb([])
        with patch("app.places._centroid", return_value=(28.36, -81.25)), patch(
            "app.discovery_route.service_client", return_value=sb
        ), patch("app.discovery_route.activity_radius_meters", return_value=40000.0):
            event_distances_near_block("zip-32827")
            self.assertEqual(sb.rpc.call_args[0][1]["p_radius_meters"], 40000.0)
            event_distances_near_block("zip-32827", radius_meters=200_000.0)
            self.assertEqual(sb.rpc.call_args[0][1]["p_radius_meters"], 200_000.0)


if __name__ == "__main__":
    unittest.main()


class CouldNotCheckTests(unittest.TestCase):
    """The matcher could not run and nothing contains the word: Lana says she couldn't
    check. Never every event shown as a match, never "nothing matched", and never the
    ring or the far probe re-running the same failing check."""

    def _turn(self, rows, *, message="any cricket?", llm="raises", ctx=None):
        ctx = ctx if ctx is not None else {"activity_browse_active": True,
                                           "browse_draft": {"_asked": True},
                                           "phone_verified": True}
        llm_patches = [patch("app.orchestrator.llm.llm_configured", return_value=llm != "off")]
        if llm == "raises":
            llm_patches += [
                patch("app.orchestrator.llm.llm_json", side_effect=RuntimeError("down")),
                patch("app.orchestrator.llm.router_model", return_value="m"),
            ]
        # One search now (distance admission, PR #151): forced to its distance-only
        # fallback so _fetch_block_events controls the rows. There is no second ring, so
        # "the turn moved on" is the far probe — `widen` is kept as that alias.
        with patch("app.activity_browse._fetch_block_events", return_value=list(rows)), patch(
            "app.activity_browse._fetch_admitted_events", return_value=None
        ), patch("app.activity_browse._far_offer") as far, patch(
            "app.activity_browse._zip_gate_frame", return_value=None
        ):
            widen = far
            for p in llm_patches:
                p.start()
            try:
                reply = run_activity_browse_turn(
                    user_message=message, session_ctx=ctx, history=[],
                    user_jwt="jwt", home_block_id="b1",
                )
            finally:
                for p in llm_patches:
                    p.stop()
        return reply, ctx, widen, far

    def test_a_failed_model_call_says_it_could_not_check(self):
        rows = [_ev("e1", "Book club"), _ev("e2", "Pickup basketball")]
        reply, ctx, widen, far = self._turn(rows, llm="raises")
        self.assertIn("couldn't check", reply.lower())
        self.assertIn("cricket", reply.lower())
        self.assertEqual(ctx.get("activity_previews"), [])
        self.assertEqual(
            ctx["browse_draft"]["suggestions"], ["Yes, listen for me", "Widen the search"]
        )
        self.assertTrue(ctx["browse_draft"]["_seek_offer"])
        self.assertIsNone(ctx.get("browse_scores"))
        widen.assert_not_called()
        far.assert_not_called()

    def test_no_model_configured_says_the_same(self):
        rows = [_ev("e1", "Book club")]
        reply, ctx, widen, far = self._turn(rows, llm="off")
        self.assertIn("couldn't check", reply.lower())
        self.assertEqual(ctx.get("activity_previews"), [])
        widen.assert_not_called()
        far.assert_not_called()

    def test_never_claims_nothing_matched(self):
        reply, _ctx, _w, _f = self._turn([_ev("e1", "Book club")], llm="raises")
        self.assertNotIn("no **cricket** activities", reply.lower())
        self.assertNotIn("near you", reply.lower())

    def test_a_genuine_keyword_hit_is_still_shown(self):
        rows = [_ev("e1", "Sunday Cricket"), _ev("e2", "Book club")]
        reply, ctx, widen, _far = self._turn(rows, message="cricket", llm="raises")
        self.assertNotIn("couldn't check", reply.lower())
        self.assertEqual(
            [p["title"] for p in ctx.get("activity_previews") or []], ["Sunday Cricket"]
        )
        widen.assert_not_called()

    def test_a_long_ask_is_not_parroted(self):
        reply, _ctx, _w, _f = self._turn(
            [_ev("e1", "Book club")],
            message="are there any cricket games for my six year old this month",
            llm="raises",
        )
        self.assertIn("couldn't check what's on for that", reply.lower())

    def test_stale_offer_pills_are_cleared(self):
        ctx = {"activity_browse_active": True, "phone_verified": True,
               "browse_draft": {"_asked": True, "_area_offer_chip": "Look in Foster City",
                                "_area_offer_block_id": "b9", "_community_chip": "Look beyond X"}}
        _reply, ctx, _w, _f = self._turn([_ev("e1", "Book club")], ctx=ctx)
        draft = ctx["browse_draft"]
        self.assertIsNone(draft.get("_area_offer_chip"))
        self.assertIsNone(draft.get("_area_offer_block_id"))
        self.assertIsNone(draft.get("_community_chip"))

    def test_the_listen_pill_is_read_as_accept_next_turn(self):
        _reply, ctx, _w, _f = self._turn([_ev("e1", "Book club")], llm="raises")
        with patch("app.orchestrator.llm.llm_configured", return_value=False), patch(
            "app.look_meet.start_meet_seek_from_interest", return_value="saved"
        ) as seek, patch("app.discovery_route.resolve_block_id", return_value="b1"):
            reply = run_activity_browse_turn(
                user_message="Yes, listen for me", session_ctx=ctx, history=[],
                user_jwt="jwt", home_block_id="b1",
            )
        self.assertEqual(reply, "saved")
        self.assertEqual(seek.call_args.kwargs["interest"], "any cricket?")

    def test_the_community_shape_names_the_community_pill(self):
        from app.community_scope import CTX_KEY

        ctx = {"activity_browse_active": True, "browse_draft": {"_asked": True},
               "phone_verified": True, CTX_KEY: {"place_id": "p1", "name": "CF Fitness"}}
        with patch("app.auth.jwt_user_id", return_value="me"), patch(
            "app.community_scope.community_events",
            return_value=[_ev("e1", "Book club")],
        ), patch("app.activity_browse._attach_host_names"):
            reply, ctx, widen, far = self._turn([], ctx=ctx, llm="raises")
        self.assertIn("couldn't check", reply.lower())
        self.assertIn("CF Fitness", reply)
        self.assertIn("look beyond cf fitness", reply.lower())
        self.assertEqual(
            ctx["browse_draft"]["suggestions"], ["Yes, listen for me", "Look beyond CF Fitness"]
        )
        self.assertNotIn("no **cricket** activities at", reply.lower())
        widen.assert_not_called()
        far.assert_not_called()

    def test_strings_exist_in_every_language_with_no_leftover_placeholders(self):
        from app.i18n import _STRINGS, t

        for key, kw in (
            ("browse.filter_unavailable_interest", {"interest": "cricket"}),
            ("browse.filter_unavailable_generic", {}),
            ("browse.filter_unavailable_community_interest",
             {"interest": "cricket", "community": "CF"}),
            ("browse.filter_unavailable_community_generic", {"community": "CF"}),
        ):
            self.assertEqual(set(_STRINGS[key]), {"en", "es", "pt"}, key)
            with patch("app.orchestrator.llm.llm_configured", return_value=False):
                for lang in ("en", "es", "pt"):
                    self.assertNotIn("{", t(key, lang, **kw), (key, lang))


class UncheckedRingTests(unittest.TestCase):
    """The ring must never announce rows the matcher could not judge — "Nothing near
    you, but here's what I found about 90 miles out" over unrelated meets."""

    def test_the_turn_never_shows_the_far_header_over_unchecked_rows(self):
        """End to end: the admitted search returns only far rows the matcher cannot judge —
        no far header, no cards. (Was the ring's guard before distance admission.)"""
        ring = [_ev("e1", "Book club", distance_meters=90000.0)]
        ctx = {"activity_browse_active": True, "browse_draft": {"_asked": True},
               "phone_verified": True}
        with patch("app.activity_browse._fetch_admitted_events",
                   return_value=(list(ring), False)), patch(
            "app.auth.jwt_user_id", return_value="me"
        ), patch("app.activity_browse._attach_host_names"), patch(
            "app.activity_browse._far_offer", return_value=([], "", [])
        ), patch("app.activity_browse._zip_gate_frame", return_value=None), patch(
            "app.orchestrator.llm.llm_configured", return_value=False
        ):
            reply = run_activity_browse_turn(
                user_message="cricket", session_ctx=ctx, history=[],
                user_jwt="jwt", home_block_id="b1",
            )
        self.assertNotIn("miles out", reply.lower())
        self.assertEqual(ctx.get("activity_previews"), [])


class _StretchTurnHarness:
    """Drives one browse turn with the matcher stood in (it stamps scores in place, like
    the real one), the ring / far probe as mocks, and the writer's model off."""

    _PHRASE = "asked for violin, this is a jam night"

    def _rows(self, *, score=0.7, phrase=_PHRASE):
        return [
            _ev("e1", "Book club", description="Monthly reads."),
            _ev("e2", "Sunday jam night", description="Bring an instrument."),
        ], {"e1": (0.1, "asked for violin, this is a book club"), "e2": (score, phrase)}

    def _turn(self, *, rows, judged, matched_ids=(), flag=True, stretch_first=True,
              message="violin", ctx=None, ring=([], ""), label="violin"):
        ctx = ctx if ctx is not None else {"activity_browse_active": True,
                                           "browse_draft": {"_asked": True},
                                           "phone_verified": True}

        def _filter(ev, q, **_kw):  # at_place: a community browse names its place
            # Stamp in place, exactly like the real matcher: every row scored. A judged
            # entry is (score, phrase) or (score, phrase, fits_other); fits defaults True
            # (the ask named no date/time/host, or the event meets them).
            for row in ev:
                score, phrase, *rest = judged.get(row["id"], (0.0, ""))
                row["topic_score"] = score
                row["topic_mismatch"] = phrase
                row["fits_other_constraints"] = rest[0] if rest else True
            return [r for r in ev if r["id"] in matched_ids], label

        with patch("app.activity_browse._fetch_block_events", return_value=rows), patch(
            "app.activity_browse._filter_events_by_query", side_effect=_filter
        ), patch("app.activity_browse._fetch_admitted_events", return_value=None), patch(
            "app.activity_browse._far_offer", return_value=([], "", [])
        ) as far, patch("app.activity_browse._zip_gate_frame", return_value=None), patch(
            "app.lana_paths.stretch_offer_enabled", return_value=flag
        ), patch("app.activity_browse._STRETCH_BEFORE_WIDEN", stretch_first), patch(
            # The writer's model is off: the deterministic template is the output.
            "app.orchestrator.llm.llm_configured", return_value=False
        ):
            reply = run_activity_browse_turn(
                user_message=message, session_ctx=ctx, history=[],
                user_jwt="jwt", home_block_id="b1",
            )
        # No second ring any more: "the turn moved on past the stretch" is the far probe.
        return reply, ctx, far, far

    def _rows_with_pii(self):
        rows = [_ev("e1", "Book club"), _ev("e2", "Sunday jam night"),
                _ev("e3", "Pottery"), _ev("e4", "Guitar circle")]
        judged = {
            "e1": (0.1, "asked for violin, this is a book club"),
            "e2": (0.7, "asked for violin, email me at jo@example.com"),
            "e3": (0.2, "asked for violin, this is pottery"),
            "e4": (0.5, "asked for violin, this is guitar"),
        }
        return rows, judged


class StretchOfferTurnTests(_StretchTurnHarness, unittest.TestCase):
    """Rapport Reply at turn level: nothing matched, but a NEARBY event was rated closely
    related — offer it as the one card, ending on the listen offer. Behind the flag."""

    # ── Date/time/host (Asjid's review): a stretch differs in topic ONLY ────────────

    def _badminton(self, *, tennis_fits):
        rows = [_ev("t1", "Tennis meetup", starts_at="2026-10-07T18:00:00+00:00"),
                _ev("b1", "Book club")]
        judged = {"t1": (0.7, "asked for badminton, this is tennis", tennis_fits),
                  "b1": (0.1, "asked for badminton, this is a book club", tennis_fits)}
        return self._turn(rows=rows, judged=judged, message="badminton this Saturday",
                          label="badminton")

    def test_badminton_saturday_never_offers_tennis_next_wednesday(self):
        """Asjid's case: tennis scores 0.7 but is on the wrong day. Offering it would
        read as "on Saturday too", since the reply only explains the topic. No stretch:
        today's path runs — the ring and the far probe, no card."""
        reply, ctx, widen, far = self._badminton(tennis_fits=False)
        self.assertNotIn("Tennis meetup", reply)
        self.assertEqual(ctx["activity_previews"], [])
        widen.assert_called_once()
        far.assert_called_once()
        rec = ctx["browse_no_match"]
        self.assertFalse(rec["stretch_shown"])
        self.assertEqual(rec["near_misses"][0]["event_id"], "t1")
        self.assertFalse(rec["near_misses"][0]["fits"])

    def test_the_same_tennis_meetup_on_saturday_is_offered(self):
        reply, ctx, widen, far = self._badminton(tennis_fits=True)
        self.assertIn("Tennis meetup", reply)
        self.assertIn("asked for badminton, this is tennis", reply)
        self.assertEqual([p["title"] for p in ctx["activity_previews"]], ["Tennis meetup"])
        widen.assert_not_called()
        far.assert_not_called()
        self.assertTrue(ctx["browse_no_match"]["near_misses"][0]["fits"])

    def test_an_ask_with_no_date_time_or_host_still_stretches(self):
        """"violin" names no date, time or host, so the matcher reports fits=true for
        every event and the jam night still qualifies."""
        rows, judged = self._rows()
        judged["e2"] = (0.7, self._PHRASE, True)
        reply, ctx, _w, _f = self._turn(rows=rows, judged=judged, message="violin")
        self.assertIn("Sunday jam night", reply)

    def test_a_fitting_0_6_beats_a_wrong_day_0_8(self):
        rows = [_ev("w1", "Squash ladder"), _ev("s1", "Pickleball social")]
        judged = {"w1": (0.8, "asked for badminton, this is squash", False),
                  "s1": (0.6, "asked for badminton, this is pickleball", True)}
        reply, ctx, _w, _f = self._turn(rows=rows, judged=judged,
                                        message="badminton this Saturday", label="badminton")
        self.assertIn("Pickleball social", reply)
        self.assertNotIn("Squash ladder", reply)
        self.assertEqual(ctx["browse_no_match"]["stretch_event_id"], "s1")
        self.assertEqual([n["fits"] for n in ctx["browse_no_match"]["near_misses"]][:2],
                         [False, True])

    def test_a_close_stretch_is_offered_as_one_card(self):
        rows, judged = self._rows()
        reply, ctx, widen, far = self._turn(rows=rows, judged=judged)
        self.assertIn("Sunday jam night", reply)
        self.assertIn(self._PHRASE, reply)
        self.assertEqual([p["title"] for p in ctx["activity_previews"]], ["Sunday jam night"])
        self.assertEqual(ctx["browse_draft"]["suggestions"],
                         ["Yes, listen for me", "Widen the search"])
        self.assertTrue(ctx["browse_draft"]["_seek_offer"])
        # The listen offer is for the ORIGINAL ask, never the stretch.
        self.assertEqual(ctx["browse_draft"]["interest"], "violin")
        self.assertEqual(ctx["browse_scores"], {"e2": 0.7})
        widen.assert_not_called()
        far.assert_not_called()

    def test_nothing_close_runs_todays_path(self):
        rows, judged = self._rows(score=0.4)
        reply, ctx, widen, far = self._turn(rows=rows, judged=judged)
        self.assertNotIn("Sunday jam night", reply)
        self.assertEqual(ctx["activity_previews"], [])
        widen.assert_called_once()
        far.assert_called_once()

    def test_a_category_ask_that_matched_never_reaches_the_picker(self):
        rows = [_ev("p1", "Pizza night"), _ev("c1", "Coffee walk")]
        judged = {"p1": (0.7, ""), "c1": (0.7, "")}
        with patch("app.stretch_offer.pick_stretch") as picker:
            reply, ctx, widen, _far = self._turn(
                rows=rows, judged=judged, matched_ids=("p1", "c1"),
                message="food or drink", label="food or drink",
            )
        picker.assert_not_called()
        widen.assert_not_called()
        self.assertIn("coming up", reply.lower())
        self.assertEqual([p["title"] for p in ctx["activity_previews"]],
                         ["Pizza night", "Coffee walk"])

    def test_an_unmatched_same_thing_score_is_not_a_stretch(self):
        """0.9 unmatched = right topic, wrong date/time/host. Not a topic stretch."""
        rows, judged = self._rows(score=0.9)
        reply, ctx, widen, _far = self._turn(rows=rows, judged=judged)
        self.assertNotIn("Sunday jam night", reply)
        self.assertEqual(ctx["activity_previews"], [])
        widen.assert_called_once()

    def test_an_empty_phrase_is_not_offered(self):
        rows, judged = self._rows(phrase="")
        reply, ctx, widen, _far = self._turn(rows=rows, judged=judged)
        self.assertNotIn("Sunday jam night", reply)
        widen.assert_called_once()

    def test_flag_off_is_todays_behaviour(self):
        rows, judged = self._rows()
        reply, ctx, widen, far = self._turn(rows=rows, judged=judged, flag=False)
        self.assertNotIn("Sunday jam night", reply)
        self.assertEqual(ctx["activity_previews"], [])
        self.assertIn("keep an ear out", reply)
        widen.assert_called_once()
        far.assert_called_once()

    def test_order_flipped_still_offers_the_stretch_before_the_far_probe(self):
        rows, judged = self._rows()
        reply, ctx, _widen, far = self._turn(rows=rows, judged=judged, stretch_first=False)
        far.assert_not_called()
        self.assertIn("Sunday jam night", reply)
        self.assertEqual([p["title"] for p in ctx["activity_previews"]], ["Sunday jam night"])

    def _community_turn(self, *, score):
        from app.community_scope import CTX_KEY

        rows, judged = self._rows(score=score)
        ctx = {"activity_browse_active": True, "browse_draft": {"_asked": True},
               "phone_verified": True, CTX_KEY: {"place_id": "p1", "name": "CF Fitness"}}
        with patch("app.auth.jwt_user_id", return_value="me"), patch(
            "app.community_scope.community_events", return_value=rows
        ), patch("app.activity_browse._attach_host_names"):
            return self._turn(rows=[], judged=judged, ctx=ctx)

    def test_inside_a_community_the_closest_thing_in_it_comes_first(self):
        """Tommaso + Pouya (2026-10-01): "No yoga in Run with Maya, but Maya's group does a
        Sunday stretch session". The stretch comes from the community's OWN calendar, and
        looking beyond the community stays one tap away — never the only answer."""
        reply, ctx, widen, far = self._community_turn(score=0.7)
        self.assertIn("Sunday jam night", reply)
        self.assertIn("CF Fitness", reply)
        self.assertNotIn("near you", reply.lower())
        self.assertEqual([p["title"] for p in ctx["activity_previews"]], ["Sunday jam night"])
        draft = ctx["browse_draft"]
        self.assertEqual(draft["suggestions"][0], "Yes, listen for me")
        # The way out is the community's own pill, not "widen the search".
        self.assertEqual(draft["suggestions"][1], draft["_community_chip"])
        self.assertIn("CF Fitness", draft["_community_chip"])
        # Community scope never reaches the ring or the far probe.
        widen.assert_not_called()
        far.assert_not_called()

    def test_nothing_related_inside_a_community_still_asks_to_look_beyond(self):
        reply, ctx, _w, _f = self._community_turn(score=0.3)
        self.assertNotIn("Sunday jam night", reply)
        self.assertEqual(ctx["activity_previews"], [])
        self.assertIn("CF Fitness", ctx["browse_draft"]["_community_chip"])

    def test_stale_scores_are_cleared_on_a_no_match_turn(self):
        rows, judged = self._rows(score=0.4)
        ctx = {"activity_browse_active": True, "browse_draft": {"_asked": True},
               "phone_verified": True, "browse_scores": {"old": 0.9}}
        _reply, ctx, _w, _f = self._turn(rows=rows, judged=judged, ctx=ctx)
        self.assertIsNone(ctx["browse_scores"])

    def test_a_bare_sure_next_turn_means_listen_for_the_original_ask(self):
        """The "yes" problem: the reply ends on the listen offer, and the pill handler
        reads "sure" as accept — it saves a VIOLIN seek, never the jam night."""
        rows, judged = self._rows()
        _reply, ctx, _w, _f = self._turn(rows=rows, judged=judged)
        with patch("app.orchestrator.llm.llm_configured", return_value=False), patch(
            "app.look_meet.start_meet_seek_from_interest", return_value="saved"
        ) as seek, patch("app.discovery_route.resolve_block_id", return_value="b1"):
            reply = run_activity_browse_turn(
                user_message="sure", session_ctx=ctx, history=[],
                user_jwt="jwt", home_block_id="b1",
            )
        self.assertEqual(reply, "saved")
        self.assertEqual(seek.call_args.kwargs["interest"], "violin")


class NoMatchRecordTests(unittest.TestCase):
    """Checkpoint C: every no-match turn leaves a redacted record of what the matcher
    nearly matched, whether a stretch was shown, and whether the matcher ran at all."""

    _h = _StretchTurnHarness()

    def test_a_stretch_turn_records_the_top_three_redacted(self):
        rows, judged = self._h._rows_with_pii()
        _reply, ctx, _w, _f = self._h._turn(rows=rows, judged=judged)
        rec = ctx["browse_no_match"]
        self.assertEqual([n["event_id"] for n in rec["near_misses"]], ["e2", "e4", "e3"])
        self.assertEqual([n["score"] for n in rec["near_misses"]], [0.7, 0.5, 0.2])
        self.assertIn("[email]", rec["near_misses"][0]["mismatch"])
        self.assertNotIn("jo@example.com", str(rec))
        self.assertTrue(rec["stretch_shown"])
        self.assertEqual(rec["stretch_event_id"], "e2")
        self.assertEqual(rec["filter"], "judged")
        self.assertTrue(rec["stretch_first"])

    def test_a_no_match_without_a_stretch_still_records_near_misses(self):
        rows, judged = self._h._rows_with_pii()
        judged["e2"] = (0.4, "asked for violin, this is a jam night")
        _reply, ctx, _w, _f = self._h._turn(rows=rows, judged=judged)
        rec = ctx["browse_no_match"]
        self.assertFalse(rec["stretch_shown"])
        self.assertIsNone(rec["stretch_event_id"])
        self.assertEqual(len(rec["near_misses"]), 3)
        self.assertEqual(rec["filter"], "judged")

    def test_flag_off_still_records(self):
        rows, judged = self._h._rows_with_pii()
        _reply, ctx, _w, _f = self._h._turn(rows=rows, judged=judged, flag=False)
        rec = ctx["browse_no_match"]
        self.assertFalse(rec["stretch_shown"])
        self.assertEqual(rec["near_misses"][0]["event_id"], "e2")

    def test_an_empty_area_records_no_candidates(self):
        _reply, ctx, _w, _f = self._h._turn(rows=[], judged={})
        rec = ctx["browse_no_match"]
        self.assertEqual(rec["filter"], "no_candidates")
        self.assertEqual(rec["near_misses"], [])

    def test_a_match_turn_records_nothing(self):
        rows = [_ev("p1", "Pizza night")]
        _reply, ctx, _w, _f = self._h._turn(
            rows=rows, judged={"p1": (1.0, "")}, matched_ids=("p1",), message="pizza",
        )
        self.assertNotIn("browse_no_match", ctx)

    def test_the_unchecked_turn_records_unchecked_with_no_scores(self):
        ctx = {"activity_browse_active": True, "browse_draft": {"_asked": True},
               "phone_verified": True}
        with patch("app.activity_browse._fetch_block_events",
                   return_value=[_ev("e1", "Book club")]), patch(
            "app.activity_browse._zip_gate_frame", return_value=None
        ), patch("app.orchestrator.llm.llm_configured", return_value=False):
            run_activity_browse_turn(user_message="violin", session_ctx=ctx, history=[],
                                     user_jwt="jwt", home_block_id="b1")
        rec = ctx["browse_no_match"]
        self.assertEqual(rec["filter"], "unchecked")
        self.assertEqual(rec["near_misses"], [])


class OfferResponseTests(unittest.TestCase):
    """The next turn records which way the user answered the offer — and, after a
    stretch, which event they were answering."""

    _h = _StretchTurnHarness()

    def _offer_then(self, message, *, extra_patches=()):
        rows, judged = self._h._rows_with_pii()
        _reply, ctx, _w, _f = self._h._turn(rows=rows, judged=judged)
        self.assertEqual(ctx["browse_draft"].get("_stretch_event_id"), "e2")
        patches = [
            patch("app.orchestrator.llm.llm_configured", return_value=False),
            patch("app.look_meet.start_meet_seek_from_interest", return_value="saved"),
            patch("app.discovery_route.resolve_block_id", return_value="b1"),
            patch("app.activity_browse._fetch_block_events", return_value=[]),
            patch("app.activity_browse._fetch_admitted_events", return_value=None),
            patch("app.activity_browse._far_offer", return_value=([], "", [])),
            patch("app.activity_browse._zip_gate_frame", return_value=None),
            *extra_patches,
        ]
        for p in patches:
            p.start()
        try:
            run_activity_browse_turn(user_message=message, session_ctx=ctx, history=[],
                                     user_jwt="jwt", home_block_id="b1")
        finally:
            for p in patches:
                p.stop()
        return ctx

    def _response(self, ctx):
        return ctx["browse_offer_response"]

    def test_listen(self):
        ctx = self._offer_then("Yes, listen for me")
        self.assertEqual(self._response(ctx),
                         {"response": "listen", "offered_stretch_event_id": "e2"})

    def test_a_bare_sure_is_listen(self):
        self.assertEqual(self._response(self._offer_then("sure"))["response"], "listen")

    def test_widen(self):
        self.assertEqual(self._response(self._offer_then("Widen the search"))["response"],
                         "widen")

    def test_a_new_topic_is_a_new_search(self):
        ctx = self._offer_then("pottery")
        self.assertEqual(self._response(ctx)["response"], "new_search")
        self.assertEqual(self._response(ctx)["offered_stretch_event_id"], "e2")

    def test_a_typed_zip(self):
        ctx = self._offer_then(
            "what about 94404", extra_patches=(
                patch("app.discovery_route.resolve_zip_coverage", return_value=(None, "x")),
                patch("app.discovery_route.note_zip_out_of_coverage"),
            ),
        )
        self.assertEqual(self._response(ctx)["response"], "zip")

    def test_the_stretch_id_is_consumed_once(self):
        ctx = self._offer_then("pottery")
        self.assertNotIn("_stretch_event_id", ctx.get("browse_draft") or {})

    def test_area_and_community_chips(self):
        rows, judged = self._h._rows_with_pii()
        for chip_key, chip, expected in (
            ("_area_offer_chip", "Look in Foster City", "area"),
            ("_community_chip", "Look beyond CF Fitness", "community"),
        ):
            ctx = {"activity_browse_active": True, "phone_verified": True,
                   "browse_draft": {"_asked": True, "_seek_offer": True, "interest": "violin",
                                    chip_key: chip, "_area_offer_block_id": "b9"}}
            with patch("app.orchestrator.llm.llm_configured", return_value=False), patch(
                "app.activity_browse._fetch_block_events", return_value=[]
            ), patch("app.activity_browse._fetch_admitted_events", return_value=None), patch(
                "app.activity_browse._far_offer", return_value=([], "", [])
            ), patch("app.activity_browse._zip_gate_frame", return_value=None), patch(
                "app.community_scope.clear_active_community"
            ):
                run_activity_browse_turn(user_message=chip, session_ctx=ctx, history=[],
                                         user_jwt="jwt", home_block_id="b1")
            self.assertEqual(ctx["browse_offer_response"],
                             {"response": expected, "offered_stretch_event_id": None})


class BrowseTelemetryPlumbingTests(unittest.TestCase):
    def test_metadata_helper_carries_only_what_was_set(self):
        from app.activity_browse import browse_turn_metadata

        with patch("app.orchestrator.llm.llm_configured", return_value=False):
            self.assertEqual(browse_turn_metadata(None), {})
            self.assertEqual(browse_turn_metadata({"browse_no_match": None}), {})
            rec = {"filter": "judged"}
            self.assertEqual(
                browse_turn_metadata({"browse_no_match": rec, "browse_offer_response": {},
                                      "other": 1}),
                {"browse_no_match": rec},
            )

    def test_both_keys_are_turn_scoped(self):
        from app.turn_surfaces import TURN_SCOPED_SURFACES, clear_turn_surfaces

        self.assertIn("browse_no_match", TURN_SCOPED_SURFACES)
        self.assertIn("browse_offer_response", TURN_SCOPED_SURFACES)
        ctx = {"browse_no_match": {"filter": "judged"}, "browse_offer_response": {"r": 1}}
        clear_turn_surfaces(ctx)
        self.assertIsNone(ctx["browse_no_match"])
        self.assertIsNone(ctx["browse_offer_response"])

    def test_the_entry_turn_keeps_the_record_through_the_routing_wipe(self):
        """The first browse turn's ctx goes through _routing_ctx, which wipes turn
        surfaces. Without the re-attach the record would never reach the metadata."""
        from app.discovery_route import _start_activity_browse_from_discovery

        def _stamp(**kw):
            kw["session_ctx"]["browse_no_match"] = {"filter": "judged"}
            return "reply"

        with patch("app.orchestrator.llm.llm_configured", return_value=False), patch(
            "app.activity_browse.run_activity_browse_turn", side_effect=_stamp
        ):
            _reply, ctx, _stub, _peers = _start_activity_browse_from_discovery(
                msg="violin", session_ctx={}, history=[], user_jwt="jwt",
                home_block_id="b1",
            )
        self.assertEqual(ctx.get("browse_no_match"), {"filter": "judged"})


class EmptyCommunityBeforeAreaGateTests(unittest.TestCase):
    """Inside a community, an empty calendar is about the community, not the user's home
    ZIP. Followers and students can live anywhere, so an unopened home area must not turn
    a generic "what's going on" into "your area is still coming alive" with no way out."""

    def _turn(self, *, frame):
        from app.community_scope import CTX_KEY

        # The generic CTA entry: its seed text is dropped, so nothing names an interest
        # and, inside a community, the interest question is skipped too.
        ctx = {"activity_browse_active": True, "browse_skip_seed": True,
               "phone_verified": True, CTX_KEY: {"place_id": "p1", "name": "CF Fitness"}}
        with patch("app.auth.jwt_user_id", return_value="me"), patch(
            "app.community_scope.community_events", return_value=[]
        ), patch("app.activity_browse._attach_host_names"), patch(
            "app.activity_browse._zip_gate_frame", return_value=frame
        ), patch("app.activity_browse._compose_area_warming_empty", return_value="warming"), patch(
            "app.orchestrator.llm.llm_configured", return_value=False
        ):
            reply = run_activity_browse_turn(
                user_message="what's going on", session_ctx=ctx, history=[],
                user_jwt="jwt", home_block_id="b1",
            )
        return reply, ctx

    def test_an_unopened_home_area_still_offers_the_way_out(self):
        reply, ctx = self._turn(frame={"state": "closed"})
        self.assertNotEqual(reply, "warming")
        self.assertEqual(
            ctx["browse_draft"]["suggestions"], ["Yes, listen for me", "Look beyond CF Fitness"]
        )

    def test_an_open_home_area_is_unchanged(self):
        reply, ctx = self._turn(frame=None)
        self.assertEqual(
            ctx["browse_draft"]["suggestions"], ["Yes, listen for me", "Look beyond CF Fitness"]
        )
