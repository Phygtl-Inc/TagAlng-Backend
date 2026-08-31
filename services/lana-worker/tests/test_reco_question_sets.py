"""Typed recommendation capture: the type picks the question set, the set drives the steps.

The regression this guards: before typed capture every recommendation got the same four
questions and two AI-improvised follow-ups, so a recipe was asked "who or where?" and a
dentist was never asked what she treats.
"""

from __future__ import annotations

from typing import Any

import app.tip_share as tip_share
from app.reco_question_sets import (
    RECO_TYPES,
    carousel,
    missing_required,
    next_question,
    normalize_type,
    steps_for,
    validate_steps,
)


def test_every_type_has_required_steps_and_unique_fields() -> None:
    for t in RECO_TYPES:
        steps = steps_for(t)
        assert 7 <= len(steps) <= 10, f"{t} has {len(steps)} steps"
        fields = [s["field"] for s in steps]
        assert len(fields) == len(set(fields)), f"{t} repeats a field"
        assert [s for s in steps if s.get("required")], f"{t} has no required step"
        assert all(s["question"].endswith("?") for s in steps), f"{t} has a non-question"


def test_normalize_type_tolerates_llm_shapes() -> None:
    assert normalize_type("Restaurants") == "restaurant"
    assert normalize_type("  DIY ") == "diy"
    assert normalize_type("cafe") is None
    assert normalize_type(None) is None


def test_required_steps_come_first_and_gate_the_card() -> None:
    # Required = the type's first two FLOOR fields: what a neighbor reading a recipe cannot
    # act on it without. "why is it worth sharing" is colour, and is asked either way.
    assert next_question("recipe", {"time": "3 hours"})["field"] == "recipe"
    assert missing_required("recipe", {"recipe": "Feijoada"}) == ["ingredients"]
    assert missing_required("recipe", {"recipe": "F", "ingredients": "beans"}) == []
    filled = {s["field"]: "x" for s in steps_for("product")}
    assert next_question("product", filled) is None


def test_carousel_never_mutates_the_shared_table() -> None:
    carousel("location", {"known_for": "food stalls"})
    assert "answer" not in steps_for("location")[0]


def _run(monkeypatch: Any, msg: str, ctx: dict[str, Any], extracted: dict[str, Any]) -> tuple[str, dict]:
    monkeypatch.setattr(
        tip_share, "_extract_tip_fields", lambda **_: (extracted, None)
    )
    monkeypatch.setattr(tip_share, "_name_suggestions", lambda *a, **k: [])
    reply = tip_share.run_tip_share_turn(
        user_message=msg,
        session_ctx=ctx,
        history=[],
        user_jwt="jwt",
        home_block_id="b1",
    )
    return reply, dict(ctx.get("tip_draft") or {})


def test_typed_capture_asks_the_types_own_questions(monkeypatch: Any) -> None:
    ctx: dict[str, Any] = {}
    reply, draft = _run(
        monkeypatch,
        "Boxi Park is great — open-air food stalls and live music",
        ctx,
        {
            "name": "Boxi Park",
            "category": "food hall",
            "reco_type": "location",
            "answers": {"known_for": "Open-air food stalls and live music"},
        },
    )
    # The opener already answered step 1, so the flow asks step 2 — not step 1 again.
    assert ctx["tip_pending_question"] == "Where is it?"
    assert ctx["tip_pending_ask"] == "where"
    # 8 fallback steps + the consent step. No agree step: no neighbour has logged anything
    # on Boxi Park, and an empty "others also said" row is a dead card.
    assert "(2/9)" in reply
    assert draft["steps"][0]["answer"] == "Open-air food stalls and live music"
    assert draft["missing"] == ["where"]


def test_step_answer_lands_on_its_field_not_the_details_bag(monkeypatch: Any) -> None:
    ctx: dict[str, Any] = {
        "tip_draft": {"name": "Boxi Park", "category": "food hall", "reco_type": "location",
                      "answers": {"known_for": "food stalls"}},
        "tip_share_active": True,
        "tip_pending_ask": "why",
    }
    _, draft = _run(monkeypatch, "Kids run wild while you actually sit and eat", ctx, {})
    assert draft["answers"]["why"] == "Kids run wild while you actually sit and eat"
    assert not draft.get("details")


def test_answers_merge_across_turns(monkeypatch: Any) -> None:
    ctx: dict[str, Any] = {
        "tip_draft": {"name": "Dr. Sarah", "category": "dentist", "reco_type": "professional",
                      "answers": {"profession": "Pediatric dentist"}},
        "tip_share_active": True,
    }
    _, draft = _run(monkeypatch, "she took our insurance too", ctx,
                    {"answers": {"helped_with": "toddler cleanings"}})
    assert draft["answers"] == {
        "profession": "Pediatric dentist",
        "helped_with": "toddler cleanings",
    }


def test_fix_chip_reopens_a_step(monkeypatch: Any) -> None:
    ctx: dict[str, Any] = {
        "tip_draft": {"name": "Dr. Sarah", "category": "dentist", "reco_type": "professional",
                      "answers": {"profession": "Pediatric dentist", "best_for": "Kids"}},
        "tip_share_active": True,
        "tip_ready": True,
        "tip_asked_fields": ["profession", "best_for"],
    }
    _, draft = _run(monkeypatch, "fix:best_for", ctx, {})
    assert "best_for" not in draft["answers"]
    assert "best_for" not in (ctx.get("tip_asked_fields") or [])
    assert draft["answers"]["profession"] == "Pediatric dentist"


def test_typed_answers_reach_the_saved_signal(monkeypatch: Any) -> None:
    seen: dict[str, Any] = {}

    def fake_save(user_jwt: str, **kw: Any) -> dict[str, Any]:
        seen.update(kw)
        return {"signal_id": "sig-1", "matches_created": 0}

    monkeypatch.setattr("app.local_signals.save_local_signal", fake_save)
    ctx: dict[str, Any] = {
        "tip_draft": {
            "name": "Feijoada", "category": "family recipe", "reco_type": "recipe",
            "answers": {"recipe": "Brazilian black bean stew", "ingredients": "beans, pork",
                        "time": "~3 hours"},
        },
        "tip_share_active": True,
        "tip_ready": True,
    }
    _run(monkeypatch, "pass the tip along", ctx, {})
    assert seen["reco_type"] == "recipe"
    assert seen["reco_subject"] == "Feijoada"
    # Self-describing: the answer travels with the question it answered, because the
    # question was written for this recommendation and cannot be looked up from the key.
    time_row = next(r for r in seen["reco_fields"] if r["field"] == "time")
    assert time_row == {"field": "time", "label": "Cooks in",
                        "question": "How long does it take?", "kind": "text",
                        "answer": "~3 hours"}
    assert [r["field"] for r in seen["reco_fields"]] == ["recipe", "ingredients", "time"]
    # The answers also survive in the human-readable text a neighbor reads.
    assert "Cooks in: ~3 hours" in seen["detail_text"]


def test_optional_steps_advance_instead_of_repeating() -> None:
    """The walk must move PAST an optional already offered. Without `asked` it returned the
    same first unanswered optional every turn, the caller declined to re-ask it, and every
    step behind it was never reached — the last two questions of each set were dead."""
    answers = {"profession": "dentist", "helped_with": "cleanings"}
    asked: list[str] = []
    for _ in range(10):
        step = next_question("professional", answers, asked=asked)
        if not step:
            break
        asked.append(step["field"])
    assert asked == ["where", "contact", "liked", "stood_out", "best_for", "good_to_know"]


def test_required_step_is_re_asked_until_answered() -> None:
    step = next_question("recipe", {"recipe": "Feijoada"}, asked=["ingredients"])
    assert step["field"] == "ingredients", "a required step must not be dropped by `asked`"


def test_done_mid_carousel_jumps_to_the_ready_card(monkeypatch: Any) -> None:
    ctx: dict[str, Any] = {
        "tip_draft": {"name": "Feijoada", "category": "family recipe", "reco_type": "recipe",
                      "answers": {"recipe": "Black bean stew", "ingredients": "beans, pork"}},
        "tip_share_active": True,
        "tip_asked_fields": ["why"],
    }
    reply, draft = _run(monkeypatch, "that's it", ctx, {})
    assert ctx["tip_ready"] is True, "required steps are in — stop asking optionals"
    assert draft["ready"] is True
    assert "Pass the tip along" in reply


def test_done_early_still_blocked_by_a_required_step(monkeypatch: Any) -> None:
    ctx: dict[str, Any] = {
        "tip_draft": {"name": "Feijoada", "category": "family recipe", "reco_type": "recipe",
                      "answers": {"recipe": "Black bean stew"}},
        "tip_share_active": True,
    }
    _run(monkeypatch, "that's it", ctx, {})
    assert ctx["tip_pending_ask"] == "ingredients"
    assert not ctx.get("tip_ready")


# ── Generated sets ────────────────────────────────────────────────────────────────────


def test_generated_set_keeps_the_models_questions() -> None:
    steps = validate_steps(
        [
            {"field": "Taste!", "label": "Taste", "question": "How does it taste?",
             "placeholder": "Rich, smoky, deeply savoury"},
            {"field": "difficulty", "label": "Difficulty", "question": "How hard is it?",
             "options": ["Easy", "Some patience", "Handy only"]},
        ],
        "recipe",
    )
    fields = [s["field"] for s in steps]
    # Floor first (a recipe nobody can shop for is not a recommendation), then the model's
    # own subject-specific questions, then the tail.
    assert fields == ["recipe", "ingredients", "taste", "difficulty", "ask_ok"]
    assert [s["field"] for s in steps if s["required"]] == ["recipe", "ingredients"]
    assert steps[2]["placeholder"] == "Rich, smoky, deeply savoury"
    assert steps[2]["kind"] == "text"
    assert steps[3]["kind"] == "choice", "options ⇒ a chip row, not a text box"


def test_generated_set_drops_what_it_must_not_ask() -> None:
    steps = validate_steps(
        [
            {"field": "addr", "question": "What is her home address?"},
            {"field": "dob", "question": "What is her date of birth?"},
            {"field": "blurb", "question": "Tell me more about her."},
            {"field": "ask_ok", "question": "Can neighbours reach out to you about this?"},
            {"field": "helped_with", "question": "What did she help with?"},
            {"field": "helped_with", "question": "And what else did she help with?"},
        ],
        "professional",
    )
    fields = [s["field"] for s in steps]
    assert "addr" not in fields and "dob" not in fields, "private asks never reach a card"
    assert "blurb" not in fields, "a statement is not a question"
    assert fields.count("helped_with") == 1, "one field, one step"
    assert fields.count("ask_ok") == 1, "the consent step is ours, worded the same for all"
    assert steps[-1]["question"] == "Can neighbours ask you more?"


def test_generated_set_falls_back_when_generation_gives_nothing() -> None:
    for raw in (None, [], "nope", [{"question": "no field?"}]):
        steps = validate_steps(raw, "product")
        assert [s["field"] for s in steps][:2] == ["used_for", "where_to_buy"]
        assert steps[-1]["field"] == "ask_ok"


def test_generated_set_is_capped() -> None:
    raw = [{"field": f"q{i}", "question": f"Question {i}?"} for i in range(30)]
    steps = validate_steps(raw, "location")
    assert len(steps) <= 13, "a set is a carousel, not an interrogation"


def test_agree_step_only_exists_when_neighbours_logged_something() -> None:
    assert [s["field"] for s in validate_steps([], "location")][-1] == "ask_ok"
    steps = validate_steps(
        [], "location", tallies=[{"attr": "easy parking", "n": 2}, {"attr": "books online", "n": 1}]
    )
    assert steps[-1]["kind"] == "agree"
    assert steps[-1]["options"] == ["easy parking ×2", "books online ×1"]
    assert not steps[-1]["required"], "agreeing is a bonus, never a gate"


def test_unknown_type_generates_nothing() -> None:
    assert validate_steps([{"field": "x", "question": "Why?"}], "cafe") == []


def test_flow_uses_the_generated_set_and_writes_it_once(monkeypatch: Any) -> None:
    """The set is written the turn the type + name are in, then never rewritten — a user
    halfway through answering must not have the questions change under them."""
    monkeypatch.setattr(tip_share, "_reco_tallies", lambda **_: [])
    ctx: dict[str, Any] = {}
    reply, draft = _run(
        monkeypatch,
        "Dr. Sarah is so gentle, my toddler loves going",
        ctx,
        {
            "name": "Dr. Sarah", "category": "pediatric dentist", "reco_type": "professional",
            "answers": {"profession": "Pediatric dentist"},
            "steps_raw": [
                {"field": "profession", "label": "Profession", "question": "What does she do?"},
                {"field": "treats", "label": "Treats", "question": "What does she treat?",
                 "placeholder": "Kids' dental care"},
            ],
        },
    )
    # Floor order, with the model's own "What does she do?" kept as step 1.
    assert [s["field"] for s in draft["step_set"]] == [
        "profession", "helped_with", "contact", "treats", "ask_ok"
    ]
    assert "steps_raw" not in draft, "consumed, not carried"
    assert ctx["tip_pending_question"] == "What did they help you with?"
    assert "(2/5)" in reply

    # A later turn proposing a different set must not replace the one in flight.
    first = draft["step_set"]
    _run(monkeypatch, "she treats toddlers", ctx,
         {"steps_raw": [{"field": "other", "question": "Something else?"}]})
    assert (ctx["tip_draft"] or {})["step_set"] == first


def test_generated_questions_reach_the_saved_signal(monkeypatch: Any) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        "app.local_signals.save_local_signal",
        lambda user_jwt, **kw: (seen.update(kw), {"signal_id": "s", "matches_created": 0})[1],
    )
    ctx: dict[str, Any] = {
        "tip_draft": {
            "name": "Hatch Rest", "category": "baby gear", "reco_type": "product",
            "step_set": [
                {"field": "used_for", "label": "Used for", "question": "What is it used for?",
                 "kind": "text", "required": True},
                {"field": "where_to_buy", "label": "Where to buy", "kind": "text",
                 "question": "Where can neighbours buy it?", "required": True},
                {"field": "stood_out", "label": "Stood out", "kind": "text",
                 "question": "What stood out about it?", "required": False},
            ],
            "answers": {"used_for": "A sound machine + night light",
                        "stood_out": "Got our toddler sleeping through"},
        },
        "tip_share_active": True,
        "tip_ready": True,
    }
    _run(monkeypatch, "pass the tip along", ctx, {})
    assert [r["question"] for r in seen["reco_fields"]] == [
        "What is it used for?", "What stood out about it?"
    ]
    assert "Stood out: Got our toddler sleeping through" in seen["detail_text"]


def test_fix_chip_re_asks_the_steps_own_question(monkeypatch: Any) -> None:
    """Tapping a row on the ready card re-opens THAT step: its own question comes back, and
    the answer lands on it — not in the details bag, and not as "What should I change?"."""
    monkeypatch.setattr(tip_share, "_reco_tallies", lambda **_: [])
    step_set = [
        {"field": "profession", "label": "Profession", "question": "What does she do?",
         "kind": "text", "required": True},
        {"field": "helped_with", "label": "Helped with", "question": "What did she help with?",
         "kind": "text", "required": True},
    ]
    ctx: dict[str, Any] = {
        "tip_draft": {"name": "Dr. Sarah", "category": "dentist", "reco_type": "professional",
                      "step_set": step_set,
                      "answers": {"profession": "Dentist", "helped_with": "cleanings"}},
        "tip_share_active": True,
        "tip_ready": True,
        "tip_asked_fields": ["profession", "helped_with"],
    }
    reply, draft = _run(monkeypatch, "fix:helped_with", ctx, {})
    assert reply == "Sure — What did she help with?"
    assert ctx["tip_pending_ask"] == "helped_with"
    assert "helped_with" not in draft["answers"]

    _, draft = _run(monkeypatch, "Kids' dental care", ctx, {})
    assert draft["answers"]["helped_with"] == "Kids' dental care"
    assert not draft.get("details")


def test_a_spoken_share_reaches_the_capture_not_the_policy() -> None:
    """dev 2026-08-31: "Dr. Sarah in Lake Nona is so gentle with my toddler" came back as
    "That's a real relief to hear. What do you like most about Dr. Sarah…" — an AI-composed
    follow-up, handler=None, no question set, no carousel. Two gates were wrong: the policy
    ran ahead of the engines with no escape for sharing.tip, and the capture only ever armed
    from the CTA or the entry regex, so a share nobody labelled fell to the old cascade."""
    from app.lana_unified_pipeline import _turn_is_engine_action, _turn_is_tip_share

    spoken = "Dr. Sarah in Lake Nona is so gentle with my toddler"
    shared = {"linear_intent": "sharing.tip", "signal_intent": "tip_share", "confidence": 0.7}
    assert _turn_is_tip_share(shared, spoken), "the capture has to pick this up"

    from unittest.mock import patch

    def escapes(slots: dict[str, Any], msg: str) -> bool:
        with patch("app.discovery_slots.discovery_slots_for_turn", return_value=slots):
            return _turn_is_engine_action(
                {"routing_phase": "listening"}, msg,
                history=[], home_block_id="b1", phone_verified=True,
            )

    assert escapes(shared, spoken), "and the policy has to let it through"
    # Ordinary conversation still belongs to the policy — the escape is not "any message
    # that mentions a person".
    assert not _turn_is_tip_share({"goal": "chat", "confidence": 0.9}, "my toddler slept through")
    assert not escapes({"goal": "chat", "confidence": 0.9}, "my toddler slept through")


def test_a_where_step_is_answered_on_the_map_not_in_a_text_box() -> None:
    """A hand-typed "where is it?" is a string nobody can navigate to, so the location steps
    come back as kind=place and the FE renders the Places picker."""
    kinds = {s["field"]: s["kind"] for s in validate_steps([], "location")}
    assert kinds["where"] == "place"
    assert kinds["known_for"] == "text"

    # A model-written location step gets it too — the promotion is by field, so it applies
    # whatever the model called the question.
    generated = validate_steps(
        [{"field": "location", "label": "Location", "question": "Where can neighbours find her?"}],
        "professional",
    )
    assert {s["field"]: s["kind"] for s in generated}["location"] == "place"

    # NOT where_to_buy: "Amazon · ~$60" is the right answer, and a map search would send the
    # user hunting for a warehouse.
    assert {s["field"]: s["kind"] for s in validate_steps([], "product")}["where_to_buy"] == "text"
