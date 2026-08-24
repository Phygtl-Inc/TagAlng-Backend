"""The featured match may be someone already introduced — say so, don't re-offer.

QA 2026-08-18: the top match (Tommaso, +3 same-place on a shared gym) came back with
"Would you like me to introduce you two?" and a "Send Tommaso db a nudge" chip, over a
card badged "✓ Sent". The DB agreed with the badge — nudge pending, tier 'nudge' — so the
chip could only fail the 7-day pair cooldown. The match is kept; the offer changes.
"""

import unittest
from unittest.mock import patch

from app.ui_actions import derive_ui_actions, intro_offer_actions
from app.ui_intent import derive_ui_intent


def _peer(**over) -> dict:
    row = {
        "peer_user_id": "p-tom",
        "nickname": "Tommaso db",
        "matching_peer_label": "your gym",
        "shared_labels": ["your gym", "Interested in sports"],
    }
    row.update(over)
    return row


class TestIntroStateReachesTheOffer(unittest.TestCase):
    def test_state_survives_the_onion_reshape(self) -> None:
        # _shape builds a fresh dict; connection used to be dropped there, so the reply
        # writer never saw what the card would later render.
        from app.onion import _shape

        self.assertEqual(
            _shape({"peer_user_id": "p1", "connection": "intro_sent"})["connection"],
            "intro_sent",
        )

    def test_state_is_armed_on_the_offer(self) -> None:
        from app.intro_proposal import stamp_intro_offer_ctx

        ctx: dict = {}
        stamp_intro_offer_ctx(ctx, peer=_peer(connection="intro_sent"), match_reason="r")
        self.assertEqual(ctx["pending_intro_offer"]["intro_state"], "intro_sent")

        ctx2: dict = {}
        stamp_intro_offer_ctx(ctx2, peer=_peer(), match_reason="r")
        self.assertIsNone(ctx2["pending_intro_offer"]["intro_state"])


class TestChips(unittest.TestCase):
    def test_sent_state_offers_someone_else_not_a_nudge(self) -> None:
        actions = intro_offer_actions(
            nickname="Tommaso db", peer_user_id="p-tom", intro_state="intro_sent"
        )
        self.assertEqual([a["id"] for a in actions], ["intro_look_elsewhere", "intro_pass"])
        for a in actions:
            self.assertNotIn("nudge", a["label"].lower())
            self.assertNotIn("nudge", a["message"].lower())

    def test_open_state_keeps_the_nudge_chip(self) -> None:
        actions = intro_offer_actions(nickname="Tommaso db", peer_user_id="p-tom")
        self.assertEqual([a["id"] for a in actions], ["intro_propose", "intro_pass"])

    def test_the_send_text_is_a_request_an_engine_can_serve(self) -> None:
        # It must NOT read as an accept of this offer, or try_propose_intro_from_preview
        # swallows the tap and we are back to a doomed RPC.
        from app.intro_proposal import accepts_intro_offer, wants_neighbor_intro

        send = intro_offer_actions(intro_state="intro_sent")[0]["message"]
        self.assertFalse(accepts_intro_offer(send))
        self.assertFalse(wants_neighbor_intro(send))

    def test_chips_render_from_the_armed_offer(self) -> None:
        from app.intro_proposal import stamp_intro_offer_ctx

        ctx: dict = {}
        stamp_intro_offer_ctx(ctx, peer=_peer(connection="intro_sent"), match_reason="r")
        labels = [a["label"] for a in derive_ui_actions(ctx, derive_ui_intent(ctx))]
        self.assertEqual(labels, ["Find someone else", "Not yet"])


class TestCopy(unittest.TestCase):
    def _fallback(self, fn, *args):
        import app.intro_proposal as ip

        with patch.object(ip, "compose_reply", side_effect=lambda **kw: kw.get("fallback", "")):
            return fn(*args)

    def test_sent_match_is_still_shown_but_not_re_offered(self) -> None:
        from app.intro_proposal import format_intro_offer_turn

        out = self._fallback(
            format_intro_offer_turn, _peer(connection="intro_sent"), "You both: your gym"
        )
        self.assertIn("Tommaso db", out)          # the match is kept
        self.assertIn("already out", out)          # the state is owned
        self.assertNotIn("introduce you", out)     # the dead offer is gone
        self.assertIn("someone else", out)         # a live next step remains

    def test_connected_reads_as_connected_not_as_a_pending_intro(self) -> None:
        from app.intro_proposal import format_intro_offer_turn

        out = self._fallback(format_intro_offer_turn, _peer(connection="connected"), "r")
        self.assertIn("already connected", out)
        self.assertNotIn("waiting on them", out)

    def test_open_match_still_offers_the_intro(self) -> None:
        from app.intro_proposal import format_intro_offer_turn

        out = self._fallback(format_intro_offer_turn, _peer(), "You both: your gym")
        self.assertIn("introduce you two", out)

    def test_typed_yes_sends_nothing_and_leaves_the_lane(self) -> None:
        # The offer asked "want me to look at someone else?" — a yes must not send an
        # intro to whoever ranks next. Declining hands the turn to normal routing, which
        # runs the search the question promised.
        import app.intro_proposal as ip

        ctx = {"pending_intro_offer": {"candidate_nickname": "Tommaso db",
                                       "intro_state": "intro_sent",
                                       "candidate_user_id": "p-tom"}}
        with patch.object(ip, "propose_neighbor_intro") as rpc:
            out = ip.try_propose_intro_from_preview(
                msg="yes", session_ctx=ctx, user_jwt="jwt",
                peers=[_peer(connection="intro_sent"), _peer(peer_user_id="p2", nickname="Pouya")],
                identity_snippet=None,
            )
        rpc.assert_not_called()
        self.assertIsNone(out)
        self.assertIsNone(ctx.get("pending_intro_offer"))   # spent, not re-readable

    def test_the_chip_tap_declines_the_intro_lane(self) -> None:
        # "find someone else nearby" got classified as propose_intro, which forced this
        # lane; the armed offer then handed back the same peer and the turn answered
        # "you've already sent Tommaso an intro" without finding anyone (QA 2026-08-18).
        from app.intro_proposal import pick_peer_for_intro

        pending = {"candidate_user_id": "p-tom", "candidate_nickname": "Tommaso db",
                   "intro_state": "intro_sent"}
        peers = [_peer(connection="intro_sent"), _peer(peer_user_id="p2", nickname="Pouya")]
        self.assertIsNone(
            pick_peer_for_intro(peers, msg="find someone else nearby", pending=pending)
        )

    def test_an_intro_ask_picks_someone_who_can_receive_one(self) -> None:
        from app.intro_proposal import pick_peer_for_intro

        pending = {"candidate_user_id": "p-tom", "candidate_nickname": "Tommaso db",
                   "intro_state": "intro_sent"}
        peers = [_peer(connection="intro_sent"), _peer(peer_user_id="p2", nickname="Pouya")]
        self.assertEqual(
            pick_peer_for_intro(peers, msg="introduce me", pending=pending)["nickname"], "Pouya"
        )
        # Nobody reachable: keep the old pick so the caller's cooldown branch says so.
        self.assertEqual(
            pick_peer_for_intro([peers[0]], msg="introduce me", pending=pending)["nickname"],
            "Tommaso db",
        )
        # An offer with no state is untouched — the normal path must not change.
        self.assertEqual(
            pick_peer_for_intro(
                peers, msg="yes",
                pending={"candidate_user_id": "p-tom", "candidate_nickname": "Tommaso db"},
            )["nickname"],
            "Tommaso db",
        )

    def test_a_named_peer_still_wins_over_the_skip(self) -> None:
        # "introduce me to Tommaso" is explicit: it goes through and gets the truthful
        # cooldown answer, rather than being silently redirected to someone else.
        from app.intro_proposal import pick_peer_for_intro

        peers = [_peer(connection="intro_sent"), _peer(peer_user_id="p2", nickname="Pouya")]
        self.assertEqual(
            pick_peer_for_intro(peers, msg="introduce me to Tommaso")["peer_user_id"], "p-tom"
        )

    def test_a_named_request_still_goes_through_the_rpc(self) -> None:
        # "introduce me to Sofia" may resolve to a different peer than the armed offer,
        # so it must not be short-circuited by that offer's state.
        import app.intro_proposal as ip

        pending = {"candidate_nickname": "Tommaso db", "intro_state": "intro_sent",
                   "candidate_user_id": "p-tom"}
        with patch.object(ip, "compose_reply", side_effect=lambda **kw: kw.get("fallback", "")), \
             patch.object(ip, "propose_neighbor_intro", return_value={"intro_id": "i1"}) as rpc:
            ip.try_propose_intro_from_preview(
                msg="introduce me to Sofia", session_ctx={"pending_intro_offer": pending},
                user_jwt="jwt", peers=[_peer(peer_user_id="p-sofia", nickname="Sofia")],
                identity_snippet=None, peer_name="Sofia",
            )
        rpc.assert_called_once()


class TestListTurnAndOfferAgree(unittest.TestCase):
    """QA 2026-08-18: "Three neighbors near you share some things in common" shipped over
    ONE card. The previous turn's offer was still armed, so derive_ui_intent returned
    offer_neighbor_intro and the FE rendered the single-card intro surface instead of the
    list the prose had just counted."""

    def _peers(self):
        return [
            _peer(peer_user_id="p-dan", nickname="Daniel", connection="intro_sent"),
            _peer(peer_user_id="p-pouya", nickname="Pouya"),
            _peer(peer_user_id="p-sofia", nickname="Sofia"),
        ]

    def _stale(self):
        return {"pending_intro_offer": {"candidate_user_id": "p-tom",
                                        "candidate_nickname": "Tommaso db",
                                        "intro_state": "intro_sent"}}

    def _attach(self, ctx, peers, *, matches=True):
        import app.discovery_route as dr
        import app.intro_proposal as ip

        with patch.object(ip, "compose_reply", side_effect=lambda **kw: kw.get("fallback", "")), \
             patch.object(dr, "peer_matches_identity_snippet", return_value=matches), \
             patch.object(dr, "build_match_reason", return_value="You both: Likes sports"):
            return dr._maybe_attach_intro_offer(
                reply="Three neighbors near you share some things in common.",
                peers=peers, ctx=ctx, identity_snippet="sports",
                msg="find someone else nearby",
            )

    def test_the_featured_peer_can_actually_receive_an_intro(self) -> None:
        ctx = self._stale()
        reply = self._attach(ctx, self._peers())
        self.assertEqual(ctx["pending_intro_offer"]["candidate_nickname"], "Pouya")
        self.assertIsNone(ctx["pending_intro_offer"]["intro_state"])
        self.assertEqual([p["nickname"] for p in ctx["peer_matches"]], ["Pouya"])
        self.assertIn("Pouya", reply)
        self.assertNotIn("Three neighbors", reply)   # one card, one-match copy
        labels = [a["label"] for a in derive_ui_actions(ctx, derive_ui_intent(ctx))]
        self.assertEqual(labels, ["Send Pouya a nudge", "Not yet"])

    def test_a_declined_offer_leaves_the_list_a_list(self) -> None:
        ctx = self._stale()
        ctx["peer_matches"] = self._peers()
        reply = self._attach(ctx, self._peers(), matches=False)
        self.assertIn("Three neighbors", reply)
        self.assertIsNone(ctx["pending_intro_offer"])          # stale offer gone
        self.assertEqual(len(ctx["peer_matches"]), 3)          # count matches the cards
        self.assertNotEqual(derive_ui_intent(ctx), "offer_neighbor_intro")

    def test_an_all_introduced_list_owns_up_instead_of_offering(self) -> None:
        peers = [_peer(peer_user_id="p-dan", nickname="Daniel", connection="intro_sent")]
        ctx: dict = {}
        reply = self._attach(ctx, peers)
        self.assertEqual(ctx["pending_intro_offer"]["intro_state"], "intro_sent")
        self.assertNotIn("introduce you two", reply)
        self.assertIn("already out", reply)


class TestStaleOfferAtTheRenderChokePoint(unittest.TestCase):
    """The offer surface must not outlive its own turn — enforced once, for every path.

    QA 2026-08-18, second instance: "There are 3 people near you…" over ONE Sofia card
    with her nudge chips. Three rows were stamped; the previous turn's offer was still
    armed, so derive_ui_intent returned offer_neighbor_intro and the FE drew the
    single-card intro surface. That turn came through the orchestrator's find_peers tool,
    which never touches the offer — hence the fix sits where every path converges,
    stamp_peer_discovery_ctx, immediately before derive_ui_intent (main._onboarding_fields).
    """

    OFFER = {"candidate_user_id": "p-sofia", "candidate_nickname": "Sofia"}

    def _rows(self, *ids):
        return [{"peer_user_id": i, "nickname": i} for i in ids]

    def _run(self, rows, offer):
        from app.peer_discovery_surface import drop_stale_intro_offer

        ctx = {
            "peer_matches": rows, "pending_intro_offer": offer, "intro_offer_shown": True,
            "routing_phase": "preview", "active_intent": "discovery.find_peers",
        }
        drop_stale_intro_offer(ctx, rows)
        intent = derive_ui_intent(ctx, peer_count=len(rows), phone_verified=True)
        return ctx, intent

    def test_a_list_turn_drops_the_stale_offer_and_shows_the_list(self) -> None:
        rows = self._rows("p-dan", "p-sofia", "p-x")
        ctx, intent = self._run(rows, dict(self.OFFER))
        self.assertIsNone(ctx["pending_intro_offer"])
        self.assertIsNone(ctx["intro_offer_shown"])
        self.assertEqual(intent, "show_peer_preview")       # cards match the count
        self.assertEqual(derive_ui_actions(ctx, intent), [])

    def test_the_offers_own_turn_survives(self) -> None:
        ctx, intent = self._run(self._rows("p-sofia"), dict(self.OFFER))
        self.assertIsNotNone(ctx["pending_intro_offer"])
        self.assertEqual(intent, "offer_neighbor_intro")
        self.assertEqual(
            [a["label"] for a in derive_ui_actions(ctx, intent)],
            ["Send Sofia a nudge", "Not yet"],
        )

    def test_a_single_row_that_is_someone_else_drops_it(self) -> None:
        ctx, _ = self._run(self._rows("p-dan"), dict(self.OFFER))
        self.assertIsNone(ctx["pending_intro_offer"])

    def test_the_accept_window_is_untouched_when_no_rows_ship(self) -> None:
        # No peer_matches this turn: the "yes" still has an offer to land on. The caller
        # returns before the drop, so this pins the caller, not just the helper.
        from app.peer_discovery_surface import stamp_peer_discovery_ctx

        ctx = {"pending_intro_offer": dict(self.OFFER), "intro_offer_shown": True}
        stamp_peer_discovery_ctx(ctx, phone_verified=True, user_id="u1")
        self.assertIsNotNone(ctx["pending_intro_offer"])

    def test_cards_stand_on_the_intent_not_on_the_offer(self) -> None:
        """The regression the drop exposed, 2026-08-18.

        A propose-intro turn held four real peer rows, the stale offer was correctly
        dropped, and the reply then shipped with NO card and NO chips: social.propose_intro
        was missing from PEER_DISCOVERY_ACTIVE_INTENTS, so derive_ui_intent fell through to
        `chat` and main._onboarding_fields filtered peer_matches to []. The offer had been
        the only thing making an intro turn render as a peer surface.
        """
        from app.ui_intent import PEER_DISCOVERY_ACTIVE_INTENTS, PEER_SURFACE_UI_INTENTS

        rows = self._rows("p-tom", "p-dan", "p-sofia", "p-x")
        ctx, intent = self._run(rows, dict(self.OFFER))
        ctx["active_intent"] = "social.propose_intro"
        intent = derive_ui_intent(ctx, peer_count=len(rows), phone_verified=True)
        self.assertEqual(intent, "show_peer_preview")
        # main._onboarding_fields' own test for whether cards ship.
        self.assertTrue(
            intent in PEER_SURFACE_UI_INTENTS
            or (bool(rows) and ctx["active_intent"] in PEER_DISCOVERY_ACTIVE_INTENTS)
        )

    def test_a_recommendation_strip_also_moves_the_surface(self) -> None:
        # tip_rec rows return early from stamp_peer_discovery_ctx — the drop runs first.
        from app.peer_discovery_surface import stamp_peer_discovery_ctx

        rows = [{"peer_user_id": "p-dan", "nickname": "Daniel", "tip_rec": "great dentist"}]
        ctx = {"peer_matches": rows, "pending_intro_offer": dict(self.OFFER)}
        stamp_peer_discovery_ctx(ctx, phone_verified=True, user_id="u1")
        self.assertIsNone(ctx["pending_intro_offer"])


if __name__ == "__main__":
    unittest.main()
