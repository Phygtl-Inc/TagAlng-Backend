"""A recommendation ask is ANSWERED, and only becomes a posting if the user says yes.

The bug this covers (QA 2026-08-04, screenshots): "recommend me a doctor" wrote a
tip_seek signal before answering anything, replied with the plumbing ("I've noted you're
looking for a recommendation…"), rendered the amber ASKING FOR A TIP receipt, and then
re-posted on every lane re-entry — a "maybe later" tap and a "Remove my posting" tap each
produced ANOTHER live posting. Removal was offered in prose with nothing behind it.
"""

from __future__ import annotations

import unittest
from unittest import mock
from unittest.mock import patch

from app.discovery_route import (
    PHASE_NEED_ZIP,
    _read_offer_reply,
    handle_discovery_turn,
)
from app.ui_actions import derive_ui_actions


def _tip_slots(detail: str = "good doctor", *, confidence: float = 0.9) -> dict:
    return {
        "goal": "save_signal",
        "in_discovery": True,
        "confidence": confidence,
        "linear_intent": "looking.tip",
        "signal_intent": "tip_seek",
        "signal_detail": detail,
        "signal_category": "health",
    }


def _turn(msg: str, session_ctx: dict | None = None):
    return handle_discovery_turn(
        msg,
        session_ctx=session_ctx if session_ctx is not None else {"routing_phase": "listening"},
        user_jwt="jwt",
        phone_verified=True,
        home_block_id="block-a",
        is_anonymous=False,
        history=[],
        user_id="user-1",
    )


def _places(*names: str) -> list[dict]:
    return [{"name": n, "address": "1 Main St", "place_id": f"p-{n}", "attrs": {}} for n in names]


class TestTipSeekAnswersWithoutPosting(unittest.TestCase):
    """The answer turn: reads only, claims only what it did, offers the posting."""

    def setUp(self) -> None:
        self.save = mock.patch("app.discovery_route.save_local_signal").start()
        self.tips = mock.patch(
            "app.discovery_route.find_neighbor_tips", return_value=[]
        ).start()
        self.search = mock.patch(
            "app.discovery_route._search_tip_places", return_value=_places("Lake Nona Family Care")
        ).start()
        mock.patch("app.discovery_route.discovery_ai_enabled", return_value=True).start()
        self.slots = mock.patch(
            "app.discovery_route.discovery_slots_for_turn", return_value=_tip_slots()
        ).start()
        self.addCleanup(mock.patch.stopall)

    def test_nothing_is_written_and_places_are_returned(self) -> None:
        reply, ctx, routing, _peers = _turn("recommend me a doctor")

        self.save.assert_not_called()
        self.assertIsNone(ctx.get("signal_saved"))
        self.assertEqual(
            [p["name"] for p in ctx["google_place_suggestions"]], ["Lake Nona Family Care"]
        )
        assert reply is not None
        # The posting claim is the specific lie to guard against.
        for claim in ("posted your ask", "I've noted", "your ask is posted"):
            self.assertNotIn(claim.lower(), reply.lower())
        self.assertEqual(routing.get("tool_to_call"), "tip_seek_answered")

    def test_offer_is_armed_with_its_own_chips(self) -> None:
        _reply, ctx, _routing, _peers = _turn("recommend me a doctor")

        self.assertEqual(ctx["tip_ask_offer"]["detail"], "good doctor")
        # The pending twin is what the next turn reads; both must be set.
        self.assertEqual(ctx["tip_ask_offer_pending"]["detail"], "good doctor")
        labels = [a["label"] for a in derive_ui_actions(ctx, "chat")]
        self.assertEqual(labels, ["Yes, ask my neighbors", "No, just the list"])

    def test_personalizer_refine_chip_rides_along_with_the_offer(self) -> None:
        """Arming the offer must not hide the angles the personalizer found."""
        ctx = {
            "tip_ask_offer": {"detail": "restaurants", "category": "food"},
            "rec_chips": [
                {"label": "Vegetarian", "message": "vegetarian restaurant"},
                {"label": "See all food", "message": "show me all food"},
            ],
        }
        labels = [a["label"] for a in derive_ui_actions(ctx, "chat")]
        self.assertEqual(labels, ["Yes, ask my neighbors", "Vegetarian", "No, just the list"])

    def test_neighbor_tip_beats_google_and_still_writes_nothing(self) -> None:
        self.tips.return_value = [
            {
                "detail_text": "Dr. Patel on Narcoossee — great with toddlers",
                "category": "health",
                "match_strength": 0.82,
                "neighbor_label": "Marisol",
            }
        ]
        reply, ctx, routing, _peers = _turn("recommend me a doctor")

        self.save.assert_not_called()
        self.search.assert_not_called()
        assert reply is not None
        self.assertIn("Dr. Patel", reply)
        self.assertEqual(routing.get("tool_to_call"), "tip_seek_neighbor_tip")
        self.assertTrue(ctx.get("tip_ask_offer_pending"))

    def test_no_results_says_so_without_inventing_places(self) -> None:
        self.search.return_value = []
        reply, ctx, _routing, _peers = _turn("recommend me a doctor")

        self.save.assert_not_called()
        assert reply is not None
        self.assertIn("couldn't find", reply.lower())
        self.assertTrue(ctx.get("tip_ask_offer_pending"))

    def test_missing_block_asks_for_zip_and_keeps_the_ask(self) -> None:
        reply, ctx, _routing, _peers = handle_discovery_turn(
            "recommend me a doctor",
            session_ctx={"routing_phase": "listening"},
            user_jwt="jwt",
            phone_verified=True,
            home_block_id=None,
            is_anonymous=False,
            history=[],
            user_id="user-1",
        )
        self.save.assert_not_called()
        assert reply is not None
        self.assertIn("ZIP", reply)
        self.assertEqual(ctx.get("routing_phase"), PHASE_NEED_ZIP)
        self.assertEqual(ctx["tip_seek_pending"]["detail"], "good doctor")

    def test_guest_verify_gate_stashes_the_ask_not_a_posting(self) -> None:
        _reply, ctx, routing, _peers = handle_discovery_turn(
            "recommend me a doctor",
            session_ctx={"routing_phase": "listening"},
            user_jwt="jwt",
            phone_verified=False,
            home_block_id=None,
            is_anonymous=True,
            history=[],
            user_id=None,
        )
        self.save.assert_not_called()
        self.assertEqual(routing.get("tool_to_call"), "tip_seek_need_verify")
        self.assertEqual(ctx["tip_seek_pending"]["detail"], "good doctor")
        # signal_pending is the POST-on-resume stash — a question must not use it.
        self.assertIsNone(ctx.get("signal_pending"))

    def test_post_verify_resume_answers_instead_of_posting(self) -> None:
        _reply, ctx, routing, _peers = _turn(
            "ok",
            {
                "routing_phase": "listening",
                "tip_seek_pending": {"detail": "good doctor", "category": "health"},
            },
        )
        self.save.assert_not_called()
        self.assertEqual(routing.get("tool_to_call"), "tip_seek_answered")
        self.assertIsNone(ctx.get("tip_seek_pending"))


class TestOfferReply(unittest.TestCase):
    """accept writes once; decline writes nothing; a refinement falls through."""

    def setUp(self) -> None:
        self.save = mock.patch(
            "app.discovery_route.save_local_signal",
            return_value={
                "signal_id": "sig-1",
                "intent": "tip_seek",
                "category": "health",
                "detail_text": "good doctor",
                "block_id": "block-a",
                "matches_created": 0,
                "reused": False,
            },
        ).start()
        mock.patch("app.discovery_route.fetch_my_block_log", return_value=[]).start()
        mock.patch("app.discovery_route.find_neighbor_tips", return_value=[]).start()
        mock.patch(
            "app.discovery_route._search_tip_places", return_value=_places("Care Clinic")
        ).start()
        mock.patch("app.discovery_route.discovery_ai_enabled", return_value=True).start()
        mock.patch(
            "app.discovery_route.discovery_slots_for_turn", return_value=_tip_slots()
        ).start()
        self.addCleanup(mock.patch.stopall)

    def _armed(self) -> dict:
        return {
            "routing_phase": "listening",
            "tip_ask_offer_pending": {"detail": "good doctor", "category": "health"},
        }

    def test_accept_posts_once_and_offers_a_real_removal(self) -> None:
        reply, ctx, routing, _peers = _turn("Yes, ask my neighbors", self._armed())

        self.save.assert_called_once()
        self.assertEqual(self.save.call_args.kwargs["intent"], "tip_seek")
        self.assertEqual(ctx["signal_saved"]["signal_id"], "sig-1")
        self.assertEqual(routing.get("tool_to_call"), "tip_ask_posted")
        # Offer consumed, removal armed and pointed at the row it would close.
        self.assertIsNone(ctx.get("tip_ask_offer_pending"))
        self.assertEqual(ctx["posting_manage_pending"]["signal_id"], "sig-1")
        self.assertEqual(ctx["last_saved_signal"]["signal_id"], "sig-1")
        labels = [a["label"] for a in derive_ui_actions(ctx, "signal_saved")]
        self.assertEqual(labels, ["Show my neighborhood log", "Take it down"])
        assert reply is not None
        self.assertNotIn("noted", reply.lower())

    def test_decline_writes_nothing_and_closes_warmly(self) -> None:
        reply, ctx, routing, _peers = _turn("No, just the list", self._armed())

        self.save.assert_not_called()
        self.assertEqual(routing.get("tool_to_call"), "tip_ask_ask_neighbors_declined")
        self.assertIsNone(ctx.get("tip_ask_offer_pending"))
        self.assertIsNone(ctx.get("signal_saved"))
        assert reply is not None
        self.assertIn("nothing posted", reply.lower())

    def test_declining_via_ai_read_does_not_repost(self) -> None:
        """The screenshot case: "maybe later" used to re-classify as the same tip_seek and
        write a second posting. The offer reader owns that turn instead."""
        with patch("app.tip_ask_ai.interpret_offer_reply", return_value="decline"):
            _reply, ctx, routing, _peers = _turn("maybe later", self._armed())

        self.save.assert_not_called()
        self.assertEqual(routing.get("tool_to_call"), "tip_ask_ask_neighbors_declined")
        self.assertIsNone(ctx.get("tip_ask_offer_pending"))

    def test_accept_beats_a_tip_share_misread(self) -> None:
        """Dev QA 2026-08-04: tapping "Yes, ask my neighbors" was classified tip_share (the
        word "recommendation" was all over the context), the SHARE capture grabbed the turn
        and asked "Where, roughly?", and the user ended up recorded as SHARING a doctor
        recommendation — the inverse of their ask. The armed offer must be read first."""
        with patch(
            "app.discovery_route.discovery_slots_for_turn",
            return_value={
                "goal": "save_signal",
                "confidence": 0.9,
                "linear_intent": "sharing.tip",
                "signal_intent": "tip_share",
                "signal_detail": "doctor recommendation",
            },
        ):
            _reply, ctx, routing, _peers = _turn("Yes, ask my neighbors", self._armed())

        self.save.assert_called_once()
        self.assertEqual(self.save.call_args.kwargs["intent"], "tip_seek")  # NOT tip_share
        self.assertEqual(self.save.call_args.kwargs["detail_text"], "good doctor")
        self.assertEqual(routing.get("tool_to_call"), "tip_ask_posted")
        self.assertIsNone(ctx.get("signal_draft"))

    def test_refinement_falls_through_to_the_recommendation_lane(self) -> None:
        """"kid-friendly ones" is neither accept nor decline — it must reach the lane and
        still not write anything."""
        with patch("app.tip_ask_ai.interpret_offer_reply", return_value="other"):
            _reply, ctx, routing, _peers = _turn("show me all doctors", self._armed())

        self.save.assert_not_called()
        self.assertEqual(routing.get("tool_to_call"), "tip_seek_answered")
        # The old offer was consumed; the fresh answer arms its own (so the posting is still
        # one tap away) — what must never happen is the refinement being read as an accept.
        self.assertEqual(ctx["tip_ask_offer_pending"]["detail"], "good doctor")

    def test_chip_payloads_are_the_no_llm_floor(self) -> None:
        with patch("app.tip_ask_ai.interpret_offer_reply", return_value=None):
            self.assertEqual(
                _read_offer_reply(offer="ask_neighbors", detail="x", msg="Yes, ask my neighbors"),
                "accept",
            )
            self.assertEqual(
                _read_offer_reply(offer="ask_neighbors", detail="x", msg="No, just the list"),
                "decline",
            )
            self.assertEqual(
                _read_offer_reply(offer="manage_posting", detail="x", msg="Take my posting down"),
                "remove",
            )
            # Anything else falls through rather than guessing.
            self.assertEqual(
                _read_offer_reply(offer="ask_neighbors", detail="x", msg="what about pizza"),
                "other",
            )


class TestRemovePosting(unittest.TestCase):
    """The removal Lana always offered now happens (close_local_signal)."""

    def setUp(self) -> None:
        self.close = mock.patch(
            "app.discovery_route.close_local_signal",
            return_value={
                "closed": True,
                "signal_id": "sig-1",
                "intent": "tip_seek",
                "detail_text": "good doctor",
                "matches_removed": 2,
            },
        ).start()
        self.save = mock.patch("app.discovery_route.save_local_signal").start()
        mock.patch("app.discovery_route.find_neighbor_tips", return_value=[]).start()
        mock.patch("app.discovery_route._search_tip_places", return_value=[]).start()
        mock.patch("app.discovery_route.discovery_ai_enabled", return_value=True).start()
        self.slots = mock.patch(
            "app.discovery_route.discovery_slots_for_turn",
            return_value={
                "goal": "chat",
                "confidence": 0.9,
                "linear_intent": "settings.remove_posting",
            },
        ).start()
        self.addCleanup(mock.patch.stopall)

    def test_chip_tap_closes_the_posting(self) -> None:
        reply, ctx, routing, _peers = _turn(
            "Take my posting down",
            {
                "routing_phase": "listening",
                "posting_manage_pending": {"signal_id": "sig-1", "detail": "good doctor"},
            },
        )
        self.close.assert_called_once_with("jwt", signal_id="sig-1")
        self.save.assert_not_called()
        self.assertEqual(routing.get("tool_to_call"), "posting_closed")
        self.assertIsNone(ctx.get("posting_manage_pending"))
        self.assertIsNone(ctx.get("last_saved_signal"))
        assert reply is not None
        self.assertIn("taken that down", reply.lower())

    def test_typed_removal_uses_the_last_posting_when_no_offer_is_armed(self) -> None:
        _reply, _ctx, routing, _peers = _turn(
            "actually remove my posting",
            {
                "routing_phase": "listening",
                "last_saved_signal": {"signal_id": "sig-9", "detail": "good doctor"},
            },
        )
        self.close.assert_called_once_with("jwt", signal_id="sig-9")
        self.assertEqual(routing.get("tool_to_call"), "posting_closed")

    def test_nothing_posted_says_so_instead_of_claiming_a_removal(self) -> None:
        self.close.return_value = {"closed": False, "reason": "not_found"}
        reply, _ctx, routing, _peers = _turn("remove my posting")

        self.assertEqual(routing.get("tool_to_call"), "posting_close_none")
        assert reply is not None
        self.assertIn("nothing", reply.lower())

    def test_failed_removal_never_claims_success(self) -> None:
        self.close.return_value = {"closed": False, "reason": "unavailable"}
        reply, _ctx, routing, _peers = _turn("remove my posting")

        self.assertEqual(routing.get("tool_to_call"), "posting_close_unavailable")
        assert reply is not None
        self.assertIn("still up", reply.lower())


class TestPolicyGate(unittest.TestCase):
    """decide_turn runs BEFORE the engines, so a recommendation ask must be kept off it.

    Dev QA 2026-08-04: "recommend me a doctor nearby" was answered by the policy with
    "I'll keep an ear out and let you know if a neighbor recommends a doctor nearby" — a
    listening promise nothing had armed — and the turn log showed handler=None, i.e. the
    answer turn never ran. The prompt is told to hand these off; this gate does not rely
    on it complying.
    """

    def _is_tip_ask(self, slots):
        from app.lana_unified_pipeline import _turn_is_engine_action

        with patch("app.discovery_slots.discovery_slots_for_turn", return_value=slots):
            return _turn_is_engine_action(
                {"routing_phase": "listening"},
                "recommend me a doctor nearby",
                history=[],
                home_block_id="block-a",
                phone_verified=True,
            )

    def test_recommendation_ask_is_kept_off_the_policy(self) -> None:
        self.assertTrue(self._is_tip_ask(_tip_slots()))
        # linear_intent alone is enough — signal_intent may be absent.
        self.assertTrue(
            self._is_tip_ask({"linear_intent": "looking.tip", "confidence": 0.8, "goal": "save_signal"})
        )

    def test_other_intents_still_belong_to_the_policy(self) -> None:
        self.assertFalse(self._is_tip_ask({"goal": "chat", "confidence": 0.9}))
        self.assertFalse(
            self._is_tip_ask({"linear_intent": "discovery.find_peers", "goal": "peers", "confidence": 0.9})
        )
        # A tip_share (naming a provider they vouch for) is a different lane.
        self.assertFalse(
            self._is_tip_ask({"linear_intent": "sharing.tip", "signal_intent": "tip_share", "confidence": 0.9})
        )
        # Too unsure to divert.
        self.assertFalse(self._is_tip_ask(_tip_slots(confidence=0.3)))

    def test_classifier_failure_leaves_the_gate_unchanged(self) -> None:
        from app.lana_unified_pipeline import _turn_is_engine_action

        with patch(
            "app.discovery_slots.discovery_slots_for_turn", side_effect=RuntimeError("boom")
        ):
            self.assertFalse(
                _turn_is_engine_action(
                    {}, "recommend me a doctor", history=[], home_block_id=None, phone_verified=True
                )
            )


class TestClassifierSeesTheOffer(unittest.TestCase):
    """The router's state line must SAY an offer is armed.

    Root cause of the tip_share inversion: an armed offer reported active_capture=none, so
    the router judged "Yes, ask my neighbors" from the transcript alone — whose recent
    bubbles were full of "recommendation" and "share your tip".
    """

    def _capture(self, ctx: dict) -> str:
        from app.discovery_slots import _active_capture_context

        return _active_capture_context(ctx)

    def test_no_offer_is_inert(self) -> None:
        self.assertEqual(self._capture({}), "none")
        self.assertEqual(self._capture({"tip_ask_offer_pending": None}), "none")

    def test_armed_ask_offer_names_what_was_offered(self) -> None:
        line = self._capture({"tip_ask_offer_pending": {"detail": "good doctor"}})
        self.assertTrue(line.startswith("offer_reply"))
        self.assertIn("good doctor", line)
        self.assertIn("ask their neighbors", line)
        self.assertIn("NEVER tip_share", line)
        self.assertIn("Nothing has been posted yet", line)

    def test_armed_removal_offer_describes_the_posting(self) -> None:
        line = self._capture({"posting_manage_pending": {"detail": "good doctor"}})
        self.assertTrue(line.startswith("offer_reply"))
        self.assertIn("take that posting down", line)

    def test_offer_outranks_other_captures(self) -> None:
        # Both armed: the offer wins, because its accept writes a posting.
        line = self._capture(
            {"tip_ask_offer_pending": {"detail": "good doctor"}, "look_meet_active": True}
        )
        self.assertTrue(line.startswith("offer_reply"))

    def test_state_line_reaches_the_payload(self) -> None:
        from app.discovery_slots import _discovery_slot_payload

        payload = _discovery_slot_payload(
            "Yes, ask my neighbors",
            routing_phase="listening",
            history=[{"role": "assistant", "content": "…ask your neighbors too?"}],
            has_block=True,
            has_identity=True,
            phone_verified=True,
            session_ctx={"tip_ask_offer_pending": {"detail": "good doctor"}},
        )
        self.assertIn("active_capture: offer_reply", payload)
        self.assertIn("NEVER tip_share", payload)


class TestLegacyPathStillAvailable(unittest.TestCase):
    """LANA_TIP_ASK_CONSENT=0 restores the write-first behavior (rollback switch)."""

    def test_flag_off_saves_on_the_ask(self) -> None:
        save = mock.patch(
            "app.discovery_route.save_local_signal",
            return_value={
                "signal_id": "sig-legacy",
                "intent": "tip_seek",
                "detail_text": "good doctor",
                "matches_created": 0,
            },
        ).start()
        mock.patch("app.discovery_route.fetch_my_block_log", return_value=[]).start()
        mock.patch("app.discovery_route._search_tip_places", return_value=[]).start()
        mock.patch("app.discovery_route.discovery_ai_enabled", return_value=True).start()
        mock.patch(
            "app.discovery_route.discovery_slots_for_turn", return_value=_tip_slots()
        ).start()
        mock.patch.dict("os.environ", {"LANA_TIP_ASK_CONSENT": "0"}).start()
        self.addCleanup(mock.patch.stopall)

        _reply, ctx, _routing, _peers = _turn("recommend me a doctor")
        save.assert_called_once()
        self.assertEqual(ctx["signal_saved"]["signal_id"], "sig-legacy")


if __name__ == "__main__":
    unittest.main()
