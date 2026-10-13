"""AI-first routing: a keyword match never outranks a confident model, and the confidence
it leaves behind is the model's own (no 0.85 bump). Plus the guard that keeps new code
from calling a keyword matcher directly, and the per-turn routing record."""

from __future__ import annotations

import ast
import collections
import pathlib

import pytest

from app.layer1_intents import enrich_slots, keyword_may_decide
from app.lana_unified_pipeline import _turn_is_community_create, _turn_is_tip_share
from app.routing_log import build_routing_record

APP = pathlib.Path(__file__).resolve().parent.parent / "app"


def _ai(linear: str | None, conf: float, **extra) -> dict:
    """Slots as discovery_slots returns them: the live fields plus the frozen AI verdict."""
    return {
        "linear_intent": linear,
        "confidence": conf,
        "ai_linear_intent": linear,
        "ai_confidence": conf,
        **extra,
    }


# ── keyword_may_decide ──────────────────────────────────────────────────────────────


def test_keyword_may_decide_only_when_model_did_not():
    assert keyword_may_decide(None) is True
    assert keyword_may_decide(_ai(None, 0.0)) is True
    assert keyword_may_decide(_ai("discovery.find_peers", 0.4)) is True
    assert keyword_may_decide(_ai("discovery.find_peers", 0.6)) is False
    assert keyword_may_decide(_ai(None, 0.2, clarify="intent")) is False


def test_keyword_may_decide_reads_frozen_verdict_not_rewritten_fields():
    # A reconciler rewrote the live fields; the model's own read was unsure.
    slots = {**_ai("discovery.find_peers", 0.3), "linear_intent": "looking.tip", "confidence": 0.9}
    assert keyword_may_decide(slots) is True


# ── "startup community" ─────────────────────────────────────────────────────────────

STARTUP = "any recommendations for a startup community?"


def test_startup_community_not_created_when_model_is_confident():
    assert _turn_is_community_create(_ai("looking.tip", 0.9), STARTUP) is False


def test_create_community_keyword_still_fills_in_for_an_unsure_model():
    slots = _ai(None, 0.2)
    assert _turn_is_community_create(slots, "I want to create a community") is True
    assert slots["decided_by"] == "keyword"


def test_create_community_keyword_without_any_classifier_read():
    assert _turn_is_community_create(None, "I want to create a community") is True


# ── rescue rules: no override of a confident model, no bump ─────────────────────────


@pytest.mark.parametrize(
    "msg,target",
    [
        ("can you recommend a good dentist?", "looking.tip"),
        ("looking for a used crib", "looking.swap"),
        ("I want to host a bbq this saturday", "sharing.host"),
        ("Dr Patel is a great dentist, highly recommend him", "sharing.tip"),
    ],
)
def test_keyword_rule_defers_to_confident_model(msg, target):
    out = enrich_slots(_ai("discovery.find_peers", 0.9, goal="peers"), msg=msg)
    assert out["linear_intent"] == "discovery.find_peers"
    assert out.get("decided_by") is None


@pytest.mark.parametrize(
    "msg,target",
    [
        ("can you recommend a good dentist?", "looking.tip"),
        ("looking for a used crib", "looking.swap"),
        ("I want to host a bbq this saturday", "sharing.host"),
        ("Dr Patel is a great dentist, highly recommend him", "sharing.tip"),
    ],
)
def test_keyword_rule_fills_in_for_unsure_model_without_bumping(msg, target):
    out = enrich_slots(_ai("discovery.find_peers", 0.4, goal="peers"), msg=msg)
    assert out["linear_intent"] == target
    assert out["decided_by"] == "keyword"
    assert out["confidence"] == 0.4  # the model's own score, not 0.85


def test_model_own_signal_still_corrects_its_label():
    # The model filed it as peers but its own signal field says tip_seek — AI vs AI, kept.
    slots = _ai("discovery.find_peers", 0.7, goal="peers", signal_intent="tip_seek")
    out = enrich_slots(slots, msg="who should I ask about this")
    assert out["linear_intent"] == "looking.tip"
    assert out["decided_by"] == "ai_signal"
    assert out["confidence"] == 0.7


def test_tip_share_turn_follows_confident_model():
    msg = "Dr Patel is a great dentist, highly recommend him"
    assert _turn_is_tip_share(_ai("discovery.find_peers", 0.9, goal="peers"), msg) is False
    assert _turn_is_tip_share(_ai("discovery.find_peers", 0.3, goal="peers"), msg) is True
    assert _turn_is_tip_share(None, msg) is True


# ── no new direct callers ───────────────────────────────────────────────────────────

_MATCHERS = {
    "utterance_indicates_tip_share",
    "utterance_indicates_tip_seek",
    "utterance_indicates_swap_seek",
    "utterance_indicates_swap_offer",
    "utterance_indicates_hosting_plan",
    "phrase_linear_intent",
    "looks_like_community_create",
}

# Calls that predate the gate. Each one should move behind keyword_may_decide (or go,
# once the classifier handles the case it rescues) — shrink this, never grow it.
_KNOWN_DIRECT_CALLS = {
    ("activity_browse.py", "utterance_indicates_tip_seek"): 1,
    ("discovery_route.py", "phrase_linear_intent"): 6,
    ("discovery_route.py", "utterance_indicates_swap_seek"): 4,
    ("discovery_route.py", "utterance_indicates_tip_seek"): 6,
    ("discovery_slots.py", "utterance_indicates_swap_seek"): 1,
    ("discovery_slots.py", "utterance_indicates_tip_seek"): 1,
    # Both gated: they run only with no classifier read or keyword_may_decide.
    ("lana_unified_pipeline.py", "looks_like_community_create"): 1,
    ("lana_unified_pipeline.py", "utterance_indicates_tip_share"): 1,
}


def _direct_calls() -> dict[tuple[str, str], int]:
    found: collections.Counter = collections.Counter()
    for path in APP.rglob("*.py"):
        if path.name == "layer1_intents.py":
            continue  # the routing module itself, where the gate lives
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
            if name in _MATCHERS:
                found[(str(path.relative_to(APP)), name)] += 1
    return dict(found)


def test_no_new_direct_keyword_matcher_calls():
    found = _direct_calls()
    grown = {k: v for k, v in found.items() if v > _KNOWN_DIRECT_CALLS.get(k, 0)}
    assert not grown, (
        f"New direct keyword-matcher call(s) {grown}: a keyword must not pick a lane over "
        "the model. Read the classifier's slots, or gate the match on keyword_may_decide()."
    )
    shrunk = {k: v for k, v in _KNOWN_DIRECT_CALLS.items() if found.get(k, 0) < v}
    assert not shrunk, f"Calls removed {shrunk} — lower _KNOWN_DIRECT_CALLS to match."


# ── per-turn routing record ─────────────────────────────────────────────────────────


def _ctx(msg: str, slots: dict | None, routing: dict | None = None) -> dict:
    return {
        "_discovery_slots": slots,
        "_discovery_slots_for": msg if slots is not None else None,
        "last_routing": routing,
        "routing_phase": "listening",
    }


def test_record_shows_raw_ai_verdict_next_to_final_label():
    msg = "can you recommend a good dentist?"
    slots = enrich_slots(_ai("discovery.find_peers", 0.4, goal="peers"), msg=msg)
    routing = {"outcome": "T", "intent_class": "discovery", "tool_to_call": "tip_seek"}
    rec = build_routing_record(_ctx(msg, slots, routing), user_message=msg, intent_hint=None, prev_routing=None)
    assert rec["decided_by"] == "keyword"
    assert rec["ai_label"] == "discovery.find_peers"
    assert rec["ai_confidence"] == 0.4
    assert rec["final_label"] == "looking.tip"
    assert rec["tool"] == "tip_seek"


def test_record_button_and_no_classifier():
    msg = "Host a meet"
    rec = build_routing_record(_ctx(msg, None), user_message=msg, intent_hint="host_event", prev_routing=None)
    assert rec["decided_by"] == "button"
    rec = build_routing_record(_ctx(msg, None), user_message=msg, intent_hint=None, prev_routing=None)
    assert rec["decided_by"] == "no_classifier"
    assert rec["ai_label"] is None


def test_record_ignores_slots_from_a_previous_message():
    ctx = _ctx("earlier message", _ai("looking.tip", 0.9))
    rec = build_routing_record(ctx, user_message="yes please", intent_hint=None, prev_routing=None)
    assert rec["decided_by"] == "no_classifier"
    assert rec["ai_label"] is None


def test_record_drops_last_turns_routing():
    stale = {"outcome": "A", "intent_class": "discovery"}
    ctx = _ctx("hi", _ai("help.who_are_you", 0.9), routing=stale)
    rec = build_routing_record(ctx, user_message="hi", intent_hint=None, prev_routing=stale)
    assert rec["flow"] is None
    assert rec["decided_by"] == "ai"
