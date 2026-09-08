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


# The basics: a phone number and an address have no answer set, and are not meant to.
_FLOOR_ISH = {
    "subject", "profession", "service", "helped_with", "contact", "dish", "where",
    "recipe", "ingredients", "steps", "used_for", "where_to_buy", "known_for", "fixes",
    "how", "needs", "cuisine", "good_to_know", "best_for",
}


def test_every_type_has_required_steps_and_unique_fields() -> None:
    for t in RECO_TYPES:
        steps = steps_for(t)
        assert 6 <= len(steps) <= 10, f"{t} has {len(steps)} steps"
        # Every step but the type's own basics carries taps, or the carousel is a
        # column of text boxes and nothing on the card is filterable.
        facets = [s for s in steps if s["field"] not in _FLOOR_ISH]
        assert any(s.get("options") for s in facets), f"{t} has no tappable facet"
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
    # The subject comes first and is required: a recommendation with no subject is a card
    # with no title, and every set is now head-first (see `head_step`).
    assert next_question("recipe", {"time": "3 hours"})["field"] == "subject"
    assert next_question("recipe", {"subject": "Feijoada", "time": "3 h"})["field"] == "recipe"
    assert missing_required("recipe", {"recipe": "Feijoada"}) == ["subject", "ingredients"]
    assert missing_required(
        "recipe", {"subject": "Feijoada", "recipe": "F", "ingredients": "beans"}
    ) == []
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
    # Two steps are already answered — the subject by the name in the opener, `known_for` by
    # what they said about it — so the flow asks the THIRD, not either of those again.
    assert ctx["tip_pending_question"] == "Does it cost anything?"
    assert ctx["tip_pending_ask"] == "cost"
    # subject + 5 fallback steps + the consent step. `where` is gone (a location's subject
    # IS the map point, so it would be the same question twice) and so is every colour step
    # the prompt bans.
    assert "(3/7)" in reply
    assert "where" not in [s["field"] for s in draft["steps"]]
    assert draft["steps"][0]["answer"] == "Boxi Park"
    assert draft["steps"][1]["answer"] == "Open-air food stalls and live music"
    assert draft["missing"] == []


def test_step_answer_lands_on_its_field_not_the_details_bag(monkeypatch: Any) -> None:
    ctx: dict[str, Any] = {
        "tip_draft": {"name": "Boxi Park", "category": "food hall", "reco_type": "location",
                      "answers": {"known_for": "food stalls"}},
        "tip_share_active": True,
        "tip_pending_ask": "good_to_know",
    }
    _, draft = _run(monkeypatch, "Kids run wild while you actually sit and eat", ctx, {})
    assert draft["answers"]["good_to_know"] == "Kids run wild while you actually sit and eat"
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
        # Pre-answered from the name already in the draft, so the carousel shows it done
        # instead of asking who this is.
        "subject": "Dr. Sarah",
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
            "trait": "worth the wait", "locality": "Lake Nona",
            "details": ["freezes well"],
            "answers": {"recipe": "Brazilian black bean stew", "ingredients": "beans, pork",
                        "time": "~3 hours"},
        },
        "tip_share_active": True,
        "tip_ready": True,
    }
    _run(monkeypatch, "pass the tip along", ctx, {})
    assert seen["reco_type"] == "recipe"
    assert seen["reco_subject"] == "Feijoada"
    # The card head lands in its own columns — the reader renders these instead of
    # splitting detail_text back apart (20261120120000).
    assert seen["reco_name"] == "Feijoada"
    assert seen["reco_place"] == "Lake Nona"
    assert seen["reco_description"] == "worth the wait · freezes well"
    # Self-describing: the answer travels with the question it answered, because the
    # question was written for this recommendation and cannot be looked up from the key.
    time_row = next(r for r in seen["reco_fields"] if r["field"] == "time")
    assert time_row == {"field": "time", "label": "Cooks in",
                        "question": "How long does it take?", "kind": "choice",
                        "answer": "~3 hours"}
    assert [r["field"] for r in seen["reco_fields"]] == ["recipe", "ingredients", "time"]
    # The answers also survive in the human-readable text a neighbor reads.
    assert "Cooks in: ~3 hours" in seen["detail_text"]


def test_optional_steps_advance_instead_of_repeating() -> None:
    """The walk must move PAST an optional already offered. Without `asked` it returned the
    same first unanswered optional every turn, the caller declined to re-ask it, and every
    step behind it was never reached — the last two questions of each set were dead."""
    answers = {"subject": "Dr. Sarah", "profession": "dentist", "helped_with": "cleanings"}
    asked: list[str] = []
    for _ in range(10):
        step = next_question("professional", answers, asked=asked)
        if not step:
            break
        asked.append(step["field"])
    assert asked == ["where", "contact", "ages", "wait", "best_for", "good_to_know"]


def test_required_step_is_re_asked_until_answered() -> None:
    step = next_question(
        "recipe", {"subject": "Feijoada", "recipe": "Feijoada"}, asked=["ingredients"]
    )
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
             "placeholder": "Rich, smoky, deeply savoury",
             "options": ["Rich and smoky", "Fresh and light", "Properly spicy"]},
            {"field": "difficulty", "label": "Difficulty", "question": "How hard is it?",
             "options": ["Easy", "Some patience", "Handy only"]},
            # No answer set and not a floor field: this is the text box "hundred dollars"
            # came out of, so it does not get written at all.
            {"field": "vibe", "label": "Vibe", "question": "What is the vibe of it?"},
        ],
        "recipe",
    )
    fields = [s["field"] for s in steps]
    # Floor first (a recipe nobody can shop for is not a recommendation), then the model's
    # own subject-specific questions, then the tail.
    assert fields == ["subject", "recipe", "ingredients", "taste", "difficulty", "ask_ok"]
    assert "vibe" not in fields, "a facet with no answer set is a text box, not a facet"
    assert [s["field"] for s in steps if s["required"]] == [
        "subject", "recipe", "ingredients"
    ]
    assert steps[3]["placeholder"] == "Rich, smoky, deeply savoury"
    assert steps[3]["kind"] == "choice"
    assert steps[4]["kind"] == "choice", "options ⇒ a chip row, not a text box"


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


def test_a_place_is_never_asked_what_google_already_knows() -> None:
    """The Barnes & Noble card asked "price range?" and got "hundred dollars" — a step
    spent on a listing fact, answered uselessly (dev QA 2026-09-07)."""
    steps = validate_steps(
        [
            {"field": "subject", "question": "Which shop?"},
            {"field": "known_for", "question": "What is Barnes & Noble known for?"},
            {"field": "price_range", "question": "What is the price range?"},
            {"field": "hours", "question": "What are their opening hours?"},
            {"field": "phone", "question": "What is their phone number?"},
            {"field": "quiet_corners", "question": "Are there quiet corners to read in?",
             "options": ["Plenty", "A couple", "None"]},
        ],
        "location",
    )
    fields = [s["field"] for s in steps]
    assert "quiet_corners" in fields, "what only a visitor knows survives"
    for lookup in ("price_range", "hours", "phone"):
        assert lookup not in fields, f"{lookup} is on the listing, not word-of-mouth"
    # A product's price is not a listing fact — it stays askable, as bands.
    assert "price" in [
        s["field"]
        for s in validate_steps(
            [
                {"field": "used_for", "question": "What is it used for?"},
                {"field": "price", "question": "Roughly what does it cost?",
                 "options": ["Under $50", "$50-150", "$150+"]},
            ],
            "product",
        )
    ], "a product's price is the neighbour's to tell"


def test_a_barber_shop_is_picked_on_the_map_and_a_plumber_is_typed(monkeypatch: Any) -> None:
    """"a barber shop near me" was asked "Who is it?" in a plain text box: the type table
    files barbers with the plumbers, and a plumber genuinely has no storefront (dev QA
    2026-09-08). The extractor's place_based read is what tells them apart."""
    shop = steps_for("service", place_based=True)
    assert shop[0]["kind"] == "place", "a barber shop is a door you walk through"
    plumber = steps_for("service", place_based=False)
    assert plumber[0]["kind"] == "text", "a plumber by referral has no address"

    # A clinic too — and its duplicate "where are they based?" goes, since the pin has it.
    clinic = validate_steps(
        [{"field": "treats", "question": "Which ages?", "options": ["Kids", "Adults"]}],
        "professional", place_based=True,
    )
    assert clinic[0]["kind"] == "place"
    assert "where" not in [s["field"] for s in clinic], "the pin already answers it"

    # But never a recipe, however confident the model is that daal is on a map.
    assert steps_for("recipe", place_based=True)[0]["kind"] == "text"
    assert steps_for("product", place_based=True)[0]["kind"] == "text"


def test_the_chat_fork_offers_places_for_a_place_subject(monkeypatch: Any) -> None:
    """kind=place is also what triggers the nearby-places chips, so the barber-shop fix
    has to reach the reply and not just the carousel."""
    # Not via `_run`: that helper stubs the place lookup empty, which is the thing under
    # test here.
    monkeypatch.setattr(tip_share, "_reco_tallies", lambda **_: [])
    monkeypatch.setattr(
        tip_share, "_extract_tip_fields",
        lambda **_: ({"category": "barber shop", "reco_type": "service",
                      "place_based": True}, None),
    )
    monkeypatch.setattr(
        tip_share, "_name_suggestions", lambda *a, **k: ["Fade Room", "Nona Barbers"]
    )
    ctx: dict[str, Any] = {}
    tip_share.run_tip_share_turn(
        user_message="a barber shop near me", session_ctx=ctx, history=[],
        user_jwt="jwt", home_block_id="b1",
    )
    draft = dict(ctx.get("tip_draft") or {})
    assert ctx["tip_pending_ask"] == "subject"
    assert draft["suggestions"] == ["Fade Room", "Nona Barbers"], "real shops to tap"


def test_the_subject_step_offers_real_places_to_tap(monkeypatch: Any) -> None:
    """"Which stationery shop?" arrived with nothing to tap and no ZIP in the session, so
    the user was left typing a name Lana could have found (dev QA 2026-09-08). A map search
    needs a centre, and for a signed-in user the only one that resolves is their home —
    which means the user id has to reach `nearby_place_suggestions`."""
    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        "app.places.nearby_place_suggestions",
        lambda **kw: (seen.update(kw), ["Sam Flax Orlando", "Rifle Paper Co."])[1],
    )
    monkeypatch.setattr("app.auth.jwt_user_id", lambda _jwt: "u-1")
    out = tip_share._name_suggestions(
        {"category": "stationery shop", "reco_type": "location"},
        zip_code=None,
        block_id="b1",
        user_jwt="jwt",
    )
    assert out == ["Sam Flax Orlando", "Rifle Paper Co."]
    assert seen["user_id"] == "u-1", "no user id, no map centre, no places to tap"


def test_generated_set_falls_back_when_generation_gives_nothing() -> None:
    for raw in (None, [], "nope", [{"question": "no field?"}]):
        steps = validate_steps(raw, "product")
        assert [s["field"] for s in steps][:3] == ["subject", "used_for", "where_to_buy"]
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
                {"field": "treats", "label": "Treats", "question": "Which ages does she treat?",
                 "placeholder": "Toddlers up", "options": ["Babies", "Toddlers", "Big kids"]},
            ],
        },
    )
    # Floor order, with the model's own "What does she do?" kept as step 1.
    assert [s["field"] for s in draft["step_set"]] == [
        "subject", "profession", "helped_with", "contact", "treats", "ask_ok"
    ]
    assert "steps_raw" not in draft, "consumed, not carried"
    assert ctx["tip_pending_question"] == "What did they help you with?"
    assert "(3/6)" in reply

    # A later turn proposing a different set must not replace the one in flight.
    first = draft["step_set"]
    _run(monkeypatch, "she treats toddlers", ctx,
         {"steps_raw": [{"field": "other", "question": "Something else?"}]})
    assert (ctx["tip_draft"] or {})["step_set"] == first


def _priced_ctx() -> dict[str, Any]:
    return {
        "tip_draft": {
            "name": "Barnes & Noble", "category": "stationery shop",
            "reco_type": "location", "draft_id": "d1",
            "step_set": [
                {"field": "subject", "label": "Which shop", "kind": "place",
                 "question": "Which shop?", "required": True},
                {"field": "known_for", "label": "Known for", "kind": "text",
                 "question": "What is Barnes & Noble known for?", "required": True},
                {"field": "price_range", "label": "Price range", "kind": "text",
                 "question": "What is the price range?", "required": False},
            ],
            "answers": {"subject": "Barnes & Noble", "known_for": "paperback books"},
        },
        "tip_share_active": True,
        "tip_asked_fields": ["subject", "known_for", "price_range"],
        "tip_pending_ask": "price_range",
    }


def test_an_answer_that_does_not_answer_gets_one_nudge(monkeypatch: Any) -> None:
    """"hundred dollars" to "what is the price range?" was stored and rendered as a fact
    (dev QA 2026-09-07). Now the step re-opens — once."""
    ctx = _priced_ctx()
    reply, draft = _run(
        monkeypatch, "hundred dollars", ctx,
        {"weak_answer": {"field": "price_range", "why": "an amount, not a range"}},
    )
    assert "price_range" not in draft["answers"], "the junk answer is not kept"
    assert ctx["tip_pending_ask"] == "price_range", "the same step is re-opened"
    assert ctx["tip_pending_question"] == "What is the price range?"
    assert "price_range" not in (ctx["tip_asked_fields"] or []), "or the walk would skip it"
    assert "price range" in reply.lower()

    # Second try, still weak → it stands. A neighbour doing us a favour is not a form.
    reply2, draft2 = _run(
        monkeypatch, "like a hundred bucks", ctx,
        {"weak_answer": {"field": "price_range", "why": "still not a range"}},
    )
    assert draft2["answers"]["price_range"] == "like a hundred bucks", "one nudge, then it stands"
    assert ctx["tip_pending_ask"] != "price_range"


def test_the_nudge_answers_the_user_before_re_asking(monkeypatch: Any) -> None:
    """"the petrol is expensive" got a bare re-ask that read straight past it (dev QA
    2026-09-08). Lana has to react to what was said, and offer a way out of an optional
    question she has now asked twice."""
    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        tip_share, "compose_reply",
        lambda **kw: (seen.update(kw), str(kw.get("fallback") or ""))[1],
    )
    ctx = _priced_ctx()
    _run(
        monkeypatch, "the petrol is expensive", ctx,
        {"weak_answer": {"field": "price_range", "why": "not about the shop"}},
    )
    assert "the petrol is expensive" in " ".join(seen["facts"]), "she read it"
    assert "REACT" in seen["goal"], "and reacts to it before re-asking"
    assert tip_share._SKIP_CHIP in (dict(ctx["tip_draft"]).get("suggestions") or [])


def test_the_nudge_never_touches_the_subject_or_the_tail(monkeypatch: Any) -> None:
    """The subject is a map pick and the tail is consent — neither is a wrong-shaped answer
    to argue with."""
    for field in ("subject", "ask_ok", "others_also_said"):
        ctx = _priced_ctx()
        _run(monkeypatch, "whatever", ctx, {"weak_answer": {"field": field, "why": "no"}})
        assert not (ctx.get("tip_reasked_fields") or []), f"{field} is never re-asked"


def test_no_nudge_when_the_user_is_done(monkeypatch: Any) -> None:
    """"pass the tip along" on a weak answer posts the tip — arguing on the way out is how
    a finished card becomes a loop."""
    ctx = _priced_ctx()
    ctx["tip_draft"]["answers"]["price_range"] = "hundred dollars"
    _run(
        monkeypatch, "pass the tip along", ctx,
        {"weak_answer": {"field": "price_range", "why": "an amount, not a range"}},
    )
    assert not (ctx.get("tip_reasked_fields") or [])


def test_questions_wait_until_lana_knows_which_shop(monkeypatch: Any) -> None:
    """A category alone got a set written about "a stationery shop" in general — every
    question said "this stationery shop" and asked what fits any shop on earth (dev QA
    2026-09-08). Now the subject step comes first and the set is written after it."""
    monkeypatch.setattr(tip_share, "_reco_tallies", lambda **_: [])
    ctx: dict[str, Any] = {}
    _, draft = _run(
        monkeypatch, "a stationery shop near me with cool stuff", ctx,
        {
            "category": "stationery shop", "reco_type": "location",
            "steps_raw": [{"field": "parking", "question": "What is parking like?",
                           "options": ["Easy lot", "Street only"]}],
        },
    )
    assert not draft.get("step_set"), "no subject yet, so no questions written about it"
    assert ctx["tip_pending_ask"] == "subject", "the subject is asked first"

    # The subject lands → NOW the set is written, and it can name the real place.
    _, draft = _run(
        monkeypatch, "Gifts & More at The Paper Store", ctx,
        {"name": "Gifts & More at The Paper Store",
         "answers": {"subject": "Gifts & More at The Paper Store"},
         "steps_raw": [{"field": "parking", "label": "Parking",
                        "question": "Is parking easy at The Paper Store?",
                        "options": ["Easy lot", "Street only", "Tough parking"]}]},
    )
    assert [s["field"] for s in draft["step_set"]][:2] == ["subject", "known_for"]
    assert "parking" in [s["field"] for s in draft["step_set"]]


def test_the_cards_fork_waits_for_questions_about_this_subject(monkeypatch: Any) -> None:
    """The carousel opened on the type's generic table — eight text boxes asking "What do
    they do?" while the chat fork was offering Men's haircuts / Kids' cuts / Shaves for
    this barber shop (dev QA 2026-09-08). `tailored` is what the client holds the fork on."""
    monkeypatch.setattr(tip_share, "_reco_tallies", lambda **_: [])
    ctx: dict[str, Any] = {}
    _, draft = _run(
        monkeypatch, "a barber shop near me", ctx,
        {"category": "barber shop", "reco_type": "service", "place_based": True},
    )
    assert draft["tailored"] is False, "no subject yet, so the questions are still generic"

    _, draft = _run(
        monkeypatch, "Genteel barbershop", ctx,
        {"name": "Genteel barbershop", "answers": {"subject": "Genteel barbershop"},
         "steps_raw": [{"field": "service", "label": "Service",
                        "question": "What does Genteel do?",
                        "options": ["Men's haircuts", "Kids' cuts", "Shaves"]}]},
    )
    assert draft["tailored"] is True, "written for this shop — the fork can open"
    service = next(s for s in draft["step_set"] if s["field"] == "service")
    assert service["options"] == ["Men's haircuts", "Kids' cuts", "Shaves"]
    assert service["kind"] == "choice", "taps in the carousel, not a text box"


def test_asking_why_does_not_destroy_the_recommendation(monkeypatch: Any) -> None:
    """"why are u asking this? why should i tell?" reset the whole capture — the card
    vanished and Lana offered to help with something else (dev QA 2026-09-08)."""
    ctx = _priced_ctx()
    ctx["tip_pending_ask"] = "price_range"
    meta = {"goal": "chat"}

    # The lane is KEPT: a capture with a question outstanding owns "why are you asking".
    assert not tip_share.tip_share_should_release("why are u asking this?", ctx, meta)

    reply = tip_share.run_tip_share_turn(
        user_message="why are u asking this? why should i tell?",
        session_ctx=ctx, history=[], user_jwt="jwt", home_block_id="b1", slots=meta,
    )
    draft = dict(ctx.get("tip_draft") or {})
    assert draft.get("name") == "Barnes & Noble", "the recommendation survives"
    assert "price_range" not in (draft.get("answers") or {}), "the question is not the answer"
    assert ctx["tip_share_active"], "still in the capture"
    assert tip_share._SKIP_CHIP in (draft.get("suggestions") or []), "an optional step can be skipped"
    assert "price range" in reply.lower()

    # Skipping it moves on instead of re-asking.
    _run(monkeypatch, tip_share._SKIP_CHIP, ctx, {})
    assert "price_range" in (ctx.get("tip_asked_fields") or [])
    assert (dict(ctx.get("tip_draft") or {}).get("answers") or {}).get("price_range") is None


def test_the_question_is_never_stored_as_the_answer(monkeypatch: Any) -> None:
    """"why should i answer this" landed on the card as the LIKED value with a green tick
    (dev QA 2026-09-08). The classifier had not called that turn `chat`, so the meta branch
    never ran — the extractor, which is the only one holding the question that was on
    screen, now says what the message IS."""
    ctx = _priced_ctx()
    ctx["tip_pending_ask"] = "price_range"
    reply, draft = _run(
        monkeypatch, "why should i answer this", ctx,
        # No slots at all: the classifier is not consulted, exactly the case that broke.
        {"reply_role": "asks_why"},
    )
    assert (draft.get("answers") or {}).get("price_range") is None, (
        "their question is not their answer"
    )
    assert ctx["tip_pending_ask"] == "price_range", "the step is still open"
    assert draft.get("name") == "Barnes & Noble", "and the card survives"
    assert tip_share._SKIP_CHIP in (draft.get("suggestions") or [])

    # An ANSWER is still an answer, however grudging.
    _, draft2 = _run(monkeypatch, "cheap enough", ctx, {"reply_role": "answer"})
    assert (draft2.get("answers") or {}).get("price_range") == "cheap enough"


def test_a_required_step_is_explained_not_skipped(monkeypatch: Any) -> None:
    """No skip chip on a step the card cannot be finished without — offering one would
    dead-end the flow."""
    ctx = _priced_ctx()
    ctx["tip_pending_ask"] = "known_for"
    ctx["tip_draft"]["answers"] = {"subject": "Barnes & Noble"}
    tip_share.run_tip_share_turn(
        user_message="why do you need that?", session_ctx=ctx, history=[],
        user_jwt="jwt", home_block_id="b1", slots={"goal": "chat"},
    )
    assert tip_share._SKIP_CHIP not in (
        (dict(ctx.get("tip_draft") or {})).get("suggestions") or []
    )


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


def test_an_unnamed_recommendation_still_gets_its_own_questions(monkeypatch: Any) -> None:
    """dev QA 2026-09-04: "i found a great electric kettle" and "found a new hiking place it
    was great" both came back with the same hand-written "Who or where? A name helps me find
    them." and step_set=[] — a kettle asked WHO, and neither reached the carousel at all,
    because the name was collected by a gate OUTSIDE the set. The name is the set's first
    STEP now, so every type has its questions from the first turn."""
    monkeypatch.setattr(tip_share, "_reco_tallies", lambda **_: [])
    for reco_type, category, kind in (
        ("product", "electric kettle", "text"),
        ("location", "hiking spot", "place"),
    ):
        ctx: dict[str, Any] = {}
        reply, draft = _run(
            monkeypatch, "i found a great one", ctx,
            {"category": category, "reco_type": reco_type, "trait": "great"},
        )
        steps = draft["steps"]
        assert len(steps) > 1, f"{reco_type} got no question set"
        assert steps[0]["field"] == "subject", f"{reco_type} does not lead with the subject"
        assert steps[0]["kind"] == kind, f"{reco_type} subject control"
        assert ctx["tip_pending_ask"] == "subject"
        assert "Who or where" not in reply


def test_each_recommendation_gets_its_own_id(monkeypatch: Any) -> None:
    """dev QA 2026-09-04: "my kids loved the new trampoline park" opened the CHAT fork with
    0/7 answered — the cards-or-chat pick leaked from the previous recommendation. The FE
    keyed "same recommendation?" on `name`, which is null until the subject step is
    answered, so two nameless recommendations in a row looked like one. The id is stable
    for a draft's life and new for the next one."""
    monkeypatch.setattr(tip_share, "_reco_tallies", lambda **_: [])
    ctx: dict[str, Any] = {}
    extracted = {"category": "trampoline park", "reco_type": "location", "trait": "loved it"}
    _, first = _run(monkeypatch, "my kids loved the new trampoline park", ctx, extracted)
    one = first["draft_id"]
    assert one, "a draft with no name still has an identity"

    # Same recommendation, now named — the id must NOT change, or the FE reads it as a new
    # one and throws the user back to the fork over a half-answered set.
    _, named = _run(monkeypatch, "Altitude Trampoline Park", ctx, {"name": "Altitude Trampoline Park"})
    assert named["draft_id"] == one
    assert named["name"] == "Altitude Trampoline Park"

    # A different recommendation gets a different id, so the lane pick does not carry over.
    tip_share.reset_tip_share_state(ctx)
    ctx["tip_draft"] = None
    _, second = _run(monkeypatch, "i found a great electric kettle", ctx,
                     {"category": "electric kettle", "reco_type": "product"})
    assert second["draft_id"] and second["draft_id"] != one


def test_fix_name_reopens_the_subject_step(monkeypatch: Any) -> None:
    """The name chip has no gate to go back to any more, so it re-opens the subject step —
    with the step's OWN question, not the old type-blind one."""
    monkeypatch.setattr(tip_share, "_reco_tallies", lambda **_: [])
    ctx: dict[str, Any] = {
        "tip_draft": {"name": "Dr. Sarah", "category": "dentist", "reco_type": "professional",
                      "answers": {"subject": "Dr. Sarah", "profession": "Dentist"}},
        "tip_share_active": True,
        "tip_ready": True,
        "tip_asked_fields": ["subject", "profession"],
    }
    reply, draft = _run(monkeypatch, "fix:name", ctx, {})
    assert ctx["tip_pending_ask"] == "subject"
    assert "subject" not in draft["answers"]
    assert not draft.get("name"), "cleared, so the mirror cannot put it straight back"
    assert "Who or where" not in reply

    _, draft = _run(monkeypatch, "Dr. Sara Ahmed", ctx, {})
    assert draft["answers"]["subject"] == "Dr. Sara Ahmed"
    assert draft["name"] == "Dr. Sara Ahmed", "the subject answer IS the name"


def test_a_where_step_is_answered_on_the_map_not_in_a_text_box() -> None:
    """A hand-typed "where is it?" is a string nobody can navigate to, so the location steps
    come back as kind=place and the FE renders the Places picker."""
    kinds = {s["field"]: s["kind"] for s in validate_steps([], "location")}
    # A location's SUBJECT is the map point, so it is the place step — and the set's own
    # "where is it?" is dropped rather than asked right after it (dev QA 2026-09-04).
    assert kinds["subject"] == "place"
    assert "where" not in kinds
    assert kinds["known_for"] == "text"
    # A person is not a place: professional keeps its own map step, and its subject is typed.
    prof = {s["field"]: s["kind"] for s in validate_steps([], "professional")}
    assert prof["subject"] == "text"
    assert prof["where"] == "place"

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
