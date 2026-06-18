import unittest
from unittest.mock import patch

from app.discovery_route import PHASE_PREVIEW, handle_discovery_turn
from app.layer1_handlers import format_identity_profile_reply, stamp_identity_profile_ctx
from app.layer1_intents import (
    attr_filter_tokens,
    enrich_slots,
    is_block_activity_browse,
    phrase_linear_intent,
    slots_linear_intent,
)
from app.signal_capture import (
    PHASE_SIGNAL_CONFIRM,
    advance_signal_draft,
    draft_from_slots,
    needs_confirm,
    normalize_size_answer,
)
from app.ui_intent import UI_INTENT_SHOW_IDENTITY_PROFILE, derive_ui_intent


class TestLayer1IntentCatalog(unittest.TestCase):
    def test_open_ended_intents_left_to_ai_not_phrase_regex(self) -> None:
        """Find neighbors, swap, tip — classified by Flash, not phrase regex."""
        for msg in (
            "find people like me on the block",
            "what's happening on my block",
            "find italian moms",
            "i need you to find italian moms in my block",
            "find italian dads",
            "what are people looking for swap in my block",
            "what are swaps on my block",
            "Dr Smith is a great pediatrician",
            "do you know a good pediatrician",
            "I'm looking for rain boots",
            "i am looking for a laptop",
            "also looking for a bicycle buddy",
            "do you know good teacher for my kid?",
            "I wanna swap my kid bicycle",
            "I am looking to swap my rain coat",
            "i am looking for a rain coat to wear",
            "i want to giveup my kids bicycle",
        ):
            self.assertIsNone(phrase_linear_intent(msg), msg)

    def test_enrich_slots_hosting_overrides_misclassified_attr_filter(self) -> None:
        slots = enrich_slots({
            "linear_intent": "discovery.find_by_attrs",
            "attr_filter": "brazilian",
            "confidence": 0.88,
            "goal": "peers",
            "in_discovery": True,
        }, msg="i want brazilian coffee this weekend")
        self.assertEqual(slots_linear_intent(slots), "sharing.host")
        self.assertEqual(slots.get("signal_intent"), "host_meet")
        self.assertEqual(slots.get("goal"), "save_signal")
        self.assertNotIn("attr_filter", slots)

    def test_utterance_indicates_hosting_plan(self) -> None:
        from app.layer1_intents import utterance_indicates_hosting_plan

        self.assertTrue(utterance_indicates_hosting_plan("i want brazilian coffee this weekend"))
        self.assertFalse(utterance_indicates_hosting_plan("show me brazilian moms"))

    def test_enrich_slots_ai_find_by_attrs_not_overridden_by_regex(self) -> None:
        slots = enrich_slots({
            "linear_intent": "discovery.find_by_attrs",
            "attr_filter": "italian moms",
            "confidence": 0.88,
            "goal": "peers",
            "in_discovery": True,
        }, msg="find italian moms")
        self.assertEqual(slots_linear_intent(slots), "discovery.find_by_attrs")
        self.assertEqual(slots.get("attr_filter"), "italian moms")
        self.assertNotEqual(slots_linear_intent(slots), "looking.tip")

    def test_enrich_slots_ai_swap_offer_from_slots(self) -> None:
        slots = enrich_slots({
            "linear_intent": "sharing.swap",
            "confidence": 0.9,
            "signal_detail": "kids bicycle",
            "goal": "save_signal",
        }, msg="i want to giveup my kids bicycle")
        self.assertEqual(slots_linear_intent(slots), "sharing.swap")
        self.assertEqual(slots.get("signal_intent"), "swap_offer")

    def test_enrich_slots_show_peer_profile_from_ai(self) -> None:
        slots = enrich_slots({
            "linear_intent": "discovery.show_peer_profile",
            "peer_name": "Kashaf",
            "confidence": 0.9,
            "goal": "chat",
            "in_discovery": False,
        }, msg="show me identity claims of kashaf")
        self.assertEqual(slots_linear_intent(slots), "discovery.show_peer_profile")
        self.assertEqual(slots.get("peer_name"), "Kashaf")

    def test_phrase_block_log(self) -> None:
        self.assertEqual(
            phrase_linear_intent("who matched with me"),
            "discovery.block_log",
        )
        self.assertEqual(
            phrase_linear_intent("show my block logs"),
            "discovery.block_log",
        )

    def test_phrase_my_name_is_change_name(self) -> None:
        self.assertEqual(
            phrase_linear_intent("my name is Zane"),
            "settings.change_name",
        )
        self.assertEqual(
            phrase_linear_intent("add my name as Ada"),
            "settings.change_name",
        )

    def test_phrase_block_browse_not_block_log(self) -> None:
        self.assertIsNone(
            phrase_linear_intent("what are people looking for swap in my block"),
        )
        self.assertTrue(
            is_block_activity_browse("what are people looking for swap in my block"),
        )

    def test_phrase_block_browse_swaps(self) -> None:
        self.assertTrue(
            is_block_activity_browse("what are swaps on my block"),
        )
        self.assertTrue(
            is_block_activity_browse("show those 11 asks on my block"),
        )

    def test_phrase_edit_claim_remove(self) -> None:
        self.assertEqual(
            phrase_linear_intent("remove italian heritage. I am pakistani"),
            "identity.edit_claim",
        )

    def test_phrase_profile_ack(self) -> None:
        self.assertEqual(phrase_linear_intent("ok thats me"), "identity.complete_profile")

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

    def test_stamp_identity_profile_sorts_heritage_first(self) -> None:
        ctx: dict = {}
        stamp_identity_profile_ctx(ctx, {
            "profile": {"nickname": "Kashaf"},
            "claims": [
                {"label": "Walking", "bucket": "activity", "confidence": 0.95},
                {"label": "Brazilian", "bucket": "heritage", "confidence": 0.85},
                {"label": "Mom", "bucket": "stage", "confidence": 0.9},
            ],
        })
        labels = [c.get("label") for c in ctx["identity_profile"]["claims"]]
        self.assertEqual(labels[0], "Brazilian")


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

    def test_laptop_swap_skips_kids_stage_prompt(self) -> None:
        draft = draft_from_slots(
            {
                "linear_intent": "looking.swap",
                "confidence": 0.9,
                "signal_detail": "hp laptop with good condition",
            },
            msg="hp laptop with good condition",
        )
        need, field, _ = needs_confirm(draft)
        self.assertFalse(need)
        self.assertEqual(field, "")

    def test_kids_bicycle_skips_clothing_size_prompt(self) -> None:
        draft = draft_from_slots(
            {
                "linear_intent": "looking.swap",
                "confidence": 0.9,
                "signal_detail": "buy a bicycle for my kid",
            },
            msg="i wanna buy a bycycle for my kid",
        )
        need, field, _ = needs_confirm(draft)
        self.assertFalse(need)
        self.assertEqual(field, "")

    def test_rain_boots_swap_asks_kids_stage(self) -> None:
        draft = draft_from_slots(
            {
                "linear_intent": "looking.swap",
                "confidence": 0.9,
                "signal_detail": "rain boots",
            },
            msg="rain boots",
        )
        need, field, prompt = needs_confirm(draft)
        self.assertTrue(need)
        self.assertEqual(field, "stage")
        self.assertIn("kid", prompt.lower())

    def test_adult_rain_coat_skips_size_prompt(self) -> None:
        draft = draft_from_slots(
            {
                "linear_intent": "looking.swap",
                "confidence": 0.9,
                "signal_detail": "rain coat for adults",
            },
            msg="rain coat for adults",
        )
        need, field, _ = needs_confirm(draft)
        self.assertFalse(need)
        self.assertEqual(field, "")

    def test_normalize_size_answer_adults(self) -> None:
        self.assertEqual(normalize_size_answer("for adults"), "adult")
        self.assertEqual(normalize_size_answer("adults"), "adult")

    def test_swap_offer_detail_strips_giveup_verb(self) -> None:
        draft = draft_from_slots(
            {
                "linear_intent": "sharing.swap",
                "confidence": 0.9,
                "signal_detail": "give up my kids bicycle",
            },
            msg="i want to giveup my kids bicycle",
        )
        self.assertEqual(str(draft.get("detail") or ""), "kids bicycle")

    def test_adults_answer_advances_draft(self) -> None:
        draft = draft_from_slots(
            {
                "linear_intent": "looking.swap",
                "confidence": 0.9,
                "signal_detail": "rain coat",
            },
            msg="rain coat",
        )
        draft["phase"] = "signal_confirm_missing"
        draft["confirm_field"] = "stage"
        updated, prompt, ready = advance_signal_draft(draft, msg="for adults")
        self.assertTrue(ready)
        self.assertIsNone(prompt)
        self.assertIn("adult", str(updated.get("detail") or "").lower())

    def test_teacher_tip_infers_education_category(self) -> None:
        draft = draft_from_slots(
            {
                "linear_intent": "looking.tip",
                "confidence": 0.9,
                "signal_detail": "do you know good teacher for my kid?",
            },
            msg="do you know good teacher for my kid?",
        )
        need, field, _ = needs_confirm(draft)
        self.assertFalse(need)
        self.assertEqual(field, "")
        self.assertEqual(draft.get("category"), "education")

    def test_stroller_swap_skips_clothing_size_prompt(self) -> None:
        draft = draft_from_slots(
            {
                "linear_intent": "looking.swap",
                "confidence": 0.9,
                "signal_detail": "looking for a stroller",
            },
            msg="looking for a stroller",
        )
        need, field, _ = needs_confirm(draft)
        self.assertFalse(need)
        self.assertEqual(field, "")

    def test_pediatrician_tip_normalizes_detail(self) -> None:
        draft = draft_from_slots(
            {
                "linear_intent": "looking.tip",
                "confidence": 0.9,
            },
            msg="what do you have any tip for me ? like do you know a good pediatrician",
        )
        self.assertEqual(draft.get("category"), "health")
        self.assertIn("pediatrician", str(draft.get("detail") or "").lower())
        self.assertNotIn("what do you have", str(draft.get("detail") or "").lower())

    def test_tip_category_confirm_stays_tip_seek(self) -> None:
        draft = {
            "linear_intent": "looking.tip",
            "intent": "tip_seek",
            "detail": "good place near me",
            "phase": PHASE_SIGNAL_CONFIRM,
            "confirm_field": "category",
            "confirm_attempts": {},
        }
        updated, prompt, ready = advance_signal_draft(draft, msg="food")
        self.assertTrue(ready)
        self.assertIsNone(prompt)
        self.assertEqual(updated.get("intent"), "tip_seek")
        self.assertEqual(updated.get("category"), "food")


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


class TestSlotsIndicatePeerDiscovery(unittest.TestCase):
    def test_attr_filter_slots(self) -> None:
        from app.discovery_slots import slots_indicate_peer_discovery

        self.assertTrue(
            slots_indicate_peer_discovery(
                {
                    "linear_intent": "discovery.find_by_attrs",
                    "goal": "peers",
                    "attr_filter": "american moms",
                    "confidence": 0.9,
                }
            )
        )

    def test_identity_slots_not_discovery(self) -> None:
        from app.discovery_slots import slots_indicate_peer_discovery

        self.assertFalse(
            slots_indicate_peer_discovery(
                {
                    "linear_intent": "identity.add_claim",
                    "goal": "none",
                    "confidence": 0.9,
                }
            )
        )

    def test_no_slots_not_discovery(self) -> None:
        from app.discovery_slots import slots_indicate_peer_discovery

        self.assertFalse(slots_indicate_peer_discovery(None))
        self.assertFalse(slots_indicate_peer_discovery({}))


class TestSlotsPickingShownPeer(unittest.TestCase):
    def test_peer_name_in_session_cards(self) -> None:
        from app.discovery_slots import slots_picking_shown_peer

        session = {
            "peer_matches": [
                {"peer_user_id": "u1", "nickname": "Ada"},
                {"peer_user_id": "u2", "nickname": "Kashaf"},
            ]
        }
        self.assertTrue(
            slots_picking_shown_peer(
                {"goal": "peers", "peer_name": "Kashaf", "confidence": 0.8},
                session,
            )
        )

    def test_propose_intro_goal(self) -> None:
        from app.discovery_slots import slots_picking_shown_peer

        self.assertTrue(
            slots_picking_shown_peer(
                {
                    "goal": "propose_intro",
                    "linear_intent": "social.propose_intro",
                    "confidence": 0.9,
                },
                {},
            )
        )
