"""A person in pain right now is not a rapport opportunity.

QA 2026-08-03: "i didnt get much sleep in the car", "my stomach was hurting" got
"is there a favorite blue thing that lifts your mood?" — three times, worded
differently each time. The colour was the third-worst thing about it. The real
fault: on that turn every move left on the policy's menu was either a question
about their profile or a capability pitch, and the dead-end backstop rejects any
turn that ends without a question — so care was not a shippable output.

These cover the two properties that make the gate safe rather than a mode:
  * it never fires on boredom / low energy / a request (the user's own worry:
    "what if it starts doing it in normal things");
  * a bridge is DEFERRED, not dropped — the offer lands one turn later.
"""

import unittest
from unittest.mock import patch

from app.policy.decide import (
    NextAction, _apply_distress_gate, apply_defer, ask_streak, note_ask_streak,
    parse_next_action,
)


def _action(**kw):
    base = {"kind": "reply", "utterance": "…", "distress_turn": True}
    base.update(kw)
    return NextAction(**base)


class TestDistressGateDowngrades(unittest.TestCase):
    def test_profile_question_becomes_a_plain_reply(self) -> None:
        gated = _apply_distress_gate(
            _action(
                kind="ask_gap",
                utterance="That sounds rough. Is there a favorite blue thing that lifts you?",
                goal_id="gap:row1",
                chips=[{"label": "Kind of", "send": "Kind of"}],
            )
        )
        self.assertEqual(gated.kind, "reply")
        self.assertIsNone(gated.goal_id)
        self.assertEqual(gated.chips, [])

    def test_place_ask_becomes_a_plain_reply(self) -> None:
        gated = _apply_distress_gate(
            _action(kind="ground_place", goal_id="circle:gym", pending_action="host_meet")
        )
        self.assertEqual(gated.kind, "reply")
        self.assertIsNone(gated.pending_action)

    def test_offer_is_deferred_not_dropped(self) -> None:
        """The whole point of capture_defer over a ban: the offer still happens,
        one turn later, once they've been heard."""
        gated = _apply_distress_gate(
            _action(
                kind="bridge_offer",
                utterance="Want me to set up a steakhouse night nearby?",
                goal_id="cap:sharing.host",
                chips=[{"label": "Yes", "send": "Yes"}],
            )
        )
        self.assertEqual(gated.kind, "capture_defer")
        self.assertEqual(gated.defer_goal_id, "cap:sharing.host")
        self.assertEqual(gated.chips, [])

        # …and the deferred goal really does come back as fair game next turn.
        ctx: dict = {}
        apply_defer(ctx, gated)
        self.assertEqual(ctx["deferred_goal_ids"], ["cap:sharing.host"])

    def test_handoff_is_untouched(self) -> None:
        """Safety rails and action engines own their turns — including the
        medical rail, which must still get the turn it is designed for."""
        gated = _apply_distress_gate(_action(kind="handoff", utterance=""))
        self.assertEqual(gated.kind, "handoff")

    def test_downgrade_clears_the_ask_streak(self) -> None:
        ctx = {"policy_ask_streak": 2}
        note_ask_streak(ctx, _apply_distress_gate(_action(kind="ask_gap", goal_id="gap:row1")))
        self.assertEqual(ask_streak(ctx), 0)


class TestGateStaysOffNormalTurns(unittest.TestCase):
    """The user's fear, as tests: "I'm bored" must keep acting like today."""

    def test_boredom_keeps_its_offer_and_chips(self) -> None:
        offer = NextAction(
            kind="bridge_offer",
            utterance="Fancy a run with neighbours at Lake Nona?",
            goal_id="cap:sharing.host",
            chips=[{"label": "Yes, set it up", "send": "Yes, set it up"}],
            distress_turn=False,
        )
        gated = _apply_distress_gate(offer)
        self.assertEqual(gated.kind, "bridge_offer")
        self.assertEqual(len(gated.chips), 1)
        self.assertEqual(gated.goal_id, "cap:sharing.host")

    def test_low_energy_keeps_its_gentle_question(self) -> None:
        ask = NextAction(
            kind="ask_gap", utterance="Long weeks are the worst. What's your usual unwind?",
            goal_id="gap:row9", distress_turn=False,
        )
        self.assertEqual(_apply_distress_gate(ask).kind, "ask_gap")

    def test_pain_as_a_footnote_keeps_the_thread(self) -> None:
        """"I was running a competition so my foot hurts" — the subject is the
        RACE, and "runs competitively" is the most matchable thing they've said
        all session. Gating this would trade their news for silence, so the
        prompt reads what they BROUGHT (a share, pain as a footnote) rather than
        matching on the word "hurts". Here the model has judged it not-distress;
        this asserts the gate then keeps its hands off."""
        follow_up = NextAction(
            kind="ask_gap",
            utterance="Ouch — hope the foot eases up. How did the race go?",
            goal_id="gap:running",
            chips=[{"label": "It went well", "send": "It went well"}],
            distress_turn=False,
        )
        gated = _apply_distress_gate(follow_up)
        self.assertEqual(gated.kind, "ask_gap")
        self.assertEqual(gated.goal_id, "gap:running")
        self.assertEqual(len(gated.chips), 1)

    def test_stringy_false_is_not_truthy(self) -> None:
        """The model emits JSON; "false" must not gate the turn."""
        for raw in ("false", "no", "False", ""):
            action = parse_next_action(
                {"kind": "ask_gap", "utterance": "What's your usual unwind?",
                 "distress_turn": raw}
            )
            assert action is not None
            self.assertFalse(action.distress_turn, raw)

    def test_distress_true_parses(self) -> None:
        action = parse_next_action(
            {"kind": "reply", "utterance": "I hope you get some rest.",
             "distress_turn": True}
        )
        assert action is not None
        self.assertTrue(action.distress_turn)


_PAYLOAD_BASE = {
    "world": {}, "candidate_goals": [], "recent_turns": [],
}


def _decide(raw_actions):
    """Run decide_turn with the LLM stubbed to return `raw_actions` in order.
    Returns (action, call_count) — call_count 2 means the dead-end backstop
    fired its corrective retry."""
    calls = {"n": 0}

    def _llm_json(**_kw):
        i = min(calls["n"], len(raw_actions) - 1)
        calls["n"] += 1
        return raw_actions[i]

    from app.policy import decide as mod

    with patch.object(mod, "_system_prompt", return_value="sys"), \
         patch("app.orchestrator.llm.llm_configured", return_value=True), \
         patch("app.orchestrator.llm.synthesizer_model", return_value="m"), \
         patch("app.orchestrator.llm.llm_json", side_effect=_llm_json), \
         patch("app.policy.world.world_state", return_value={"states": []}), \
         patch("app.policy.goals.candidate_goals", return_value=[]):
        action = mod.decide_turn(
            user_id="u1", session_ctx={}, history=[], user_message="i'm wiped, no sleep",
        )
    return action, calls["n"]


class TestDeadEndBackstopExemption(unittest.TestCase):
    def test_distress_reply_survives_without_a_question(self) -> None:
        """The gate is only real if silence ships. Without the exemption the
        backstop re-prompts and a question comes straight back."""
        action, calls = _decide([
            {"kind": "reply", "distress_turn": True, "why": "in pain now",
             "utterance": "That sounds miserable. I hope you get some real rest tonight."},
        ])
        assert action is not None
        self.assertEqual(action.kind, "reply")
        self.assertEqual(calls, 1, "backstop must not retry a distress turn")
        self.assertNotIn("?", action.utterance)

    def test_ordinary_dead_end_still_gets_its_retry(self) -> None:
        """The 2026-07-30 backstop must keep working for everyone else."""
        action, calls = _decide([
            {"kind": "reply", "utterance": "Thanks for sharing your go-to spot."},
            {"kind": "ask_gap", "utterance": "Which nights are you usually free?"},
        ])
        assert action is not None
        self.assertEqual(calls, 2)
        self.assertEqual(action.kind, "ask_gap")


if __name__ == "__main__":
    unittest.main()
