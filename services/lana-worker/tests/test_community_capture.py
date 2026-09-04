"""Create-a-community capture: the recommendation capture's twin.

What these guard, in order of how badly they bite:

  * the place can NEVER arrive as text — `add_circle` rejects a place-less create, and an
    ungrounded community is invisible everywhere, so a typed place name would produce a
    community nobody can see;
  * nothing is written until the user confirms — a community is shared state other people
    join, not a draft only its author sees;
  * the question set is written once, per type, and every type has one.
"""

from __future__ import annotations

from typing import Any
from unittest import mock

import app.community_capture as cc
from app.circles_capture import CIRCLE_TYPES
from app.community_question_sets import (
    COMMUNITY_SUBJECT_FIELD,
    community_steps_for,
    normalize_community_type,
    validate_community_steps,
)
from app.reco_question_sets import missing_required, next_question

_PLACE = {
    "place_id": "ChIJtest",
    "name": "Rosetta's Bakery",
    "address": "1 Main St, Lake Nona, FL",
    "lat": 28.4,
    "lng": -81.2,
    "zip": "32827",
    "types": ["bakery"],
}


def _run(monkeypatch: Any, msg: str, ctx: dict[str, Any], extracted: dict[str, Any]) -> tuple[str, dict]:
    monkeypatch.setattr(cc, "_extract_fields", lambda **_: extracted)
    monkeypatch.setattr(cc, "_place_suggestions", lambda *a, **k: [])
    monkeypatch.setattr(
        "app.reply_compose.compose_reply", lambda *, goal, facts, fallback, **k: fallback
    )
    reply = cc.run_community_capture_turn(
        user_message=msg,
        session_ctx=ctx,
        history=[],
        user_jwt="jwt",
        user_id="user-1",
        home_block_id="b1",
    )
    return reply, dict(ctx.get("community_draft") or {})


# ── The tables ────────────────────────────────────────────────────────────────────────


def test_every_circle_type_has_an_askable_set() -> None:
    for t in CIRCLE_TYPES:
        steps = validate_community_steps(None, t)
        assert steps, f"{t} generates no questions at all"
        head = steps[0]
        assert head["field"] == COMMUNITY_SUBJECT_FIELD, f"{t} does not lead with the place"
        assert head["kind"] == "place", f"{t} subject must be pinned, never typed"
        assert head["required"], f"{t} could publish with no place"
        assert steps[-1]["field"] == "who_for", f"{t} lost the closing step"
        fields = [s["field"] for s in steps]
        assert len(fields) == len(set(fields)), f"{t} repeats a field"
        assert all(s["question"].endswith("?") for s in steps), f"{t} has a non-question"


def test_a_community_set_has_no_second_place_step() -> None:
    """The subject already pinned the map, so a "where is it?" step would ask for the
    thing the picker just returned — the same duplicate the reco capture dropped."""
    for t in CIRCLE_TYPES:
        kinds = [s["kind"] for s in validate_community_steps(None, t)]
        assert kinds.count("place") == 1, f"{t} asks for a place twice"

    generated = validate_community_steps(
        [{"field": "location", "label": "Location", "question": "Where exactly is it?"}],
        "friends",
    )
    assert "location" not in [s["field"] for s in generated]


def test_unknown_type_generates_nothing() -> None:
    assert validate_community_steps([{"field": "x", "question": "Why?"}], "cafe") == []
    assert community_steps_for("cafe") == []
    assert normalize_community_type("Fitness") == "fitness"
    assert normalize_community_type(None) is None


def test_required_steps_gate_the_card() -> None:
    steps = validate_community_steps(None, "fitness")
    assert missing_required(steps, {}) == [COMMUNITY_SUBJECT_FIELD, "draws", "when"]
    assert next_question(steps, {})["field"] == COMMUNITY_SUBJECT_FIELD
    filled = {COMMUNITY_SUBJECT_FIELD: "CF", "draws": "lifting", "when": "6am"}
    assert missing_required(steps, filled) == []


# ── The place is never text ───────────────────────────────────────────────────────────


def test_the_place_only_ever_comes_from_the_picker(monkeypatch: Any) -> None:
    """A typed place name cannot be grounded, and an ungrounded community is invisible
    everywhere — so text answers to the subject step are dropped and it is re-asked."""
    ctx: dict[str, Any] = {}
    _run(monkeypatch, "our bakery", ctx, {"circle_type": "friends", "name": "our bakery"})
    assert ctx["community_pending_ask"] == COMMUNITY_SUBJECT_FIELD

    _, draft = _run(monkeypatch, "Rosetta's Bakery on Main Street", ctx, {})
    assert COMMUNITY_SUBJECT_FIELD not in (draft.get("answers") or {})
    assert ctx["community_pending_ask"] == COMMUNITY_SUBJECT_FIELD, "still open"
    assert not draft.get("google_place_id")


def test_the_extractor_can_never_write_the_subject(monkeypatch: Any) -> None:
    """Even when the model helpfully returns one — the same reason: it would be a name,
    not a map point."""
    monkeypatch.setattr(
        "app.orchestrator.llm.llm_configured", lambda: True
    )
    monkeypatch.setattr(
        "app.orchestrator.llm.llm_json",
        lambda **_: {
            "name": "Rosetta's Bakery",
            "circle_type": "friends",
            "answers": {COMMUNITY_SUBJECT_FIELD: "Rosetta's Bakery", "draws": "sourdough"},
        },
    )
    found = cc._extract_fields(history=[], user_message="Rosetta's Bakery", prev={})
    assert found["answers"] == {"draws": "sourdough"}


def test_pinning_a_place_answers_the_subject_step() -> None:
    ctx: dict[str, Any] = {"community_draft": {"circle_type": "friends"}}
    with mock.patch("app.places.place_details", return_value=_PLACE):
        details = cc.set_community_place(ctx, google_place_id="ChIJtest")
    assert details
    draft = ctx["community_draft"]
    # Google's fields, not the client's — a caller cannot rename a shared place.
    assert draft["name"] == "Rosetta's Bakery"
    assert draft["google_place_id"] == "ChIJtest"
    assert draft["answers"][COMMUNITY_SUBJECT_FIELD] == "Rosetta's Bakery"
    assert missing_required(validate_community_steps(None, "friends"), draft["answers"]) == [
        "draws",
        "when",
    ]


def test_an_unresolvable_place_pins_nothing() -> None:
    ctx: dict[str, Any] = {"community_draft": {"circle_type": "friends"}}
    with mock.patch("app.places.place_details", return_value=None):
        assert cc.set_community_place(ctx, google_place_id="ChIJbogus") is None
    assert not (ctx["community_draft"].get("google_place_id"))


# ── Nothing is created until they confirm ─────────────────────────────────────────────


def test_the_ready_card_creates_nothing_on_its_own(monkeypatch: Any) -> None:
    """A community is shared state other people join, so the assembled card is a draft
    and the write waits for the CTA."""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "app.circles_flow.add_circle",
        lambda user_id, **kw: calls.append(kw) or {"affiliation_id": "a", "place_id": "p"},
    )
    ctx: dict[str, Any] = {
        "community_draft": {
            "circle_type": "friends",
            "name": "Rosetta's Bakery",
            "google_place_id": "ChIJtest",
            "step_set": validate_community_steps(None, "friends"),
            "answers": {COMMUNITY_SUBJECT_FIELD: "Rosetta's Bakery", "draws": "coffee", "when": "Sat"},
        },
        "community_create_active": True,
        "community_asked_fields": [s["field"] for s in validate_community_steps(None, "friends")],
    }
    _, draft = _run(monkeypatch, "that's it", ctx, {})
    assert ctx["community_ready"] is True
    assert draft["ready"] is True
    assert not draft.get("published")
    assert calls == [], "nothing may be created before the user taps share"


def test_the_share_cta_creates_the_community(monkeypatch: Any) -> None:
    calls: list[dict[str, Any]] = []
    features: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "app.circles_flow.add_circle",
        lambda user_id, **kw: calls.append({"user_id": user_id, **kw})
        or {"affiliation_id": "aff-1", "place_id": "place-1", "grounded": True},
    )
    monkeypatch.setattr(
        "app.circles_capture.upsert_place_feature",
        lambda **kw: features.append(kw) or True,
    )
    ctx: dict[str, Any] = {
        "community_draft": {
            "circle_type": "friends",
            "name": "Rosetta's Bakery",
            "google_place_id": "ChIJtest",
            "blurb": "best sourdough on the block",
            "step_set": validate_community_steps(None, "friends"),
            "answers": {
                COMMUNITY_SUBJECT_FIELD: "Rosetta's Bakery",
                "draws": "everyone catches up over coffee",
                "when": "Saturday mornings",
            },
        },
        "community_create_active": True,
        "community_ready": True,
    }
    _, draft = _run(monkeypatch, "share it with the community", ctx, {})
    assert draft["published"] is True
    assert draft["community_id"] == "place-1"
    assert ctx["community_published_now"] is True
    assert not ctx.get("community_create_active"), "the lane must let go once it's live"

    assert calls == [
        {
            "user_id": "user-1",
            "circle_type": "friends",
            "detail": "Rosetta's Bakery",
            "google_place_id": "ChIJtest",
            "source": "profile_add",
        }
    ]
    # Every answer becomes a place feature, so the community profile reads back what the
    # creator said instead of an empty page. The SUBJECT is not one — it is the place.
    written = {f["key"]: f["value"] for f in features}
    assert written == {
        "draws": "everyone catches up over coffee",
        "when": "Saturday mornings",
        "blurb": "best sourdough on the block",
    }
    assert all(f["place_id"] == "place-1" for f in features)
    assert all(f["contributed_by"] == "user-1" for f in features)


def test_publishing_without_a_pin_reopens_the_place_step(monkeypatch: Any) -> None:
    """`add_circle` rejects a place-less create, so this is recoverable in-turn rather
    than an apology over a lost draft."""
    ctx: dict[str, Any] = {
        "community_draft": {
            "circle_type": "friends",
            "name": "Rosetta's Bakery",
            "step_set": validate_community_steps(None, "friends"),
            "answers": {"draws": "coffee", "when": "Sat"},
        },
        "community_create_active": True,
        "community_ready": True,
    }
    _, draft = _run(monkeypatch, "share it with the community", ctx, {})
    assert not draft.get("published")
    assert ctx["community_pending_ask"] == COMMUNITY_SUBJECT_FIELD
    assert not ctx.get("community_ready")

    result, err = cc.publish_community(draft={"circle_type": "friends"}, user_id="u")
    assert (result, err) == (None, "place_required")
    result, err = cc.publish_community(draft={"google_place_id": "x"}, user_id="u")
    assert (result, err) == (None, "type_required")


# ── Flow shape ───────────────────────────────────────────────────────────────────────


def test_the_type_gate_offers_chips_and_selects_the_set(monkeypatch: Any) -> None:
    """The type picks the question set, so nothing can be asked before it lands."""
    ctx: dict[str, Any] = {}
    reply, draft = _run(monkeypatch, "I want to start a community", ctx, {"name": "CF Fitness"})
    assert ctx["community_pending_ask"] == "circle_type"
    assert draft["suggestions"] == cc.TYPE_SUGGESTIONS
    assert "What kind of place" in reply
    assert not draft.get("step_set")

    # A tapped chip resolves to the taxonomy key, not to its label.
    _, draft = _run(monkeypatch, "A gym or studio", ctx, {})
    assert draft["circle_type"] == "fitness"
    assert [s["field"] for s in draft["step_set"]][0] == COMMUNITY_SUBJECT_FIELD


def test_each_community_draft_gets_its_own_id(monkeypatch: Any) -> None:
    """The FE keys its cards-or-chat pick on this and never on the name — the name lands
    on the subject step, which is the bug that made the tip fork leak between drafts."""
    ctx: dict[str, Any] = {}
    _, first = _run(monkeypatch, "start a community", ctx, {"circle_type": "friends"})
    one = first["draft_id"]
    assert one

    _, again = _run(monkeypatch, "it's the bakery", ctx, {"name": "Rosetta's Bakery"})
    assert again["draft_id"] == one, "same draft, same id"

    cc.reset_community_state(ctx)
    ctx["community_draft"] = None
    _, second = _run(monkeypatch, "start another one", ctx, {"circle_type": "fitness"})
    assert second["draft_id"] and second["draft_id"] != one


def test_changing_the_type_rewrites_the_questions(monkeypatch: Any) -> None:
    """The set was written FOR the old type: keeping it would ask a gym's questions about
    a church."""
    ctx: dict[str, Any] = {
        "community_draft": {
            "circle_type": "fitness",
            "name": "CF Fitness",
            "step_set": validate_community_steps(None, "fitness"),
            "answers": {"draws": "lifting"},
        },
        "community_create_active": True,
    }
    _, draft = _run(monkeypatch, "fix:circle_type", ctx, {})
    assert not draft.get("step_set")
    assert not draft.get("circle_type")
    assert draft["suggestions"] == cc.TYPE_SUGGESTIONS


def test_cancel_drops_the_draft(monkeypatch: Any) -> None:
    ctx: dict[str, Any] = {
        "community_draft": {"circle_type": "friends", "name": "X"},
        "community_create_active": True,
    }
    _run(monkeypatch, "never mind", ctx, {})
    assert not ctx.get("community_draft")
    assert not ctx.get("community_create_active")


def test_create_is_not_swallowed_by_the_hosting_lane() -> None:
    """dev 2026-09-04: every create utterance came back sharing.host — the hosting rule
    claims a bare create by name ("a bare 'I want to create an event' is STILL
    host_meet"). The prompt was fixed for the phrasings that carry a place; the bare one
    still misroutes 4/4, so the structural matcher is what makes the front door
    deterministic."""
    from app.lana_unified_pipeline import _turn_is_community_create

    host_misread = {
        "linear_intent": "sharing.host",
        "signal_intent": "host_meet",
        "goal": "save_signal",
        "confidence": 0.9,
    }
    for text in (
        "i want to create a community",
        "start a community for our bakery",
        "add my gym as a community",
        "can we make it a community?",
        "launch a community",
    ):
        assert cc.looks_like_community_create(text), text
        assert _turn_is_community_create(host_misread, text), text


def test_browsing_communities_is_never_a_create() -> None:
    """The matcher needs a create VERB, which is what keeps the discovery lane's own
    traffic out of it — the mistake that would hijack every "any communities near me?"."""
    from app.lana_unified_pipeline import _turn_is_community_create

    browse = {
        "linear_intent": "discovery.communities",
        "goal": "chat",
        "confidence": 0.95,
    }
    for text in (
        "show me communities around me",
        "what communities am I in",
        "communities I can join",
        "who is in Mizu Sushi",
        "any groups near me?",
        # A community's MEET is one gathering — the hosting lane's, not this one's.
        "create a community event this weekend",
    ):
        assert not cc.looks_like_community_create(text), text
        assert not _turn_is_community_create(browse, text), text


def test_the_classifier_alone_can_arm_it() -> None:
    """No keyword needed when the AI is confident — the utterance half is a floor, not
    the gate."""
    from app.lana_unified_pipeline import _turn_is_community_create

    slots = {
        "linear_intent": "sharing.community",
        "goal": "create_community",
        "confidence": 0.9,
    }
    msg = "Rosetta's Bakery is where everyone ends up on Saturdays, let's make it a thing"
    assert not cc.looks_like_community_create(msg)
    assert _turn_is_community_create(slots, msg)


def test_the_ui_intents_follow_the_capture() -> None:
    from app.ui_intent import derive_ui_intent

    assert derive_ui_intent({"community_create_active": True}) == "collect_community_detail"
    assert derive_ui_intent({"community_published_now": True}) == "community_created"


def test_the_draft_survives_the_wire() -> None:
    """The response model filters to its own fields, so an undeclared key is dropped
    silently — this is what catches a draft key the FE can never see."""
    from app.main import _community_draft_from_dict

    draft = {
        "draft_id": "abc123",
        "name": "Rosetta's Bakery",
        "google_place_id": "ChIJtest",
        "circle_type": "friends",
        "blurb": "best sourdough",
        "pending_field": "draws",
        "steps": validate_community_steps(None, "friends"),
        "answers": {"draws": "coffee"},
        "missing": ["when"],
        "ready": False,
        "published": False,
    }
    model = _community_draft_from_dict(draft)
    assert model
    for key, value in draft.items():
        assert getattr(model, key) or value in (False, [], {}), f"{key} lost on the wire"
    assert model.pending_field == "draws"
    assert model.steps[0].kind == "place"
