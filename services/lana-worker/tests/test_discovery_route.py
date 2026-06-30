import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app.discovery_route import (
    PHASE_NEED_DISPLAY_NAME,
    PHASE_NEED_IDENTITY,
    PHASE_NEED_ZIP,
    PHASE_PREVIEW,
    _effective_discovery_goal,
    _intro_should_use_block_log,
    _try_block_log_intro_turn,
    _try_neighbor_intro_turn,
    _try_pending_heritage_turn,
    extract_zip,
    fetch_blocks_for_zip,
    format_preview_message,
    handle_discovery_turn,
    invalid_zip_hint,
    wants_discovery_turn,
    wants_more_peer_detail,
    wants_verify_help,
)
from app.intro_proposal import pick_block_log_entry_for_intro
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
        self.assertFalse(wants_more_peer_detail("show me identity claims of kashaf"))
        self.assertFalse(wants_more_peer_detail("how is it 100% match"))

    def test_wants_verify_help(self) -> None:
        self.assertTrue(wants_verify_help("How can I verify"))
        self.assertTrue(wants_verify_help("verify my phone please"))

    def test_effective_goal_clears_stale_activities(self) -> None:
        session: dict = {"discovery_goal": "activities"}
        slots = {"goal": "chat", "confidence": 0.9}
        goal = _effective_discovery_goal("my name is Zane", session, slots)
        self.assertEqual(goal, "chat")
        self.assertNotIn("discovery_goal", session)

    def test_effective_goal_pivots_on_save_signal(self) -> None:
        session: dict = {"discovery_goal": "activities"}
        slots = {
            "goal": "save_signal",
            "confidence": 0.9,
            "linear_intent": "looking.swap",
            "signal_intent": "swap_seek",
        }
        goal = _effective_discovery_goal("looking for a toy car", session, slots)
        self.assertEqual(goal, "save_signal")
        self.assertNotIn("discovery_goal", session)


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

    @patch("app.event_location.geocode_zip", return_value=None)
    @patch("app.discovery_route.call_rpc")
    def test_ungeocodable_zip_asks_to_recheck(self, mock_rpc, _mock_geo) -> None:
        # ZIP not in any block AND not geocodable (no Google match) → we can't create a block,
        # so ask the user to re-check the digits (the only remaining dead-end).
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
        self.assertIn("double-check", reply.lower())
        self.assertEqual(ctx["routing_phase"], PHASE_NEED_ZIP)

    @patch("app.event_location.geocode_zip", return_value=(32.7157, -117.1611, "San Diego"))
    @patch("app.discovery_route.fetch_blocks_for_zip", return_value=[])
    @patch("app.discovery_route.call_rpc")
    def test_new_zip_creates_waitlist_block(self, mock_rpc, _mock_fetch, _mock_geo) -> None:
        # An uncovered but geocodable ZIP → create a waitlist block and proceed (no dead-end).
        mock_rpc.return_value = {
            "block_id": "zip-92104",
            "display_name": "San Diego (92104)",
            "block_state": "waitlist",
        }
        result = handle_discovery_turn(
            "92104",
            session_ctx={"routing_phase": PHASE_NEED_ZIP, "active_intent": "discovery.find_peers"},
            user_jwt="jwt",
            phone_verified=False,
            home_block_id=None,
            is_anonymous=True,
        )
        self.assertIsNotNone(result)
        reply, ctx, _, _ = result
        # It must NOT dead-end on the re-check message — the block was created and assigned.
        self.assertNotIn("double-check", reply.lower())
        self.assertEqual(ctx.get("preview_block_id"), "zip-92104")
        # create_block_for_zip was the RPC invoked.
        self.assertTrue(
            any("create_block_for_zip" in str(c.args) for c in mock_rpc.call_args_list)
        )

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

    @patch("app.discovery_route.phone_has_registered_account", return_value=True)
    def test_existing_phone_on_verify_gate_routes_to_login(self, _mock_phone) -> None:
        result = handle_discovery_turn(
            "+15552232233",
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
        reply, ctx, _, _ = result
        self.assertEqual(ctx["routing_phase"], "await_login_otp")
        self.assertEqual(ctx["auth_action"]["type"], "send_login_otp")
        self.assertEqual(ctx["auth_action"]["phone"], "+15552232233")
        self.assertIn("found your account", reply.lower())

    @patch("app.discovery_route.discovery_ai_enabled", return_value=True)
    @patch("app.discovery_route.discovery_slots_for_turn")
    def test_find_activities_starts_browse(self, mock_slots, _mock_ai) -> None:
        """A browse intent starts the agentic events browse (ask interest first)."""
        mock_slots.return_value = {
            "goal": "activities",
            "in_discovery": True,
            "confidence": 0.92,
            "identity_snippet": None,
        }
        with patch("app.discovery_route.fetch_preview_events_on_block") as mock_events:
            result = handle_discovery_turn(
                "what's happening near me this weekend",
                session_ctx={"routing_phase": "listening"},
                user_jwt="jwt",
                phone_verified=False,
                home_block_id=None,
                is_anonymous=True,
            )
            self.assertIsNotNone(result)
            reply, ctx, _, peers = result
            # Enters the browse flow and asks the refining question before fetching events.
            self.assertTrue(ctx.get("activity_browse_active"))
            self.assertEqual(peers, [])
            self.assertIn("what kind of thing", reply.lower())
            mock_events.assert_not_called()

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

            # Pivoting to activities hands off to the agentic browse (ask interest first) —
            # no peer rows, no events fetch yet, browse now active.
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
            self.assertTrue(ctx.get("activity_browse_active"))
            self.assertEqual(peer_rows, [])
            self.assertIn("what kind of thing", reply.lower())
            mock_events.assert_not_called()

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

    @patch("app.discovery_route.discovery_ai_enabled", return_value=True)
    @patch("app.discovery_slots.discovery_ai_enabled", return_value=True)
    @patch("app.discovery_slots.ai_parse_discovery_turn")
    @patch("app.discovery_route.fetch_my_intros")
    def test_preview_sent_intro_question_shows_sent_intros(
        self, mock_fetch_intros, mock_slots, _mock_ai, _mock_ai2
    ) -> None:
        mock_slots.return_value = {
            "in_discovery": True,
            "goal": "list_intros",
            "zip": None,
            "identity_snippet": None,
            "intro_direction": "sent",
            "confidence": 0.9,
        }
        mock_fetch_intros.return_value = [
            {
                "id": "intro-1",
                "nickname": "Natasha",
                "direction": "sent",
                "match_reason": "You both fit mom.",
                "expires_at": None,
            }
        ]
        result = handle_discovery_turn(
            "show me what did you send to them",
            session_ctx={
                "active_intent": "social.propose_intro",
                "routing_phase": PHASE_PREVIEW,
                "preview_block_id": "block-1",
                "identity_snippet": "pakistani mom",
                "peer_matches": [{"matching_peer_label": "Mom of toddlers", "preview": True}],
            },
            user_jwt="jwt",
            phone_verified=True,
            home_block_id="block-1",
            is_anonymous=False,
        )
        self.assertIsNotNone(result)
        reply, ctx, _, peers = result
        self.assertIn("Natasha", reply)
        self.assertIn("you sent", reply.lower())
        self.assertEqual(ctx.get("active_intent"), "social.list_intros")
        self.assertEqual(peers, [])

    @patch("app.discovery_route.discovery_ai_enabled", return_value=True)
    @patch("app.discovery_slots.discovery_ai_enabled", return_value=True)
    @patch("app.discovery_slots.ai_parse_discovery_turn")
    @patch("app.discovery_route.fetch_my_intros")
    def test_show_my_intros_lists_received_when_sent_filter_empty(
        self, mock_fetch_intros, mock_slots, _mock_ai, _mock_ai2
    ) -> None:
        mock_slots.return_value = {
            "in_discovery": True,
            "goal": "list_intros",
            "zip": None,
            "identity_snippet": None,
            "intro_direction": "sent",
            "confidence": 0.9,
        }
        mock_fetch_intros.return_value = [
            {
                "id": "intro-1",
                "nickname": "Kashaf",
                "direction": "received",
                "match_reason": "Pakistani Heritage",
                "expires_at": "2099-01-01T00:00:00Z",
            }
        ]
        result = handle_discovery_turn(
            "show my intros",
            session_ctx={
                "active_intent": "social.propose_intro",
                "routing_phase": PHASE_PREVIEW,
                "preview_block_id": "block-1",
                "recent_intro_duplicate": {
                    "candidate_user_id": "peer-1",
                    "candidate_nickname": "Kashaf",
                    "match_reason": "Pakistani Heritage",
                },
            },
            user_jwt="jwt",
            phone_verified=True,
            home_block_id="block-1",
            is_anonymous=False,
        )
        self.assertIsNotNone(result)
        reply, ctx, _, peers = result
        self.assertIn("Kashaf", reply)
        self.assertIn("waiting on you", reply.lower())
        self.assertNotIn("i already sent", reply.lower())
        self.assertEqual(ctx.get("active_intent"), "social.list_intros")
        self.assertEqual(peers, [])
        self.assertEqual(mock_fetch_intros.call_count, 1)

    @patch("app.discovery_route.discovery_ai_enabled", return_value=True)
    @patch("app.discovery_slots.discovery_ai_enabled", return_value=True)
    @patch("app.discovery_slots.ai_parse_discovery_turn")
    @patch("app.discovery_route.fetch_my_intros")
    def test_list_intros_empty_shows_inbox_message_not_session_duplicate(
        self, mock_fetch_intros, mock_slots, _mock_ai, _mock_ai2
    ) -> None:
        mock_slots.return_value = {
            "in_discovery": True,
            "goal": "list_intros",
            "zip": None,
            "identity_snippet": None,
            "intro_direction": "sent",
            "confidence": 0.9,
        }
        mock_fetch_intros.return_value = []
        result = handle_discovery_turn(
            "what did you send to natasha",
            session_ctx={
                "active_intent": "social.propose_intro",
                "routing_phase": PHASE_PREVIEW,
                "preview_block_id": "block-1",
                "recent_intro_duplicate": {
                    "candidate_user_id": "peer-1",
                    "candidate_nickname": "Natasha",
                    "match_reason": "Mom",
                },
            },
            user_jwt="jwt",
            phone_verified=True,
            home_block_id="block-1",
            is_anonymous=False,
        )
        self.assertIsNotNone(result)
        reply, ctx, _, peers = result
        self.assertIn("don't have any pending intros", reply.lower())
        self.assertNotIn("recent intro to Natasha".lower(), reply.lower())
        self.assertEqual(ctx.get("active_intent"), "social.list_intros")
        self.assertEqual(peers, [])

    @patch("app.layer1_tier.fetch_my_intros")
    def test_bare_ok_with_no_pending_intro_falls_through(self, mock_fetch_intros) -> None:
        # Post-verify handshake: the PWA posts a literal "ok" after signup/login.
        # With no pending intro it must NOT surface "I don't see a pending intro" —
        # it should fall through to normal routing so the greeting is shown.
        from app.discovery_route import _try_respond_nudge_turn

        mock_fetch_intros.return_value = []
        result = _try_respond_nudge_turn(
            msg="ok",
            session_ctx={},
            user_jwt="jwt",
            phone_verified=True,
            phase="listening",
        )
        self.assertIsNone(result)

    @patch("app.layer1_tier.fetch_my_intros")
    def test_explicit_intro_response_with_no_pending_still_surfaces(self, mock_fetch_intros) -> None:
        # An explicit intro reference is unambiguous, so the "no pending intro"
        # message is the right reply even when nothing is pending.
        from app.discovery_route import _try_respond_nudge_turn

        mock_fetch_intros.return_value = []
        result = _try_respond_nudge_turn(
            msg="yes introduce us",
            session_ctx={},
            user_jwt="jwt",
            phone_verified=True,
            phase="listening",
        )
        self.assertIsNotNone(result)
        reply = result[0]
        self.assertIn("pending intro", reply.lower())

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
                "intro_proposal": {"intro_id": "intro-1"},
                "peer_matches": [{"matching_peer_label": "Mom", "preview": True}],
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
        self.assertNotIn("intro_proposal", ctx)
        self.assertNotIn("peer_matches", ctx)
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


class TestPeerDrilldownRouting(unittest.TestCase):
    @patch("app.discovery_route.discovery_ai_enabled", return_value=True)
    @patch("app.discovery_slots.discovery_ai_enabled", return_value=True)
    @patch("app.discovery_slots.ai_parse_discovery_turn")
    def test_second_neighbor_details_not_list_intros(
        self, mock_slots, _mock_ai, _mock_ai2
    ) -> None:
        mock_slots.return_value = {
            "in_discovery": True,
            "goal": "list_intros",
            "confidence": 0.9,
        }
        peers = [
            {"matching_peer_label": "Mom of toddlers", "preview": True},
            {"matching_peer_label": "Pakistani mom", "preview": True, "peer_user_id": "peer-2"},
        ]
        with patch("app.discovery_route._preview_peers_with_ids", return_value=peers):
            result = handle_discovery_turn(
                "show me second neighbour mom details",
                session_ctx={
                    "routing_phase": PHASE_PREVIEW,
                    "preview_block_id": "block-1",
                    "peer_matches": peers,
                },
                user_jwt="jwt",
                phone_verified=True,
                home_block_id="block-1",
                is_anonymous=False,
            )
        self.assertIsNotNone(result)
        reply, ctx, _, returned = result
        self.assertIn("pakistani mom", reply.lower())
        self.assertEqual(ctx.get("active_intent"), "discovery.find_peers")
        self.assertNotEqual(ctx.get("active_intent"), "social.list_intros")
        self.assertEqual(len(returned), 1)


class TestPeerTraitAndRefine(unittest.TestCase):
    def test_trait_question_confirms_brazilian_from_label(self) -> None:
        from app.discovery_route import _try_peer_trait_question_turn

        peers = [
            {
                "peer_user_id": "p1",
                "matching_peer_label": "Brazilian · Mom",
                "preview": True,
            },
            {
                "peer_user_id": "p2",
                "matching_peer_label": "Mom",
                "preview": True,
            },
        ]
        result = _try_peer_trait_question_turn(
            msg="is she brazilian?",
            session_ctx={"peer_matches": peers, "routing_phase": PHASE_PREVIEW},
            phone_verified=False,
            home_block_id="block-1",
            phase=PHASE_PREVIEW,
        )
        self.assertIsNotNone(result)
        reply, _, _, _ = result
        self.assertIn("Yes", reply)
        self.assertIn("Brazilian", reply)

    @patch("app.discovery_route.fetch_peers_by_attr_filter")
    @patch("app.discovery_route._resolve_block_id_for_turn", return_value="block-1")
    def test_attr_refine_reruns_search(self, _mock_block, mock_fetch) -> None:
        from app.discovery_route import _try_attr_refine_turn

        mock_fetch.return_value = [
            {
                "peer_user_id": "p1",
                "matching_peer_label": "Brazilian · Mom",
            }
        ]
        result = _try_attr_refine_turn(
            msg="no i want brazilian moms",
            slots={},
            session_ctx={
                "peer_matches": [{"matching_peer_label": "Mom", "preview": True}],
                "routing_phase": PHASE_PREVIEW,
            },
            user_jwt="jwt",
            phone_verified=True,
            home_block_id="block-1",
            phase=PHASE_PREVIEW,
        )
        self.assertIsNotNone(result)
        reply, ctx, _, peers = result
        self.assertIn("brazilian", reply.lower())
        self.assertEqual(ctx.get("active_intent"), "discovery.find_by_attrs")
        self.assertEqual(len(peers), 1)

    def test_attr_refine_ignores_casual_i_want_pizza(self) -> None:
        from app.discovery_route import _try_attr_refine_turn

        result = _try_attr_refine_turn(
            msg="I want a pizza",
            slots={},
            session_ctx={
                "peer_matches": [{"matching_peer_label": "Mom", "preview": True}],
                "routing_phase": PHASE_PREVIEW,
            },
            user_jwt="jwt",
            phone_verified=True,
            home_block_id="block-1",
            phase=PHASE_PREVIEW,
        )
        self.assertIsNone(result)


class TestHeritageIntroRouting(unittest.TestCase):
    def test_pending_heritage_yields_to_intro_request(self) -> None:
        pending = {
            "from_label": "American",
            "label": "Brazilian Heritage",
            "claim": {
                "concept": "brazilian_heritage",
                "label": "Brazilian Heritage",
                "confidence": 0.9,
                "bucket": "heritage",
            },
        }
        result = _try_pending_heritage_turn(
            msg="introduce me to Natasha",
            session_ctx={"pending_heritage_change": pending},
            user_id="user-1",
            user_jwt="jwt",
            phone_verified=True,
            phase="listening",
        )
        self.assertIsNone(result)

    def test_pending_heritage_still_asks_on_ambiguous_reply(self) -> None:
        pending = {
            "from_label": "American",
            "label": "Brazilian Heritage",
            "claim": {
                "concept": "brazilian_heritage",
                "label": "Brazilian Heritage",
                "confidence": 0.9,
                "bucket": "heritage",
            },
        }
        result = _try_pending_heritage_turn(
            msg="maybe later",
            session_ctx={"pending_heritage_change": pending},
            user_id="user-1",
            user_jwt="jwt",
            phone_verified=True,
            phase="listening",
        )
        self.assertIsNotNone(result)
        reply, _, _, _ = result
        self.assertIn("heritage", reply.lower())

    def test_pending_heritage_yields_to_attr_peer_search(self) -> None:
        pending = {
            "from_label": "Brazilian Heritage",
            "label": "American Heritage",
            "claim": {
                "concept": "american_heritage",
                "label": "American Heritage",
                "confidence": 0.9,
                "bucket": "heritage",
            },
        }
        result = _try_pending_heritage_turn(
            msg="show me american moms",
            session_ctx={"pending_heritage_change": pending},
            user_id="user-1",
            user_jwt="jwt",
            phone_verified=True,
            phase="listening",
            slots={"linear_intent": "discovery.find_by_attrs", "goal": "peers", "confidence": 0.9},
        )
        self.assertIsNone(result)

    def test_pending_heritage_yields_to_find_me_american_moms(self) -> None:
        pending = {
            "from_label": "Brazilian Heritage",
            "label": "American Heritage",
            "claim": {
                "concept": "american_heritage",
                "label": "American Heritage",
                "confidence": 0.9,
                "bucket": "heritage",
            },
        }
        result = _try_pending_heritage_turn(
            msg="i said find me american moms",
            session_ctx={"pending_heritage_change": pending},
            user_id="user-1",
            user_jwt="jwt",
            phone_verified=True,
            phase="listening",
            slots={
                "linear_intent": "discovery.find_by_attrs",
                "goal": "peers",
                "attr_filter": "american moms",
                "confidence": 0.9,
            },
        )
        self.assertIsNone(result)

    def test_pending_heritage_yields_to_doctor_tip_seek(self) -> None:
        pending = {
            "from_label": "American Heritage",
            "label": "Brazilian Heritage",
            "claim": {
                "concept": "brazilian_heritage",
                "label": "Brazilian Heritage",
                "confidence": 0.9,
                "bucket": "heritage",
            },
        }
        result = _try_pending_heritage_turn(
            msg="Is there any good doctor in our block",
            session_ctx={"pending_heritage_change": pending},
            user_id="user-1",
            user_jwt="jwt",
            phone_verified=True,
            phase=PHASE_PREVIEW,
            slots={"goal": "peers", "confidence": 0.9},
        )
        self.assertIsNone(result)


class TestSignalPivotRouting(unittest.TestCase):
    @patch("app.discovery_route.save_local_signal")
    @patch("app.discovery_route.discovery_ai_enabled", return_value=True)
    @patch("app.discovery_route.discovery_slots_for_turn")
    @patch("app.discovery_route.fetch_peer_matches")
    def test_doctor_query_not_peer_preview(
        self, mock_peers, mock_slots, _mock_ai, mock_save
    ) -> None:
        mock_save.return_value = {"signal_id": "sig-1", "matches_created": 0}
        mock_slots.return_value = {
            "goal": "peers",
            "in_discovery": True,
            "confidence": 0.92,
            "identity_snippet": "brazilian mom",
        }
        mock_peers.return_value = [
            {"nickname": "Natasha", "match_score": 1.0, "traits": ["Mom"]},
        ]
        pending = {
            "from_label": "American Heritage",
            "label": "Brazilian Heritage",
            "claim": {"concept": "brazilian_heritage", "label": "Brazilian Heritage"},
        }
        result = handle_discovery_turn(
            "Is there any good doctor in our block",
            session_ctx={
                "routing_phase": PHASE_PREVIEW,
                "preview_block_id": "block-a",
                "discovery_goal": "peers",
                "identity_snippet": "brazilian mom",
                "pending_heritage_change": pending,
            },
            user_jwt="jwt",
            phone_verified=True,
            home_block_id="block-a",
            is_anonymous=False,
            history=[],
            user_id="user-1",
        )
        self.assertIsNotNone(result)
        reply, _, _, peers = result
        assert reply is not None
        mock_peers.assert_not_called()
        self.assertEqual(peers, [])
        self.assertNotIn("I found 5 neighbors", reply)

    @patch("app.discovery_route.save_local_signal")
    @patch("app.discovery_route.discovery_ai_enabled", return_value=True)
    @patch("app.discovery_route.discovery_slots_for_turn")
    @patch("app.discovery_route.fetch_peer_matches")
    def test_computer_swap_not_peer_preview(
        self, mock_peers, mock_slots, _mock_ai, mock_save
    ) -> None:
        mock_save.return_value = {"signal_id": "sig-2", "matches_created": 0}
        mock_slots.return_value = {
            "goal": "peers",
            "in_discovery": True,
            "confidence": 0.92,
            "identity_snippet": "brazilian mom",
        }
        mock_peers.return_value = [
            {"nickname": "Natasha", "match_score": 1.0, "traits": ["Mom"]},
        ]
        result = handle_discovery_turn(
            "I am looking for a computer for my kid",
            session_ctx={
                "routing_phase": PHASE_PREVIEW,
                "preview_block_id": "block-a",
                "discovery_goal": "peers",
                "identity_snippet": "brazilian mom",
            },
            user_jwt="jwt",
            phone_verified=True,
            home_block_id="block-a",
            is_anonymous=False,
            history=[],
            user_id="user-1",
        )
        self.assertIsNotNone(result)
        reply, _, _, peers = result
        assert reply is not None
        mock_peers.assert_not_called()
        self.assertEqual(peers, [])
        self.assertNotIn("I found 5 neighbors", reply)

    @patch("app.discovery_route.discovery_ai_enabled", return_value=True)
    @patch("app.discovery_route.discovery_slots_for_turn")
    @patch("app.discovery_route.fetch_peer_matches")
    def test_meta_chat_not_peer_preview(
        self, mock_peers, mock_slots, _mock_ai
    ) -> None:
        mock_slots.return_value = {
            "goal": "peers",
            "in_discovery": True,
            "confidence": 0.92,
        }
        mock_peers.return_value = [
            {"nickname": "Natasha", "match_score": 1.0, "traits": ["Mom"]},
        ]
        result = handle_discovery_turn(
            "Are you dumb?",
            session_ctx={
                "routing_phase": PHASE_PREVIEW,
                "preview_block_id": "block-a",
                "discovery_goal": "peers",
            },
            user_jwt="jwt",
            phone_verified=True,
            home_block_id="block-a",
            is_anonymous=False,
            history=[],
            user_id="user-1",
        )
        self.assertIsNotNone(result)
        reply, _, _, peers = result
        assert reply is not None
        mock_peers.assert_not_called()
        self.assertEqual(peers, [])
        self.assertIn("Lana", reply)
        self.assertNotIn("I found 5 neighbors", reply)


class TestBlockLogIntroRouting(unittest.TestCase):
    def test_pick_block_log_entry_for_intro_index(self) -> None:
        entries = [{"id": "e1"}, {"id": "e2"}, {"id": "e3"}]
        self.assertEqual(
            pick_block_log_entry_for_intro(entries, msg="introduce me to #2")["id"],
            "e2",
        )

    def test_intro_should_use_block_log_after_block_log_turn(self) -> None:
        ctx = {"active_intent": "discovery.block_log", "block_log_entries": [{"id": "e1"}]}
        self.assertTrue(_intro_should_use_block_log("introduce me to #1", ctx, None))
        self.assertTrue(
            _intro_should_use_block_log(
                "introduce me to neighbour regarding the swap",
                ctx,
                None,
            )
        )
        self.assertFalse(
            _intro_should_use_block_log("introduce me to Kashaf", ctx, None),
        )

    @patch("app.discovery_route.propose_neighbor_intro")
    @patch("app.discovery_route.fetch_my_block_log")
    @patch("app.discovery_route.block_log_take_action")
    def test_block_log_intro_targets_swap_peer_not_identity_card(
        self, mock_action, mock_log, mock_propose
    ) -> None:
        mock_log.return_value = [
            {
                "id": "entry-1",
                "entry_id": "entry-1",
                "peer_user_id": "swap-peer-1",
                "peer_preview_label": "A neighbor on your block",
                "match_type": "inbound_for_my_seek",
                "match_strength": 0.76,
                "peer_signal_detail": "kid bicycle",
                "my_signal_detail": "bicycle for my kid",
                "peer_signal_intent": "swap_offer",
                "my_signal_intent": "swap_seek",
                "match_reasons": ["kid bicycle matches your ask"],
            }
        ]
        mock_propose.return_value = {
            "intro_id": "intro-swap-1",
            "candidate_user_id": "swap-peer-1",
        }
        result = _try_block_log_intro_turn(
            msg="introduce me to #1",
            session_ctx={"active_intent": "discovery.block_log"},
            user_jwt="jwt",
            phone_verified=True,
            phase=PHASE_PREVIEW,
            history=None,
        )
        self.assertIsNotNone(result)
        reply, ctx, _, peers = result
        self.assertIn("introduced", reply.lower())
        mock_propose.assert_called_once()
        self.assertEqual(mock_propose.call_args.kwargs["candidate_user_id"], "swap-peer-1")
        self.assertEqual(ctx.get("active_intent"), "social.propose_intro")
        self.assertFalse(ctx.get("peer_matches"))

    @patch("app.discovery_route.try_propose_intro_from_preview")
    @patch("app.discovery_route._preview_peers_with_ids")
    def test_neighbor_intro_skipped_when_block_log_context(
        self, mock_preview, mock_intro
    ) -> None:
        mock_preview.return_value = [
            {"peer_user_id": "peer-1", "nickname": "Natasha", "matching_peer_label": "Mom"},
        ]
        result = _try_neighbor_intro_turn(
            msg="introduce me to #1",
            session_ctx={
                "routing_phase": PHASE_PREVIEW,
                "active_intent": "discovery.block_log",
                "block_log_entries": [{"entry_id": "e1"}],
            },
            ctx_base={"routing_phase": PHASE_PREVIEW},
            user_jwt="jwt",
            block_id="block-1",
            phone_verified=True,
            goal="propose_intro",
            slots={"goal": "propose_intro", "confidence": 0.95},
        )
        self.assertIsNone(result)
        mock_intro.assert_not_called()


class TestUnifiedOpening(unittest.TestCase):
    def test_opening(self) -> None:
        opening, status, ctx, _ = lana_unified_opening()
        self.assertEqual(status, "continue")
        self.assertTrue(ctx.get("unified_mode"))
        self.assertTrue(opening.strip())


class TestOutOfScopeRouting(unittest.TestCase):
    """An errand TagAlng can't do (deliver pizza) must NOT funnel into find_peers — it is
    declined + logged when clear, or clarified once when the classifier is unsure."""

    @patch("app.discovery_route.log_feature_request")
    @patch("app.discovery_route.discovery_ai_enabled", return_value=True)
    @patch("app.discovery_route.discovery_slots_for_turn")
    def test_confident_out_of_scope_declines_and_logs(
        self, mock_slots, _mock_ai, mock_log
    ) -> None:
        mock_slots.return_value = {
            "goal": "out_of_scope",
            "linear_intent": "system.out_of_scope",
            "in_discovery": False,
            "confidence": 0.95,
            "signal_detail": "pizza delivery",
        }
        result = handle_discovery_turn(
            "I want you to deliver pizza for me",
            session_ctx={"routing_phase": "listening"},
            user_jwt="jwt",
            phone_verified=False,
            home_block_id="block-1",
            is_anonymous=True,
            history=[],
            user_id="user-1",
        )
        self.assertIsNotNone(result)
        reply, ctx, routing, peers = result
        # Declined (not a ZIP funnel prompt) and steered back to TagAlng's real lanes.
        self.assertNotIn("ZIP", reply)
        self.assertIn("pizza delivery", reply)
        self.assertNotEqual(ctx.get("active_intent"), "discovery.find_peers")
        self.assertEqual(peers, [])
        self.assertEqual(routing.get("tool_to_call"), "out_of_scope")
        # Demand is logged so the "we'll let you know" promise is keepable.
        mock_log.assert_called_once()
        kwargs = mock_log.call_args.kwargs
        self.assertEqual(kwargs.get("user_id"), "user-1")
        self.assertEqual(kwargs.get("block_id"), "block-1")
        self.assertIn("pizza", kwargs.get("request_text", "").lower())

    @patch("app.discovery_route.log_feature_request")
    @patch("app.discovery_route.discovery_ai_enabled", return_value=True)
    @patch("app.discovery_route.discovery_slots_for_turn")
    def test_unsure_out_of_scope_clarifies_first(
        self, mock_slots, _mock_ai, mock_log
    ) -> None:
        mock_slots.return_value = {
            "goal": "out_of_scope",
            "linear_intent": "system.out_of_scope",
            "in_discovery": False,
            "confidence": 0.4,  # below the 0.6 decline gate → ask, don't refuse
            "signal_detail": "food for us",
        }
        result = handle_discovery_turn(
            "can you sort out food for us",
            session_ctx={"routing_phase": "listening"},
            user_jwt="jwt",
            phone_verified=False,
            home_block_id="block-1",
            is_anonymous=True,
            history=[],
            user_id="user-1",
        )
        self.assertIsNotNone(result)
        reply, ctx, routing, _ = result
        self.assertTrue(ctx.get("out_of_scope_pending"))
        self.assertEqual(routing.get("tool_to_call"), "clarify_out_of_scope")
        # Asks rather than refuses — nothing logged yet.
        mock_log.assert_not_called()

    @patch("app.discovery_route.log_feature_request")
    @patch("app.discovery_route.discovery_ai_enabled", return_value=True)
    @patch("app.discovery_route.discovery_slots_for_turn")
    def test_clarifier_reply_confirming_declines_and_logs(
        self, mock_slots, _mock_ai, mock_log
    ) -> None:
        # The reply re-reads as out_of_scope (still confirming the errand) → decline + log.
        mock_slots.return_value = {
            "goal": "out_of_scope",
            "linear_intent": "system.out_of_scope",
            "in_discovery": False,
            "confidence": 0.5,
            "signal_detail": "food delivery",
        }
        result = handle_discovery_turn(
            "no, just deliver it to me",
            session_ctx={"routing_phase": "listening", "out_of_scope_pending": True},
            user_jwt="jwt",
            phone_verified=False,
            home_block_id="block-1",
            is_anonymous=True,
            history=[],
            user_id="user-1",
        )
        self.assertIsNotNone(result)
        reply, ctx, _, _ = result
        self.assertIsNone(ctx.get("out_of_scope_pending"))  # flag cleared, never leaks
        self.assertNotEqual(ctx.get("active_intent"), "discovery.find_peers")
        mock_log.assert_called_once()

    @patch("app.discovery_route.fetch_blocks_for_zip")
    @patch("app.discovery_route.log_feature_request")
    @patch("app.discovery_route.discovery_ai_enabled", return_value=True)
    @patch("app.discovery_route.discovery_slots_for_turn")
    def test_clarifier_reply_pivoting_to_supported_falls_through(
        self, mock_slots, _mock_ai, mock_log, mock_blocks
    ) -> None:
        # The reply pivots to a real supported intent (find peers) → fall through to the
        # funnel, NOT a decline, and the pending flag is cleared.
        mock_slots.return_value = {
            "goal": "peers",
            "in_discovery": True,
            "confidence": 0.9,
            "identity_snippet": None,
        }
        mock_blocks.return_value = [{"block_id": "block-1", "label": "Whisper Park"}]
        result = handle_discovery_turn(
            "actually, just find me neighbors",
            session_ctx={"routing_phase": "listening", "out_of_scope_pending": True},
            user_jwt="jwt",
            phone_verified=False,
            home_block_id=None,
            is_anonymous=True,
            history=[],
            user_id="user-1",
        )
        self.assertIsNotNone(result)
        reply, ctx, _, _ = result
        self.assertIsNone(ctx.get("out_of_scope_pending"))
        self.assertIn("ZIP", reply)  # reached the find-peers funnel
        mock_log.assert_not_called()


class TestUnsafeRouting(unittest.TestCase):
    """Inappropriate/abusive content must be refused + moderation-logged, never captured as
    a swap/tip, never logged as a feature request, never funnelled into find_peers."""

    @patch("app.discovery_route.log_feature_request")
    @patch("app.discovery_route.log_moderation_flag")
    @patch("app.discovery_route.discovery_ai_enabled", return_value=True)
    @patch("app.discovery_route.discovery_slots_for_turn")
    def test_ai_unsafe_refuses_and_moderation_logs(
        self, mock_slots, _mock_ai, mock_mod, mock_feat
    ) -> None:
        mock_slots.return_value = {
            "goal": "unsafe",
            "linear_intent": "system.unsafe",
            "in_discovery": False,
            "confidence": 0.9,
            "unsafe_kind": "sexual",
        }
        result = handle_discovery_turn(
            "find me someone to have sex with",
            session_ctx={"routing_phase": "listening"},
            user_jwt="jwt",
            phone_verified=False,
            home_block_id="block-1",
            is_anonymous=True,
            history=[],
            user_id="user-1",
        )
        self.assertIsNotNone(result)
        reply, ctx, routing, peers = result
        self.assertIn("not able to help", reply.lower())
        self.assertNotIn("let you know", reply.lower())  # NO feature-request promise
        self.assertNotEqual(ctx.get("active_intent"), "discovery.find_peers")
        self.assertEqual(peers, [])
        self.assertEqual(routing.get("tool_to_call"), "unsafe_refusal")
        mock_mod.assert_called_once()
        self.assertEqual(mock_mod.call_args.kwargs.get("kind"), "sexual")
        mock_feat.assert_not_called()  # never a feature request

    @patch("app.discovery_route.log_moderation_flag")
    @patch("app.discovery_route.discovery_ai_enabled", return_value=True)
    @patch("app.discovery_route.discovery_slots_for_turn")
    def test_regex_backstop_catches_when_ai_misses(
        self, mock_slots, _mock_ai, mock_mod
    ) -> None:
        # AI misreads it as a benign swap, but the regex backstop refuses anyway.
        mock_slots.return_value = {
            "goal": "save_signal",
            "signal_intent": "swap_seek",
            "in_discovery": False,
            "confidence": 0.8,
        }
        result = handle_discovery_turn(
            "anyone got a sex doll to swap",
            session_ctx={"routing_phase": "listening"},
            user_jwt="jwt",
            phone_verified=False,
            home_block_id="block-1",
            is_anonymous=True,
            history=[],
            user_id="user-1",
        )
        self.assertIsNotNone(result)
        reply, _, routing, _ = result
        self.assertEqual(routing.get("tool_to_call"), "unsafe_refusal")
        mock_mod.assert_called_once()

    def test_wants_discovery_turn_forced_by_unsafe_regex(self) -> None:
        # Even with no slots/AI, an egregious message must reach the discovery handler.
        self.assertTrue(
            wants_discovery_turn("send me nudes", {"routing_phase": "listening"}, [])
        )

    def test_clean_message_is_not_flagged_unsafe(self) -> None:
        from app.discovery_route import _unsafe_kind_for_turn

        self.assertIsNone(
            _unsafe_kind_for_turn(
                {"goal": "peers", "confidence": 0.9}, "find me moms on my block"
            )
        )


if __name__ == "__main__":
    unittest.main()
