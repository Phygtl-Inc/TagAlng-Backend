"""Telemetry contract: full outcome enums, the lana_turn event, and the north-star.

QA (2026-07-08, 233 production turns) found routing.outcome returning bare letters
("T"/"R"/"A"), timing_ms always null, and no way to tell a dead-end turn from a
secured next step. These tests pin the fixed contract.
"""

import unittest

from app.turn_telemetry import (
    OUTCOME_NAMES,
    build_lana_turn_props,
    full_outcome,
    gate_info,
    north_star_secured,
)


class TestFullOutcome(unittest.TestCase):
    def test_letters_map_to_full_names(self) -> None:
        self.assertEqual(full_outcome("R"), "reply")
        self.assertEqual(full_outcome("A"), "ask")
        self.assertEqual(full_outcome("T"), "tool_call")
        self.assertEqual(full_outcome("C"), "capture")

    def test_lowercase_letter_still_maps(self) -> None:
        self.assertEqual(full_outcome("t"), "tool_call")

    def test_proper_values_pass_through_unchanged(self) -> None:
        for value in ("activity_browse", "pass_along", "tip_share", "look_meet", "rapport_answer"):
            self.assertEqual(full_outcome(value), value)

    def test_none_and_blank_stay_none(self) -> None:
        self.assertIsNone(full_outcome(None))
        self.assertIsNone(full_outcome(""))
        self.assertIsNone(full_outcome("   "))

    def test_unknown_letter_never_leaks_as_single_char(self) -> None:
        self.assertEqual(full_outcome("X"), "unknown")

    def test_every_mapped_name_is_multichar(self) -> None:
        for name in OUTCOME_NAMES.values():
            self.assertGreater(len(name), 1)


class TestOutcomeNeverSingleLetterAcrossDispatchPaths(unittest.TestCase):
    """The API boundary (_routing_from_ctx) must never emit a bare letter, whatever
    dispatch path stamped last_routing."""

    def _stubs(self) -> list[dict]:
        from app.discovery_route import _discovery_routing_stub
        from app.lana_dispatch import lana_unified_opening
        from app.main import _event_routing_stub, _profile_routing_stub

        return [
            # Discovery rails — the "T"(27x)/"A" letters QA saw in production.
            _discovery_routing_stub("preview", None),
            _discovery_routing_stub("preview", "lana_propose_neighbor_intro"),
            # Fast-path stubs ("R").
            _event_routing_stub(),
            _profile_routing_stub(),
            # Unified opening ("R" — 51x in QA's sample).
            lana_unified_opening()[2]["last_routing"],
            # hosting_cta-style literal ("A" — 2x in QA's sample).
            {"outcome": "A", "intent_class": "sharing", "tool_called": "hosting_open"},
            # Orchestrator capture path ("C").
            {"outcome": "C", "intent_class": "off_topic", "tool_to_call": "capture_inquiry", "capture_fired": True},
            # Already-proper unified-flow values must survive unchanged.
            {"outcome": "pass_along", "intent_class": "swap", "tool_called": "save_local_signal"},
            {"outcome": "activity_browse", "intent_class": "discovery", "tool_called": None},
        ]

    def test_boundary_outcomes_are_full_names(self) -> None:
        from app.main import _routing_from_ctx

        for stub in self._stubs():
            routing = _routing_from_ctx({"last_routing": stub})
            self.assertIsNotNone(routing, msg=f"stub={stub}")
            self.assertIsNotNone(routing.outcome, msg=f"stub={stub}")
            self.assertGreater(
                len(routing.outcome), 1, msg=f"single-letter outcome leaked: {stub}"
            )

    def test_proper_values_keep_backward_compatible_spelling(self) -> None:
        from app.main import _routing_from_ctx

        routing = _routing_from_ctx(
            {"last_routing": {"outcome": "pass_along", "intent_class": "swap"}}
        )
        self.assertEqual(routing.outcome, "pass_along")

    def test_tool_called_read_from_either_key(self) -> None:
        from app.main import _routing_from_ctx

        # Unified flows stamp "tool_called"; orchestrator stamps "tool_to_call".
        self.assertEqual(
            _routing_from_ctx(
                {"last_routing": {"outcome": "tip_share", "tool_called": "save_local_signal"}}
            ).tool_called,
            "save_local_signal",
        )
        self.assertEqual(
            _routing_from_ctx(
                {"last_routing": {"outcome": "T", "tool_to_call": "find_peers"}}
            ).tool_called,
            "find_peers",
        )

    def test_capture_fired_survives_to_payload(self) -> None:
        from app.main import _routing_from_ctx

        routing = _routing_from_ctx(
            {"last_routing": {"outcome": "C", "capture_fired": True}}
        )
        self.assertTrue(routing.capture_fired)


class TestNorthStarSecured(unittest.TestCase):
    def test_published_meet(self) -> None:
        self.assertEqual(north_star_secured({"event_published_now": True}), "published")
        self.assertEqual(north_star_secured({"ui_intent": "event_created"}), "published")

    def test_rsvp_ish_turn(self) -> None:
        self.assertEqual(north_star_secured({"event_joined_now": True}), "rsvp")

    def test_intro_sent(self) -> None:
        turn = {
            "last_routing": {"tool_to_call": "lana_propose_neighbor_intro"},
            "intro_proposal": {"status": "proposed"},
            "ui_intent": "propose_neighbor_intro",
        }
        self.assertEqual(north_star_secured(turn), "intro_sent")

    def test_duplicate_intro_is_not_secured(self) -> None:
        turn = {
            "last_routing": {"tool_to_call": "lana_propose_neighbor_intro"},
            "intro_proposal": {"status": "duplicate"},
        }
        self.assertIsNone(north_star_secured(turn))

    def test_listen_alert_saved(self) -> None:
        self.assertEqual(
            north_star_secured({"ui_intent": "look_meet_saved"}), "signal_saved"
        )
        self.assertEqual(
            north_star_secured({"tip_listed_now": True}), "signal_saved"
        )
        self.assertEqual(
            north_star_secured({"signal_saved": {"signal_id": "sig-1"}}), "signal_saved"
        )

    def test_waitlist_join(self) -> None:
        self.assertEqual(north_star_secured({"waitlist_joined_now": True}), "waitlist")

    def test_dead_end_turn_is_null(self) -> None:
        turn = {
            "last_routing": {"outcome": "R", "intent_class": "companionship"},
            "ui_intent": "chat",
            "routing_phase": "listening",
        }
        self.assertIsNone(north_star_secured(turn))

    def test_verify_gated_rsvp_attempt_is_not_secured(self) -> None:
        # The QA-invisible case: user asked to RSVP, hit the verify gate instead.
        turn = {
            "last_routing": {"outcome": "A", "intent_class": "discovery"},
            "routing_phase": "gate_verify",
            "requires_phone_verification": True,
            "ui_intent": "collect_email",
        }
        self.assertIsNone(north_star_secured(turn))
        self.assertEqual(gate_info(turn), (True, "verify"))


class TestLanaTurnEventPayload(unittest.TestCase):
    def _props(self, merged_ctx: dict, ui_intent: str, latency: int = 1234) -> dict:
        return build_lana_turn_props(
            session_id="sess-1",
            turn_index=3,
            merged_ctx=merged_ctx,
            ui_intent=ui_intent,
            latency_ms=latency,
            block_resolved=True,
        )

    def test_payload_shape(self) -> None:
        props = self._props(
            {
                "last_routing": {"outcome": "T", "intent_class": "discovery", "tool_to_call": "find_peers"},
                "lang": "es",
            },
            ui_intent="show_peer_preview",
        )
        self.assertEqual(
            set(props),
            {
                "session_id",
                "turn_index",
                "ui_intent",
                "intent",
                "outcome",
                "secured_step",
                "gate_shown",
                "gate_type",
                "block_resolved",
                "lang",
                "latency_ms",
            },
        )
        self.assertEqual(props["session_id"], "sess-1")
        self.assertEqual(props["turn_index"], 3)
        self.assertEqual(props["outcome"], "tool_call")  # full name, never "T"
        self.assertEqual(props["intent"], "discovery")
        self.assertEqual(props["lang"], "es")
        self.assertEqual(props["latency_ms"], 1234)
        self.assertIs(props["block_resolved"], True)
        self.assertIs(props["gate_shown"], False)

    def test_secured_step_on_save_turn(self) -> None:
        props = self._props(
            {
                "last_routing": {"outcome": "look_meet", "intent_class": "discovery"},
                "look_meet_saved_now": True,
            },
            ui_intent="look_meet_saved",
        )
        self.assertEqual(props["secured_step"], "signal_saved")
        self.assertEqual(props["outcome"], "look_meet")

    def test_dead_end_turn_has_null_secured_step(self) -> None:
        props = self._props(
            {"last_routing": {"outcome": "R", "intent_class": "companionship"}},
            ui_intent="chat",
        )
        self.assertIsNone(props["secured_step"])
        self.assertEqual(props["outcome"], "reply")

    def test_gate_shown_with_type(self) -> None:
        props = self._props(
            {
                "last_routing": {"outcome": "A", "intent_class": "discovery"},
                "routing_phase": "await_signup_otp",
            },
            ui_intent="collect_otp",
        )
        self.assertIs(props["gate_shown"], True)
        self.assertEqual(props["gate_type"], "signup_otp")


class TestTimingPopulated(unittest.TestCase):
    def test_turn_payload_carries_timing(self) -> None:
        from app.models import SendMessageResponse
        from app.turn_timing import TurnTimer

        timer = TurnTimer()
        timer.add("llm_router", 42)
        resp = SendMessageResponse(
            session_id="sess-1",
            status="continue",
            assistant_message="hi",
            timing_ms=timer.to_dict(),
        )
        self.assertIsNotNone(resp.timing_ms)
        self.assertEqual(resp.timing_ms["llm_router"], 42)
        self.assertGreaterEqual(resp.timing_ms["total_ms"], 42)
        # And it serializes onto the wire (the field QA found always-null).
        self.assertIn("timing_ms", resp.model_dump())
        self.assertEqual(resp.model_dump()["timing_ms"]["total_ms"], 42)


if __name__ == "__main__":
    unittest.main()
