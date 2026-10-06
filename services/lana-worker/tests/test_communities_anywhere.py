"""Communities that are not anywhere can be found, and say where they are run from.

Asjid, 2026-10-06: a podcasters community made in chat had no location, so every search —
the map, "near me", Lana's "is there a community for…" — passed it by. Search now also runs
on what a community IS (discover_communities_anywhere, 20270109120000), and a community
with no place is asked where it is run from before it goes live.
"""

from __future__ import annotations

from typing import Any
from unittest import mock

import app.community_capture as cc
import app.community_discovery as cd
from app.community_hq import _label, geocode_city
from app.discovery_slots import slots_community_topic

_POD = {
    "place_id": "pPod",
    "name": "Podcast Club",
    "place_type": None,
    "hq_city": "Orlando, FL",
    "member_count": 3,
    "is_member": False,
    "matched_on": "about",
    "matched_label": None,
}


class _Rpc:
    def __init__(self, data: Any = None, exc: Exception | None = None) -> None:
        self.data, self.exc, self.calls = data, exc, []

    def rpc(self, fn: str, params: dict[str, Any]) -> "_Rpc":
        self.calls.append((fn, params))
        return self

    def execute(self) -> Any:
        if self.exc:
            raise self.exc
        return mock.Mock(data=self.data)


# ── the search wrapper ───────────────────────────────────────────────────────────


def test_anywhere_search_asks_sql_and_shapes_cards(monkeypatch: Any) -> None:
    client = _Rpc([_POD, dict(_POD, place_id="pAddr", name="12345")])
    monkeypatch.setattr(cd, "service_client", lambda: client)

    rows = cd.discover_communities_anywhere("u1", "podcasting", placeless_only=False, limit=3)

    fn, args = client.calls[0]
    assert fn == "discover_communities_anywhere"
    assert args["p_query"] == "podcasting" and args["p_placeless_only"] is False
    assert args["p_limit"] == 3
    # Own words only: no member-claim vector is sent (20270110120000).
    assert set(args) == {"p_user_id", "p_query", "p_placeless_only", "p_limit"}
    # A bare ZIP "community" is never offered; the real one is.
    assert [r["place_id"] for r in rows] == ["pPod"]
    card = rows[0]
    # Not anywhere: no point, no address — the HQ is provenance in the status line.
    assert card["lat"] is None and card["place_address"] is None
    assert card["status_line"].startswith("Run from Orlando, FL · ")
    assert card["matched_on"] == "about"


def test_anywhere_search_never_embeds(monkeypatch: Any) -> None:
    """Matching members' claims made a gym a podcast community (prod 2026-10-06); the
    search no longer reads them, so it no longer pays for an embedding either."""
    monkeypatch.setattr(cd, "service_client", lambda: _Rpc([_POD]))
    embed = mock.Mock(side_effect=AssertionError("embedded"))
    monkeypatch.setattr("app.layer1_handlers._embed_attr_filter", embed)
    assert cd.discover_communities_anywhere("u1", "podcasting")[0]["place_id"] == "pPod"
    embed.assert_not_called()


def test_anywhere_search_fails_soft(monkeypatch: Any) -> None:
    monkeypatch.setattr(cd, "service_client", lambda: _Rpc(exc=RuntimeError("db")))
    monkeypatch.setattr("app.layer1_handlers._embed_attr_filter", lambda ask: None)
    assert cd.discover_communities_anywhere("u1", "podcasting") == []
    assert cd.discover_communities_anywhere("u1", "  ") == []


# ── Lana's answer ────────────────────────────────────────────────────────────────


def _turn(monkeypatch: Any, *, anywhere: list, nearby: list, **kw: Any) -> tuple[str, dict, dict]:
    seen: dict = {}

    def compose(*, goal: str, facts: list, fallback: str, **_: Any) -> str:
        seen.update(goal=goal, facts=facts)
        return fallback

    monkeypatch.setattr("app.reply_compose.compose_reply", compose)
    monkeypatch.setattr(cd, "_my_communities", lambda uid: [])
    monkeypatch.setattr(cd, "discover_communities_anywhere", lambda *a, **k: list(anywhere))
    monkeypatch.setattr(cd, "discover_communities", lambda *a, **k: list(nearby))
    ctx: dict[str, Any] = {}
    out = cd.communities_chat_turn("u1", message="any communities for podcasters?",
                                   session_ctx=ctx, **kw)
    return out, ctx, seen


def _card(pid: str, name: str, *, member: bool = False) -> dict:
    return {"place_id": pid, "place_name": name, "is_member": member,
            "status_line": "Run from Orlando, FL · 3 members", "member_count": 3}


def test_a_topic_ask_finds_the_placeless_community(monkeypatch: Any) -> None:
    out, ctx, seen = _turn(monkeypatch, anywhere=[_card("pPod", "Podcast Club")], nearby=[],
                           community_topic="podcasting")
    cards = ctx["community_discovery"]["communities"]
    assert [c["place_id"] for c in cards] == ["pPod"]
    # The card heading says what this list is, not "near you".
    assert ctx["community_discovery"]["topic"] == "podcasting"
    # The card can be joined from the next message, exactly like a nearby one.
    assert ctx["community_join_pending"]["places"][0]["place_id"] == "pPod"
    joined = " ".join(seen["facts"])
    assert "podcasting" in joined and "from anywhere" in joined
    assert "Never call these near them" in joined


def test_a_topic_with_nothing_offers_to_start_one(monkeypatch: Any) -> None:
    out, ctx, seen = _turn(monkeypatch, anywhere=[], nearby=[], community_topic="beekeeping")
    assert "no community about beekeeping" in out.lower()
    assert ctx["community_join_pending"] is None
    assert "start one" in seen["goal"]


def test_their_own_topic_community_is_shown_first_but_not_offered_to_join(
    monkeypatch: Any,
) -> None:
    """Hiding it made "Podcasters" vanish for the person who started it (prod 2026-10-06)."""
    out, ctx, seen = _turn(
        monkeypatch,
        anywhere=[_card("pPod", "Podcast Club"), _card("pMine", "My Pod Crew", member=True)],
        nearby=[], community_topic="podcasting",
    )
    assert [c["place_id"] for c in ctx["community_discovery"]["communities"]] == ["pMine", "pPod"]
    assert [p["place_id"] for p in ctx["community_join_pending"]["places"]] == ["pPod"]
    assert any("ALREADY IN one about it: My Pod Crew" in f for f in seen["facts"])


def test_when_theirs_is_the_only_one_lana_says_so(monkeypatch: Any) -> None:
    out, ctx, seen = _turn(
        monkeypatch, anywhere=[_card("pMine", "Podcasters", member=True)], nearby=[],
        community_topic="podcasting",
    )
    assert "You're already in Podcasters" in out
    assert ctx["community_join_pending"] is None
    assert "start one" not in seen["goal"]


def test_the_topic_reaches_the_response_card() -> None:
    from app.main import _community_discovery_from_ctx

    resp = _community_discovery_from_ctx({"community_discovery": {
        "communities": [{"place_id": "pPod", "place_name": "Podcast Club"}],
        "topic": "podcasting",
    }})
    assert resp is not None and resp.topic == "podcasting"
    near = _community_discovery_from_ctx({"community_discovery": {
        "communities": [{"place_id": "pGym", "place_name": "Gym"}]}})
    assert near is not None and near.topic is None


def test_no_topic_keeps_the_nearby_answer(monkeypatch: Any) -> None:
    called: list = []
    monkeypatch.setattr(cd, "_topic_communities_turn", lambda *a, **k: called.append(1) or "T")
    monkeypatch.setattr("app.community_affinity.attach_affinity", lambda *a, **k: None)
    _turn(monkeypatch, anywhere=[], nearby=[], community_topic=None)
    assert called == []


def test_a_named_community_far_away_is_found(monkeypatch: Any) -> None:
    far = dict(_card("pSJSU", "San Jose State University"), matched_on="name")
    asked: dict = {}

    def anywhere(uid: str, q: str, **k: Any) -> list:
        asked.update(q=q, **k)
        return [far]

    monkeypatch.setattr(cd, "_my_communities", lambda uid: [])
    monkeypatch.setattr(cd, "discover_communities", lambda *a, **k: [])
    monkeypatch.setattr(cd, "discover_communities_anywhere", anywhere)
    about = mock.Mock(return_value="ABOUT")
    monkeypatch.setattr(cd, "_community_about_turn", about)
    out = cd.communities_chat_turn("u1", message="tell me about San Jose State University",
                                   session_ctx={}, community_name="San Jose State University")
    assert out == "ABOUT"
    assert asked["placeless_only"] is False
    assert about.call_args.kwargs["community"]["place_id"] == "pSJSU"
    # Its exact name, so it is the answer — not a guess flagged as "did you mean".
    assert about.call_args.kwargs["inexact"] is None


def test_the_topic_slot_is_read_from_the_ai() -> None:
    assert slots_community_topic({"community_topic": " podcasting "}) == "podcasting"
    assert slots_community_topic({"community_topic": None}) is None
    assert slots_community_topic(None) is None


# ── HQ ───────────────────────────────────────────────────────────────────────────


def test_hq_label_reads_like_a_place() -> None:
    us = [
        {"long_name": "Orlando", "types": ["locality"]},
        {"long_name": "Florida", "short_name": "FL", "types": ["administrative_area_level_1"]},
        {"long_name": "United States", "short_name": "US", "types": ["country"]},
    ]
    assert _label(us, "x") == "Orlando, FL"
    pt = [
        {"long_name": "Lisbon", "types": ["locality"]},
        {"long_name": "Portugal", "short_name": "PT", "types": ["country"]},
    ]
    assert _label(pt, "x") == "Lisbon, Portugal"


def test_a_street_address_is_not_an_hq(monkeypatch: Any) -> None:
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "k")
    payload = {"results": [{"types": ["street_address"],
                            "geometry": {"location": {"lat": 1.0, "lng": 2.0}},
                            "address_components": []}]}
    resp = mock.Mock(json=lambda: payload)
    with mock.patch("httpx.Client") as c:
        c.return_value.__enter__.return_value.get.return_value = resp
        assert geocode_city("123 Main St") is None


def _capture(monkeypatch: Any, msg: str, ctx: dict, *, geo: Any = None) -> tuple[str, mock.Mock, mock.Mock]:
    monkeypatch.setattr(cc, "compose_reply", lambda *, goal, facts, fallback, **k: fallback)
    monkeypatch.setattr(cc, "_handle_offer", lambda *a, **k: None)
    monkeypatch.setattr("app.community_hq.geocode_city", lambda text: geo)
    publish = mock.Mock(return_value=({"place_id": "pNew"}, ""))
    write = mock.Mock(return_value={"status": "saved"})
    monkeypatch.setattr(cc, "publish_community", publish)
    monkeypatch.setattr("app.community_hq.write_community_hq", write)
    reply = cc.run_community_capture_turn(
        user_message=msg, session_ctx=ctx, history=[], user_jwt="jwt",
        user_id="u1", home_block_id=None,
    )
    return reply, publish, write


def test_a_placeless_community_asks_where_it_is_run_from_first(monkeypatch: Any) -> None:
    ctx: dict[str, Any] = {"community_ready": True,
                           "community_draft": {"name": "Podcast Club", "circle_type": "hobby"}}
    reply, publish, _ = _capture(monkeypatch, "publish", ctx)
    publish.assert_not_called()
    assert ctx["community_pending_ask"] == "hq"
    assert "city" in reply.lower()

    orlando = {"city": "Orlando, FL", "lat": 28.5, "lng": -81.4}
    reply, publish, write = _capture(monkeypatch, "Orlando", ctx, geo=orlando)
    publish.assert_called_once()
    write.assert_called_once_with("u1", "pNew", {"city": "Orlando, FL", "lat": 28.5, "lng": -81.4})
    assert ctx["community_draft"]["published"] is True
    assert ctx["community_pending_ask"] is None


def test_an_unplaceable_city_is_asked_again_not_published(monkeypatch: Any) -> None:
    ctx: dict[str, Any] = {"community_ready": True, "community_pending_ask": "hq",
                           "community_draft": {"name": "Podcast Club", "circle_type": "hobby"}}
    reply, publish, _ = _capture(monkeypatch, "asdfgh", ctx, geo=None)
    publish.assert_not_called()
    assert "city" in reply.lower()


def test_a_community_on_a_real_place_publishes_without_asking(monkeypatch: Any) -> None:
    ctx: dict[str, Any] = {"community_ready": True,
                           "community_draft": {"name": "Fitness CF", "circle_type": "fitness",
                                               "google_place_id": "gp-gym"}}
    _, publish, write = _capture(monkeypatch, "publish", ctx)
    publish.assert_called_once()
    write.assert_not_called()


def test_a_correction_chip_is_not_read_as_a_city(monkeypatch: Any) -> None:
    ctx: dict[str, Any] = {"community_ready": True, "community_pending_ask": "hq",
                           "community_draft": {"name": "Podcast Club", "circle_type": "hobby"}}
    geo = mock.Mock(return_value={"city": "X", "lat": 1.0, "lng": 1.0})
    monkeypatch.setattr("app.community_hq.geocode_city", geo)
    monkeypatch.setattr(cc, "compose_reply", lambda *, goal, facts, fallback, **k: fallback)
    cc.run_community_capture_turn(
        user_message="fix:name", session_ctx=ctx, history=[], user_jwt="jwt",
        user_id="u1", home_block_id=None,
    )
    geo.assert_not_called()
