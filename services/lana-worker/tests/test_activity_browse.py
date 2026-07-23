import unittest
from unittest.mock import patch

from app.activity_browse import (
    activity_browse_should_release,
    reset_activity_browse_state,
    run_activity_browse_turn,
)
from app.discovery_route import (
    _browse_or_seek_decision,
    _resolve_browse_or_meet_answer,
    handle_discovery_turn,
)


class TestBrowseSeekDecision(unittest.TestCase):
    def test_browse(self) -> None:
        self.assertEqual(
            _browse_or_seek_decision({"goal": "activities", "confidence": 0.9}, "what's happening"),
            "browse",
        )

    def test_clear_meet_seek_searches_first(self) -> None:
        # Meet ≡ activity: a confident meet_seek enters the events browse (search-first);
        # the seek is offered only when the search comes up empty.
        self.assertEqual(
            _browse_or_seek_decision(
                {"goal": "save_signal", "signal_intent": "meet_seek",
                 "linear_intent": "looking.meet", "confidence": 0.9},
                "find me a tennis partner",
            ),
            "browse",
        )

    def test_clarify_when_model_torn(self) -> None:
        self.assertEqual(
            _browse_or_seek_decision(
                {"goal": "activities", "confidence": 0.9, "clarify": "browse_or_meet"},
                "something fun this weekend",
            ),
            "clarify",
        )

    def test_clarify_when_low_confidence(self) -> None:
        self.assertEqual(
            _browse_or_seek_decision({"goal": "activities", "confidence": 0.4}, "something fun"),
            "clarify",
        )

    def test_none_for_other_intent(self) -> None:
        self.assertIsNone(_browse_or_seek_decision({"goal": "peers", "confidence": 0.9}, "find moms"))

    def test_service_recommendation_never_enters_browse(self) -> None:
        # A place/service recommendation is a tip_seek — even when the classifier misreads
        # it as the activities space, the browse must decline so routing falls through to
        # the tip path (Google), not an events search answered with unrelated activities.
        self.assertIsNone(
            _browse_or_seek_decision(
                {"goal": "activities", "confidence": 0.9},
                "Recommend babysitting service",
            )
        )


class TestBrowseRelease(unittest.TestCase):
    def test_refine_to_another_activity_stays(self) -> None:
        # "show me cricket instead" is a re-filter, not a pivot.
        self.assertFalse(
            activity_browse_should_release(
                "show me cricket ones instead",
                {"activity_browse_active": True},
                {"goal": "activities", "confidence": 0.9},
            )
        )

    def test_semantic_abandon_releases(self) -> None:
        self.assertTrue(
            activity_browse_should_release(
                "eh i have mixed feelings about this",
                {"activity_browse_active": True},
                {"abandon": True, "confidence": 0.7},
            )
        )

    def test_cross_lane_pivot_releases(self) -> None:
        self.assertTrue(
            activity_browse_should_release(
                "actually connect me with neighbors like me",
                {"activity_browse_active": True},
                {"goal": "peers", "confidence": 0.8},
            )
        )

    def test_switch_to_meet_releases(self) -> None:
        self.assertTrue(
            activity_browse_should_release(
                "actually i want to meet people for this",
                {"activity_browse_active": True},
                {"goal": "save_signal", "signal_intent": "meet_seek", "confidence": 0.8},
            )
        )

    def test_self_claim_during_seek_offer_releases(self) -> None:
        # The seek-offer trap: after an empty search, "I like badminton" must NOT be
        # swallowed as a fresh search kind (→ another "No badminton activities…") — it is
        # an identity claim and releases to the profile brain.
        self.assertTrue(
            activity_browse_should_release(
                "i like badminton",
                {
                    "activity_browse_active": True,
                    "browse_draft": {"interest": "swimming", "_seek_offer": True},
                },
                {"linear_intent": "identity.add_claim", "goal": "chat", "confidence": 0.9},
            )
        )

    def test_accept_pill_during_seek_offer_stays(self) -> None:
        # "Yes, listen for me" reads as a foreign meet_seek to the classifier but is THIS
        # lane's own pill — the seek-offer accept must never release.
        self.assertFalse(
            activity_browse_should_release(
                "Yes, listen for me",
                {
                    "activity_browse_active": True,
                    "browse_draft": {"interest": "badminton", "_seek_offer": True},
                },
                {"goal": "save_signal", "signal_intent": "meet_seek", "confidence": 0.8},
            )
        )

    def test_fresh_kind_during_seek_offer_stays(self) -> None:
        # A new kind after the empty-search offer is a re-search, not a pivot.
        self.assertFalse(
            activity_browse_should_release(
                "what about cricket",
                {
                    "activity_browse_active": True,
                    "browse_draft": {"interest": "badminton", "_seek_offer": True},
                },
                {"goal": "activities", "confidence": 0.9},
            )
        )


class TestResolveClarifier(unittest.TestCase):
    def test_resolve_seek(self) -> None:
        self.assertEqual(_resolve_browse_or_meet_answer("set up a meet", None), "seek")

    def test_resolve_browse(self) -> None:
        self.assertEqual(_resolve_browse_or_meet_answer("see what's happening", None), "browse")


class TestRunBrowseTurn(unittest.TestCase):
    def test_button_entry_asks_interest(self) -> None:
        # The CTA's generic seed is dropped (browse_skip_seed), leaving nothing to mine —
        # only then does P1 ask the interest with chips.
        ctx: dict = {
            "activity_browse_active": True,
            "browse_draft": None,
            "browse_skip_seed": True,
        }
        reply = run_activity_browse_turn(
            user_message="I'm looking for a meet or playgroup",
            session_ctx=ctx,
            history=[],
            user_jwt="jwt",
            home_block_id="b1",
        )
        self.assertIn("what kind of thing", reply.lower())
        self.assertTrue((ctx.get("browse_draft") or {}).get("_asked"))

    @patch(
        "app.activity_browse._fetch_block_events",
        return_value=[
            {
                "title": "FIFA watch party",
                "starts_at": "2026-06-27T18:00:00",
                "venue_name": "The Pub",
                "cohort_tags": [],
            }
        ],
    )
    def test_nl_entry_searches_immediately(self, _fetch) -> None:
        # A natural-language entry carries the interest — no generic "what kind of thing"
        # re-ask; the message is searched on the entry turn.
        ctx: dict = {
            "activity_browse_active": True,
            "browse_draft": None,
            "phone_verified": True,
        }
        reply = run_activity_browse_turn(
            user_message="fifa",
            session_ctx=ctx,
            history=[],
            user_jwt="jwt",
            home_block_id="b1",
        )
        self.assertNotIn("what kind of thing", reply.lower())
        self.assertIn("coming up", reply.lower())
        self.assertEqual(
            [p["title"] for p in ctx.get("activity_previews") or []], ["FIFA watch party"]
        )
        self.assertEqual((ctx.get("browse_draft") or {}).get("interest"), "fifa")

    @patch("app.activity_browse._fetch_block_events", return_value=[])
    def test_dispatched_chip_topic_wins_over_generic_send(self, _fetch) -> None:
        # Tapping "See badminton events" dispatches with a model-authored send that can be
        # generic ("show me what's happening this weekend") — the offer's structured topic
        # must drive the search, not the send text.
        ctx: dict = {"activity_browse_active": True, "browse_draft": None}
        run_activity_browse_turn(
            user_message="show me what's happening this weekend",
            session_ctx=ctx,
            history=[],
            user_jwt="jwt",
            home_block_id="b1",
            slots={
                "_forced_kind": "find_activities",
                "signal_detail": "badminton",
                "goal": "activities",
                "confidence": 0.9,
            },
        )
        self.assertEqual((ctx.get("browse_draft") or {}).get("interest"), "badminton")

    @patch("app.activity_browse._fetch_block_events", return_value=[])
    def test_nl_entry_empty_block_offers_seek(self, _fetch) -> None:
        # Nothing on the block → clearly say so and offer to listen (the seek fallback),
        # instead of a generic re-ask or a verify wall.
        ctx: dict = {"activity_browse_active": True, "browse_draft": None}
        reply = run_activity_browse_turn(
            user_message="any fifa activities for my 6 year old?",
            session_ctx=ctx,
            history=[],
            user_jwt="jwt",
            home_block_id="b1",
        )
        self.assertIn("keep an ear out", reply.lower())
        self.assertTrue((ctx.get("browse_draft") or {}).get("_seek_offer"))
        # The raw sentence is not parroted back as the "kind".
        self.assertNotIn("any fifa activities for my 6 year old", reply.lower())

    @patch(
        "app.activity_browse._fetch_block_events",
        return_value=[
            {
                "title": "FIFA watch party",
                "starts_at": "2026-06-27T18:00:00",
                "venue_name": "The Pub",
                "cohort_tags": [],
            }
        ],
    )
    def test_shows_events_after_interest(self, _fetch) -> None:
        ctx: dict = {
            "activity_browse_active": True,
            "browse_draft": {"_asked": True},
            "phone_verified": True,
        }
        reply = run_activity_browse_turn(
            user_message="sports",
            session_ctx=ctx,
            history=[],
            user_jwt="jwt",
            home_block_id="b1",
        )
        self.assertIn("coming up", reply.lower())
        # The events live in the card list, not duplicated in the chat text.
        self.assertNotIn("FIFA watch party", reply)
        self.assertEqual(
            [p["title"] for p in ctx.get("activity_previews") or []], ["FIFA watch party"]
        )

    def test_reset_clears_state(self) -> None:
        ctx = {"activity_browse_active": True, "browse_draft": {"interest": "x"}, "browse_turns": 3}
        reset_activity_browse_state(ctx)
        self.assertFalse(ctx.get("activity_browse_active"))
        self.assertIsNone(ctx.get("browse_draft"))


class TestBrowseInlineZip(unittest.TestCase):
    """A ZIP the user already said (AI slot) is used — never re-asked. Bare digits alone
    are NOT assumed to be a ZIP unless we explicitly asked for one."""

    @patch(
        "app.activity_browse._fetch_block_events",
        return_value=[
            {
                "title": "Rooftop hang",
                "starts_at": "2026-07-14T18:00:00",
                "venue_name": "The Roof",
                "cohort_tags": [],
            }
        ],
    )
    @patch(
        "app.discovery_route.resolve_zip_coverage",
        return_value=({"block_id": "b-nyc", "display_name": "Upper West Side (10025)"}, "created"),
    )
    def test_entry_with_inline_zip_never_reasks(self, mock_resolve, _fetch) -> None:
        # "I'm in NYC, zip 10025. anything at all this week?" — the AI slot carries the
        # ZIP; the block is resolved (created if uncovered) and events show immediately.
        ctx: dict = {"activity_browse_active": True, "browse_draft": None}
        reply = run_activity_browse_turn(
            user_message="hi! just checking this out. I'm in NYC, zip 10025. anything at all this week?",
            session_ctx=ctx,
            history=[],
            user_jwt="jwt",
            home_block_id=None,
            slots={"zip": "10025"},
        )
        mock_resolve.assert_called_once_with("jwt", "10025")
        self.assertNotIn("zip", reply.lower())
        self.assertEqual(ctx.get("preview_block_id"), "b-nyc")
        self.assertEqual(ctx.get("preview_zip"), "10025")

    @patch("app.discovery_route.resolve_zip_coverage")
    def test_free_text_number_is_not_a_zip(self, mock_resolve) -> None:
        # A 5-digit number the AI did NOT flag as a ZIP (a price) is never resolved as one.
        ctx: dict = {"activity_browse_active": True, "browse_draft": None}
        reply = run_activity_browse_turn(
            user_message="looking for anything fun, my budget is 10000 for the month",
            session_ctx=ctx,
            history=[],
            user_jwt="jwt",
            home_block_id=None,
            slots={"zip": None},
        )
        mock_resolve.assert_not_called()
        self.assertTrue((ctx.get("browse_draft") or {}).get("_need_zip"))
        self.assertIn("zip", reply.lower())

    @patch("app.activity_browse._fetch_block_events", return_value=[])
    @patch(
        "app.discovery_route.resolve_zip_coverage",
        return_value=({"block_id": "b-nyc", "display_name": "Upper West Side (10025)"}, "covered"),
    )
    def test_bare_digits_accepted_when_zip_was_asked(self, mock_resolve, _fetch) -> None:
        # After Lana explicitly asked for the ZIP, a plain "10025" reply is the answer —
        # no AI slot needed.
        ctx: dict = {
            "activity_browse_active": True,
            "browse_draft": {"interest": "fifa", "_need_zip": True, "_asked": True},
        }
        run_activity_browse_turn(
            user_message="10025",
            session_ctx=ctx,
            history=[],
            user_jwt="jwt",
            home_block_id=None,
        )
        mock_resolve.assert_called_once_with("jwt", "10025")
        self.assertEqual(ctx.get("preview_block_id"), "b-nyc")

    @patch("app.discovery_route.resolve_zip_coverage", return_value=(None, "invalid"))
    def test_fake_zip_asks_recheck(self, _mock_resolve) -> None:
        # The geocoder confirmed the ZIP isn't real → ask to double-check the digits
        # (distinct from out-of-coverage; never "try another ZIP").
        ctx: dict = {"activity_browse_active": True, "browse_draft": None}
        reply = run_activity_browse_turn(
            user_message="anything this week? zip 99999",
            session_ctx=ctx,
            history=[],
            user_jwt="jwt",
            home_block_id=None,
            slots={"zip": "99999"},
        )
        self.assertIn("double-checking", reply.lower())
        self.assertNotIn("try another", reply.lower())


class TestBrowseOutOfCoverage(unittest.TestCase):
    """A real ZIP Lana doesn't serve yet: honest copy, demand captured, ZIP remembered,
    and a launch-text opt-in that routes guests into the existing verify gate."""

    @patch("app.discovery_route.log_feature_request")
    @patch("app.discovery_route.resolve_zip_coverage", return_value=(None, "uncovered"))
    def test_uncovered_zip_offers_launch_text(self, _mock_resolve, mock_log) -> None:
        ctx: dict = {"activity_browse_active": True, "browse_draft": None}
        reply = run_activity_browse_turn(
            user_message="hi! just checking this out. I'm in NYC, zip 10025. anything at all this week?",
            session_ctx=ctx,
            history=[],
            user_jwt="jwt",
            home_block_id=None,
            slots={"zip": "10025"},
            user_id="u-guest",
        )
        # Honest out-of-coverage copy, never a rejection or a re-ask.
        self.assertIn("10025", reply)
        self.assertNotIn("try another", reply.lower())
        self.assertNotIn("what's your zip", reply.lower())
        # Demand captured + ZIP remembered + opt-in offered.
        mock_log.assert_called_once()
        self.assertEqual(mock_log.call_args.kwargs.get("category"), "expansion_zip")
        self.assertEqual(mock_log.call_args.kwargs.get("user_id"), "u-guest")
        self.assertEqual(ctx.get("pending_zip"), "10025")
        draft = ctx.get("browse_draft") or {}
        self.assertEqual(draft.get("_expansion_offer"), "10025")
        self.assertIn("Yes, text me at launch", draft.get("suggestions") or [])

    def test_expansion_accept_gates_guest_into_verify(self) -> None:
        ctx: dict = {
            "activity_browse_active": True,
            "browse_draft": {"_expansion_offer": "10025", "_asked": True},
        }
        reply = run_activity_browse_turn(
            user_message="Yes, text me at launch",
            session_ctx=ctx,
            history=[],
            user_jwt="jwt",
            home_block_id=None,
            user_id="u-guest",
        )
        # The same verify gate guests already use — verifying attaches contact info to
        # the logged demand (anonymous auth user keeps its id on signup).
        self.assertTrue(ctx.get("requires_phone_verification"))
        self.assertEqual(ctx.get("routing_phase"), "await_signup_phone")
        self.assertFalse(ctx.get("activity_browse_active"))
        self.assertIn("email", reply.lower())

    def test_expansion_accept_verified_user_is_done(self) -> None:
        ctx: dict = {
            "activity_browse_active": True,
            "phone_verified": True,
            "browse_draft": {"_expansion_offer": "10025", "_asked": True},
        }
        reply = run_activity_browse_turn(
            user_message="yes please",
            session_ctx=ctx,
            history=[],
            user_jwt="jwt",
            home_block_id=None,
            user_id="u-verified",
        )
        # Already reachable — confirm and release, no verify gate.
        self.assertFalse(ctx.get("requires_phone_verification"))
        self.assertFalse(ctx.get("activity_browse_active"))
        self.assertIn("10025", reply)

    def test_expansion_decline_closes_warmly(self) -> None:
        ctx: dict = {
            "activity_browse_active": True,
            "browse_draft": {"_expansion_offer": "10025", "_asked": True},
        }
        reply = run_activity_browse_turn(
            user_message="No thanks",
            session_ctx=ctx,
            history=[],
            user_jwt="jwt",
            home_block_id=None,
        )
        self.assertFalse(ctx.get("activity_browse_active"))
        self.assertNotIn("zip", reply.lower())

    @patch("app.activity_browse._fetch_block_events", return_value=[])
    @patch(
        "app.discovery_route.resolve_zip_coverage",
        return_value=({"block_id": "b1", "display_name": "Lake Nona"}, "covered"),
    )
    def test_fresh_zip_in_offer_reply_reresolves(self, mock_resolve, _fetch) -> None:
        # "oh — my sister's block is 32827" while the launch offer is up: the new ZIP is
        # consumed as a ZIP answer, not as an accept/decline.
        ctx: dict = {
            "activity_browse_active": True,
            "browse_draft": {"_expansion_offer": "10025", "_asked": True, "interest": "fifa"},
        }
        run_activity_browse_turn(
            user_message="32827",
            session_ctx=ctx,
            history=[],
            user_jwt="jwt",
            home_block_id=None,
        )
        mock_resolve.assert_called_once_with("jwt", "32827")
        self.assertEqual(ctx.get("preview_block_id"), "b1")


class TestBrowseChips(unittest.TestCase):
    def test_no_chips_on_zip_ask(self) -> None:
        # Asking for a ZIP → no interest-category pills (the answer is a ZIP).
        from app.ui_actions import activity_browse_actions

        ctx = {
            "activity_browse_active": True,
            "browse_draft": {"interest": "fifa", "_need_zip": True, "suggestions": []},
        }
        self.assertEqual(activity_browse_actions(ctx), [])

    def test_p1_ask_sends_interest_chips(self) -> None:
        from app.ui_actions import activity_browse_actions

        ctx = {
            "activity_browse_active": True,
            "browse_draft": {"_asked": True, "suggestions": ["Sports", "Family & kids"]},
        }
        self.assertEqual(
            [a["label"] for a in activity_browse_actions(ctx)],
            ["Sports", "Family & kids"],
        )

    @patch(
        "app.activity_browse._fetch_block_events",
        return_value=[
            {
                "title": "FIFA watch party",
                "starts_at": "2026-06-27T18:00:00",
                "venue_name": "The Pub",
                "cohort_tags": ["sports", "kids_led_activity"],
            }
        ],
    )
    def test_results_chips_come_from_event_tags(self, _fetch) -> None:
        from app.ui_actions import activity_browse_actions

        ctx: dict = {
            "activity_browse_active": True,
            "browse_draft": None,
            "phone_verified": True,
        }
        run_activity_browse_turn(
            user_message="fifa",
            session_ctx=ctx,
            history=[],
            user_jwt="jwt",
            home_block_id="b1",
        )
        # Machine-style tags are humanized for the pills.
        self.assertEqual(
            [a["label"] for a in activity_browse_actions(ctx)],
            ["Sports", "Kids led activity"],
        )


class TestClarifierRouting(unittest.TestCase):
    @patch("app.discovery_route.discovery_ai_enabled", return_value=True)
    @patch("app.discovery_route.discovery_slots_for_turn")
    def test_ambiguous_asks_clarifier(self, mock_slots, _ai) -> None:
        mock_slots.return_value = {
            "goal": "activities",
            "confidence": 0.9,
            "clarify": "browse_or_meet",
            "in_discovery": True,
        }
        reply, ctx, _, _ = handle_discovery_turn(
            "i want to do something fun this weekend",
            session_ctx={"routing_phase": "listening"},
            user_jwt="jwt",
            phone_verified=False,
            home_block_id=None,
            is_anonymous=True,
        )
        self.assertTrue(ctx.get("browse_or_meet_pending"))
        self.assertIn("happening", reply.lower())


class TestClarifierAnswerCarriesContext(unittest.TestCase):
    """A chip tap answers browse-vs-seek but carries none of the original constraints —
    the chosen lane must be seeded with the utterance that triggered the clarifier
    ('fun with my 4 year old this week'), not the chip label."""

    _ORIGINAL = "looking for something fun to do with my 4 year old this week"
    _CHIPS = ["Show activities nearby", "Find neighbors to do something with"]

    def _ask_clarifier(self, mock_slots) -> dict:
        mock_slots.return_value = {
            "goal": "activities",
            "confidence": 0.9,
            "clarify": "browse_or_meet",
            "clarify_options": list(self._CHIPS),
            "in_discovery": True,
        }
        session_ctx = {"routing_phase": "listening"}
        handle_discovery_turn(
            self._ORIGINAL,
            session_ctx=session_ctx,
            user_jwt="jwt",
            phone_verified=False,
            home_block_id=None,
            is_anonymous=True,
        )
        return session_ctx

    @patch("app.discovery_route.discovery_ai_enabled", return_value=True)
    @patch("app.discovery_route.discovery_slots_for_turn")
    def test_chip_tap_seeds_browse_with_original_ask(self, mock_slots, _ai) -> None:
        session_ctx = self._ask_clarifier(mock_slots)
        self.assertEqual(session_ctx.get("browse_or_meet_origin"), self._ORIGINAL)
        mock_slots.return_value = {"goal": "activities", "confidence": 0.9, "in_discovery": True}
        with patch(
            "app.discovery_route._start_activity_browse_from_discovery",
            return_value=("ok", {}, {}, []),
        ) as mock_start:
            handle_discovery_turn(
                "Show activities nearby",
                session_ctx=session_ctx,
                user_jwt="jwt",
                phone_verified=False,
                home_block_id=None,
                is_anonymous=True,
            )
        self.assertEqual(mock_start.call_args.kwargs.get("msg"), self._ORIGINAL)

    @patch("app.discovery_route.discovery_ai_enabled", return_value=True)
    @patch("app.discovery_route.discovery_slots_for_turn")
    def test_neighbors_chip_seeds_seek_with_original_ask(self, mock_slots, _ai) -> None:
        session_ctx = self._ask_clarifier(mock_slots)
        mock_slots.return_value = {
            "goal": "peers", "signal_intent": "meet_seek", "confidence": 0.9,
            "in_discovery": True,
        }
        with patch(
            "app.discovery_route._start_look_meet_from_discovery",
            return_value=("ok", {}, {}, []),
        ) as mock_start:
            handle_discovery_turn(
                "Find neighbors to do something with",
                session_ctx=session_ctx,
                user_jwt="jwt",
                phone_verified=False,
                home_block_id=None,
                is_anonymous=True,
            )
        self.assertEqual(mock_start.call_args.kwargs.get("msg"), self._ORIGINAL)

    @patch("app.discovery_route.discovery_ai_enabled", return_value=True)
    @patch("app.discovery_route.discovery_slots_for_turn")
    def test_free_text_answer_wins_over_stash(self, mock_slots, _ai) -> None:
        session_ctx = self._ask_clarifier(mock_slots)
        mock_slots.return_value = {"goal": "activities", "confidence": 0.9, "in_discovery": True}
        with patch(
            "app.discovery_route._start_activity_browse_from_discovery",
            return_value=("ok", {}, {}, []),
        ) as mock_start:
            handle_discovery_turn(
                "actually just show me sports stuff",
                session_ctx=session_ctx,
                user_jwt="jwt",
                phone_verified=False,
                home_block_id=None,
                is_anonymous=True,
            )
        self.assertEqual(
            mock_start.call_args.kwargs.get("msg"), "actually just show me sports stuff"
        )

    def test_neighbors_chip_resolves_to_seek_without_slots(self) -> None:
        self.assertEqual(
            _resolve_browse_or_meet_answer("Find neighbors to do something with", None),
            "seek",
        )


class TestEventWhenParts(unittest.TestCase):
    """#56: date-only events (has_time False) must not show the filter LLM a clock —
    the placeholder midnight would match 'mornings'/'after 6pm' asks nobody scheduled."""

    def test_timed_event_renders_local_clock(self) -> None:
        from app.activity_browse import _event_when_parts

        # 23:00 UTC = 7 PM ET (summer) — the local-time rendering this guards.
        self.assertEqual(
            _event_when_parts("2026-08-12T23:00:00Z", has_time=True),
            "2026-08-12 Wed 7:00 PM",
        )

    def test_dateonly_event_omits_clock(self) -> None:
        from app.activity_browse import _event_when_parts

        # Placeholder midnight is event-local, stored as 04:00 UTC.
        self.assertEqual(
            _event_when_parts("2026-08-12T04:00:00Z", has_time=False),
            "2026-08-12 Wed (no time set)",
        )


class TestActivityPreviewHasTime(unittest.TestCase):
    def test_preview_passes_flag_through(self) -> None:
        from app.discovery_route import activity_previews_from_events

        rows = activity_previews_from_events(
            [
                {"id": "1", "title": "Dated party", "starts_at": "2026-08-12T04:00:00Z",
                 "has_time": False},
                {"id": "2", "title": "Timed party", "starts_at": "2026-08-12T23:00:00Z",
                 "has_time": True},
                {"id": "3", "title": "Legacy row", "starts_at": "2026-08-12T23:00:00Z"},
            ]
        )
        self.assertEqual([p["has_time"] for p in rows], [False, True, True])
        # The compact label never carries a clock, so it stays truthful either way.
        self.assertEqual(rows[0]["starts_label"], "Wed Aug 12")


if __name__ == "__main__":
    unittest.main()
