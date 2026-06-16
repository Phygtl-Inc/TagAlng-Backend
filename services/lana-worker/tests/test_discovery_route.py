import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app.discovery_route import (
    PHASE_NEED_DISPLAY_NAME,
    PHASE_NEED_IDENTITY,
    PHASE_NEED_ZIP,
    PHASE_PREVIEW,
    _try_neighbor_intro_turn,
    extract_zip,
    fetch_blocks_for_zip,
    format_preview_message,
    handle_discovery_turn,
    invalid_zip_hint,
    wants_discovery_turn,
    wants_more_peer_detail,
    wants_verify_help,
)
from app.lana_dispatch import lana_unified_opening


class TestDiscoveryHelpers(unittest.TestCase):
    def test_extract_zip(self) -> None:
        self.assertEqual(extract_zip("I'm in 32827"), "32827")
        self.assertIsNone(extract_zip("3116"))
        self.assertIsNone(extract_zip("0000123"))

    def test_invalid_zip_hint(self) -> None:
        self.assertIn("4 digits", invalid_zip_hint("3116") or "")
        self.assertIn("5-digit", invalid_zip_hint("0000123") or "")
        self.assertIsNone(invalid_zip_hint("hello"))

    def test_wants_more(self) -> None:
        self.assertTrue(wants_more_peer_detail("show me their names"))
        self.assertFalse(wants_more_peer_detail("ok whats my name"))

    def test_wants_verify_help(self) -> None:
        self.assertTrue(wants_verify_help("How can I verify"))
        self.assertTrue(wants_verify_help("verify my phone please"))


class TestDiscoveryRouting(unittest.TestCase):
    @patch("app.discovery_route.discovery_ai_enabled", return_value=True)
    @patch("app.discovery_route.discovery_slots_for_turn")
    def test_meet_new_people_asks_zip_when_slots_say_peers(
        self, mock_slots, _mock_ai
    ) -> None:
        mock_slots.return_value = {
            "goal": "peers",
            "in_discovery": True,
            "confidence": 0.92,
            "identity_snippet": None,
        }
        result = handle_discovery_turn(
            "Hey I wanna meet new people",
            session_ctx={"routing_phase": "listening"},
            user_jwt="jwt",
            phone_verified=False,
            home_block_id=None,
            is_anonymous=True,
            history=[],
        )
        self.assertIsNotNone(result)
        reply, ctx, _, _ = result
        self.assertIn("ZIP", reply)
        self.assertEqual(ctx["routing_phase"], PHASE_NEED_ZIP)

    @patch("app.discovery_route.discovery_ai_enabled", return_value=True)
    @patch("app.discovery_route.discovery_slots_for_turn")
    @patch("app.discovery_route.fetch_preview_peers_on_block")
    @patch("app.discovery_route.fetch_blocks_for_zip")
    def test_late_find_uses_chat_history_for_identity(
        self, mock_blocks, mock_preview, mock_slots, _mock_ai
    ) -> None:
        mock_slots.side_effect = [
            {
                "goal": "peers",
                "in_discovery": True,
                "confidence": 0.95,
                "identity_snippet": "italian person new to block; toddlers; parents",
            },
            {
                "goal": "continue",
                "in_discovery": True,
                "confidence": 0.9,
                "identity_snippet": "italian person new to block; toddlers; parents",
            },
        ]
        mock_blocks.return_value = [{"block_id": "block-1", "label": "Whisper Park"}]
        mock_preview.return_value = [
            {"matching_peer_label": "Italian parent", "preview": True},
        ]
        history = [
            {"role": "assistant", "content": "Tell me about your family"},
            {"role": "user", "content": "I am an italian person new to this block"},
            {"role": "assistant", "content": "Little ones?"},
            {"role": "user", "content": "toddlers"},
        ]
        result = handle_discovery_turn(
            "can you find me people? stop asking questions",
            session_ctx={"routing_phase": "listening"},
            user_jwt="jwt",
            phone_verified=False,
            home_block_id=None,
            is_anonymous=True,
            history=history,
        )
        self.assertIsNotNone(result)
        reply, ctx, _, peers = result
        self.assertIn("ZIP", reply)
        self.assertEqual(ctx["routing_phase"], PHASE_NEED_ZIP)
        zip_result = handle_discovery_turn(
            "32827",
            session_ctx=ctx,
            user_jwt="jwt",
            phone_verified=False,
            home_block_id=None,
            is_anonymous=True,
            history=history,
        )
        self.assertIsNotNone(zip_result)
        _, zip_ctx, _, zip_peers = zip_result
        self.assertEqual(zip_ctx["routing_phase"], PHASE_PREVIEW)
        self.assertTrue(zip_peers)
        self.assertIn("italian", (zip_ctx.get("identity_snippet") or "").lower())

    def test_find_peers_asks_zip(self) -> None:
        result = handle_discovery_turn(
            "find people like me",
            session_ctx={},
            user_jwt="jwt",
            phone_verified=False,
            home_block_id=None,
            is_anonymous=True,
        )
        self.assertIsNotNone(result)
        reply, ctx, _, peers = result
        self.assertIn("ZIP", reply)
        self.assertEqual(ctx["routing_phase"], PHASE_NEED_ZIP)
        self.assertEqual(peers, [])

    @patch("app.discovery_route.fetch_blocks_for_zip")
    def test_short_zip_gives_hint(self, _mock_blocks) -> None:
        result = handle_discovery_turn(
            "3116",
            session_ctx={"routing_phase": PHASE_NEED_ZIP, "active_intent": "discovery.find_peers"},
            user_jwt="jwt",
            phone_verified=False,
            home_block_id=None,
            is_anonymous=True,
        )
        self.assertIsNotNone(result)
        reply, _, _, _ = result
        self.assertIn("5-digit", reply)

    @patch("app.discovery_route.call_rpc")
    def test_unknown_zip_returns_friendly_reply(self, mock_rpc) -> None:
        mock_rpc.side_effect = HTTPException(
            status_code=502,
            detail='rpc_failed:{"message":"zip_not_found"}',
        )
        self.assertEqual(fetch_blocks_for_zip("jwt", "32872"), [])
        result = handle_discovery_turn(
            "32872",
            session_ctx={"routing_phase": PHASE_NEED_ZIP, "active_intent": "discovery.find_peers"},
            user_jwt="jwt",
            phone_verified=False,
            home_block_id=None,
            is_anonymous=True,
        )
        self.assertIsNotNone(result)
        reply, ctx, _, _ = result
        self.assertIn("couldn't find blocks", reply.lower())
        self.assertEqual(ctx["routing_phase"], PHASE_NEED_ZIP)

    @patch("app.discovery_route.fetch_blocks_for_zip")
    def test_zip_then_identity(self, mock_blocks) -> None:
        mock_blocks.return_value = [{"block_id": "block-1", "label": "Whisper Park"}]
        result = handle_discovery_turn(
            "32827",
            session_ctx={"active_intent": "discovery.find_peers", "routing_phase": PHASE_NEED_ZIP},
            user_jwt="jwt",
            phone_verified=False,
            home_block_id=None,
            is_anonymous=True,
        )
        self.assertIsNotNone(result)
        reply, ctx, _, _ = result
        self.assertEqual(ctx["routing_phase"], PHASE_NEED_IDENTITY)
        self.assertIn("one thing", reply.lower())

    @patch("app.discovery_route.fetch_preview_peers_on_block")
    @patch("app.discovery_route.user_needs_display_name", return_value=True)
    @patch("app.discovery_route.fetch_blocks_for_zip")
    def test_identity_goes_to_preview_without_display_name_gate(
        self, mock_blocks, _mock_needs_name, mock_preview
    ) -> None:
        mock_blocks.return_value = [{"block_id": "block-1", "label": "Whisper Park"}]
        mock_preview.return_value = [
            {"matching_peer_label": "Weekend activities", "preview": True},
        ]
        result = handle_discovery_turn(
            "I'm a Latino mom looking for friends",
            session_ctx={
                "active_intent": "discovery.find_peers",
                "routing_phase": PHASE_NEED_IDENTITY,
                "preview_block_id": "block-1",
            },
            user_jwt="jwt",
            phone_verified=True,
            home_block_id=None,
            is_anonymous=False,
            user_id="user-1",
        )
        self.assertIsNotNone(result)
        reply, ctx, _, peers = result
        self.assertEqual(ctx["routing_phase"], PHASE_PREVIEW)
        self.assertIn("neighbor", reply.lower())
        self.assertEqual(len(peers), 1)

    @patch("app.discovery_route.fetch_preview_peers_on_block")
    @patch("app.discovery_route.fetch_blocks_for_zip")
    def test_zip_reuses_identity_from_history(self, mock_blocks, mock_preview) -> None:
        mock_blocks.return_value = [{"block_id": "block-1", "label": "Whisper Park"}]
        mock_preview.return_value = [
            {"matching_peer_label": "British heritage", "preview": True},
        ]
        history = [
            {"role": "user", "content": "I am a British dad who recently moved to this block"},
            {"role": "assistant", "content": "What ZIP code is your block?"},
        ]
        result = handle_discovery_turn(
            "32827",
            session_ctx={"active_intent": "discovery.find_peers", "routing_phase": PHASE_NEED_ZIP},
            user_jwt="jwt",
            phone_verified=False,
            home_block_id=None,
            is_anonymous=True,
            history=history,
        )
        self.assertIsNotNone(result)
        reply, ctx, _, peers = result
        self.assertEqual(ctx["routing_phase"], PHASE_PREVIEW)
        self.assertIn("British", ctx.get("identity_snippet") or "")
        self.assertEqual(len(peers), 1)

    @patch("app.discovery_route.fetch_preview_peers_on_block")
    def test_need_identity_short_answer_goes_preview(self, mock_preview) -> None:
        mock_preview.return_value = [
            {"matching_peer_label": "Dad on block", "preview": True},
        ]
        result = handle_discovery_turn(
            "british",
            session_ctx={
                "active_intent": "discovery.find_peers",
                "routing_phase": PHASE_NEED_IDENTITY,
                "preview_block_id": "block-1",
            },
            user_jwt="jwt",
            phone_verified=False,
            home_block_id=None,
            is_anonymous=True,
        )
        self.assertIsNotNone(result)
        reply, ctx, _, peers = result
        self.assertEqual(ctx["routing_phase"], PHASE_PREVIEW)
        self.assertEqual(ctx.get("identity_snippet"), "british")
        self.assertIn("neighbor", reply.lower())
        self.assertEqual(len(peers), 1)

    @patch("app.discovery_route.fetch_preview_peers_on_block")
    @patch("app.discovery_route.fetch_blocks_for_zip")
    def test_preview_redacted(self, mock_blocks, mock_preview) -> None:
        mock_blocks.return_value = [{"block_id": "block-1", "label": "Whisper Park"}]
        mock_preview.return_value = [
            {"matching_peer_label": "Weekend activities", "preview": True},
        ]
        result = handle_discovery_turn(
            "I'm a Latino mom looking for friends",
            session_ctx={
                "active_intent": "discovery.find_peers",
                "routing_phase": PHASE_NEED_IDENTITY,
                "preview_block_id": "block-1",
            },
            user_jwt="jwt",
            phone_verified=False,
            home_block_id=None,
            is_anonymous=True,
        )
        self.assertIsNotNone(result)
        _, ctx, _, peers = result
        self.assertEqual(ctx["routing_phase"], PHASE_PREVIEW)
        self.assertTrue(peers[0].get("preview"))

    def test_more_details_gates_verify(self) -> None:
        result = handle_discovery_turn(
            "show me names",
            session_ctx={
                "active_intent": "discovery.find_peers",
                "routing_phase": PHASE_PREVIEW,
                "preview_block_id": "block-1",
                "identity_snippet": "latino mom",
            },
            user_jwt="jwt",
            phone_verified=False,
            home_block_id=None,
            is_anonymous=True,
        )
        self.assertIsNotNone(result)
        reply, ctx, _, _ = result
        self.assertEqual(ctx["routing_phase"], "await_signup_phone")
        self.assertIn("verify", reply.lower())

    def test_verify_question_gates_phone(self) -> None:
        result = handle_discovery_turn(
            "How can I verify",
            session_ctx={
                "active_intent": "discovery.find_peers",
                "routing_phase": PHASE_PREVIEW,
                "preview_block_id": "block-1",
                "identity_snippet": "brazilian",
            },
            user_jwt="jwt",
            phone_verified=False,
            home_block_id=None,
            is_anonymous=True,
        )
        self.assertIsNotNone(result)
        reply, ctx, _, _ = result
        self.assertEqual(ctx["routing_phase"], "await_signup_phone")
        self.assertIn("number", reply.lower())

    @patch("app.discovery_route.discovery_ai_enabled", return_value=True)
    @patch("app.discovery_slots.discovery_ai_enabled", return_value=True)
    @patch("app.discovery_slots.ai_parse_discovery_turn")
    def test_ai_verify_goal_gates_phone_without_regex(self, mock_slots, _mock_ai, _mock_ai2) -> None:
        """Signup intent is AI-routed (goal=verify), not hardcoded phrase matching."""
        mock_slots.return_value = {
            "in_discovery": True,
            "goal": "verify",
            "zip": None,
            "identity_snippet": None,
            "confidence": 0.92,
        }
        result = handle_discovery_turn(
            "ok sign me up",
            session_ctx={
                "active_intent": "discovery.find_peers",
                "routing_phase": PHASE_PREVIEW,
                "preview_block_id": "block-1",
                "identity_snippet": "dads who like soccer",
                "display_name_saved": True,
            },
            user_jwt="jwt",
            phone_verified=False,
            home_block_id=None,
            is_anonymous=True,
        )
        self.assertIsNotNone(result)
        reply, ctx, _, _ = result
        self.assertEqual(ctx["routing_phase"], "await_signup_phone")
        self.assertIn("number", reply.lower())

    def test_signup_intent_latches_until_zip_resolves_into_block(self) -> None:
        """When user says sign up before ZIP, we should start phone-gate after ZIP."""
        first = handle_discovery_turn(
            "ok sign me up",
            session_ctx={"routing_phase": "listening"},
            user_jwt="jwt",
            phone_verified=False,
            home_block_id=None,
            is_anonymous=True,
            history=[],
        )
        self.assertIsNotNone(first)
        reply, ctx, _, peers = first
        self.assertIn("ZIP", reply)
        self.assertEqual(peers, [])
        self.assertTrue(bool(ctx.get("pending_signup_gate")))

        with patch("app.discovery_route.fetch_blocks_for_zip") as mock_blocks:
            mock_blocks.return_value = [
                {"block_id": "block-1", "label": "Whisper Park"}
            ]
            second = handle_discovery_turn(
                "32827",
                session_ctx=ctx,
                user_jwt="jwt",
                phone_verified=False,
                home_block_id=None,
                is_anonymous=True,
                history=[],
            )

        self.assertIsNotNone(second)
        reply2, ctx2, _, peers2 = second
        self.assertEqual(ctx2["routing_phase"], "await_signup_phone")
        self.assertTrue(bool(ctx2.get("requires_phone_verification")))
        self.assertEqual(peers2, [])
        self.assertIn("number", reply2.lower())

    def test_signup_intent_latches_until_zip_resolves_for_sign_up_variants(self) -> None:
        """Voice often drops 'me' (sign up) or joins (signup)."""
        for msg in ("ok sign up", "ok signup"):
            first = handle_discovery_turn(
                msg,
                session_ctx={"routing_phase": "listening"},
                user_jwt="jwt",
                phone_verified=False,
                home_block_id=None,
                is_anonymous=True,
                history=[],
            )
            self.assertIsNotNone(first)
            _, ctx, _, peers = first
            self.assertEqual(peers, [])
            self.assertTrue(bool(ctx.get("pending_signup_gate")))

            with patch("app.discovery_route.fetch_blocks_for_zip") as mock_blocks:
                mock_blocks.return_value = [
                    {"block_id": "block-1", "label": "Whisper Park"}
                ]
                second = handle_discovery_turn(
                    "32827",
                    session_ctx=ctx,
                    user_jwt="jwt",
                    phone_verified=False,
                    home_block_id=None,
                    is_anonymous=True,
                    history=[],
                )

            self.assertIsNotNone(second)
            _, ctx2, _, peers2 = second
            self.assertEqual(ctx2["routing_phase"], "await_signup_phone")
            self.assertTrue(bool(ctx2.get("requires_phone_verification")))
            self.assertEqual(peers2, [])

    def test_need_zip_meta_question_passes_to_orchestrator(self) -> None:
        result = handle_discovery_turn(
            "are you real?",
            session_ctx={
                "active_intent": "discovery.find_peers",
                "routing_phase": PHASE_NEED_ZIP,
            },
            user_jwt="jwt",
            phone_verified=False,
            home_block_id=None,
            is_anonymous=True,
        )
        self.assertIsNone(result)
        self.assertFalse(
            wants_discovery_turn(
                "are you real?",
                {"routing_phase": PHASE_NEED_ZIP, "active_intent": "discovery.find_peers"},
            )
        )

    def test_listening_insult_passes_to_orchestrator(self) -> None:
        result = handle_discovery_turn(
            "are you dumb?",
            session_ctx={"routing_phase": "listening"},
            user_jwt="jwt",
            phone_verified=False,
            home_block_id=None,
            is_anonymous=True,
        )
        self.assertIsNone(result)

    def test_preview_confusion_passes_to_orchestrator(self) -> None:
        result = handle_discovery_turn(
            "What are you saying",
            session_ctx={
                "active_intent": "discovery.find_peers",
                "routing_phase": PHASE_PREVIEW,
                "preview_block_id": "block-1",
                "identity_snippet": "brazilian",
            },
            user_jwt="jwt",
            phone_verified=False,
            home_block_id=None,
            is_anonymous=True,
        )
        self.assertIsNone(result)
        self.assertFalse(
            wants_discovery_turn(
                "What are you saying",
                {"routing_phase": PHASE_PREVIEW, "preview_block_id": "block-1"},
            )
        )

    @patch("app.discovery_route.fetch_preview_events_on_block")
    def test_rsvp_in_preview_gates_verify(self, mock_events) -> None:
        mock_events.return_value = [{"title": "Sunday brunch", "starts_at": "2026-06-15"}]
        result = handle_discovery_turn(
            "I want to take part in Sunday brunch",
            session_ctx={
                "active_intent": "discovery.find_peers",
                "routing_phase": PHASE_PREVIEW,
                "preview_block_id": "block-1",
                "identity_snippet": "brazilian",
            },
            user_jwt="jwt",
            phone_verified=False,
            home_block_id=None,
            is_anonymous=True,
        )
        self.assertIsNotNone(result)
        reply, ctx, _, _ = result
        self.assertEqual(ctx["routing_phase"], "await_signup_phone")
        self.assertIn("Sunday brunch", reply)

    def test_phone_on_preview_with_verify_flag_advances_otp(self) -> None:
        """Safety net: preview + requires_phone_verification + E.164 in chat."""
        result = handle_discovery_turn(
            "+15550999012",
            session_ctx={
                "active_intent": "discovery.find_peers",
                "routing_phase": PHASE_PREVIEW,
                "requires_phone_verification": True,
                "preview_block_id": "block-1",
            },
            user_jwt="jwt",
            phone_verified=False,
            home_block_id=None,
            is_anonymous=True,
        )
        self.assertIsNotNone(result)
        _, ctx, _, _ = result
        self.assertEqual(ctx["routing_phase"], "await_signup_otp")
        self.assertEqual(ctx["auth_action"]["type"], "link_phone_signup")
        self.assertEqual(ctx["auth_action"]["phone"], "+15550999012")

    @patch("app.discovery_route.discovery_ai_enabled", return_value=True)
    @patch("app.discovery_route.discovery_slots_for_turn")
    def test_activities_zip_turn_shows_events_not_neighbors(
        self, mock_slots, _mock_ai
    ) -> None:
        mock_slots.side_effect = [
            {
                "goal": "activities",
                "in_discovery": True,
                "confidence": 0.92,
                "identity_snippet": None,
            },
            {
                "goal": "continue",
                "in_discovery": True,
                "confidence": 0.9,
                "identity_snippet": None,
            },
        ]
        with patch("app.discovery_route.fetch_blocks_for_zip") as mock_blocks, patch(
            "app.discovery_route.fetch_preview_events_on_block"
        ) as mock_events:
            mock_blocks.return_value = [{"block_id": "block-1", "label": "Lake Nona"}]
            mock_events.return_value = [{"title": "Park walk", "venue_name": "Central"}]
            first = handle_discovery_turn(
                "help me find activites",
                session_ctx={"routing_phase": "listening"},
                user_jwt="jwt",
                phone_verified=False,
                home_block_id=None,
                is_anonymous=True,
            )
            self.assertIsNotNone(first)
            _, ctx1, _, _ = first
            self.assertEqual(ctx1.get("discovery_goal"), "activities")
            self.assertIn("activities", first[0].lower())

            second = handle_discovery_turn(
                "32827",
                session_ctx=ctx1,
                user_jwt="jwt",
                phone_verified=False,
                home_block_id=None,
                is_anonymous=True,
            )
            self.assertIsNotNone(second)
            reply, ctx2, _, peers = second
            self.assertIn("Park walk", reply)
            self.assertEqual(peers, [])
            self.assertEqual(ctx2.get("discovery_goal"), "activities")
            self.assertEqual(len(ctx2.get("activity_previews") or []), 1)

    @patch("app.discovery_route.discovery_ai_enabled", return_value=True)
    @patch("app.discovery_route.discovery_slots_for_turn")
    @patch("app.discovery_route.fetch_preview_peers_on_block")
    @patch("app.discovery_route.fetch_blocks_for_zip")
    def test_pivot_neighbors_to_activities(
        self, mock_blocks, mock_peers, mock_slots, _mock_ai
    ) -> None:
        mock_blocks.return_value = [{"block_id": "block-1", "label": "Lake Nona"}]
        mock_peers.return_value = [
            {"matching_peer_label": "Mom of toddlers", "preview": True},
        ]
        mock_slots.side_effect = [
            {
                "goal": "peers",
                "in_discovery": True,
                "confidence": 0.9,
                "identity_snippet": "italian mom",
            },
            {
                "goal": "activities",
                "in_discovery": True,
                "confidence": 0.95,
                "identity_snippet": None,
            },
        ]
        with patch("app.discovery_route.fetch_preview_events_on_block") as mock_events:
            mock_events.return_value = [{"title": "Stroller walk", "venue_name": "Park"}]
            peers_turn = handle_discovery_turn(
                "find neighbors",
                session_ctx={
                    "routing_phase": "listening",
                    "preview_block_id": "block-1",
                    "identity_snippet": "italian mom",
                },
                user_jwt="jwt",
                phone_verified=False,
                home_block_id=None,
                is_anonymous=True,
            )
            self.assertIsNotNone(peers_turn)
            self.assertTrue(peers_turn[3])

            activities_turn = handle_discovery_turn(
                "find activities",
                session_ctx=peers_turn[1],
                user_jwt="jwt",
                phone_verified=False,
                home_block_id=None,
                is_anonymous=True,
            )
            self.assertIsNotNone(activities_turn)
            reply, ctx, _, peer_rows = activities_turn
            self.assertIn("Stroller walk", reply)
            self.assertEqual(peer_rows, [])
            self.assertEqual(ctx.get("discovery_goal"), "activities")

    @patch("app.discovery_route.fetch_preview_events_on_block")
    def test_activities_in_preview_not_peer_loop(self, mock_events) -> None:
        mock_events.return_value = [{"title": "Park walk", "venue_name": "Central"}]
        result = handle_discovery_turn(
            "I want to find activities",
            session_ctx={
                "active_intent": "discovery.find_peers",
                "routing_phase": PHASE_PREVIEW,
                "preview_block_id": "block-1",
                "identity_snippet": "brazilian",
            },
            user_jwt="jwt",
            phone_verified=False,
            home_block_id=None,
            is_anonymous=True,
        )
        self.assertIsNotNone(result)
        reply, ctx, _, peers = result
        self.assertEqual(ctx["routing_phase"], PHASE_PREVIEW)
        self.assertIn("Park walk", reply)
        self.assertEqual(peers, [])
        self.assertEqual(ctx.get("peer_matches"), [])
        previews = ctx.get("activity_previews") or []
        self.assertEqual(len(previews), 1)
        self.assertEqual(previews[0].get("title"), "Park walk")

    @patch("app.discovery_route.discovery_ai_enabled", return_value=True)
    @patch("app.discovery_slots.discovery_ai_enabled", return_value=True)
    @patch("app.discovery_slots.ai_parse_discovery_turn")
    def test_preview_pushback_passes_to_orchestrator(self, mock_slots, _mock_ai, _mock_ai2) -> None:
        mock_slots.return_value = {
            "in_discovery": False,
            "goal": "chat",
            "zip": None,
            "identity_snippet": None,
            "confidence": 0.9,
        }
        result = handle_discovery_turn(
            "dont you have some dads? you showed all moms",
            session_ctx={
                "active_intent": "discovery.find_peers",
                "routing_phase": PHASE_PREVIEW,
                "preview_block_id": "block-1",
                "identity_snippet": "dads who like playing soccer",
                "peer_matches": [
                    {"matching_peer_label": "Mom of toddlers", "preview": True},
                ],
            },
            user_jwt="jwt",
            phone_verified=False,
            home_block_id=None,
            is_anonymous=True,
        )
        self.assertIsNone(result)


    def test_profile_question_passes_to_orchestrator(self) -> None:
        result = handle_discovery_turn(
            "ok so what are my identity claims?",
            session_ctx={"routing_phase": "listening"},
            user_jwt="jwt",
            phone_verified=True,
            home_block_id="block-amanda",
            is_anonymous=False,
            history=[{"role": "assistant", "content": "You're signed in as Amanda."}],
        )
        self.assertIsNone(result)

    @patch("app.discovery_route.user_needs_display_name", return_value=True)
    def test_pending_post_verify_asks_name_without_jwt_verified(self, _mock_needs_name) -> None:
        result = handle_discovery_turn(
            "still waiting",
            session_ctx={
                "active_intent": "discovery.find_peers",
                "routing_phase": PHASE_PREVIEW,
                "preview_block_id": "block-1",
                "identity_snippet": "italian mom",
                "pending_post_verify": True,
            },
            user_jwt="jwt",
            phone_verified=False,
            home_block_id=None,
            is_anonymous=False,
            user_id="user-1",
        )
        self.assertIsNotNone(result)
        reply, ctx, _, _ = result
        self.assertEqual(ctx["routing_phase"], PHASE_NEED_DISPLAY_NAME)
        self.assertIn("call you", reply.lower())

    @patch("app.discovery_route.persist_profile_patch")
    @patch("app.discovery_route._try_assign_home_block", return_value="block-1")
    @patch("app.discovery_route.fetch_peer_matches")
    @patch("app.discovery_route.user_needs_display_name", return_value=True)
    def test_post_verify_asks_name_then_matches(
        self, _mock_needs_name, mock_match, _mock_assign, _mock_persist
    ) -> None:
        mock_match.return_value = [
            {
                "nickname": "Marina",
                "matching_peer_label": "Weekend BBQ",
                "similarity_score": 0.8,
            }
        ]
        result = handle_discovery_turn(
            "Tom",
            session_ctx={
                "active_intent": "discovery.find_peers",
                "routing_phase": PHASE_NEED_DISPLAY_NAME,
                "preview_block_id": "block-1",
                "identity_snippet": "dad, italian",
                "pending_post_verify": True,
                "phone_verified": True,
            },
            user_jwt="jwt",
            phone_verified=True,
            home_block_id=None,
            is_anonymous=False,
            user_id="user-1",
        )
        self.assertIsNotNone(result)
        reply, ctx, _, peers = result
        self.assertIn("Marina", reply)
        self.assertFalse(ctx.get("pending_post_verify"))
        self.assertEqual(len(peers), 1)

    @patch("app.discovery_route.discovery_ai_enabled", return_value=True)
    @patch("app.discovery_slots.discovery_ai_enabled", return_value=True)
    @patch("app.discovery_slots.ai_parse_discovery_turn")
    def test_preview_question_passes_to_orchestrator(self, mock_slots, _mock_ai, _mock_ai2) -> None:
        mock_slots.return_value = {
            "in_discovery": False,
            "goal": "chat",
            "zip": None,
            "identity_snippet": None,
            "confidence": 0.9,
        }
        result = handle_discovery_turn(
            "do you have brazilian moms?",
            session_ctx={
                "active_intent": "discovery.find_peers",
                "routing_phase": PHASE_PREVIEW,
                "preview_block_id": "block-1",
                "identity_snippet": "italian mom, 2 kids",
                "peer_matches": [{"matching_peer_label": "Mom of toddlers", "preview": True}],
            },
            user_jwt="jwt",
            phone_verified=False,
            home_block_id=None,
            is_anonymous=True,
        )
        self.assertIsNone(result)

    @patch("app.discovery_route.fetch_preview_peers_on_block")
    @patch("app.discovery_route.discovery_ai_enabled", return_value=True)
    @patch("app.discovery_slots.discovery_ai_enabled", return_value=True)
    @patch("app.discovery_slots.ai_parse_discovery_turn")
    def test_preview_ai_refetch_on_new_identity(self, mock_slots, _mock_ai, _mock_ai2, mock_preview) -> None:
        mock_slots.return_value = {
            "in_discovery": True,
            "goal": "peers",
            "zip": None,
            "identity_snippet": "dad who likes soccer",
            "confidence": 0.9,
        }
        mock_preview.return_value = [
            {"matching_peer_label": "Dad on block", "preview": True},
        ]
        result = handle_discovery_turn(
            "I am a dad looking for other dads on the block",
            session_ctx={
                "active_intent": "discovery.find_peers",
                "routing_phase": PHASE_PREVIEW,
                "preview_block_id": "block-1",
                "identity_snippet": "italian mom",
            },
            user_jwt="jwt",
            phone_verified=False,
            home_block_id=None,
            is_anonymous=True,
        )
        self.assertIsNotNone(result)
        _, ctx, _, peers = result
        self.assertIn("dad", (ctx.get("identity_snippet") or "").lower())
        self.assertEqual(len(peers), 1)

    def test_need_identity_still_collects_when_in_funnel(self) -> None:
        result = handle_discovery_turn(
            "hello",
            session_ctx={
                "active_intent": "discovery.find_peers",
                "routing_phase": PHASE_NEED_IDENTITY,
                "preview_block_id": "block-1",
            },
            user_jwt="jwt",
            phone_verified=False,
            home_block_id=None,
            is_anonymous=True,
        )
        self.assertIsNotNone(result)
        reply, ctx, _, _ = result
        self.assertEqual(ctx["routing_phase"], PHASE_NEED_IDENTITY)
        self.assertIn("Tell me one thing", reply)


    @patch("app.discovery_route._user_nickname", return_value="Amanda")
    def test_signed_in_user_logout_returns_auth_action(self, _nick) -> None:
        from app.ui_intent import UI_INTENT_SIGN_OUT, derive_ui_intent

        result = handle_discovery_turn(
            "I want to logout",
            session_ctx={},
            user_jwt="jwt",
            phone_verified=True,
            home_block_id="block-1",
            is_anonymous=False,
            user_id="user-amanda",
        )
        self.assertIsNotNone(result)
        reply, ctx, _, _ = result
        self.assertIn("signing you out", reply.lower())
        self.assertEqual(ctx.get("auth_action", {}).get("type"), "logout")
        self.assertEqual(ctx.get("auth_intent"), "logout")
        self.assertEqual(ctx.get("routing_phase"), "await_logout")
        self.assertEqual(derive_ui_intent(ctx), UI_INTENT_SIGN_OUT)

    @patch("app.discovery_route._user_nickname", return_value="Amanda")
    def test_cancel_logout_clears_await_logout(self, _nick) -> None:
        from app.ui_intent import UI_INTENT_CHAT, derive_ui_intent

        result = handle_discovery_turn(
            "stay logged in",
            session_ctx={
                "auth_intent": "logout",
                "routing_phase": "await_logout",
                "guest_step": "await_logout",
                "active_intent": "discovery.find_peers",
            },
            user_jwt="jwt",
            phone_verified=True,
            home_block_id="block-1",
            is_anonymous=False,
            user_id="user-amanda",
        )
        self.assertIsNotNone(result)
        reply, ctx, _, _ = result
        self.assertIn("stay logged in", reply.lower())
        self.assertIsNone(ctx.get("auth_intent"))
        self.assertEqual(ctx.get("routing_phase"), "listening")
        self.assertNotIn("auth_action", ctx)
        self.assertEqual(derive_ui_intent(ctx), UI_INTENT_CHAT)

    @patch("app.discovery_route._user_nickname", return_value="Amanda")
    def test_signed_in_user_login_intent_not_re_asked(self, _nick) -> None:
        result = handle_discovery_turn(
            "I want to login",
            session_ctx={"auth_intent": "login", "guest_step": "await_login_phone"},
            user_jwt="jwt",
            phone_verified=True,
            home_block_id="block-1",
            is_anonymous=False,
            user_id="user-amanda",
        )
        self.assertIsNotNone(result)
        reply, ctx, _, _ = result
        self.assertIn("already signed in", reply.lower())
        self.assertIn("amanda", reply.lower())
        self.assertIsNone(ctx.get("auth_intent"))
        self.assertEqual(ctx.get("routing_phase"), "listening")

    @patch("app.discovery_route.try_propose_intro_from_preview")
    @patch("app.discovery_route._preview_peers_with_ids")
    def test_intro_turn_returns_only_selected_peer_match(
        self, mock_preview_with_ids, mock_intro
    ) -> None:
        mock_preview_with_ids.return_value = [
            {"peer_user_id": "peer-1", "nickname": "Natasha", "matching_peer_label": "Pakistani Heritage"},
            {"peer_user_id": "peer-2", "nickname": "Mina", "matching_peer_label": "Mom"},
        ]
        mock_intro.return_value = (
            "Done — I introduced you to Natasha.",
            {
                "intro_id": "intro-1",
                "candidate_user_id": "peer-1",
                "match_reason": "Shared heritage",
            },
        )
        result = _try_neighbor_intro_turn(
            msg="introduce me to that mom",
            session_ctx={
                "routing_phase": PHASE_PREVIEW,
                "preview_block_id": "block-1",
                "peer_matches": mock_preview_with_ids.return_value,
            },
            ctx_base={"routing_phase": PHASE_PREVIEW, "peer_matches": mock_preview_with_ids.return_value},
            user_jwt="jwt",
            block_id="block-1",
            phone_verified=True,
            goal="propose_intro",
            slots={"goal": "propose_intro", "confidence": 0.95},
        )
        self.assertIsNotNone(result)
        _, ctx, _, peers = result
        self.assertEqual(len(peers), 1)
        self.assertEqual(peers[0]["peer_user_id"], "peer-1")
        self.assertEqual(len(ctx.get("peer_matches") or []), 1)
        self.assertEqual(ctx["peer_matches"][0]["peer_user_id"], "peer-1")


class TestUnifiedOpening(unittest.TestCase):
    def test_opening(self) -> None:
        opening, status, ctx, _ = lana_unified_opening()
        self.assertEqual(status, "continue")
        self.assertTrue(ctx.get("unified_mode"))
        self.assertIn("concierge", opening.lower())


if __name__ == "__main__":
    unittest.main()
