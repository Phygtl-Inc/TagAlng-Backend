import unittest
from unittest.mock import patch

from app.discovery_route import PHASE_PREVIEW, handle_discovery_turn
from app.layer1_handlers import format_identity_profile_reply
from app.layer1_intents import (
    attr_filter_tokens,
    enrich_slots,
    phrase_linear_intent,
    slots_linear_intent,
)
from app.signal_capture import (
    PHASE_SIGNAL_CONFIRM,
    advance_signal_draft,
    draft_from_slots,
    needs_confirm,
)
from app.ui_intent import UI_INTENT_SHOW_IDENTITY_PROFILE, derive_ui_intent


class TestLayer1IntentCatalog(unittest.TestCase):
    def test_phrase_find_peers_overrides_block(self) -> None:
        self.assertEqual(
            phrase_linear_intent("find people like me on the block"),
            "discovery.find_peers",
        )

    def test_phrase_find_in_block(self) -> None:
        self.assertEqual(
            phrase_linear_intent("what's happening on my block"),
            "discovery.find_in_block",
        )

    def test_phrase_block_log(self) -> None:
        self.assertEqual(
            phrase_linear_intent("who matched with me"),
            "discovery.block_log",
        )

    def test_attr_filter_tokens_multi(self) -> None:
        tokens = attr_filter_tokens("find pakistani mom")
        self.assertIn("pakistani", tokens)
        self.assertIn("mom", tokens)

    def test_enrich_slots_maps_looking_swap(self) -> None:
        slots = enrich_slots({
            "linear_intent": "looking.swap",
            "confidence": 0.9,
            "signal_detail": "3T rain boots",
        })
        self.assertEqual(slots_linear_intent(slots), "looking.swap")
        self.assertEqual(slots["signal_intent"], "swap_seek")
        self.assertEqual(slots["goal"], "save_signal")

    def test_legacy_goal_maps_to_linear(self) -> None:
        slots = enrich_slots({"goal": "save_signal", "signal_intent": "meet_seek", "confidence": 0.9})
        self.assertEqual(slots_linear_intent(slots), "looking.meet")

    def test_format_identity_profile_with_claims(self) -> None:
        reply = format_identity_profile_reply({
            "profile": {"nickname": "Maria", "block_display_name": "Whisper Park"},
            "mapped_summary": "Brazilian mom · toddlers · runner",
            "claims": [
                {"label": "Brazilian", "bucket": "heritage"},
                {"label": "Toddlers", "bucket": "stage"},
            ],
        })
        self.assertIn("Maria", reply)
        self.assertIn("Brazilian mom", reply)
        self.assertIn("2 identity threads", reply)


class TestSignalCapture(unittest.TestCase):
    def test_swap_short_detail_triggers_confirm(self) -> None:
        draft = draft_from_slots(
            {
                "linear_intent": "looking.swap",
                "confidence": 0.9,
                "signal_detail": "boots",
            },
            msg="boots",
        )
        need, field, prompt = needs_confirm(draft)
        self.assertTrue(need)
        self.assertEqual(field, "detail")
        self.assertIn("specific", prompt.lower())

    def test_meet_without_when_triggers_confirm(self) -> None:
        draft = draft_from_slots(
            {
                "linear_intent": "looking.meet",
                "confidence": 0.9,
                "signal_detail": "stroller walk buddies",
            },
            msg="stroller walk buddies",
        )
        need, _, prompt = needs_confirm(draft)
        self.assertTrue(need)
        self.assertIn("when", prompt.lower())


class TestLayer1DiscoveryRouting(unittest.TestCase):
    @patch("app.discovery_route.discovery_ai_enabled", return_value=True)
    @patch("app.discovery_route.fetch_identity_dashboard")
    @patch("app.discovery_route.discovery_slots_for_turn")
    def test_show_profile_turn(
        self, mock_slots, mock_dashboard, _mock_ai
    ) -> None:
        mock_slots.return_value = {
            "linear_intent": "identity.show_my_profile",
            "goal": "chat",
            "in_discovery": False,
            "confidence": 0.92,
        }
        mock_dashboard.return_value = {
            "profile": {"nickname": "Maria"},
            "mapped_summary": "Brazilian mom",
            "claims": [{"label": "Brazilian", "bucket": "heritage"}],
            "stats": {},
        }
        reply, ctx, _, peers = handle_discovery_turn(
            "what do you know about me",
            session_ctx={"routing_phase": PHASE_PREVIEW},
            user_jwt="jwt",
            phone_verified=True,
            home_block_id="block-1",
            is_anonymous=False,
            user_id="user-1",
        )
        self.assertEqual(peers, [])
        self.assertEqual(ctx.get("active_intent"), "identity.show_my_profile")
        self.assertIn("Maria", reply)
        self.assertEqual(derive_ui_intent(ctx), UI_INTENT_SHOW_IDENTITY_PROFILE)

    @patch("app.discovery_route.discovery_ai_enabled", return_value=True)
    @patch("app.discovery_route.discovery_slots_for_turn")
    def test_looking_meet_linear_intent_asks_when(self, mock_slots, _mock_ai) -> None:
        mock_slots.return_value = {
            "linear_intent": "looking.meet",
            "goal": "save_signal",
            "in_discovery": True,
            "confidence": 0.9,
            "signal_intent": "meet_seek",
            "signal_detail": "stroller walk buddies",
        }
        reply, ctx, _, _ = handle_discovery_turn(
            "looking for stroller walk buddies",
            session_ctx={"routing_phase": PHASE_PREVIEW, "preview_block_id": "block-1"},
            user_jwt="jwt",
            phone_verified=True,
            home_block_id="block-1",
            is_anonymous=False,
        )
        self.assertEqual(ctx.get("active_intent"), "looking.meet")
        self.assertIn("when", reply.lower())
        self.assertIn("signal_draft", ctx)

    @patch("app.discovery_route.discovery_ai_enabled", return_value=True)
    @patch("app.discovery_route.save_local_signal")
    @patch("app.discovery_route.discovery_slots_for_turn")
    def test_looking_meet_saves_after_when(
        self, mock_slots, mock_save, _mock_ai
    ) -> None:
        mock_slots.return_value = {
            "linear_intent": "looking.meet",
            "goal": "save_signal",
            "in_discovery": True,
            "confidence": 0.9,
            "signal_intent": "meet_seek",
            "signal_detail": "stroller walk buddies",
        }
        mock_save.return_value = {
            "signal_id": "sig-1",
            "intent": "meet_seek",
            "detail_text": "stroller walk buddies — weekend mornings",
            "matches_created": 0,
        }
        draft = draft_from_slots(mock_slots.return_value, msg="stroller walk buddies")
        draft["phase"] = PHASE_SIGNAL_CONFIRM
        draft["confirm_field"] = "when_hint"
        reply, ctx, _, _ = handle_discovery_turn(
            "weekend mornings",
            session_ctx={
                "routing_phase": PHASE_PREVIEW,
                "preview_block_id": "block-1",
                "signal_draft": draft,
            },
            user_jwt="jwt",
            phone_verified=True,
            home_block_id="block-1",
            is_anonymous=False,
        )
        self.assertEqual(ctx.get("active_intent"), "looking.meet")
        mock_save.assert_called_once()
        self.assertIn("stroller", reply.lower())

    @patch("app.discovery_route.discovery_ai_enabled", return_value=True)
    @patch("app.discovery_route.discovery_slots_for_turn")
    def test_help_what_can_you_do(self, mock_slots, _mock_ai) -> None:
        mock_slots.return_value = {
            "linear_intent": "help.what_can_you_do",
            "goal": "chat",
            "in_discovery": False,
            "confidence": 0.95,
        }
        reply, ctx, _, _ = handle_discovery_turn(
            "what can you do",
            session_ctx={"routing_phase": "listening"},
            user_jwt="jwt",
            phone_verified=True,
            home_block_id=None,
            is_anonymous=False,
        )
        self.assertEqual(ctx.get("active_intent"), "help.what_can_you_do")
        self.assertIn("concierge", reply.lower())
