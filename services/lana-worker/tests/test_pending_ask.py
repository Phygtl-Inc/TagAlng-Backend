"""One notion of "what Lana is waiting on": registry coverage, priority, the classifier
payload line, the policy gate stepping aside for an answer, and the routing record."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.discovery_slots import _discovery_slot_payload  # noqa: E402
from app.lana_unified_pipeline import _reply_owned_downstream  # noqa: E402
from app.pending_ask import (  # noqa: E402
    NOT_AN_ASK,
    READ_KEYS,
    pending_ask,
    prompt_line,
    reply_verdict,
)
from app.routing_log import build_routing_record  # noqa: E402
from test_login_carry_covers_pending_keys import _discovered_keys  # noqa: E402

HISTORY = [
    {"role": "user", "content": "find me people who like chess"},
    {"role": "assistant", "content": "Nobody nearby yet. Want me to keep an ear out, or widen the search?"},
]


# ── coverage ────────────────────────────────────────────────────────────────────────


def test_every_pending_key_is_an_ask_or_explicitly_not() -> None:
    missing = sorted(_discovered_keys() - READ_KEYS - set(NOT_AN_ASK))
    assert not missing, (
        f"Pending keys Lana's reply check cannot see: {missing}. Register each in "
        "app/pending_ask.py (_REGISTRY + READ_KEYS) or list it in NOT_AN_ASK with the reason."
    )


def test_read_and_not_an_ask_do_not_overlap() -> None:
    assert not (READ_KEYS & set(NOT_AN_ASK))


# ── what is pending ─────────────────────────────────────────────────────────────────


def test_nothing_pending() -> None:
    assert pending_ask({}, HISTORY) is None
    assert pending_ask(None) is None
    assert prompt_line(None) == "none"


def test_quotes_lanas_last_message() -> None:
    ask = pending_ask({"peer_seek_offer_pending": {"filter": "chess"}}, HISTORY)
    assert ask.kind == "peer_seek_offer"
    assert ask.after_policy is True
    assert ask.question == HISTORY[-1]["content"]
    line = prompt_line(ask)
    assert "kind=peer_seek_offer" in line and HISTORY[-1]["content"] in line


def test_offered_options_are_listed() -> None:
    ask = pending_ask({"community_join_pending": {"places": [
        {"place_id": "p1", "place_name": "Lake Nona Runners"},
        {"place_id": "p2", "place_name": "Chess at the Y"},
    ]}})
    assert ask.options == ["Lake Nona Runners", "Chess at the Y"]
    ask = pending_ask({"out_of_scope_pending": True,
                       "out_of_scope_offer": {"q": "?", "opts": ["Get neighbors together"]}})
    assert ask.options == ["Get neighbors together"]


def test_falls_back_to_the_chips_shown_last_turn() -> None:
    ask = pending_ask({"clarify_pending": True, "_offered_chip_msgs": ["Browse events", "Meet people"]})
    assert ask.options == ["Browse events", "Meet people"]


def test_specific_ask_outranks_the_flow_it_sits_in() -> None:
    ctx = {"activity_browse_active": True, "pending_confirmation": {"tool": "x"},
           "policy_pending_question": "what do you like?"}
    assert pending_ask(ctx).kind == "confirmation"
    ctx.pop("pending_confirmation")
    assert pending_ask(ctx).kind == "activity_browse"
    ctx.pop("activity_browse_active")
    assert pending_ask(ctx).kind == "conversation"
    assert pending_ask(ctx).after_policy is False  # the policy answers its own question


def test_malformed_key_does_not_break_the_turn() -> None:
    ask = pending_ask({"community_join_pending": {"places": "not-a-list"}, "clarify_pending": True})
    assert ask.kind == "community_join"
    assert ask.options == []


def test_an_entry_that_raises_is_skipped() -> None:
    import app.pending_ask as pa

    def boom(_ctx):
        raise ValueError("bad state")

    with patch.object(pa, "_REGISTRY", (("broken", boom, "x", True, None),) + pa._REGISTRY):
        assert pending_ask({"clarify_pending": True}).kind == "clarify"


def test_reply_verdict_only_known_values() -> None:
    assert reply_verdict({"reply_to_pending": "Answer"}) == "answer"
    assert reply_verdict({"reply_to_pending": "maybe"}) is None
    assert reply_verdict(None) is None


# ── the classifier sees it ──────────────────────────────────────────────────────────


def _payload(ctx: dict) -> str:
    return _discovery_slot_payload(
        "yes please", routing_phase="listening", history=HISTORY, has_block=True,
        has_identity=False, phone_verified=True, session_ctx=ctx,
    )


def test_payload_carries_the_pending_ask() -> None:
    assert "pending_ask: kind=peer_seek_offer" in _payload({"peer_seek_offer_pending": {"filter": "chess"}})
    assert "pending_ask: none" in _payload({})


# ── the policy gate steps aside for an answer ───────────────────────────────────────


def _owned(ctx: dict, verdict: str | None) -> bool:
    with patch("app.lana_unified_pipeline._tip_share_slots", return_value={"reply_to_pending": verdict}):
        return _reply_owned_downstream(
            ctx, "yes please", history=HISTORY, home_block_id="b1", phone_verified=True,
        )


def test_answer_to_a_downstream_offer_is_held_from_the_policy() -> None:
    ctx = {"peer_seek_offer_pending": {"filter": "chess"}}
    assert _owned(ctx, "answer") is True
    assert _owned(ctx, "decline") is False
    assert _owned(ctx, "new_ask") is False
    assert _owned(ctx, None) is False


def test_policy_keeps_its_own_question_and_upstream_flows() -> None:
    assert _owned({"policy_pending_question": "what do you like about it?"}, "answer") is False
    assert _owned({"rapport_active": True}, "answer") is False
    assert _owned({}, "answer") is False


# ── routing record ──────────────────────────────────────────────────────────────────


def test_record_carries_pending_kind_and_verdict() -> None:
    slots = {"linear_intent": None, "confidence": 0.9, "ai_linear_intent": None,
             "ai_confidence": 0.9, "goal": "continue", "reply_to_pending": "answer"}
    ctx = {"_discovery_slots": slots, "_discovery_slots_for": "yes please"}
    rec = build_routing_record(ctx, user_message="yes please", intent_hint=None,
                               prev_routing=None, pending_kind="peer_seek_offer")
    assert rec["pending_kind"] == "peer_seek_offer"
    assert rec["reply_to_pending"] == "answer"


def test_gate_is_wired_into_the_decide_turn_condition() -> None:
    """The helper above is only useful while the policy gate calls it."""
    import ast

    src = (Path(__file__).resolve().parents[1] / "app" / "lana_unified_pipeline.py").read_text()
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.If):
            continue
        test = ast.unparse(node.test)
        if "_decide_mode == 'on'" in test:
            assert "_reply_owned_downstream(" in test
            return
    raise AssertionError("decide_turn 'on' gate not found")
