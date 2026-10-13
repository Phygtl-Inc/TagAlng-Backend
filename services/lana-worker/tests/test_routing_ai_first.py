"""The classifier decides; keyword rules only fill in when it did not.

Pins the AI-first routing fixes of 2026-10-14 (Pouya's intent-classification review):
safety reads survive every keyword rule at any confidence, and the tip-share keyword no
longer overrules a confident pick of another lane."""

from __future__ import annotations

import pytest

from app.layer1_intents import enrich_slots, slots_linear_intent


@pytest.mark.parametrize(
    ("label", "goal", "msg"),
    [
        # phrase fallback: _EDIT_CLAIM_RE matches "delete"
        ("system.crisis", "crisis", "I just want to delete myself, nobody would notice"),
        # phrase fallback: _CHANGE_NAME_RE matches "call me"
        ("system.crisis", "crisis", "call me a failure, I can't do this anymore"),
        # hosting reconciler: want-verb + event noun
        ("system.crisis", "crisis", "I want to plan a party but honestly I can't go on anymore"),
        # tip-share reconciler
        ("system.medical", "medical", "Dr Smith is a great dentist but my chest hurts badly"),
        ("system.unsafe", "unsafe", "I want to host a party and get everyone drunk without them knowing"),
    ],
)
@pytest.mark.parametrize("conf", [0.3, 0.55])
def test_a_safety_read_is_never_rewritten(label, goal, msg, conf) -> None:
    out = enrich_slots({"linear_intent": label, "confidence": conf}, msg=msg)
    assert slots_linear_intent(out) == label
    assert out["goal"] == goal
    assert "signal_intent" not in out


def test_a_safety_goal_without_a_label_is_kept() -> None:
    out = enrich_slots({"goal": "crisis", "confidence": 0.4}, msg="call me, I need help, I can't go on")
    assert out["linear_intent"] == "system.crisis"
    assert out["goal"] == "crisis"


def test_the_crisis_detector_sees_a_low_confidence_crisis() -> None:
    from app.discovery_route import _is_crisis_turn

    slots = {"linear_intent": "system.crisis", "goal": "crisis", "confidence": 0.55}
    assert _is_crisis_turn(slots, "I just want to delete myself, nobody would notice")


def test_ordinary_turns_still_enrich() -> None:
    out = enrich_slots({"linear_intent": "discovery.find_peers", "confidence": 0.3},
                       msg="change my name to Sam")
    assert out["linear_intent"] == "settings.change_name"


# ── tip-share keyword ────────────────────────────────────────────────────────


def test_the_tip_share_keyword_does_not_overrule_a_confident_other_lane() -> None:
    from app.lana_unified_pipeline import _turn_is_tip_share

    for linear in ("discovery.find_peers", "sharing.host", "discovery.communities"):
        slots = {"linear_intent": linear, "confidence": 0.9}
        assert not _turn_is_tip_share(slots, "Dr Smith is a great dentist"), linear


def test_the_tip_share_keyword_still_fills_in_when_unsure_or_unread() -> None:
    from app.lana_unified_pipeline import _turn_is_tip_share

    assert _turn_is_tip_share({"linear_intent": "discovery.find_peers", "confidence": 0.4},
                              "Dr Smith is a great dentist")
    assert _turn_is_tip_share(None, "Dr Smith is a great dentist")


def test_the_tip_share_keyword_does_not_overrule_a_clarifying_model() -> None:
    from app.lana_unified_pipeline import _turn_is_tip_share

    asking = {"linear_intent": "", "confidence": 0.3, "clarify": "Recommending, or looking?"}
    assert not _turn_is_tip_share(asking, "Dr Smith is a great dentist")
