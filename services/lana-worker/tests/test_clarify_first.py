"""When the model chooses to ask, the user gets its question with options.

Dev 2026-10-16: "not sure, what do you think?" and "help" were classified with clarify
set, but decide_turn runs ahead of the clarify step and answered in prose, with no
options. A clarify read now belongs to the engine — except on the reply to a question
Lana just asked, which re-classifies normally so the two can't loop.
"""

from unittest.mock import patch

import pytest

from app.discovery_slots import _alternative_intent
from app.lana_unified_pipeline import _turn_is_engine_action
from app.routing_log import build_routing_record


def _engine(slots: dict, ctx: dict | None = None) -> bool:
    with patch("app.discovery_slots.discovery_slots_for_turn", return_value=slots):
        return _turn_is_engine_action(
            {"routing_phase": "listening", **(ctx or {})},
            "not sure, what do you think?",
            history=[],
            home_block_id="block-a",
            phone_verified=True,
        )


@pytest.mark.parametrize("kind", ["intent", "browse_or_meet", "scope"])
def test_a_clarify_read_skips_the_policy(kind):
    assert _engine({"goal": "chat", "confidence": 0.55, "clarify": kind}) is True


def test_no_clarify_stays_with_the_policy():
    assert _engine({"goal": "chat", "confidence": 0.9}) is False


def test_the_reply_to_a_clarify_is_not_asked_again():
    assert _engine({"goal": "chat", "confidence": 0.55, "clarify": "intent"},
                   {"clarify_pending": True}) is False


def test_leaving_never_becomes_a_question():
    assert _engine({"goal": "chat", "confidence": 0.5, "clarify": "intent", "abandon": True}) is False


def test_alternative_intent_is_a_real_different_label_or_nothing():
    assert _alternative_intent(
        {"linear_intent": "discovery.find_activities", "alternative_intent": "sharing.host"}
    ) == "sharing.host"
    assert _alternative_intent(
        {"linear_intent": "sharing.host", "alternative_intent": "sharing.host"}
    ) is None
    assert _alternative_intent({"linear_intent": "sharing.host", "alternative_intent": "bananas"}) is None
    assert _alternative_intent({"linear_intent": "sharing.host"}) is None


def test_the_runner_up_is_on_the_routing_log():
    msg = "any pottery events near me? or maybe I'll make my own"
    slots = {"linear_intent": "discovery.find_activities", "confidence": 0.7,
             "alternative_intent": "sharing.host"}
    ctx = {"_discovery_slots": slots, "_discovery_slots_for": msg, "routing_phase": "listening"}
    rec = build_routing_record(ctx, user_message=msg, intent_hint=None, prev_routing=None)
    assert rec["ai_alternative"] == "sharing.host"


def test_no_capture_flow_opens_on_a_turn_the_model_is_asking_about():
    """"I just moved here … baby stuff to get rid of": clarify=intent, label sharing.swap.
    The give-away flow opened ahead of the three-option question (dev 2026-10-19)."""
    from app.lana_unified_pipeline import (
        _turn_is_community_create,
        _turn_is_pass_along,
        _turn_is_tip_share,
    )

    asking = {"linear_intent": "sharing.swap", "ai_linear_intent": "sharing.swap",
              "confidence": 0.7, "ai_confidence": 0.7, "clarify": "intent"}
    msg = "I just moved here, kids are bored and I have a ton of baby stuff to get rid of"
    assert _turn_is_pass_along(asking, msg) is False
    assert _turn_is_tip_share({**asking, "linear_intent": "sharing.tip"}, msg) is False
    assert _turn_is_community_create({**asking, "linear_intent": "sharing.community"}, msg) is False
    # Without the question the same read opens the flow, as before.
    assert _turn_is_pass_along({k: v for k, v in asking.items() if k != "clarify"}, msg) is True
