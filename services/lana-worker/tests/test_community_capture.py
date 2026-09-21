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
        assert head["field"] == COMMUNITY_SUBJECT_FIELD, f"{t} does not lead with the subject"
        # A creator community has no building to pin, so its subject is the name the
        # creator types (20261207120000). Every OTHER type still comes off the map: a gym
        # answered as free text cannot be grounded, and an ungrounded community is
        # invisible everywhere.
        want = "text" if t == "creator" else "place"
        assert head["kind"] == want, f"{t} subject must be {want}, not {head['kind']}"
        # Offered first, never mandatory (20261207120000). A community that is not
        # anywhere is a real community — a creator's following, a group with no venue —
        # and making the place a gate is what left "long course triathlon and race prep"
        # stuck on a place search it could never satisfy.
        assert not head["required"], f"{t} still forces a place before it can publish"
        assert steps[-1]["field"] == "who_for", f"{t} lost the closing step"
        fields = [s["field"] for s in steps]
        assert len(fields) == len(set(fields)), f"{t} repeats a field"
        assert all(s["question"].endswith("?") for s in steps), f"{t} has a non-question"


def test_a_community_set_has_no_second_place_step() -> None:
    """The subject already pinned the map, so a "where is it?" step would ask for the
    thing the picker just returned — the same duplicate the reco capture dropped.

    A creator community has no place step at all, and must not acquire one: it is not
    anywhere, and a stray map answer would give it coordinates the schema forbids.
    """
    for t in CIRCLE_TYPES:
        kinds = [s["kind"] for s in validate_community_steps(None, t)]
        want = 0 if t == "creator" else 1
        assert kinds.count("place") == want, f"{t} has {kinds.count('place')} place steps, wanted {want}"

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
    # The subject is asked first but does not gate: what a neighbour cannot do without is
    # what happens here and when, not where.
    assert missing_required(steps, {}) == ["draws", "when"]
    assert missing_required(steps, {"draws": "lifting", "when": "6am"}) == []

    # The subject still LEADS the set — the carousel renders steps in order, so the place
    # is card 1 of 6 with an Optional affordance on it.
    assert steps[0]["field"] == COMMUNITY_SUBJECT_FIELD

    # In the chat walk it trades places with the floor, because `next_question` lets
    # required steps jump the queue. Offered once, never a gate.
    assert next_question(steps, {})["field"] == "draws"
    assert next_question(steps, {"draws": "lifting", "when": "6am"})["field"] == (
        COMMUNITY_SUBJECT_FIELD
    )


# ── The place is never text ───────────────────────────────────────────────────────────


def test_the_place_only_ever_comes_from_the_picker(monkeypatch: Any) -> None:
    """A typed place name cannot be grounded, and an ungrounded community is invisible
    everywhere — so text answers to the subject step are dropped and it is re-asked."""
    ctx: dict[str, Any] = {}
    _run(monkeypatch, "our bakery", ctx, {"circle_type": "friends", "name": "our bakery"})

    _, draft = _run(monkeypatch, "Rosetta's Bakery on Main Street", ctx, {})
    # Still the invariant, and the one the optional place did NOT relax: on a kind="place"
    # step only the picker can answer. Typed text is not a map point, so it is dropped
    # rather than stored as if the community had been grounded.
    assert COMMUNITY_SUBJECT_FIELD not in (draft.get("answers") or {})
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
    # The type is still the one thing nothing can be asked without — it picks the set.
    result, err = cc.publish_community(draft={"google_place_id": "x"}, user_id="u")
    assert (result, err) == (None, "type_required")
    # With no place AND no name there is nothing to key the community on, so it re-asks
    # rather than minting an unnameable row.
    result, err = cc.publish_community(draft={"circle_type": "friends"}, user_id="u")
    assert (result, err) == (None, "name_required")


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


def test_the_lane_survives_its_own_seed_turn() -> None:
    """dev 2026-09-07: the "Create a community" CTA came back as a decide_turn pitch
    ("want to set one up for your kids, your gym…?") with policy chips and no capture.
    The matcher armed the lane, then the release guard re-read the SAME utterance with the
    classifier — which calls a bare create sharing.host — and released it on the spot, so
    the turn fell through to the policy. A seed turn has nothing to pivot away from."""
    host_misread = {
        "linear_intent": "sharing.host",
        "signal_intent": "host_meet",
        "goal": "save_signal",
        "confidence": 0.9,
    }
    seed = {"community_create_active": True, "community_turns": 0}
    assert not cc.community_capture_should_release(
        "I want to create a community", seed, host_misread
    )
    # Mid-flow the same misread IS a pivot — the user is never trapped.
    mid = {"community_create_active": True, "community_turns": 2}
    assert cc.community_capture_should_release("host a coffee morning", mid, host_misread)


def test_the_lanes_own_intent_keeps_the_turn() -> None:
    """`community.create` is not a registered intent, so the lane's native list could
    never match its own classified turn and every mid-flow turn released."""
    from app.layer1_intents import LINEAR_INTENTS

    assert cc._NATIVE_LINEARS <= LINEAR_INTENTS
    mine = {
        "linear_intent": "sharing.community",
        "goal": "create_community",
        "confidence": 0.9,
    }
    mid = {"community_create_active": True, "community_turns": 3}
    assert not cc.community_capture_should_release("make my gym a community", mid, mine)


def test_the_router_is_told_the_capture_is_in_flight() -> None:
    """With active_capture=none the router read the "which place?" answer ("Rosetta's
    Bakery — best sourdough on the block") as sharing.tip, which is a confident pivot, so
    the lane released one turn after it opened and Lana answered the user's own community
    with Google listings — the tip capture's 2026-08-05 bug, same cause."""
    from app.discovery_slots import _active_capture_context

    line = _active_capture_context(
        {
            "community_create_active": True,
            "community_pending_question": "Which place should I add as a community?",
        }
    )
    assert line.startswith("community_create")
    assert "sharing.community" in line
    assert _active_capture_context({}) == "none"


def test_a_creator_community_can_actually_be_named(monkeypatch: Any) -> None:
    """The one type whose subject is text, not a map point.

    The step set has always declared a creator's subject as kind="text" (asserted above),
    because there is no building to pin — its question is literally "What's the community
    called?". But the pending handler dropped every typed subject answer regardless, so
    publish_community's "the subject answer wins, draft['name'] is only the fallback" rule
    could never fire. Result on prod: a community named "people who follow my Jack Russell
    account" — the phrase the creator opened with, not the name they chose.
    """
    ctx: dict[str, Any] = {}
    _run(monkeypatch, "I want a community for people who follow my Jack Russell account",
         ctx, {"circle_type": "creator", "name": "people who follow my Jack Russell account"})
    # The subject is not the first thing asked — answer whatever leads, then the name step.
    _run(monkeypatch, "Dog people", ctx, {})

    _, draft = _run(monkeypatch, "Jack Russell Owners Club", ctx, {})
    assert (draft.get("answers") or {}).get(COMMUNITY_SUBJECT_FIELD) == "Jack Russell Owners Club"

    # And publish resolves to it, not to the opening phrase.
    answers = draft.get("answers") or {}
    resolved = str(answers.get(COMMUNITY_SUBJECT_FIELD) or "").strip() or str(
        draft.get("name") or ""
    ).strip()
    assert resolved == "Jack Russell Owners Club"


def test_a_placed_community_still_refuses_a_typed_subject(monkeypatch: Any) -> None:
    """The exception is scoped to the step's own kind. A gym answered as free text still
    cannot be grounded, and an ungrounded community is invisible everywhere."""
    ctx: dict[str, Any] = {}
    _run(monkeypatch, "a community for my gym", ctx,
         {"circle_type": "fitness", "name": "my gym"})
    # Walk to the subject step the same way, then try to answer it with text.
    for _ in range(4):
        if str(ctx.get("community_pending_ask") or "") == COMMUNITY_SUBJECT_FIELD:
            break
        _run(monkeypatch, "something", ctx, {})
    assert str(ctx.get("community_pending_ask") or "") == COMMUNITY_SUBJECT_FIELD, "never reached it"

    _, draft = _run(monkeypatch, "Fitness CF on Main Street", ctx, {})
    assert COMMUNITY_SUBJECT_FIELD not in (draft.get("answers") or {})


def _walk_to_subject(monkeypatch: Any, ctx: dict[str, Any]) -> None:
    """Open a creator capture and answer steps until the subject is the pending question."""
    _run(monkeypatch, "I want a creator community", ctx,
         {"circle_type": "creator", "name": "my thing"})
    for _ in range(5):
        if str(ctx.get("community_pending_ask") or "") == COMMUNITY_SUBJECT_FIELD:
            return
        _run(monkeypatch, "something", ctx, {})
    raise AssertionError("never reached the subject step")


def test_a_name_containing_a_control_word_is_a_name(monkeypatch: Any) -> None:
    """_CANCEL_RE matches stop / never mind / not now; _PASS_RE matches pass / skip / done /
    all good. At a step whose answer is a NAME those eat real ones — "Stop the Stigma"
    destroyed the whole draft and "Pass the Mic" was silently dropped. Both are plausible
    community names, and neither is a command."""
    for name in ("Stop the Stigma", "Pass the Mic", "All Good Vibes", "Skip Day Club"):
        ctx: dict[str, Any] = {}
        _walk_to_subject(monkeypatch, ctx)
        _, draft = _run(monkeypatch, name, ctx, {})
        assert (draft.get("answers") or {}).get(COMMUNITY_SUBJECT_FIELD) == name, name


def test_a_bare_control_word_still_gets_the_user_out(monkeypatch: Any) -> None:
    """The exception is scoped to the phrase being essentially the WHOLE message. Someone
    who genuinely wants out while being asked the name must not be trapped."""
    ctx: dict[str, Any] = {}
    _walk_to_subject(monkeypatch, ctx)
    _run(monkeypatch, "cancel", ctx, {})
    assert not ctx.get("community_draft"), "a bare cancel must still drop the draft"


def test_the_published_name_is_what_the_caller_sees(monkeypatch: Any) -> None:
    """publish_community rebound `draft` locally, so the row got the chosen name while the
    community filter label and the celebration line kept printing the extractor's opening
    phrase. Row and copy disagreeing is worse than both being wrong."""
    draft: dict[str, Any] = {
        "circle_type": "creator",
        "name": "people who follow my Jack Russell account",
        "answers": {COMMUNITY_SUBJECT_FIELD: "Jack Russell Owners Club"},
    }
    with mock.patch("app.circles_flow.add_circle",
                    return_value={"place_id": "p1", "affiliation_id": "a1"}), \
         mock.patch.object(cc, "upsert_place_feature", create=True, return_value=None):
        cc.publish_community(draft=draft, user_id="u1")
    assert draft["name"] == "Jack Russell Owners Club"
