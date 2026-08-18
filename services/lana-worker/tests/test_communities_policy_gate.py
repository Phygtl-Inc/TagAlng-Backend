"""A communities ask belongs to its engine, and an accepted pitch must run.

QA 2026-08-18, "can u show me communities around me":
  1. answered "Yep — I can look for people and activities around you. Want me to
     find nearby people who share one of your interests?" — decide_turn runs ahead
     of the engines and the gate had no escape for discovery.communities, so
     communities_chat_turn never ran (capability_index had no communities row
     either, so find_peers was the closest thing on the policy's menu);
  2. tapping the offered "Find nearby people" chip re-offered the same thing,
     twice — a policy pitch can only be RUN by an engine, but the accept stayed
     with the policy;
  3. the search that finally ran returned one neighbor whose card read "✓ Sent"
     under prose asking "Want an intro?".
"""

import unittest
from unittest.mock import patch

from app.lana_unified_pipeline import (
    POLICY_ENGINE_ONLY_INTENTS,
    _policy_pitch_accepted,
    _turn_is_engine_action,
    policy_pitch_for,
)
from app.policy.decide import NextAction


def _slots(intent: str, confidence: float) -> dict:
    return {"linear_intent": intent, "goal": "chat", "confidence": confidence}


class TestCommunitiesKeptOffPolicy(unittest.TestCase):
    def _is_engine_action(self, slots: dict) -> bool:
        with patch("app.discovery_slots.discovery_slots_for_turn", return_value=slots):
            return _turn_is_engine_action(
                {"routing_phase": "listening"},
                "can u show me communities around me",
                history=[],
                home_block_id="block-a",
                phone_verified=True,
            )

    def test_communities_ask_escapes_the_policy(self) -> None:
        self.assertTrue(self._is_engine_action(_slots("discovery.communities", 0.95)))

    def test_below_the_handlers_own_bar_stays_with_the_policy(self) -> None:
        # Escaping under discovery.communities' 0.85 threshold would leave the turn to
        # neither the policy nor the layer-1 handler.
        self.assertFalse(self._is_engine_action(_slots("discovery.communities", 0.6)))

    def test_conversational_turns_still_belong_to_the_policy(self) -> None:
        self.assertFalse(self._is_engine_action({"goal": "chat", "confidence": 0.9}))

    def test_escape_list_is_the_registry(self) -> None:
        from app.layer1_intents import LINEAR_INTENTS

        self.assertIn("discovery.communities", POLICY_ENGINE_ONLY_INTENTS)
        # Every escape must name a real intent, or it silently never fires.
        self.assertTrue(POLICY_ENGINE_ONLY_INTENTS <= LINEAR_INTENTS)


class TestPolicyPitchAccept(unittest.TestCase):
    CHIP = "find nearby people who share one of my interests"

    def _ctx(self) -> dict:
        return {"policy_pitch": "discovery.find_peers", "policy_chip_msgs": [self.CHIP]}

    def test_tap_on_a_pitch_leaves_the_policy(self) -> None:
        self.assertTrue(_policy_pitch_accepted(self._ctx(), self.CHIP))

    def test_accept_converts_once(self) -> None:
        ctx = self._ctx()
        self.assertTrue(_policy_pitch_accepted(ctx, self.CHIP))
        # Cleared with None, never popped — a popped key resurrects on the session merge.
        self.assertIn("policy_pitch", ctx)
        self.assertIsNone(ctx["policy_pitch"])
        self.assertFalse(_policy_pitch_accepted(ctx, self.CHIP))

    def test_typed_reply_that_is_not_the_chip_stays_with_the_policy(self) -> None:
        self.assertFalse(_policy_pitch_accepted(self._ctx(), "what else can you do?"))

    def test_conversational_chip_stays_with_the_policy(self) -> None:
        # Nothing was pitched (a rapport answer, a place-grounding tap) — the policy
        # owns those chips.
        ctx = {"policy_pitch": None, "policy_chip_msgs": ["Sports"]}
        self.assertFalse(_policy_pitch_accepted(ctx, "Sports"))


class TestPolicyPitchArming(unittest.TestCase):
    def _chip(self) -> list[dict[str, str]]:
        return [{"label": "Find nearby people", "send": "find nearby people"}]

    def test_capability_pitch_is_armed_by_id(self) -> None:
        action = NextAction(
            kind="bridge_offer", utterance="Want me to?", chips=self._chip(),
            goal_id="cap:discovery.find_peers",
        )
        self.assertEqual(policy_pitch_for(action), "discovery.find_peers")

    def test_pitch_without_a_goal_id_is_still_armed(self) -> None:
        # audit_offer_goal exists because bridge_offer ships goal_id=None often enough to
        # matter (prod 2026-08-06 pitched sharing.host with none). The accept must escape
        # whether or not the offer labelled itself.
        action = NextAction(kind="bridge_offer", utterance="Want me to?", chips=self._chip())
        self.assertEqual(policy_pitch_for(action), "bridge_offer")

    def test_conversational_turns_arm_nothing(self) -> None:
        for kind in ("reply", "ask_gap", "ground_place", "capture_defer", "follow_thread"):
            with self.subTest(kind=kind):
                action = NextAction(
                    kind=kind, utterance="What's your gym?", chips=self._chip(),
                    goal_id="gap:row-1",
                )
                self.assertIsNone(policy_pitch_for(action))

    def test_a_pitch_with_no_chip_arms_nothing(self) -> None:
        action = NextAction(
            kind="bridge_offer", utterance="Want me to?", goal_id="cap:sharing.host",
        )
        self.assertIsNone(policy_pitch_for(action))


class TestIntroAlreadySent(unittest.TestCase):
    """Prose must not offer the one thing the card says is already done."""

    def _reply(self, peers: list[dict]) -> dict:
        from app import layer1_handlers

        seen: dict = {}

        def fake_compose(**kwargs):
            seen.update(kwargs)
            return str(kwargs.get("fallback") or "")

        with patch.object(layer1_handlers, "compose_reply", side_effect=fake_compose):
            seen["reply"] = layer1_handlers.format_attr_peers_reply(
                peers, filter_text="sports"
            )
        return seen

    def test_no_intro_offered_when_every_match_already_has_one(self) -> None:
        seen = self._reply([{"nickname": "Daniel", "connection": "intro_sent"}])
        self.assertIn("Intros already sent and awaiting a reply: 1 of 1", seen["facts"])
        self.assertIn("Never offer an intro", seen["goal"])
        self.assertNotIn("introduce you", seen["reply"])

    def test_open_row_still_gets_the_intro_offer(self) -> None:
        seen = self._reply([{"nickname": "Daniel"}])
        self.assertNotIn("Never offer an intro", seen["goal"])
        self.assertIn("introduce you", seen["reply"])

    def test_mixed_list_still_offers_for_the_new_rows(self) -> None:
        seen = self._reply(
            [{"nickname": "Daniel", "connection": "intro_sent"}, {"nickname": "Sofia"}]
        )
        self.assertIn("introduce you", seen["reply"])


class TestIntroStateStampedAtTheSource(unittest.TestCase):
    """The reply writer needs the fact the Nudge button has, before it composes."""

    def test_nudge_tier_row_is_stamped_and_connected_row_dropped(self) -> None:
        from app import peer_discovery_surface

        rows = [{"peer_user_id": "p1"}, {"peer_user_id": "p2"}]
        with patch.object(
            peer_discovery_surface,
            "peer_tiers",
            return_value={"p1": "nudge", "p2": "direct"},
        ):
            kept = peer_discovery_surface.drop_connected_peers(rows, user_id="u1")

        self.assertEqual([r["peer_user_id"] for r in kept], ["p1"])
        self.assertEqual(kept[0]["connection"], "intro_sent")

    def test_stranger_rows_are_left_alone(self) -> None:
        from app import peer_discovery_surface

        rows = [{"peer_user_id": "p1"}]
        with patch.object(
            peer_discovery_surface, "peer_tiers", return_value={"p1": "stranger"}
        ):
            kept = peer_discovery_surface.drop_connected_peers(rows, user_id="u1")

        self.assertEqual(kept, [{"peer_user_id": "p1"}])


if __name__ == "__main__":
    unittest.main()
