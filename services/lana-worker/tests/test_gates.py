"""Write-only verification gate policy (app/gates.py).

QA 2026-07-08: the bare "Verify your email first" imperative ended 17% of turns —
on search refinements, read questions, and in sessions with no resolved block.
These tests pin the new policy: only write actions gate, no gate without a resolved
block, benefit-framed copy, and analytics on show/pass.
"""

import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app.gates import (
    BLOCK_SCOPED_ACTIONS,
    GATE_COPY,
    WRITE_ACTIONS,
    gate_reply,
    needs_verification,
)

APP_DIR = Path(__file__).resolve().parent.parent / "app"

_READ_ACTIONS = (
    "find_by_attrs",
    "find_peers",
    "show_my_profile",
    "show_peer_profile",
    "explain_peer_match",
    "block_log",
    "list_intros",
    "browse_activities",
    "tip_question",  # the dentist question from QA
)


class TestGatePolicy(unittest.TestCase):
    def test_read_actions_never_gate_even_unverified(self) -> None:
        ctx = {"phone_verified": False, "preview_block_id": "block-1"}
        for action in _READ_ACTIONS:
            self.assertFalse(needs_verification(action, ctx, verified=False), action)
            self.assertIsNone(gate_reply(action, ctx, verified=False), action)

    def test_write_actions_gate_when_unverified(self) -> None:
        ctx = {"preview_block_id": "block-1"}
        for action in WRITE_ACTIONS:
            self.assertTrue(needs_verification(action, ctx, verified=False), action)
            self.assertEqual(gate_reply(action, ctx, verified=False), GATE_COPY[action])

    def test_write_actions_pass_when_verified(self) -> None:
        ctx = {"preview_block_id": "block-1"}
        for action in WRITE_ACTIONS:
            self.assertFalse(needs_verification(action, ctx, verified=True), action)
            self.assertIsNone(gate_reply(action, ctx, verified=True), action)

    def test_verified_falls_back_to_session_ctx_flag(self) -> None:
        self.assertFalse(
            needs_verification(
                "save_signal", {"phone_verified": True, "preview_block_id": "b1"}
            )
        )
        self.assertTrue(
            needs_verification(
                "save_signal", {"phone_verified": False, "preview_block_id": "b1"}
            )
        )

    def test_block_scoped_write_never_gates_without_resolved_block(self) -> None:
        for action in BLOCK_SCOPED_ACTIONS:
            self.assertFalse(needs_verification(action, {}, verified=False), action)
            self.assertIsNone(gate_reply(action, {}, verified=False), action)
        # home_block_id counts as a resolved block.
        self.assertTrue(
            needs_verification("save_signal", {}, verified=False, home_block_id="b1")
        )
        # Non-block-scoped writes (profile photo) still gate without a block.
        self.assertTrue(needs_verification("profile_photo_save", {}, verified=False))

    def test_copy_is_benefit_framed_not_bare_imperative(self) -> None:
        self.assertEqual(set(GATE_COPY), set(WRITE_ACTIONS))
        for action, copy in GATE_COPY.items():
            self.assertNotIn("Verify your email first", copy, action)
            self.assertIn("email", copy.lower(), action)
            # Benefit leads: copy must not OPEN with the ask.
            self.assertFalse(copy.lower().startswith("verify"), action)


class TestGateAnalytics(unittest.TestCase):
    def test_gate_shown_emitted_with_action(self) -> None:
        with patch("app.gates.track") as mock_track:
            reply = gate_reply(
                "save_signal",
                {"preview_block_id": "b1"},
                verified=False,
                user_id="user-1",
            )
        self.assertEqual(reply, GATE_COPY["save_signal"])
        mock_track.assert_called_once_with(
            "gate_shown",
            user_id="user-1",
            event_properties={"action": "save_signal"},
        )

    def test_gate_passed_emitted_for_verified_write(self) -> None:
        with patch("app.gates.track") as mock_track:
            reply = gate_reply(
                "save_signal",
                {"preview_block_id": "b1"},
                verified=True,
                user_id="user-1",
            )
        self.assertIsNone(reply)
        mock_track.assert_called_once_with(
            "gate_passed",
            user_id="user-1",
            event_properties={"action": "save_signal"},
        )

    def test_no_analytics_for_read_actions(self) -> None:
        with patch("app.gates.track") as mock_track:
            gate_reply("find_by_attrs", {}, verified=False, user_id="user-1")
            gate_reply("find_by_attrs", {}, verified=True, user_id="user-1")
        mock_track.assert_not_called()

    def test_no_gate_passed_when_unresolved_block_suppresses_gate(self) -> None:
        with patch("app.gates.track") as mock_track:
            self.assertIsNone(gate_reply("save_signal", {}, verified=False, user_id="u"))
        mock_track.assert_not_called()


class TestNoLegacyGateCopy(unittest.TestCase):
    """Grep-style: every call site must route through app/gates.py — the bare
    imperative may not reappear anywhere in app/ (gates.py quotes it in its own
    docstring for history)."""

    def test_bare_imperative_absent_from_app(self) -> None:
        offenders = []
        for path in sorted(APP_DIR.rglob("*.py")):
            if path.name == "gates.py":
                continue
            if "Verify your email first" in path.read_text(encoding="utf-8"):
                offenders.append(str(path.relative_to(APP_DIR)))
        self.assertEqual(offenders, [])

    def test_call_site_modules_import_the_helper(self) -> None:
        for fname in (
            "profile_photo.py",
            "intro_proposal.py",
            "discovery_route.py",
            "lana_unified_pipeline.py",
            "main.py",
        ):
            text = (APP_DIR / fname).read_text(encoding="utf-8")
            self.assertIn("from app.gates import", text, fname)


class TestRouteLevelGating(unittest.TestCase):
    def test_save_signal_gates_with_benefit_copy_when_block_resolved(self) -> None:
        from app.discovery_route import _try_save_signal_turn

        result = _try_save_signal_turn(
            msg="looking for a double stroller",
            slots={
                "goal": "save_signal",
                "confidence": 0.9,
                "signal_intent": "swap_seek",
                "signal_detail": "double stroller",
            },
            session_ctx={"preview_block_id": "block-1"},
            user_jwt="jwt",
            phone_verified=False,
            home_block_id=None,
            phase="listening",
        )
        self.assertIsNotNone(result)
        reply, _ctx, routing, peers = result
        self.assertEqual(reply, GATE_COPY["save_signal"])
        self.assertEqual(routing["tool_to_call"], "save_signal_need_verify")
        self.assertEqual(peers, [])

    def test_save_signal_without_block_asks_zip_never_gates(self) -> None:
        from app.discovery_route import _try_save_signal_turn

        result = _try_save_signal_turn(
            msg="looking for a double stroller",
            slots={
                "goal": "save_signal",
                "confidence": 0.9,
                "signal_intent": "swap_seek",
                "signal_detail": "double stroller",
            },
            session_ctx={},
            user_jwt="jwt",
            phone_verified=False,
            home_block_id=None,
            phase="listening",
        )
        self.assertIsNotNone(result)
        reply, _ctx, routing, _peers = result
        self.assertIn("zip", reply.lower())
        self.assertNotIn("verify", reply.lower())
        self.assertNotIn("email", reply.lower())
        self.assertEqual(routing["tool_to_call"], "save_signal_need_zip")

    def test_signal_lane_without_block_asks_zip_never_gates(self) -> None:
        from app.discovery_route import _try_signal_lane_turn

        result = _try_signal_lane_turn(
            msg="I need a stroller",
            slots={
                "linear_intent": "looking.swap",
                "goal": "save_signal",
                "confidence": 0.9,
            },
            session_ctx={},
            user_jwt="jwt",
            phone_verified=False,
            home_block_id=None,
            phase="listening",
        )
        self.assertIsNotNone(result)
        reply, _ctx, routing, _peers = result
        self.assertIn("zip", reply.lower())
        self.assertNotIn("verify", reply.lower())
        self.assertNotIn("email", reply.lower())
        self.assertEqual(routing["tool_to_call"], "save_signal_need_zip")

    def test_list_intros_unverified_shows_empty_inbox_not_gate(self) -> None:
        from app.discovery_route import _try_list_intros_turn

        with patch("app.discovery_route.fetch_my_intros") as mock_fetch:
            mock_fetch.side_effect = HTTPException(
                status_code=403, detail="phone_not_verified"
            )
            result = _try_list_intros_turn(
                msg="show my intros",
                slots={"goal": "list_intros", "confidence": 0.9},
                session_ctx={},
                user_jwt="jwt",
                phone_verified=False,
                phase="listening",
            )
        self.assertIsNotNone(result)
        reply, _ctx, _routing, _peers = result
        self.assertIn("don't have any pending intros", reply.lower())
        self.assertNotIn("verify", reply.lower())

    def test_find_by_attrs_unverified_searches_not_gate(self) -> None:
        from app.discovery_route import _try_layer1_intent_turn

        with patch("app.discovery_route.fetch_peers_by_attr_filter") as mock_fetch, patch(
            "app.discovery_route.summarize_partial_claim_matches"
        ) as mock_partial:
            mock_fetch.return_value = []
            mock_partial.return_value = None
            result = _try_layer1_intent_turn(
                msg="find latina moms with toddlers",
                slots={
                    "linear_intent": "discovery.find_by_attrs",
                    "goal": "peers",
                    "confidence": 0.9,
                },
                session_ctx={"preview_block_id": "block-1"},
                user_jwt="jwt",
                phone_verified=False,
                home_block_id=None,
                phase="preview",
                user_id="user-1",
            )
        self.assertIsNotNone(result)
        reply, _ctx, routing, _peers = result
        self.assertNotIn("verify", reply.lower())
        self.assertEqual(routing["tool_to_call"], "find_peers_by_attr_filter")
        mock_fetch.assert_called_once()
