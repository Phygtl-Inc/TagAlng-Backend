"""A tapped Nudge means one person, and the tile stops asking about pinned places.

Three bugs from the 2026-08-18 report, one file:
  * the Nudge on a community roster posted "introduce me to Rust" and nothing else,
    so the intro flow looked Rust up in the caller's find-peers list, missed, and
    sent no nudge at all — the button carries the peer's id now
  * the miss then recited who else it COULD see, naming neighbors the caller never
    asked about
  * the rapport tile asked "which Fitness CF is yours?" about a gym already pinned
"""

import unittest
from unittest.mock import MagicMock, patch

from app.circles_flow import prune_grounded_gaps
from app.intro_proposal import pick_peer_for_intro


class _Q:
    """Chainable query stub: any attribute/call returns self; execute() too."""

    def __init__(self, data=None):
        self.data = data or []

    def __getattr__(self, _name):
        return self

    def __call__(self, *args, **kwargs):
        return self


class TestPickPeerById(unittest.TestCase):
    PEERS = [
        {"peer_user_id": "u-tommaso", "nickname": "Tommaso"},
        {"peer_user_id": "u-rust", "nickname": "Rust"},
    ]

    def test_id_beats_the_name_in_the_message(self) -> None:
        # The message names Tommaso; the tapped card said Rust. The card wins.
        peer = pick_peer_for_intro(
            self.PEERS, msg="introduce me to Tommaso", peer_user_id="u-rust"
        )
        self.assertEqual(peer["peer_user_id"], "u-rust")

    def test_id_beats_a_stale_pending_offer(self) -> None:
        peer = pick_peer_for_intro(
            self.PEERS,
            msg="introduce me to Rust",
            peer_user_id="u-rust",
            pending={"candidate_user_id": "u-tommaso", "candidate_nickname": "Tommaso"},
        )
        self.assertEqual(peer["peer_user_id"], "u-rust")

    def test_unknown_id_falls_back_to_the_name(self) -> None:
        # Never a dead end: an id we can't see in the list is not a veto.
        peer = pick_peer_for_intro(
            self.PEERS, msg="introduce me to Rust", peer_user_id="u-ghost"
        )
        self.assertEqual(peer["peer_user_id"], "u-rust")


class TestPruneGroundedGaps(unittest.TestCase):
    ROWS = [{"gap_row_id": "g1", "affiliation_ref": "a1"}]

    def _run(self, affiliations):
        sb = MagicMock()
        sb.table.side_effect = lambda _name: _Q(affiliations)
        with patch("app.circles_flow.service_client", return_value=sb), patch(
            "app.circles_flow._close_grounding_gap"
        ) as close:
            kept = prune_grounded_gaps("u1", self.ROWS)
        return kept, close

    def test_keeps_a_genuinely_ungrounded_ask(self) -> None:
        kept, close = self._run(
            [{"id": "a1", "circle_key": "fitness_cf", "circle_type": "fitness", "place_ref": None}]
        )
        self.assertEqual(kept, self.ROWS)
        close.assert_not_called()

    def test_drops_and_closes_an_already_pinned_ask(self) -> None:
        kept, close = self._run(
            [{"id": "a1", "circle_key": "fitness_cf", "circle_type": "fitness", "place_ref": "p1"}]
        )
        self.assertEqual(kept, [])
        close.assert_called_once_with("a1")

    def test_drops_a_duplicate_of_a_pinned_community(self) -> None:
        # The reported case: "fitness_cf" ungrounded beside the pinned
        # "fitness_cf_st_cloud" — same gym, and the tile asked about it anyway.
        kept, close = self._run(
            [
                {"id": "a1", "circle_key": "fitness_cf", "circle_type": "fitness", "place_ref": None},
                {
                    "id": "a2",
                    "circle_key": "fitness_cf_st_cloud",
                    "circle_type": "fitness",
                    "place_ref": "p1",
                },
            ]
        )
        self.assertEqual(kept, [])
        close.assert_called_once_with("a1")

    def test_keeps_a_different_community_of_the_same_type(self) -> None:
        kept, close = self._run(
            [
                {"id": "a1", "circle_key": "yoga_barn", "circle_type": "fitness", "place_ref": None},
                {
                    "id": "a2",
                    "circle_key": "fitness_cf_st_cloud",
                    "circle_type": "fitness",
                    "place_ref": "p1",
                },
            ]
        )
        self.assertEqual(kept, self.ROWS)
        close.assert_not_called()

    def test_vanished_affiliation_closes_its_orphan_gap(self) -> None:
        kept, close = self._run([])
        self.assertEqual(kept, [])
        close.assert_called_once_with("a1")

    def test_read_failure_passes_the_rows_through(self) -> None:
        sb = MagicMock()
        sb.table.side_effect = RuntimeError("boom")
        with patch("app.circles_flow.service_client", return_value=sb):
            self.assertEqual(prune_grounded_gaps("u1", self.ROWS), self.ROWS)


class TestChatQueueSkipsPinnedPlaces(unittest.TestCase):
    """The chat path has its OWN gap queue (policy/goals), not the tile ranker's."""

    def test_grounded_gap_never_reaches_the_policy_menu(self) -> None:
        from app.policy import goals

        rows = [
            {"gap_row_id": "g1", "gap_id": "ground:a1", "question": "Which gym?",
             "unlock_score": 0.85, "affiliation_ref": "a1"},
            {"gap_row_id": "g2", "gap_id": "kids", "question": "How old are they?",
             "unlock_score": 0.5, "affiliation_ref": None},
        ]
        sb = MagicMock()
        sb.table.side_effect = lambda _name: _Q(rows)
        with patch("app.policy.goals.service_client", return_value=sb), patch(
            "app.policy.goals._asked_in_chat_recently", return_value=False
        ), patch(
            "app.circles_flow.prune_grounded_gaps", return_value=[rows[1]]
        ) as prune:
            out = goals._rapport_goals("u1")
        prune.assert_called_once()
        self.assertEqual([g["id"] for g in out], ["gap:g2"])


class TestGroundingChipTruthBar(unittest.TestCase):
    """One bar for both surfaces — the chat path used to ship raw ground_options."""

    AFF = {"id": "a1", "circle_type": "fitness", "circle_key": "fitness_cf"}

    def _options(self, rows):
        from app import circles_flow

        with patch.object(circles_flow, "ground_options", return_value=rows):
            return circles_flow.grounding_chip_options("u1", self.AFF, block_id="b1")

    def test_consolations_are_dropped_and_the_missed_name_returned(self) -> None:
        kept, unmatched = self._options(
            [
                {"name": "Crunch Fitness - Lake Nona", "suggested": True,
                 "unmatched_name": "Fitness CF"},
                {"name": "Lake Nona Performance Club", "suggested": True,
                 "unmatched_name": "Fitness CF"},
            ]
        )
        self.assertEqual(kept, [])
        self.assertEqual(unmatched, "Fitness CF")

    def test_a_lone_suggestion_is_dropped(self) -> None:
        kept, unmatched = self._options([{"name": "Some Gym", "suggested": True}])
        self.assertEqual(kept, [])
        self.assertEqual(unmatched, "")

    def test_two_suggestions_are_a_real_choice(self) -> None:
        rows = [
            {"name": "Gym A", "suggested": True},
            {"name": "Gym B", "suggested": True},
        ]
        self.assertEqual(self._options(rows), (rows, ""))

    def test_matches_always_survive(self) -> None:
        rows = [{"name": "Fitness CF St. Cloud", "suggested": False}]
        self.assertEqual(self._options(rows), (rows, ""))


class TestChatGroundingSaysTheMiss(unittest.TestCase):
    def test_unfindable_place_names_the_miss_and_offers_nothing(self) -> None:
        from app import lana_unified_pipeline as pipe

        action = MagicMock()
        action.kind = "ground_place"
        action.goal_id = "circle:fitness_cf"
        action.utterance = "Nice — which Fitness CF in St. Cloud do you go to?"
        sb = MagicMock()
        sb.table.side_effect = lambda _n: _Q([{"id": "a1", "circle_key": "fitness_cf"}])
        ctx: dict = {}
        with patch("app.auth.service_client", return_value=sb), patch(
            "app.circles_flow._home_block_id", return_value="b1"
        ), patch(
            "app.circles_flow.grounding_chip_options", return_value=([], "Fitness CF")
        ), patch(
            "app.reply_compose.compose_reply", side_effect=lambda **kw: kw["fallback"]
        ):
            pipe._wire_ground_place_action(action, user_id="u1", session_ctx=ctx)

        self.assertIn("Fitness CF", action.utterance)
        self.assertIn("can't find", action.utterance)
        self.assertEqual(action.chips, [])  # no strangers offered, not even an escape
        # Still armed: whatever they type next drives a fresh search.
        self.assertEqual(ctx["rapport_grounding"]["affiliation_id"], "a1")
        self.assertEqual(ctx["rapport_followup_question"], action.utterance)


class TestChatGroundingCard(unittest.TestCase):
    """Chat asks with the tile's card now — pick-one grid, search box, skip."""

    def _wire(self, options, unmatched=""):
        from app import lana_unified_pipeline as pipe

        action = MagicMock()
        action.kind = "ground_place"
        action.goal_id = "circle:fitness_cf"
        action.utterance = "Which spot is Fitness CF for you?"
        action.pending_action = None
        aff = {
            "id": "a1",
            "circle_key": "fitness_cf",
            "circle_type": "fitness",
            "noun": "gym",
            "emoji": "🏋️",
            "place_name": "Fitness CF",
            "detail": "works out there 4x a week",
        }
        sb = MagicMock()
        sb.table.side_effect = lambda _n: _Q([aff])
        ctx: dict = {}
        with patch("app.auth.service_client", return_value=sb), patch(
            "app.circles_flow._home_block_id", return_value="b1"
        ), patch(
            "app.circles_flow.grounding_chip_options",
            return_value=(options, unmatched),
        ), patch(
            "app.reply_compose.compose_reply", side_effect=lambda **kw: kw["fallback"]
        ):
            pipe._wire_ground_place_action(action, user_id="u1", session_ctx=ctx)
        return action, ctx

    def test_card_carries_the_places_and_the_community(self) -> None:
        _action, ctx = self._wire(
            [
                {"name": "Fitness CF St. Cloud", "address": "1 Main St",
                 "google_place_id": "g1", "suggested": False},
            ]
        )
        card = ctx["grounding_card"]
        self.assertEqual(card["affiliation_id"], "a1")
        self.assertEqual(card["relation_noun"], "gym")
        self.assertEqual([o["label"] for o in card["options"]], ["Fitness CF St. Cloud"])
        self.assertIsNone(card["unmatched_name"])

    def test_nothing_found_still_ships_the_card_for_its_search_box(self) -> None:
        # The whole point: no places to offer is exactly when the user needs to search.
        action, ctx = self._wire([], unmatched="Fitness CF")
        self.assertEqual(ctx["grounding_card"]["options"], [])
        self.assertEqual(ctx["grounding_card"]["unmatched_name"], "Fitness CF")
        self.assertIn("Fitness CF", action.utterance)
        self.assertEqual(action.chips, [])

    def test_card_is_turn_scoped(self) -> None:
        from app.turn_surfaces import TURN_SCOPED_SURFACES

        self.assertIn("grounding_card", TURN_SCOPED_SURFACES)


class TestGroundByPickedId(unittest.TestCase):
    def test_a_picked_place_id_grounds_without_matching_text(self) -> None:
        from app import circles_flow

        state = {"affiliation_id": "a1", "candidates": [], "attempts": 1}
        with patch.object(
            circles_flow, "ground_and_confirm", return_value={"grounded": True}
        ) as ground, patch.object(circles_flow, "match_grounding_candidate") as match:
            out = circles_flow.handle_grounding_confirmation(
                "u1", state, "It's Fitness CF St. Cloud", place_id="g-picked"
            )
        self.assertEqual(out, {"grounded": True})
        match.assert_not_called()  # an id is not a guess
        self.assertEqual(ground.call_args.args[2], "g-picked")


class TestRosterHidesNudgeOnceSent(unittest.TestCase):
    """A nudge already out is a status; a second one can only hit the pair cooldown."""

    def _roster(self, tiers):
        from app import community_surface as cs

        members = [{"user_id": "u-me"}, {"user_id": "u-tommaso"}, {"user_id": "u-rust"}]
        users = {
            "u-me": {"nickname": "Asjid"},
            "u-tommaso": {"nickname": "Tommaso db"},
            "u-rust": {"nickname": "Rust"},
        }
        with patch.object(cs, "_resolve_place", return_value="p1"), patch.object(
            cs, "_gather", return_value={"place": {"name": "Fitness CF"}, "members": members,
                                         "shared": {}}
        ), patch.object(cs, "caller_affiliation_at", return_value={"circle_type": "fitness"}), \
            patch.object(cs, "_blocked_ids", return_value=set()), \
            patch.object(cs, "_users_by_id", return_value=users), \
            patch("app.peer_discovery_surface.peer_tiers", return_value=tiers):
            out = cs.community_members("u-me", place_id="p1")
        return {r["peer_user_id"]: r for r in out["members"]}

    def test_a_pending_nudge_replaces_the_button(self) -> None:
        rows = self._roster({"u-tommaso": "nudge", "u-rust": "stranger"})
        self.assertEqual(rows["u-tommaso"]["connection"], "intro_sent")
        self.assertEqual(rows["u-tommaso"]["actions"], [])
        # Everyone else still gets one.
        self.assertIsNone(rows["u-rust"].get("connection"))
        self.assertEqual(rows["u-rust"]["actions"][0]["id"], "peer_card_nudge")

    def test_an_existing_connection_shows_as_connected(self) -> None:
        rows = self._roster({"u-rust": "irl_peer"})
        self.assertEqual(rows["u-rust"]["connection"], "connected")
        self.assertEqual(rows["u-rust"]["actions"], [])

    def test_the_caller_never_gets_a_nudge_on_her_own_row(self) -> None:
        rows = self._roster({})
        self.assertTrue(rows["u-me"]["me"])
        self.assertEqual(rows["u-me"]["actions"], [])


class TestRosterWarmsMissingPortraits(unittest.TestCase):
    """A member who never chats again would otherwise never get a public line — and no
    viewer can warm it, since peers read get_peer_profile straight from Supabase."""

    def test_only_the_ones_without_a_line_are_queued(self) -> None:
        from app import community_surface as cs

        rows = [
            {"id": "u-1", "public_portrait": "They run at dawn."},
            {"id": "u-2", "public_portrait": None},
            {"id": "u-3", "public_portrait": "   "},
        ]
        with patch("app.profile_portrait.schedule_portrait_refresh") as sched:
            cs._warm_missing_portraits(rows)
        self.assertEqual([c.args[0] for c in sched.call_args_list], ["u-2", "u-3"])

    def test_a_page_of_strangers_is_capped(self) -> None:
        from app import community_surface as cs

        rows = [{"id": f"u-{i}", "public_portrait": None} for i in range(20)]
        with patch("app.profile_portrait.schedule_portrait_refresh") as sched:
            cs._warm_missing_portraits(rows)
        self.assertEqual(sched.call_count, cs._MAX_PORTRAIT_WARMS)

    def test_an_unmigrated_column_costs_the_roster_nothing(self) -> None:
        from app import community_surface as cs

        calls: list[str] = []

        def _table(_name):
            q = MagicMock()

            def _select(fields):
                calls.append(fields)
                if "public_portrait" in fields:
                    raise RuntimeError("column users.public_portrait does not exist")
                return _Q([{"id": "u-1", "nickname": "Rust"}])

            q.select.side_effect = _select
            return q

        sb = MagicMock()
        sb.table.side_effect = _table
        with patch.object(cs, "service_client", return_value=sb):
            out = cs._users_by_id(["u-1"])
        self.assertEqual(out["u-1"]["nickname"], "Rust")  # roster still renders
        self.assertEqual(len(calls), 2)


class TestNudgeReasonNamesTheRealTie(unittest.TestCase):
    """Roster members cross blocks by design — "you're both nearby" was a guess."""

    def test_shared_community_beats_every_inference(self) -> None:
        from app.intro_proposal import build_match_reason

        reason = build_match_reason(
            identity_snippet="I work out 4x a week",
            peer={"nickname": "Tommaso", "shared_place_name": "Fitness CF - St. Cloud",
                  "matching_peer_label": "Fitness CF - St. Cloud"},
        )
        self.assertEqual(reason, "You both go to Fitness CF - St. Cloud.")
        self.assertNotIn("nearby", reason.lower())
        self.assertNotIn("close by", reason.lower())

    def test_without_a_shared_place_nothing_changes(self) -> None:
        from app.intro_proposal import build_match_reason

        # No shared_place_name, no matching_my_label, no has_exact_concept_match —
        # the pair is not genuinely two-sided, so the reason must not assert a
        # shared trait. Under the two-outcomes rule this falls to the neutral
        # line, which is what "names the real tie or nothing" looks like when
        # there is no real tie to name.
        reason = build_match_reason(
            identity_snippet=None, peer={"matching_peer_label": "runs at dawn"}
        )
        self.assertNotIn("you both fit", reason.lower())
        self.assertNotIn("runs at dawn", reason.lower())
        self.assertIn("click", reason.lower())

    def test_the_target_row_carries_the_place_it_shares(self) -> None:
        from app import discovery_route as dr

        sb = MagicMock()
        sb.table.side_effect = lambda _n: _Q([{"id": "u-tommaso", "nickname": "Tommaso db"}])
        with patch.object(dr, "service_client", return_value=sb), patch(
            "app.community_surface.shared_community_name",
            return_value="Fitness CF - St. Cloud",
        ):
            rows = dr._peers_with_target_first("u-tommaso", [], user_id="u-me")
        self.assertEqual(rows[0]["shared_place_name"], "Fitness CF - St. Cloud")
        self.assertEqual(rows[0]["matching_peer_label"], "Fitness CF - St. Cloud")

    def test_no_shared_place_keeps_the_old_row(self) -> None:
        from app import discovery_route as dr

        sb = MagicMock()
        sb.table.side_effect = lambda _n: _Q([{"id": "u-x", "nickname": "X"}])
        with patch.object(dr, "service_client", return_value=sb), patch(
            "app.community_surface.shared_community_name", return_value=None
        ):
            rows = dr._peers_with_target_first("u-x", [], user_id="u-me")
        self.assertNotIn("shared_place_name", rows[0])


class TestNudgeBeatsAPendingOffer(unittest.TestCase):
    def test_a_nudge_turn_clears_a_hanging_rapport_offer(self) -> None:
        """Grounding leaves a bridge offer armed; it read "introduce me to Rust" as YES
        and dispatched a peer search instead of the intro (2026-08-18)."""
        from app.lana_unified_pipeline import _reset_rapport_state

        ctx = {
            "rapport_active": True,
            "rapport_offer_pending": True,
            "rapport_pending_action": {"kind": "find_neighbors", "send": "connect me…"},
            "nudge_peer_user_id": "u-rust",
        }
        _reset_rapport_state(ctx)
        self.assertIsNone(ctx["rapport_offer_pending"])
        self.assertIsNone(ctx["rapport_pending_action"])
        self.assertIsNone(ctx["rapport_active"])
        # Cleared with None, never popped — a popped key comes back on the ctx merge.
        self.assertIn("rapport_offer_pending", ctx)

    def test_the_guard_runs_before_the_rapport_state_is_read(self) -> None:
        import inspect

        from app import lana_unified_pipeline as pipe

        src = inspect.getsource(pipe.run_lana_unified_pipeline)
        guard = src.index('if session_ctx.get("nudge_peer_user_id")')
        read = src.index('rapport = session_ctx.get("rapport_answer")')
        self.assertLess(guard, read)


class TestProfileEventGlyph(unittest.TestCase):
    def test_popular_events_carry_the_meets_own_emoji(self) -> None:
        """The FE has rendered cover_emoji all along; nothing ever sent it, so one meet
        wore a calendar on the community and its own glyph everywhere else."""
        from app import community_surface as cs

        with patch.object(
            cs,
            "_events_at_place",
            return_value=[
                {"id": "e1", "title": "Fitness CF Neighbor Meetup", "starts_at": "2026-08-22T09:00:00",
                 "has_time": True, "venue_name": "Fitness CF", "cover_emoji": "🏋️"},
                {"id": "e2", "title": "No glyph on this one", "starts_at": "2026-08-23T07:30:00",
                 "has_time": True, "venue_name": "Fitness CF", "cover_emoji": None},
            ],
        ), patch.object(cs, "_going_counts", return_value={}):
            rows, _week = cs._event_rows_for_profile("p1")
        self.assertEqual([r["cover_emoji"] for r in rows], ["🏋️", None])


class TestProfileReadsMembershipOnce(unittest.TestCase):
    def test_resolve_place_hands_back_the_row_it_just_read(self) -> None:
        from app import community_surface as cs

        mine = {"id": "a1", "status": "confirmed", "circle_type": "fitness"}
        with patch.object(cs, "caller_affiliation_at", return_value=mine) as check:
            out: dict = {}
            pid = cs._resolve_place(
                "u1", affiliation_id=None, place_id="p1", out_affiliation=out
            )
        self.assertEqual(pid, "p1")
        self.assertEqual(out, mine)
        check.assert_called_once()  # the caller no longer repeats this round trip


class TestBlurbIsWrittenOnce(unittest.TestCase):
    """The description is a stored property of the place, not a per-open model call."""

    FACTS = dict(
        place_id="p1", place_name="Fitness CF", relation="gym", area="34769",
        features=["Open 24h"], members=3,
    )

    def test_stored_line_is_served_with_no_model_call(self) -> None:
        from app import community_surface as cs

        key = cs._blurb_fingerprint(
            place_name="Fitness CF", relation="gym", area="34769",
            features=["Open 24h"], members=3,
        )
        with patch.object(cs, "_BLURB_POOL") as pool:
            out = cs._blurb(**self.FACTS, stored="A 24-hour gym in St. Cloud.", stored_key=key)
        self.assertEqual(out, "A 24-hour gym in St. Cloud.")
        pool.submit.assert_not_called()

    def test_missing_line_ships_the_template_and_writes_behind(self) -> None:
        from app import community_surface as cs

        cs._BLURB_INFLIGHT.clear()
        with patch.object(cs, "_BLURB_POOL") as pool:
            out = cs._blurb(**self.FACTS, stored=None, stored_key=None)
        self.assertEqual(out, "A gym in 34769 — open 24h.")
        pool.submit.assert_called_once()
        cs._BLURB_INFLIGHT.clear()

    def test_a_moved_fact_rewrites_the_line(self) -> None:
        from app import community_surface as cs

        cs._BLURB_INFLIGHT.clear()
        with patch.object(cs, "_BLURB_POOL") as pool:
            out = cs._blurb(**self.FACTS, stored="Written when it had 2 people.",
                            stored_key="stale-fingerprint")
        # The stale line can name a feature that is gone; the template is always true.
        self.assertEqual(out, "A gym in 34769 — open 24h.")
        pool.submit.assert_called_once()
        cs._BLURB_INFLIGHT.clear()

    def test_two_concurrent_opens_buy_one_sentence(self) -> None:
        from app import community_surface as cs

        cs._BLURB_INFLIGHT.clear()
        with patch.object(cs, "_BLURB_POOL") as pool:
            cs._blurb(**self.FACTS, stored=None, stored_key=None)
            cs._blurb(**self.FACTS, stored=None, stored_key=None)
        pool.submit.assert_called_once()
        cs._BLURB_INFLIGHT.clear()

    def test_authoring_persists_the_line_and_its_fingerprint(self) -> None:
        from app import community_surface as cs

        sb = MagicMock()
        cs._BLURB_INFLIGHT.add("k1")
        with patch.object(cs, "service_client", return_value=sb), patch.object(
            cs, "_compose_blurb", return_value="A 24-hour gym in St. Cloud."
        ):
            cs._author_blurb("p1", "k1", {"place_name": "Fitness CF", "relation": "gym",
                                          "area": "34769", "features": ("Open 24h",), "members": 3})
        sb.table.assert_called_with("places")
        sb.table.return_value.update.assert_called_once_with(
            {"blurb": "A 24-hour gym in St. Cloud.", "blurb_key": "k1"}
        )
        self.assertNotIn("k1", cs._BLURB_INFLIGHT)

    def test_an_unwritable_line_stores_nothing(self) -> None:
        # Storing the template would stamp a fingerprint and stop us retrying later.
        from app import community_surface as cs

        sb = MagicMock()
        cs._BLURB_INFLIGHT.add("k2")
        with patch.object(cs, "service_client", return_value=sb), patch.object(
            cs, "_compose_blurb", return_value=None
        ):
            cs._author_blurb("p1", "k2", {"place_name": "X", "relation": "gym",
                                          "area": None, "features": (), "members": 1})
        sb.table.assert_not_called()
        self.assertNotIn("k2", cs._BLURB_INFLIGHT)

    def test_place_read_steps_down_when_the_columns_are_missing(self) -> None:
        from app import community_surface as cs

        calls: list[str] = []

        def _table(_name):
            q = MagicMock()

            def _select(fields):
                calls.append(fields)
                if "blurb" in fields:
                    raise RuntimeError('column places.blurb does not exist')
                return _Q([{"id": "p1", "name": "Fitness CF"}])

            q.select.side_effect = _select
            return q

        sb = MagicMock()
        sb.table.side_effect = _table
        with patch.object(cs, "service_client", return_value=sb):
            row = cs._place_row("p1")
        self.assertEqual(row["name"], "Fitness CF")  # profile still opens
        self.assertEqual(len(calls), 2)


class TestIntroMissNamesNobody(unittest.TestCase):
    def test_not_found_reply_lists_no_other_neighbors(self) -> None:
        """The miss branch used to print "I see: Tommaso, Zenaidy."."""
        import inspect

        from app import discovery_route

        src = inspect.getsource(discovery_route._try_neighbor_intro_turn)
        self.assertNotIn("I see:", src)
        self.assertIn("Name NO other", src)


if __name__ == "__main__":
    unittest.main()
