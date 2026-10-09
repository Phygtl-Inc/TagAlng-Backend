"""Lana asks whether a new community sits inside a bigger one the creator belongs to.

Creators rarely say "inside SJSU" unprompted, so a club made by an SJSU member went up
standalone (RCC, 2026-10-07). The question is asked ONCE per draft, only when one of the
creator's own communities could hold it — eligibility from the membership read, plausibility
from the AI — and an answer lands in the same draft state `_resolve_parent` writes, so the
chip, the attach and the inherited location run unchanged.
"""

from __future__ import annotations

from typing import Any
from unittest import mock

import pytest

import app.community_capture as cc
import app.community_chapter_ops as ops

SJSU = "11111111-1111-1111-1111-111111111111"
ACME = "22222222-2222-2222-2222-222222222222"
RCC = "33333333-3333-3333-3333-333333333333"


def _row(pid: str, name: str, **kw: Any) -> dict[str, Any]:
    row = {
        "place_id": pid, "place_name": name, "status": "confirmed", "circle_type": "school",
        "relation": "school", "detail": None, "lat": 37.3, "lng": -121.8,
        "google_place_id": f"g-{pid[:4]}", "place_type": None, "parent_place_id": None,
    }
    row.update(kw)
    return row


def _ready_draft(**kw: Any) -> dict[str, Any]:
    """A club draft with every step answered — the next turn goes to the ready card."""
    draft = {
        "draft_id": "d1", "name": "Responsible Computing Club", "circle_type": "hobby",
        "blurb": "students talking AI ethics",
        "step_set": [{"field": "subject", "label": "Where", "question": "Where do you meet?",
                      "kind": "place", "required": False}],
        "answers": {"subject": "Engineering building"},
    }
    draft.update(kw)
    return draft


@pytest.fixture
def env(monkeypatch: Any) -> dict[str, Any]:
    """Membership + judge + interpreter + compose all mocked: no database, no model."""
    state: dict[str, Any] = {
        "rows": [_row(SJSU, "San Jose State University")],
        "judge": lambda draft, cands: cands,  # every candidate plausible unless a test says
        "read": {"choice": "none"},
        "judged": 0,
        "composed": [],
    }
    monkeypatch.setattr("app.circles_flow.list_my_circles", lambda uid: [dict(r) for r in state["rows"]])

    class _NoBlurbs:
        def __getattr__(self, n: str) -> Any:
            return lambda *a, **k: self

        def execute(self) -> Any:
            return mock.Mock(data=[])

    monkeypatch.setattr("app.auth.service_client", lambda: _NoBlurbs())

    real_judge = cc._judge_umbrellas

    def judge(draft: dict, cands: list) -> list:
        # The real judge never calls the model with nothing to judge; count model calls.
        if not cands:
            return []
        state["judged"] += 1
        return state["judge"](draft, cands)[: cc._PARENT_MAX]

    monkeypatch.setattr(cc, "_judge_umbrellas", judge)
    state["real_judge"] = real_judge
    monkeypatch.setattr(cc, "_interpret_parent_answer", lambda msg, offer: state["read"])
    monkeypatch.setattr(cc, "_extract_fields", lambda **_: {})
    monkeypatch.setattr(cc, "_place_suggestions", lambda *a, **k: [])
    monkeypatch.setattr(cc, "_handle_offer", lambda *a, **k: None)
    # The link step settles without a question (no link to claim here), and there is no
    # area to offer for the city step — the closing steps under test are parent and city.
    monkeypatch.setattr(cc, "_link_check", lambda *a, **k: {"status": "not_eligible"})
    monkeypatch.setattr(cc, "_hq_offer", lambda *a, **k: None)

    def compose(*, goal: str, facts: list, fallback: str, **_: Any) -> str:
        state["composed"].append({"goal": goal, "facts": facts})
        return fallback

    monkeypatch.setattr(cc, "compose_reply", compose)
    return state


def _turn(ctx: dict[str, Any], msg: str) -> str:
    return cc.run_community_capture_turn(
        user_message=msg, session_ctx=ctx, history=[], user_jwt="jwt", user_id="u1",
        home_block_id=None,
    )


def _ctx(draft: dict[str, Any], **kw: Any) -> dict[str, Any]:
    ctx: dict[str, Any] = {"community_draft": draft, "community_create_active": True,
                           "community_turns": 3, "community_asked_fields": ["subject"]}
    ctx.update(kw)
    return ctx


# ── when it is asked ──────────────────────────────────────────────────────────────────


def test_asks_when_the_creator_belongs_to_a_plausible_umbrella(env: dict) -> None:
    ctx = _ctx(_ready_draft())
    reply = _turn(ctx, "looks good")
    draft = ctx["community_draft"]
    assert ctx["community_pending_ask"] == "parent"
    assert draft["pending_field"] == "parent"
    # The chips the PWA renders under the message: one per candidate + standalone.
    assert draft["suggestions"] == ["Part of San Jose State University", "On its own"]
    assert ctx["community_offered"] == draft["suggestions"]
    # The first closing question: the card is not ready (no Share) while it is open.
    assert ctx["community_ready"] is None
    assert "San Jose State University" in reply
    # The reply is composed from data: the candidates are facts, never wording in the goal.
    assert "San Jose State University" not in env["composed"][-1]["goal"]
    assert any("San Jose State University" in f for f in env["composed"][-1]["facts"])


def test_caps_the_chips_at_three_candidates(env: dict) -> None:
    env["rows"] = [_row(f"{i}" * 8 + SJSU[8:], f"Org {i}") for i in range(1, 6)]
    ctx = _ctx(_ready_draft())
    _turn(ctx, "looks good")
    assert len(ctx["community_draft"]["suggestions"]) == 4  # 3 candidates + on its own


@pytest.mark.parametrize(
    "rows",
    [
        [],  # no memberships at all
        [_row(SJSU, "SJSU", status="suggested")],  # not a confirmed member
        [_row(SJSU, "Data Club", parent_place_id=ACME)],  # itself a chapter: depth
    ],
)
def test_no_eligible_candidate_means_no_question_and_no_judge(env: dict, rows: list) -> None:
    env["rows"] = rows
    ctx = _ctx(_ready_draft())
    _turn(ctx, "looks good")
    assert ctx.get("community_pending_ask") != "parent"
    assert ctx["community_draft"]["suggestions"] == []
    assert env["judged"] == 0
    # Straight on to the next closing question: this draft has no place, so the city.
    assert ctx["community_pending_ask"] == "hq"


def test_the_place_being_published_is_not_its_own_parent(env: dict) -> None:
    env["rows"] = [_row(SJSU, "Rosetta's Bakery", google_place_id="gRosetta")]
    ctx = _ctx(_ready_draft(google_place_id="gRosetta"))
    _turn(ctx, "looks good")
    assert ctx.get("community_pending_ask") != "parent" and env["judged"] == 0


def test_no_question_when_the_ai_judges_none_plausible(env: dict) -> None:
    env["judge"] = lambda draft, cands: []
    ctx = _ctx(_ready_draft(name="Lake Nona morning walkers", circle_type="neighborhood"))
    _turn(ctx, "looks good")
    assert env["judged"] == 1
    assert ctx.get("community_pending_ask") != "parent"
    assert ctx["community_draft"]["suggestions"] == []
    # Judged once per draft: the next turn does not read memberships or ask the model again.
    _turn(ctx, "looks good")
    assert env["judged"] == 1


def test_never_asked_for_a_creator_community(env: dict) -> None:
    ctx = _ctx(_ready_draft(circle_type="creator", name="Iron Man Training"))
    _turn(ctx, "looks good")
    assert ctx.get("community_pending_ask") != "parent" and env["judged"] == 0


def test_never_asked_when_they_named_the_parent_themselves(env: dict) -> None:
    ctx = _ctx(_ready_draft(parent="SJSU", parent_place={"place_id": SJSU, "place_name": "SJSU",
                                                         "located": True}))
    _turn(ctx, "looks good")
    assert ctx.get("community_pending_ask") != "parent" and env["judged"] == 0
    # Named but not found is still THEIR answer — not re-asked over it.
    ctx = _ctx(_ready_draft(parent="Hogwarts", parent_unresolved=True))
    _turn(ctx, "looks good")
    assert ctx.get("community_pending_ask") != "parent" and env["judged"] == 0


def test_never_asked_again_after_a_decline(env: dict) -> None:
    ctx = _ctx(_ready_draft(parent_asked=True, parent_declined=True))
    _turn(ctx, "looks good")
    assert ctx.get("community_pending_ask") != "parent" and env["judged"] == 0


# ── what an answer does ───────────────────────────────────────────────────────────────


def _asked(env: dict, **draft_kw: Any) -> dict[str, Any]:
    ctx = _ctx(_ready_draft(**draft_kw))
    _turn(ctx, "looks good")
    assert ctx["community_pending_ask"] == "parent"
    return ctx


def test_tapping_a_candidate_sets_the_parent_and_shows_the_chip(env: dict) -> None:
    ctx = _asked(env)
    _turn(ctx, "Part of San Jose State University")
    draft = ctx["community_draft"]
    assert draft["parent"] == "San Jose State University"
    assert draft["parent_place"] == {"place_id": SJSU, "place_name": "San Jose State University",
                                     "located": True, "handle": None, "join_first": False}
    assert {"label": "Part of San Jose State University", "tone": "sky", "field": "parent"} in draft["chips"]
    # A located parent makes the city question unnecessary: straight to the ready card.
    assert ctx.get("community_pending_ask") is None
    assert draft["suggestions"] == [] and draft["ready"] is True
    assert ctx["community_ready"] is True


def test_the_chosen_parent_is_attached_at_publish_without_a_city_ask(env: dict, monkeypatch: Any) -> None:
    ctx = _asked(env)
    _turn(ctx, "Part of San Jose State University")
    publish = mock.Mock(return_value=({"place_id": RCC}, ""))
    monkeypatch.setattr(cc, "publish_community", publish)
    attached: list = []
    monkeypatch.setattr(ops, "attach_chapter", lambda u, c, p: attached.append((u, c, p)) or {"ok": True})
    _turn(ctx, "Share with the community")
    publish.assert_called_once()
    assert attached == [("u1", RCC, SJSU)]
    assert ctx["community_draft"]["parent_attached"] is True
    assert ctx.get("community_pending_ask") != "hq"


def test_standalone_sticks(env: dict, monkeypatch: Any) -> None:
    ctx = _asked(env, google_place_id="gEng")
    _turn(ctx, "On its own")
    draft = ctx["community_draft"]
    assert draft["parent_declined"] is True and not draft.get("parent")
    assert ctx.get("community_pending_ask") is None
    # Not asked again on any later turn of this draft.
    _turn(ctx, "looks good")
    assert ctx.get("community_pending_ask") != "parent"
    assert env["judged"] == 1
    publish = mock.Mock(return_value=({"place_id": RCC}, ""))
    monkeypatch.setattr(cc, "publish_community", publish)
    attach = mock.Mock()
    monkeypatch.setattr(ops, "attach_chapter", attach)
    _turn(ctx, "Share with the community")
    publish.assert_called_once()
    attach.assert_not_called()


def test_typed_answers_are_read_by_the_ai(env: dict) -> None:
    ctx = _asked(env)
    env["read"] = {"choice": "candidate", "n": 1}
    _turn(ctx, "yeah it's an SJSU club")
    assert ctx["community_draft"]["parent_place"]["place_id"] == SJSU

    ctx = _asked(env)
    env["read"] = {"choice": "standalone"}
    _turn(ctx, "no, it's independent")
    assert ctx["community_draft"]["parent_declined"] is True


def test_a_typed_other_name_goes_through_resolve_parent(env: dict, monkeypatch: Any) -> None:
    import app.community_discovery as cd

    ctx = _asked(env)
    env["read"] = {"choice": "other", "name": "IEEE"}
    said: list = []
    monkeypatch.setattr(cd, "find_named_community",
                        lambda uid, s: said.append(s) or {"place_id": ACME, "place_name": "IEEE"})
    _turn(ctx, "it's actually part of IEEE")
    assert said == ["IEEE"]
    assert ctx["community_draft"]["parent_place"]["place_id"] == ACME


def test_unclear_is_asked_once_more_then_dropped(env: dict) -> None:
    env["rows"] = [_row(SJSU, "San Jose State University"), _row(ACME, "Acme Corp")]
    ctx = _asked(env)
    env["read"] = {"choice": "unclear"}
    _turn(ctx, "yes")
    assert ctx["community_pending_ask"] == "parent"
    _turn(ctx, "yes")
    assert ctx.get("community_pending_ask") != "parent"
    assert ctx["community_draft"]["parent_declined"] is True


def test_a_non_answer_drops_the_question_and_goes_on_to_the_ready_card(
    env: dict, monkeypatch: Any
) -> None:
    ctx = _asked(env, google_place_id="gEng")
    publish = mock.Mock(return_value=({"place_id": RCC}, ""))
    monkeypatch.setattr(cc, "publish_community", publish)
    env["read"] = {"choice": "none"}
    _turn(ctx, "Share with the community")
    # No Share button was showing, so nothing is created on this turn; the question is
    # dropped and the ready card comes up.
    publish.assert_not_called()
    assert ctx["community_draft"]["parent_declined"] is True
    assert ctx["community_ready"] is True
    _turn(ctx, "Share with the community")
    publish.assert_called_once()


def test_removing_the_chip_is_a_decline(env: dict) -> None:
    ctx = _asked(env)
    _turn(ctx, "Part of San Jose State University")
    _turn(ctx, "fix:parent")
    assert ctx["community_draft"]["parent_declined"] is True
    _turn(ctx, "looks good")
    assert ctx.get("community_pending_ask") != "parent"


def test_asked_at_share_when_the_ready_card_was_skipped(env: dict, monkeypatch: Any) -> None:
    """The carousel stamps community_ready itself; the question still comes before
    anything is created, and the answer goes on to the ready card (a located parent
    skips the city)."""
    publish = mock.Mock(return_value=({"place_id": RCC}, ""))
    monkeypatch.setattr(cc, "publish_community", publish)
    monkeypatch.setattr(ops, "attach_chapter", lambda u, c, p: {"ok": True})
    ctx = _ctx(_ready_draft(), community_ready=True)
    _turn(ctx, "Share with the community")
    assert ctx["community_pending_ask"] == "parent"
    publish.assert_not_called()
    _turn(ctx, "Part of San Jose State University")
    publish.assert_not_called()
    assert ctx["community_ready"] is True and ctx.get("community_pending_ask") is None
    _turn(ctx, "Share with the community")
    publish.assert_called_once()
    assert ctx["community_draft"]["parent_attached"] is True


def test_chips_are_localized_and_a_localized_tap_still_matches(env: dict, monkeypatch: Any) -> None:
    import app.i18n as i18n

    monkeypatch.setattr(i18n, "session_lang", lambda ctx: "es")
    monkeypatch.setattr(
        i18n, "localize_labels",
        lambda labels, lang: [l.replace("Part of", "Parte de").replace("On its own", "Por su cuenta")
                              for l in labels],
    )
    ctx = _ctx(_ready_draft())
    _turn(ctx, "looks good")
    assert ctx["community_draft"]["suggestions"] == ["Parte de San Jose State University", "Por su cuenta"]
    _turn(ctx, "Parte de San Jose State University")
    assert ctx["community_draft"]["parent_place"]["place_id"] == SJSU


# ── the judge itself, with the model mocked ───────────────────────────────────────────


def test_judge_maps_numbers_back_to_candidates_and_caps(env: dict, monkeypatch: Any) -> None:
    judge = env["real_judge"]
    cands = [{"place_id": str(i), "place_name": f"Org {i}"} for i in range(5)]
    monkeypatch.setattr("app.orchestrator.llm.llm_configured", lambda: True)
    monkeypatch.setattr("app.orchestrator.llm.llm_json",
                        lambda **k: {"umbrellas": [3, "1", 9, 3, 2, 5]})
    got = judge({"name": "x"}, cands)
    assert [c["place_id"] for c in got] == ["2", "0", "1"]
    monkeypatch.setattr("app.orchestrator.llm.llm_json", lambda **k: {"umbrellas": []})
    assert judge({"name": "x"}, cands) == []
    monkeypatch.setattr("app.orchestrator.llm.llm_json", mock.Mock(side_effect=RuntimeError("x")))
    assert judge({"name": "x"}, cands) == []


def test_picking_a_linked_parent_skips_the_link_step(env: dict, monkeypatch: Any) -> None:
    """A chapter of a linked parent gets get.lana.help/{parent}/{chapter} on attach, so —
    as when they named the parent themselves — it is not asked to choose a link."""

    class _Places:
        def __getattr__(self, n: str) -> Any:
            return lambda *a, **k: self

        def execute(self) -> Any:
            return mock.Mock(data=[{"id": SJSU, "blurb": "a university", "handle": "sjsu"}])

    monkeypatch.setattr("app.auth.service_client", lambda: _Places())
    link_checks: list = []
    monkeypatch.setattr(
        cc, "_link_check",
        lambda *a, **k: link_checks.append(a) or {"status": "available", "suggestions": ["rcc"]},
    )
    ctx = _asked(env)
    _turn(ctx, "Part of San Jose State University")
    assert ctx["community_draft"]["parent_place"]["handle"] == "sjsu"
    assert ctx.get("community_pending_ask") is None and ctx["community_ready"] is True
    assert link_checks == []


def test_a_tapped_parent_whose_name_holds_a_cancel_word_is_not_a_cancel(env: dict) -> None:
    env["rows"] = [_row(SJSU, "Stop the Stigma")]
    ctx = _asked(env)
    _turn(ctx, "Part of Stop the Stigma")
    assert ctx["community_draft"]["parent_place"]["place_id"] == SJSU
    assert ctx["community_create_active"] is True


# ── creator communities, the community picked at the top, and joining the parent ──────


class _Places:
    """service_client() whose places reads return `rows` (blurb/handle/scoped reads)."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def __getattr__(self, n: str) -> Any:
        return lambda *a, **k: self

    def execute(self) -> Any:
        return mock.Mock(data=[dict(r) for r in self.rows])


PODCASTERS = "44444444-4444-4444-4444-444444444444"


def test_a_creator_community_can_be_offered_as_a_parent(env: dict) -> None:
    # attach_chapter refuses a creator community only as the CHAPTER; as a parent it is a
    # topic that holds local branches (Podcasters → Podcasters Orlando).
    env["rows"] = [_row(PODCASTERS, "Podcasters", place_type="creator", circle_type="creator",
                        google_place_id="creator:podcasters", lat=None, lng=None)]
    ctx = _ctx(_ready_draft(name="Podcasters Orlando", blurb="podcasters meeting in Orlando"))
    _turn(ctx, "looks good")
    assert ctx["community_draft"]["suggestions"][0] == "Part of Podcasters"


def test_the_judge_is_told_which_candidates_are_spread_out(env: dict, monkeypatch: Any) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr("app.orchestrator.llm.llm_configured", lambda: True)
    monkeypatch.setattr("app.orchestrator.llm.llm_json",
                        lambda **k: seen.update(k) or {"umbrellas": [1]})
    env["real_judge"]({"name": "Podcasters Orlando"},
                      [{"place_id": PODCASTERS, "place_name": "Podcasters", "located": False},
                       {"place_id": SJSU, "place_name": "SJSU", "located": True}])
    assert '"spread_out": true' in seen["user_payload"]
    assert '"spread_out": false' in seen["user_payload"]


def test_the_scoped_community_is_offered_first_without_the_judge(env: dict) -> None:
    env["rows"] = [_row(SJSU, "San Jose State University"), _row(ACME, "Acme Corp")]
    judged: list = []
    env["judge"] = lambda draft, cands: judged.append([c["place_id"] for c in cands]) or cands
    ctx = _ctx(_ready_draft(), active_community={"place_id": ACME, "name": "Acme Corp"})
    _turn(ctx, "looks good")
    assert ctx["community_draft"]["suggestions"][:2] == ["Part of Acme Corp",
                                                         "Part of San Jose State University"]
    assert judged == [[SJSU]]  # the scoped one never went to the judge


def test_a_scoped_community_they_are_not_in_is_offered_and_joined_at_publish(
    env: dict, monkeypatch: Any
) -> None:
    env["rows"] = []  # no memberships at all
    monkeypatch.setattr("app.auth.service_client", lambda: _Places(
        [{"id": PODCASTERS, "name": "Podcasters", "lat": None, "lng": None,
          "handle": "podcaster", "parent_place_ref": None, "governance_state": None,
          "google_place_id": "creator:podcasters"}]))
    ctx = _ctx(_ready_draft(name="Podcasters Orlando", google_place_id="gLib"),
               active_community={"place_id": PODCASTERS, "name": "Podcasters"})
    _turn(ctx, "looks good")
    draft = ctx["community_draft"]
    assert draft["suggestions"] == ["Part of Podcasters", "On its own"]
    assert draft["parent_offer"][0]["join_first"] is True
    assert any("not a member of Podcasters" in f for f in env["composed"][-1]["facts"])
    assert env["judged"] == 0

    _turn(ctx, "Part of Podcasters")
    assert ctx["community_draft"]["parent_place"]["join_first"] is True
    # Publish: the first attach is refused for membership, they join, the attach reruns.
    monkeypatch.setattr(cc, "publish_community", lambda **k: ({"place_id": RCC}, ""))
    attaches = iter([{"ok": False, "reason": "not_a_member_of_parent"}, {"ok": True}])
    monkeypatch.setattr(ops, "attach_chapter", lambda u, c, p: next(attaches))
    joins: list = []
    monkeypatch.setattr("app.community_discovery.join_community",
                        lambda u, p, **k: joins.append(p) or {"place_id": p})
    _turn(ctx, "Share with the community")
    assert joins == [PODCASTERS]
    assert ctx["community_draft"]["parent_attached"] is True
    assert any("now a member of it too" in f for c in env["composed"] for f in c["facts"])


def test_a_scoped_community_that_is_itself_a_chapter_is_not_offered(
    env: dict, monkeypatch: Any
) -> None:
    env["rows"] = []
    monkeypatch.setattr("app.auth.service_client", lambda: _Places(
        [{"id": RCC, "name": "RCC", "lat": 1.0, "lng": 2.0, "handle": None,
          "parent_place_ref": SJSU, "governance_state": None, "google_place_id": "gRcc"}]))
    ctx = _ctx(_ready_draft(), active_community={"place_id": RCC, "name": "RCC"})
    _turn(ctx, "looks good")
    assert ctx.get("community_pending_ask") != "parent"


def test_a_named_parent_they_are_not_in_is_joined_then_attached(
    env: dict, monkeypatch: Any
) -> None:
    ctx = _ctx(_ready_draft(google_place_id="gLib", parent="Podcasters",
                            parent_place={"place_id": PODCASTERS, "place_name": "Podcasters",
                                          "located": False}),
               community_ready=True)
    monkeypatch.setattr(cc, "publish_community", lambda **k: ({"place_id": RCC}, ""))
    attaches = iter([{"ok": False, "reason": "not_a_member_of_parent"}, {"ok": True}])
    monkeypatch.setattr(ops, "attach_chapter", lambda u, c, p: next(attaches))
    joins: list = []
    monkeypatch.setattr("app.community_discovery.join_community",
                        lambda u, p, **k: joins.append(p) or {"place_id": p})
    _turn(ctx, "Share with the community")
    assert joins == [PODCASTERS] and ctx["community_draft"]["parent_attached"] is True


def test_a_failed_join_leaves_it_standalone_and_says_why(env: dict, monkeypatch: Any) -> None:
    ctx = _ctx(_ready_draft(google_place_id="gLib", parent="Podcasters",
                            parent_place={"place_id": PODCASTERS, "place_name": "Podcasters",
                                          "located": False}),
               community_ready=True)
    monkeypatch.setattr(cc, "publish_community", lambda **k: ({"place_id": RCC}, ""))
    monkeypatch.setattr(ops, "attach_chapter",
                        lambda u, c, p: {"ok": False, "reason": "not_a_member_of_parent"})

    def boom(*a: Any, **k: Any) -> Any:
        raise ValueError("join_failed")

    monkeypatch.setattr("app.community_discovery.join_community", boom)
    _turn(ctx, "Share with the community")
    assert ctx["community_draft"]["parent_attached"] is False
    assert any("not a member of Podcasters" in f for c in env["composed"] for f in c["facts"])
