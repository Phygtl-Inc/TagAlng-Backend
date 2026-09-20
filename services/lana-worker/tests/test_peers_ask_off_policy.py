"""An explicit peers search is the engine's turn, not decide_turn's.

Prod 2026-09-18, session 3a1b4699…: "find me neighbors nearby" was classified
discovery.find_peers at 0.95 — the classifier even wrote the progress card
("Finding neighbors nearby") — and the policy answered it with

    "Got you — I can look for nearby people who share your interests, and
     badminton is already one strong thread here. Want me to find neighbors
     around here who play too?"          [ Find neighbors who play ]

    last_routing.why = "They asked to find neighbors nearby; offering the peer
                        search is the best available action and keeps it concrete."
    peer_matches = null, active_intent = null, routing_phase = "listening"

A confirmation of a request the user had already made, costing them a turn while
the search sat right there. Same bug `discovery.find_activities` was escaped for
on 2026-09-02.

The other half of this file is the part that must NOT change: bridging. The policy
pitching peers to someone who did not ask is the feature, not the bug, so every
conversational shape below has to stay on the policy.
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.lana_unified_pipeline import _turn_is_engine_action  # noqa: E402


def _gate(slots: dict, msg: str = "find me neighbors nearby", ctx: dict | None = None) -> bool:
    """True when the turn belongs to the engines rather than to decide_turn."""
    with patch("app.discovery_slots.discovery_slots_for_turn", return_value=slots):
        return _turn_is_engine_action(
            ctx if ctx is not None else {"routing_phase": "listening"},
            msg,
            history=[],
            home_block_id="block-a",
            phone_verified=True,
        )


class ExplicitPeersSearchEscapesThePolicy(unittest.TestCase):
    def test_the_prod_turn(self) -> None:
        """The exact slots prod stored for "find me neighbors nearby"."""
        self.assertTrue(
            _gate({"linear_intent": "discovery.find_peers", "goal": "peers",
                   "confidence": 0.95, "explicit_request": True})
        )

    def test_at_the_intents_own_bar(self) -> None:
        """0.75 is discovery.find_peers' threshold — the engine's bar, not a second one."""
        self.assertTrue(
            _gate({"linear_intent": "discovery.find_peers", "goal": "peers",
                   "confidence": 0.75, "explicit_request": True})
        )

    def test_below_the_bar_stays_with_the_policy(self) -> None:
        """An unsure read must not be diverted — the policy can still ask or pitch."""
        self.assertFalse(
            _gate({"linear_intent": "discovery.find_peers", "goal": "peers",
                   "confidence": 0.60, "explicit_request": True})
        )


class BridgingStaysWithThePolicy(unittest.TestCase):
    """Turns where the user did NOT ask for a search. These are the policy's whole job:
    acknowledge, bridge, offer peers with a chip. None may be diverted."""

    def test_lonely(self) -> None:
        self.assertFalse(
            _gate(
                {"linear_intent": "companionship.chat", "goal": "chat", "confidence": 0.9},
                msg="it's been really quiet since we moved here",
            )
        )

    def test_rapport_answer(self) -> None:
        self.assertFalse(
            _gate(
                {"linear_intent": "identity.add_claim", "goal": "chat", "confidence": 0.92},
                msg="I play badminton on Tuesdays",
            )
        )

    def test_bare_chat(self) -> None:
        self.assertFalse(_gate({"goal": "chat", "confidence": 0.9}, msg="how are you today"))

    def test_help_ask(self) -> None:
        self.assertFalse(
            _gate(
                {"linear_intent": "help.what_can_you_do", "goal": "chat", "confidence": 0.95},
                msg="what can you do",
            )
        )


class ExpressedNeedIsNotAnAsk(unittest.TestCase):
    """The measured collision: these classify discovery.find_peers at the SAME
    confidence as a direct order, and they are the policy's whole job. Only
    `explicit_request` tells them apart."""

    def test_we_dont_know_anyone_here_yet(self) -> None:
        self.assertFalse(
            _gate(
                {"linear_intent": "discovery.find_peers", "goal": "peers",
                 "confidence": 0.95, "explicit_request": False},
                msg="we don't know anyone here yet",
            )
        )

    def test_i_wish_i_knew_more_people(self) -> None:
        self.assertFalse(
            _gate(
                {"linear_intent": "discovery.find_peers", "goal": "peers",
                 "confidence": 0.95, "explicit_request": False},
                msg="i wish i knew more people around here",
            )
        )

    def test_missing_signal_keeps_todays_behaviour(self) -> None:
        """A model that omits the field must not divert the turn — fail safe."""
        self.assertFalse(
            _gate({"linear_intent": "discovery.find_peers", "goal": "peers", "confidence": 0.95})
        )

    def test_attribute_search_needs_it_too(self) -> None:
        """"find neighbors who play badminton" is the same ask with the trait named."""
        self.assertTrue(
            _gate(
                {"linear_intent": "discovery.find_by_attrs", "goal": "peers",
                 "confidence": 0.95, "explicit_request": True},
                msg="find neighbors who play badminton",
            )
        )
        self.assertFalse(
            _gate(
                {"linear_intent": "discovery.find_by_attrs", "goal": "peers",
                 "confidence": 0.95, "explicit_request": False},
                msg="badminton is big around here I think",
            )
        )

    def test_the_scoping_is_peers_only(self) -> None:
        """The other escapes keep the condition they shipped with: no explicit_request
        requirement, so a model that omits it cannot take a working turn away."""
        for intent, msg in (
            ("discovery.find_activities", "can u find activites aroung me"),
            ("discovery.communities", "can u show me communities around me"),
        ):
            with self.subTest(intent=intent):
                self.assertTrue(
                    _gate({"linear_intent": intent, "goal": "peers", "confidence": 0.9}, msg=msg)
                )


class NeighbouringLanesUnchanged(unittest.TestCase):
    """The other members of the escape set, and the intents deliberately outside it."""

    def test_recommendation_ask_still_escapes(self) -> None:
        self.assertTrue(
            _gate(
                {"linear_intent": "looking.tip", "signal_intent": "tip_seek", "confidence": 0.9},
                msg="recommend me a doctor nearby",
            )
        )

    def test_recommendation_share_still_escapes(self) -> None:
        self.assertTrue(
            _gate(
                {"linear_intent": "sharing.tip", "signal_intent": "tip_share", "confidence": 0.9},
                msg="Dr. Sarah in Lake Nona is so gentle with my toddler",
            )
        )

    def test_activities_still_escapes(self) -> None:
        self.assertTrue(
            _gate(
                {"linear_intent": "discovery.find_activities", "goal": "activities", "confidence": 0.9},
                msg="can u find activites aroung me",
            )
        )

    def test_communities_still_escapes(self) -> None:
        self.assertTrue(
            _gate(
                {"linear_intent": "discovery.communities", "goal": "peers", "confidence": 0.9},
                msg="can u show me communities around me",
            )
        )

    def test_tip_seek_hint_still_wins(self) -> None:
        """A deterministic entry (Find fork CTA / lit category chip) outranks the
        classifier — "show all" from the Professionals screen is not a peers search."""
        self.assertTrue(
            _gate(
                {"linear_intent": "discovery.find_peers", "goal": "peers",
                 "confidence": 0.95, "explicit_request": True},
                msg="show all",
                ctx={"routing_phase": "listening", "tip_seek_hint": True},
            )
        )

    def test_classifier_failure_fails_closed(self) -> None:
        """No verdict ⇒ the gate is left exactly as it was, not opened for everything."""
        with patch(
            "app.discovery_slots.discovery_slots_for_turn", side_effect=RuntimeError("boom")
        ):
            self.assertFalse(
                _turn_is_engine_action(
                    {"routing_phase": "listening"},
                    "find me neighbors nearby",
                    history=[],
                    home_block_id="block-a",
                    phone_verified=True,
                )
            )


class TheSetItself(unittest.TestCase):
    def test_peers_is_in_the_escape_set(self) -> None:
        """Pin it: this is the assertion that fails if peers falls back out."""
        from app.lana_unified_pipeline import POLICY_ENGINE_ONLY_INTENTS

        self.assertIn("discovery.find_peers", POLICY_ENGINE_ONLY_INTENTS)
        self.assertIn("discovery.find_activities", POLICY_ENGINE_ONLY_INTENTS)
        self.assertIn("discovery.communities", POLICY_ENGINE_ONLY_INTENTS)
        self.assertIn("discovery.find_by_attrs", POLICY_ENGINE_ONLY_INTENTS)

    def test_only_the_peer_searches_need_explicit_request(self) -> None:
        from app.lana_unified_pipeline import EXPLICIT_REQUEST_REQUIRED

        self.assertEqual(
            EXPLICIT_REQUEST_REQUIRED,
            frozenset({"discovery.find_peers", "discovery.find_by_attrs"}),
        )


if __name__ == "__main__":
    unittest.main()
