"""Named communities, prod 2026-10-07 (a new account, far away, not a member).

1. "SJSU" → "there isn't a community named SJSU near you": the alias matcher only saw hers,
   the ones near her, and a search for the literal "SJSU". The model now spells the short
   form out and the full names are searched everywhere.
2. "what's going on at SJSU this week?" as an events browse searched her home area — the
   browse never read a named community — and the whole sentence was the topic.
3. "any clubs at San Jose State focused on AI ethics?" answered with the SJSU card and never
   named RCC, the chapter whose blurb is that subject.
"""

from __future__ import annotations

from typing import Any
from unittest import mock

import app.activity_browse as ab
import app.community_discovery as cd

_FULL = {"place_id": "pFull", "place_name": "Westbrook State University", "matched_on": "name"}
_OTHER = {"place_id": "pOther", "place_name": "Westbrook State University Alumni", "matched_on": "name"}
_CLUB = {"place_id": "pClub", "place_name": "Ethics in Tech Society"}


def _anywhere(table: dict[str, list[dict]]):
    def run(user_id: str, query: str, **kw: Any) -> list[dict]:
        return [dict(r) for r in table.get(query, [])]

    return run


def _resolve(said: str, *, spelled: list[str] | None, table: dict, mine=None, alias=None):
    llm = mock.Mock(return_value={"names": spelled} if spelled is not None else None)
    with mock.patch.object(cd, "_my_communities", return_value=list(mine or [])), \
         mock.patch.object(cd, "discover_communities", return_value=[]), \
         mock.patch.object(cd, "discover_communities_anywhere", side_effect=_anywhere(table)), \
         mock.patch.object(cd, "_ai_alias_match", return_value=alias) as judge, \
         mock.patch("app.orchestrator.llm.llm_configured", return_value=True), \
         mock.patch("app.orchestrator.llm.router_model", return_value="m"), \
         mock.patch("app.orchestrator.llm.llm_json", llm):
        return cd.resolve_community_name("u1", said), judge, llm


# ── 1 · the short form is spelled out and searched anywhere ─────────────────────


def test_a_short_form_nobody_near_her_holds_is_found_by_its_full_name() -> None:
    got, judge, llm = _resolve(
        "WSU", spelled=["Westbrook State University"],
        table={"Westbrook State University": [_FULL]},
    )
    assert got["hit"]["place_id"] == "pFull"
    assert got["hit"]["far"] is True  # not hers, not near her: never "near you"
    assert "_expanded_exact" not in got["hit"]
    assert got["inexact"] is None
    judge.assert_not_called()  # the name itself matched: no second model call
    assert llm.call_count == 1


def test_a_short_form_that_stands_for_nothing_stays_an_honest_miss() -> None:
    got, judge, _ = _resolve("ZQX", spelled=[], table={})
    assert got == {"hit": None, "inexact": None, "near": []}
    judge.assert_called_once()  # the old matcher still had its chance


def test_a_spelling_that_names_nothing_on_lana_is_a_miss() -> None:
    got, _j, _ = _resolve("WSU", spelled=["Westbrook State University"], table={})
    assert got["hit"] is None and got["near"] == []


def test_two_places_by_that_full_name_is_a_which_one_never_a_guess() -> None:
    got, judge, _ = _resolve(
        "WSU", spelled=["Westbrook State University"],
        table={"Westbrook State University": [_FULL, _OTHER]},
    )
    assert got["hit"] is None
    assert [c["place_id"] for c in got["near"]] == ["pFull", "pOther"]
    judge.assert_not_called()


def test_her_own_community_by_a_short_form_is_not_called_far() -> None:
    mine = [{"place_id": "pFull", "place_name": "Westbrook State University"}]
    got, _j, _ = _resolve(
        "WSU", spelled=["Westbrook State University"],
        table={"Westbrook State University": [dict(_FULL, is_member=True)]}, mine=mine,
    )
    assert got["hit"]["place_id"] == "pFull" and not got["hit"].get("far")


def test_rows_the_spelling_only_found_by_meaning_go_to_the_judge() -> None:
    loose = {"place_id": "pLoose", "place_name": "Westbrook Rowing", "matched_on": "meaning"}
    got, judge, _ = _resolve(
        "WSU", spelled=["Westbrook State University"],
        table={"Westbrook State University": [loose]}, alias=loose,
    )
    pools = judge.call_args.args[1]
    assert [c["place_id"] for c in pools[-1]] == ["pLoose"]
    assert got["hit"]["place_id"] == "pLoose" and got["hit"]["far"] is True


def test_no_model_means_no_spelling() -> None:
    with mock.patch("app.orchestrator.llm.llm_configured", return_value=False):
        assert cd._alias_expansion_rows("u1", "WSU") == []


def test_the_communities_turn_answers_about_the_spelled_out_place() -> None:
    with mock.patch.object(cd, "resolve_community_name",
                           return_value={"hit": dict(_FULL, far=True), "inexact": None, "near": []}), \
         mock.patch.object(cd, "_community_about_turn", return_value="ABOUT") as about:
        out = cd.communities_chat_turn(
            "u1", message="what's going on at WSU?", session_ctx={},
            community_name="WSU", community_ask="about",
        )
    assert out == "ABOUT"
    assert about.call_args.kwargs["community"]["far"] is True


def test_the_communities_turn_asks_which_one_on_an_ambiguous_name() -> None:
    with mock.patch.object(cd, "resolve_community_name",
                           return_value={"hit": None, "inexact": None, "near": [_FULL, _OTHER]}), \
         mock.patch.object(cd, "_did_you_mean_turn", return_value="WHICH") as which:
        out = cd.communities_chat_turn(
            "u1", message="x", session_ctx={}, community_name="WSU", community_ask="about",
        )
    assert out == "WHICH" and len(which.call_args.kwargs["candidates"]) == 2


# ── 3 · a subject inside a named community is its chapters, named ───────────────


def _named_turn(*, ask: str, topic: str | None, chapters: list[dict]):
    with mock.patch.object(cd, "resolve_community_name",
                           return_value={"hit": dict(_FULL), "inexact": None, "near": []}), \
         mock.patch.object(cd, "community_chapters", return_value={"chapters": chapters}), \
         mock.patch.object(cd, "_chapters_turn", return_value="CHAPTERS") as chap, \
         mock.patch.object(cd, "_community_about_turn", return_value="ABOUT"):
        out = cd.communities_chat_turn(
            "u1", message="x", session_ctx={}, community_name="WSU",
            community_ask=ask, community_topic=topic,
        )
    return out, chap


def test_a_subject_asked_about_a_named_community_searches_its_chapters() -> None:
    out, chap = _named_turn(ask="about", topic="tech ethics", chapters=[dict(_CLUB)])
    assert out == "CHAPTERS"
    assert chap.call_args.kwargs["topic"] == "tech ethics"


def test_without_chapters_the_about_answer_stands() -> None:
    out, _ = _named_turn(ask="about", topic="tech ethics", chapters=[])
    assert out == "ABOUT"


def test_without_a_subject_the_about_answer_stands() -> None:
    out, _ = _named_turn(ask="about", topic=None, chapters=[dict(_CLUB)])
    assert out == "ABOUT"


def test_a_narrowed_chapters_answer_names_the_club() -> None:
    club = dict(_CLUB, status_line="2 members", is_member=False)
    seen: dict[str, Any] = {}

    def compose(*, goal, facts, fallback, **k):
        seen.update(goal=goal, fallback=fallback)
        return fallback

    with mock.patch.object(cd, "community_chapters", return_value={"chapters": [club]}), \
         mock.patch.object(cd, "discover_communities_anywhere", return_value=[dict(club)]), \
         mock.patch("app.reply_compose.compose_reply", side_effect=compose):
        cd._chapters_turn(
            "u1", parent=_FULL, topic="tech ethics", message="x", session_ctx={},
        )
    assert "Ethics in Tech Society" in seen["goal"]
    assert seen["fallback"].startswith("Yes — Ethics in Tech Society")


# ── 2 · the events browse reads a community named in the ask ────────────────────


_EV = {"id": "e1", "title": "Garden Volunteer Day", "starts_at": "2026-10-10T10:00:00",
       "venue_name": "Campus", "cohort_tags": []}


def _browse(msg: str, slots: dict | None, *, resolved: dict | None, ctx: dict | None = None):
    ctx = ctx if ctx is not None else {"activity_browse_active": True,
                                       "browse_draft": {"_asked": True}, "phone_verified": True}
    filt = mock.Mock(side_effect=lambda ev, q, **kw: (list(ev), ""))
    block = mock.Mock(return_value=[])
    admitted = mock.Mock(return_value=None)
    comm_events = mock.Mock(return_value=[dict(_EV)])
    with mock.patch("app.community_discovery.resolve_community_name",
                    return_value={"hit": resolved, "inexact": None, "near": []}) as res, \
         mock.patch("app.community_scope.community_events", comm_events), \
         mock.patch("app.auth.jwt_user_id", return_value="u1"), \
         mock.patch.object(ab, "_fetch_block_events", block), \
         mock.patch.object(ab, "_fetch_admitted_events", admitted, create=True), \
         mock.patch.object(ab, "_filter_events_by_query", filt), \
         mock.patch.object(ab, "_attach_host_names"), \
         mock.patch.object(ab, "_zip_gate_frame", return_value=None), \
         mock.patch.object(ab, "_far_offer", return_value=([], "", [])), \
         mock.patch("app.lana_paths.stretch_offer_enabled", return_value=False), \
         mock.patch("app.orchestrator.llm.llm_configured", return_value=False):
        reply = ab.run_activity_browse_turn(
            user_message=msg, session_ctx=ctx, history=[], user_jwt="jwt",
            home_block_id="b-home", slots=slots, user_id="u1",
        )
    return reply, ctx, dict(filt=filt, block=block, admitted=admitted,
                            comm_events=comm_events, resolve=res)


def test_a_community_named_in_a_browse_is_where_it_reads() -> None:
    msg = "what's going on at WSU this week?"
    reply, ctx, m = _browse(msg, {"community_name": "WSU"}, resolved=dict(_FULL, far=True))
    m["resolve"].assert_called_once_with("u1", "WSU")
    assert m["comm_events"].call_args.args[0] == "pFull"
    m["block"].assert_not_called()
    m["admitted"].assert_not_called()
    assert [p["title"] for p in ctx["activity_previews"]] == ["Garden Volunteer Day"]
    # The whole request reaches the filter ("this week"), told where the rows are.
    assert m["filt"].call_args.args[1] == msg
    assert m["filt"].call_args.kwargs["at_place"] == "Westbrook State University"
    # Scoped for this browse only — the community filter at the top is not switched.
    assert ctx.get("active_community") is None
    assert ctx["browse_draft"]["_community"]["place_id"] == "pFull"
    assert "near you" not in reply


def test_a_name_that_resolves_to_nothing_searches_as_before() -> None:
    _r, ctx, m = _browse("anything at Nowhere Club?", {"community_name": "Nowhere Club"},
                         resolved=None)
    m["comm_events"].assert_not_called()
    m["block"].assert_called_once()
    assert ctx["browse_draft"].get("_community") is None


def test_a_later_turn_naming_nothing_keeps_the_community() -> None:
    _r, ctx, _m = _browse("what's on at WSU?", {"community_name": "WSU"}, resolved=dict(_FULL))
    _r, ctx, m = _browse("only saturday", {}, resolved=None, ctx=ctx)
    m["resolve"].assert_not_called()
    assert m["comm_events"].call_args.args[0] == "pFull"


def test_a_town_replaces_a_named_community() -> None:
    draft = {"_asked": True, "_community": {"place_id": "pFull", "name": "WSU"}}
    with mock.patch("app.search_place.resolve_search_place", return_value=None):
        _r, ctx, m = _browse(
            "what about in Denver?", {"search_place": "Denver"}, resolved=None,
            ctx={"activity_browse_active": True, "browse_draft": draft, "phone_verified": True},
        )
    assert ctx["browse_draft"].get("_community") is None
    m["comm_events"].assert_not_called()


def test_the_topic_is_the_ai_read_not_the_sentence() -> None:
    _r, ctx, m = _browse("any pottery classes this weekend near me",
                         {"activity_topic": "pottery"}, resolved=None)
    assert ctx["browse_draft"]["interest"] == "pottery"
    assert m["admitted"].call_args.kwargs["interest"] == "pottery"
    assert m["filt"].call_args.args[1] == "any pottery classes this weekend near me"


def test_an_open_ask_has_no_topic_to_embed() -> None:
    _r, ctx, m = _browse("what's going on this week?", {"goal": "activities"}, resolved=None)
    assert ctx["browse_draft"]["interest"] == ""
    m["admitted"].assert_not_called()
    m["block"].assert_called_once()


def test_topic_from_slots_fallbacks() -> None:
    assert ab._topic_from_slots(None, "chess") == "chess"  # classifier never ran
    assert ab._topic_from_slots({}, "chess") == ""
    assert ab._topic_from_slots(
        {"signal_intent": "meet_seek", "signal_detail": "tennis"}, "x"
    ) == "tennis"
    assert ab._topic_from_slots({"signal_detail": "tennis"}, "x") == ""


def test_the_slot_parser_keeps_the_activity_topic() -> None:
    from app.discovery_slots import ai_parse_discovery_turn, slots_activity_topic

    with mock.patch("app.discovery_slots.discovery_ai_enabled", return_value=True), \
         mock.patch("app.discovery_slots.llm_json", return_value={
             "linear_intent": "discovery.find_activities", "goal": "activities",
             "confidence": 0.9, "activity_topic": " pottery ", "community_name": "WSU"}):
        slots = ai_parse_discovery_turn(
            "pottery at WSU?", routing_phase="listening", history=[], has_block=True,
            has_identity=False,
        )
    assert slots_activity_topic(slots) == "pottery"
    assert slots["community_name"] == "WSU"
