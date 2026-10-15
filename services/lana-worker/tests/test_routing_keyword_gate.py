"""AI-first routing: a keyword match never outranks a confident model, and the confidence
it leaves behind is the model's own (no 0.85 bump). Plus the guard that keeps new code
from calling a keyword matcher directly, and the per-turn routing record."""

from __future__ import annotations

import ast
import collections
import pathlib

import pytest

from app.layer1_intents import enrich_slots, keyword_may_decide
from app.lana_unified_pipeline import (
    _turn_is_community_create,
    _turn_is_pass_along,
    _turn_is_tip_share,
)
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


# ── typed words never open a flow before the model reads them ───────────────────────


def test_main_opens_flows_from_buttons_only():
    """The host / give-away / tip entries in main.py run BEFORE the classifier, so only a
    tapped CTA (intent_hint) may open them. A typed ask is the model's to read."""
    src = (APP / "main.py").read_text()
    for gone in (
        "looks_like_host_event_entry",
        "looks_like_pass_along_entry",
        "looks_like_tip_share_entry",
    ):
        assert gone not in src


@pytest.mark.parametrize(
    "msg,label",
    [
        # Pouya's 2026-10-14 cases: each matched an OFFERING entry phrase while asking.
        ("is anyone giving away a crib?", "looking.swap"),
        ("is anyone hosting a party this weekend?", "discovery.find_activities"),
        ("can you recommend a pizza place near me?", "looking.tip"),
    ],
)
def test_seeks_that_share_words_with_offers_open_no_offer_flow(msg, label):
    slots = _ai(label, 0.9)
    assert _turn_is_pass_along(slots, msg) is False
    assert _turn_is_tip_share(slots, msg) is False


def test_pass_along_follows_the_model():
    assert _turn_is_pass_along(_ai("sharing.swap", 0.9), "I'm giving away a stroller") is True
    # The model's own signal field counts, as it does for the other lanes.
    slots = _ai("discovery.find_peers", 0.7, goal="peers", signal_intent="swap_offer")
    assert _turn_is_pass_along(slots, "my kid outgrew this bike, anyone want it?") is True
    # No classifier read: fail closed — the words alone never open it.
    assert _turn_is_pass_along(None, "I'm giving away a stroller") is False


def test_start_host_mode_never_inherits_a_previous_event():
    from app.discovery_route import start_host_mode

    ctx = {
        "event_draft": {"title": "dads meetup"},
        "event_id": "old-event",
        "event_when_date": "2026-10-01",
        "event_venue": {"name": "Old Park"},
        "event_place_asked": True,
        "host_stage": "confirm",
    }
    start_host_mode(ctx)
    assert ctx["event_host_active"] is True
    assert ctx["event_host_turns"] == 0
    for key in ("event_draft", "event_id", "event_when_date", "event_venue", "host_stage"):
        assert ctx[key] is None, key  # set, not popped: the session merge must clear them
    assert ctx["event_place_asked"] is False


def test_start_pass_along_closes_an_open_tip():
    from app.pass_along import start_pass_along

    ctx = {"tip_share_active": True, "tip_draft": {"name": "Casa Lupe"}, "item_draft": {"title": "x"}}
    start_pass_along(ctx)
    assert ctx["pass_along_active"] is True
    assert ctx["item_draft"] is None
    assert ctx["tip_share_active"] is False
    assert ctx["tip_draft"] is None


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


def test_typed_hosting_escapes_the_policy_to_its_flow():
    """Without the pre-classifier entry phrase, sharing.host must still reach the host
    flow: the policy gate hands it to the engine instead of answering in prose."""
    from app.lana_unified_pipeline import POLICY_ENGINE_ONLY_INTENTS

    assert "sharing.host" in POLICY_ENGINE_ONLY_INTENTS


# ── 2026-10-15 live audit: five paths where a word list beat the model ──────────────


def test_crisis_is_answered_before_the_unsafe_word_list():
    """"my husband calls me a bitch and I'm scared to go home" read system.crisis and got
    the flat unsafe refusal because the slur matched first. Crisis must be checked first."""
    src = (APP / "discovery_route.py").read_text()
    assert src.index("if _is_crisis_turn(slots, msg):") < src.index(
        "_unsafe_kind = _unsafe_kind_for_turn(slots, msg)"
    )


def test_no_lane_ends_on_a_cancel_word():
    for name in ("lane_decision.py", "tip_share.py", "pass_along.py", "look_meet.py",
                 "activity_browse.py", "community_capture.py"):
        assert "_CANCEL_RE" not in (APP / name).read_text(), name


def test_tip_toggle_step_still_lets_a_crisis_out():
    from app.tip_share import tip_share_should_release

    ctx = {
        "tip_draft": {"step_set": [{"kind": "toggle", "field": "q", "question": "Can neighbours ask you more?"}]},
        "tip_pending_ask": "q",
    }
    # The toggle step's own carve-out: a plain "no" is its answer, never a release.
    assert tip_share_should_release("naah", ctx, {"abandon": True, "confidence": 0.9}) is False
    crisis = {"goal": "crisis", "linear_intent": "system.crisis", "confidence": 0.5}
    assert tip_share_should_release("I can't do this anymore", ctx, crisis) is True


def test_logout_follows_the_model():
    from app.discovery_route import logout_requested

    netflix = "haha my husband never remembers to log out of my Netflix"
    # Chat reads usually carry no linear label — the confident goal alone must hold.
    assert logout_requested(netflix, _ai(None, 0.9, goal="chat")) is False
    assert logout_requested(netflix, _ai("discovery.find_peers", 0.9, goal="peers")) is False
    assert logout_requested("log me out", _ai("auth.logout", 0.9, goal="logout")) is True
    # Model unsure or absent: the phrase still gets someone signed out.
    assert logout_requested("log me out", _ai(None, 0.2)) is True
    assert logout_requested("log me out", None) is True


def test_intros_phrase_does_not_beat_a_confident_search():
    from app.discovery_route import _phrase_may_decide

    assert _phrase_may_decide(_ai("discovery.find_by_attrs", 0.9, goal="peers")) is False
    assert _phrase_may_decide(_ai(None, 0.2)) is True
    src = (APP / "discovery_route.py").read_text()
    assert "phrase_wants_list = _phrase_may_decide(slots) and wants_list_intros_phrase(msg)" in src


def test_leaving_a_capture_never_opens_one():
    """A bare "cancel" keeps the label of the flow it leaves; that label must not re-arm
    the flow or count as an engine command (dev 2026-10-15: host reopened, tip fell into
    the signal cascade)."""
    leaving = {**_ai("sharing.tip", 0.9), "abandon": True}
    assert _turn_is_tip_share(leaving, "cancel") is False
    assert _turn_is_pass_along({**_ai("sharing.swap", 0.9), "abandon": True}, "nvm") is False


# ── 2026-10-15 group 1: words that WRITE something ──────────────────────────────────


@pytest.mark.parametrize(
    "msg,pattern_owner,pattern",
    [
        ("she's done wonders with my twins, add that", "app.tip_share", "_PASS_RE"),
        ("I'm done with this bike, my kid outgrew it", "app.pass_along", "_LIST_NOW_RE"),
        ("yes please change the name first", "app.community_capture", "_PUBLISH_RE"),
    ],
)
def test_a_control_word_inside_a_sentence_is_content(msg, pattern_owner, pattern):
    import importlib

    from app.lane_decision import is_bare_control

    assert is_bare_control(msg, getattr(importlib.import_module(pattern_owner), pattern)) is False


@pytest.mark.parametrize(
    "msg,pattern_owner,pattern",
    [
        ("pass the tip along", "app.tip_share", "_PASS_RE"),  # the post button's payload
        ("Done.", "app.tip_share", "_PASS_RE"),
        ("list it", "app.pass_along", "_LIST_NOW_RE"),  # the item card's button
        ("go ahead", "app.community_capture", "_PUBLISH_RE"),
    ],
)
def test_a_whole_message_command_is_a_typed_button(msg, pattern_owner, pattern):
    import importlib

    from app.lane_decision import is_bare_control

    assert is_bare_control(msg, getattr(importlib.import_module(pattern_owner), pattern)) is True


def test_no_name_is_read_off_the_message_before_the_model():
    main_src = (APP / "main.py").read_text()
    i = main_src.index("persist_nickname_if_stated(auth.user_id")
    assert 'if purpose == "profile_intake":' in main_src[i - 600:i]
    claims_src = (APP / "claims_persist.py").read_text()
    body = claims_src[claims_src.index("def try_upsert_claims_from_message"):]
    body = body[: body.index("incremental_claims_from_utterance(")]
    assert "persist_nickname_if_stated(" not in body  # never ahead of the extractor
