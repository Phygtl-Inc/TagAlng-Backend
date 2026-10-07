"""Chapters in chat: "what clubs does SJSU have?" lists the clubs INSIDE SJSU.

2026-10-07, SJSU pilot. Chapters were readable from the API and the community screen but no
chat path reached them. The care is in telling apart a question INSIDE one community
(chapters) from a search ACROSS communities (topic) — that is the classifier's call
(community_ask='chapters'); these tests pin what each read does with it.
"""

from __future__ import annotations

from typing import Any

import app.community_discovery as cd
from app.discovery_slots import slots_community_ask

_SJSU = {"place_id": "pSjsu", "place_name": "San Jose State University"}


def _ch(pid: str, name: str, *, member: bool = False) -> dict:
    return {"place_id": pid, "place_name": name, "is_member": member,
            "status_line": "2 members", "member_count": 2}


def _turn(
    monkeypatch: Any,
    *,
    chapters: list[dict],
    topic_hits: list[dict] | None = None,
    outside: list[dict] | None = None,
    ask: str = "chapters",
    name: str | None = "SJSU",
    topic: str | None = None,
    ctx: dict | None = None,
) -> tuple[str, dict, dict]:
    seen: dict = {"anywhere": []}

    def compose(*, goal: str, facts: list, fallback: str, **_: Any) -> str:
        seen.update(goal=goal, facts=facts)
        return fallback

    def anywhere(uid: str, q: str, **k: Any) -> list[dict]:
        seen["anywhere"].append(k)
        if k.get("placeless_only") is False:
            return [dict(c) for c in (topic_hits or [])]
        return [dict(c) for c in (outside or [])]

    monkeypatch.setattr("app.reply_compose.compose_reply", compose)
    seen["resolved"] = []
    monkeypatch.setattr(
        cd, "_resolve_named_community",
        lambda uid, n: seen["resolved"].append(n) or dict(_SJSU),
    )
    monkeypatch.setattr(cd, "_my_communities", lambda uid: [])
    monkeypatch.setattr(cd, "discover_communities", lambda *a, **k: [])
    monkeypatch.setattr(cd, "discover_communities_anywhere", anywhere)
    monkeypatch.setattr(
        cd, "community_chapters",
        lambda uid, pid: {"place_id": pid, "chapters": [dict(c) for c in chapters]},
    )
    session = ctx if ctx is not None else {}
    out = cd.communities_chat_turn(
        "u1", message="what clubs does SJSU have?", session_ctx=session,
        community_name=name, community_ask=ask, community_topic=topic,
    )
    return out, session, seen


def test_lists_the_clubs_inside_the_named_community(monkeypatch: Any) -> None:
    out, ctx, seen = _turn(monkeypatch, chapters=[_ch("pRcc", "RCC"), _ch("pData", "Data Club")])
    disc = ctx["community_discovery"]
    assert [c["place_id"] for c in disc["communities"]] == ["pRcc", "pData"]
    assert disc["within"]["place_name"] == "San Jose State University"
    # Every card says which community it sits inside.
    assert all(c["parent"]["place_id"] == "pSjsu" for c in disc["communities"])
    assert {p["place_id"] for p in ctx["community_join_pending"]["places"]} == {"pRcc", "pData"}
    assert "INSIDE San Jose State University" in " ".join(seen["facts"])
    assert "San Jose State University has 2 clubs" in out


def test_a_topic_narrows_to_matching_clubs_in_match_order(monkeypatch: Any) -> None:
    chapters = [_ch("pData", "Data Club"), _ch("pRcc", "RCC"), _ch("pChess", "Chess")]
    hits = [{"place_id": "pOther"}, {"place_id": "pRcc"}, {"place_id": "pData"}]
    _, ctx, seen = _turn(monkeypatch, chapters=chapters, topic_hits=hits, topic="AI")
    # Only this community's chapters, best match first; a match outside it is not listed.
    assert [c["place_id"] for c in ctx["community_discovery"]["communities"]] == ["pRcc", "pData"]
    assert seen["anywhere"][0]["by_meaning"] is True


def test_no_club_inside_matches_shows_outside_ones_labelled_as_outside(monkeypatch: Any) -> None:
    outside = [
        dict(_ch("pAi", "AI Builders"), reach="nearby"),
        dict(_ch("pRcc", "RCC"), reach="nearby", parent=_SJSU),  # inside after all — never "outside"
        dict(_ch("pFar", "Far AI"), reach="far"),  # not near: not offered here
    ]
    out, ctx, seen = _turn(
        monkeypatch, chapters=[_ch("pChess", "Chess")], topic_hits=[], outside=outside, topic="AI"
    )
    disc = ctx["community_discovery"]
    assert [c["place_id"] for c in disc["communities"]] == ["pAi"]
    assert disc["within"] is None
    facts = " ".join(seen["facts"])
    assert "None of San Jose State University's clubs on Lana are about AI" in facts
    assert "NOT part of San Jose State University" in facts
    assert "Nothing inside San Jose State University is about AI" in out


def test_no_clubs_at_all_offers_to_start_one_there(monkeypatch: Any) -> None:
    out, ctx, seen = _turn(monkeypatch, chapters=[])
    assert ctx["community_discovery"]["communities"] == []
    assert ctx["community_join_pending"] is None
    assert "has no clubs listed on Lana yet" in " ".join(seen["facts"])
    assert "Want to start a club there?" in out


def test_here_means_the_active_community(monkeypatch: Any) -> None:
    from app.community_scope import CTX_KEY

    ctx = {CTX_KEY: {"place_id": "pSjsu", "name": "San Jose State University"}}
    _, session, seen = _turn(monkeypatch, chapters=[_ch("pRcc", "RCC")], name=None, ctx=ctx)
    assert seen["resolved"] == ["San Jose State University"]
    assert session["community_discovery"]["within"]["place_id"] == "pSjsu"


def test_chapters_with_nothing_named_and_no_active_community_is_not_a_chapters_turn(
    monkeypatch: Any,
) -> None:
    called: list[str] = []
    monkeypatch.setattr(cd, "_chapters_turn", lambda *a, **k: called.append("chapters") or "x")
    _turn(monkeypatch, chapters=[], name=None, topic="AI")
    assert called == []


def test_slots_accept_chapters() -> None:
    assert slots_community_ask({"community_ask": "chapters"}) == "chapters"
    assert slots_community_ask({"community_ask": "nonsense"}) == "about"


# ── the about answer says chapter ↔ parent ───────────────────────────────────────


def _about(monkeypatch: Any, prof: dict, chapters: list[dict]) -> list[str]:
    seen: dict = {}

    def compose(*, goal: str, facts: list, fallback: str, **_: Any) -> str:
        seen["facts"] = facts
        return fallback

    monkeypatch.setattr("app.reply_compose.compose_reply", compose)
    monkeypatch.setattr("app.community_surface.community_profile", lambda *a, **k: dict(prof))
    monkeypatch.setattr(cd, "community_chapters", lambda uid, pid: {"chapters": chapters})
    cd._community_about_turn(
        "u1", community={"place_id": prof["place_id"], "place_name": prof["place_name"]},
        message="tell me about it", session_ctx={},
    )
    return seen["facts"]


def test_about_a_chapter_says_whose_chapter_it_is(monkeypatch: Any) -> None:
    facts = _about(
        monkeypatch,
        {"place_id": "pRcc", "place_name": "RCC", "parent": {"place_name": "San Jose State University"}},
        [],
    )
    assert "It is a chapter (a club inside) of San Jose State University" in facts


def test_about_a_parent_mentions_its_clubs(monkeypatch: Any) -> None:
    facts = _about(monkeypatch, dict(_SJSU), [_ch("pRcc", "RCC"), _ch("pData", "Data")])
    # By name — with only a count the model named a meet as "the club inside it".
    assert any("Clubs (chapters) inside it on Lana: RCC; Data" in f for f in facts)
    facts = _about(monkeypatch, dict(_SJSU), [])
    assert not any("chapters) inside" in f for f in facts)


def test_a_far_named_community_is_looked_up_by_meaning_too(monkeypatch: Any) -> None:
    """"SJSU" asked from far away: no shared word, so only meaning can surface San Jose
    State as a candidate for the alias matcher — never as an exact-name hit."""
    calls: list[dict] = []
    sjsu_row = dict(_SJSU, matched_on="meaning", status_line="4 members", is_member=False)

    def anywhere(uid: str, q: str, **k: Any) -> list[dict]:
        calls.append(k)
        return [dict(sjsu_row)]

    pools_seen: list = []
    monkeypatch.setattr(cd, "_resolve_named_community", lambda uid, n: None)
    monkeypatch.setattr(cd, "_my_communities", lambda uid: [])
    monkeypatch.setattr(cd, "discover_communities", lambda *a, **k: [])
    monkeypatch.setattr(cd, "discover_communities_anywhere", anywhere)
    monkeypatch.setattr(cd, "_ai_alias_match",
                        lambda said, pools: pools_seen.append(pools) or dict(_SJSU))
    monkeypatch.setattr(cd, "_chapters_turn", lambda *a, **k: "chapters:" + k["parent"]["place_id"])
    out = cd.communities_chat_turn("u1", message="what clubs does SJSU have?", session_ctx={},
                                   community_name="SJSU", community_ask="chapters")
    assert calls[0]["by_meaning"] is True and calls[0]["placeless_only"] is False
    # A meaning row is a candidate for the alias matcher, not an exact hit.
    assert any(sjsu_row["place_id"] in [c["place_id"] for c in pool] for pool in pools_seen[0])
    assert out == "chapters:pSjsu"
