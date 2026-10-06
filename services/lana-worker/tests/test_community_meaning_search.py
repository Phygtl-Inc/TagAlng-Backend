"""A community is found by what it means, local and chapter communities included.

Tommaso, 2026-10-06 (SJSU pilot): "a club focused on AI / AI ethics" found neither the
Responsible Computing Club nor San Jose State, and offered nothing further away. The topic
search only read placeless communities, dropped chapters, matched English stems only — and
no community on prod had an embedding to match by meaning (20270119120000).
"""

from __future__ import annotations

from typing import Any
from unittest import mock

import app.community_discovery as cd
import app.community_embeddings as ce


class _Rpc:
    def __init__(self, data: Any = None, fail_first: bool = False) -> None:
        self.data, self.fail_first, self.calls = data, fail_first, []

    def rpc(self, fn: str, params: dict[str, Any]) -> "_Rpc":
        self.calls.append((fn, dict(params)))
        return self

    def execute(self) -> Any:
        if self.fail_first and len(self.calls) == 1:
            raise RuntimeError("PGRST202 function not found")
        return mock.Mock(data=self.data)


_RCC = {
    "place_id": "pRcc",
    "name": "Responsible Computing Club (RCC)",
    "place_type": None,
    "hq_city": None,
    "member_count": 2,
    "is_member": False,
    "matched_on": "meaning",
    "score": 0.76,
    "parent_place_ref": "pSjsu",
    "distance_meters": 1200.0,
    "area": "San Jose",
    "is_placeless": False,
}


def _no_side_effects(monkeypatch: Any) -> None:
    monkeypatch.setattr(ce, "kick", lambda: None)
    monkeypatch.setattr("app.vertex_extract.vertex_embed", lambda text: [0.1] * 768)
    monkeypatch.setattr(
        "app.community_surface.parent_heads",
        lambda refs: {
            "pSjsu": {"place_id": "pSjsu", "place_name": "San Jose State University",
                      "emoji": "🎓", "member_count": 4}
        },
    )


# ── the read ─────────────────────────────────────────────────────────────────────


def test_meaning_search_sends_the_ask_vector_and_radius(monkeypatch: Any) -> None:
    _no_side_effects(monkeypatch)
    client = _Rpc([_RCC])
    monkeypatch.setattr(cd, "service_client", lambda: client)

    rows = cd.discover_communities_anywhere(
        "u1", "a club focused on AI ethics", by_meaning=True, radius_m=8000
    )

    _, args = client.calls[0]
    assert args["p_query_embedding"].startswith("[0.1,")
    assert args["p_min_similarity"] == cd._MEANING_MIN_SIMILARITY
    assert args["p_radius_meters"] == 8000.0
    card = rows[0]
    assert card["reach"] == "nearby" and card["area"] == "San Jose"
    # A chapter is never shown unlabelled.
    assert card["parent"]["place_name"] == "San Jose State University"
    # Distance never reaches the wire.
    assert "distance_meters" not in card


def test_meaning_search_without_radius_is_the_old_call(monkeypatch: Any) -> None:
    _no_side_effects(monkeypatch)
    client = _Rpc([])
    monkeypatch.setattr(cd, "service_client", lambda: client)
    cd.discover_communities_anywhere("u1", "podcasting")
    assert set(client.calls[0][1]) == {"p_user_id", "p_query", "p_placeless_only", "p_limit"}


def test_old_database_still_answers_by_name(monkeypatch: Any) -> None:
    """Worker deployed before 20270119120000: the new arguments 404, the old call runs."""
    _no_side_effects(monkeypatch)
    client = _Rpc([dict(_RCC, parent_place_ref=None)], fail_first=True)
    monkeypatch.setattr(cd, "service_client", lambda: client)
    rows = cd.discover_communities_anywhere("u1", "rcc", by_meaning=True, radius_m=8000)
    assert len(client.calls) == 2
    assert "p_query_embedding" not in client.calls[1][1]
    assert rows and rows[0]["place_id"] == "pRcc"


def test_embedding_outage_falls_back_to_words(monkeypatch: Any) -> None:
    _no_side_effects(monkeypatch)

    def boom(text: str) -> list[float]:
        raise RuntimeError("vertex down")

    monkeypatch.setattr("app.vertex_extract.vertex_embed", boom)
    client = _Rpc([])
    monkeypatch.setattr(cd, "service_client", lambda: client)
    cd.discover_communities_anywhere("u1", "ai ethics", by_meaning=True)
    assert "p_query_embedding" not in client.calls[0][1]


def test_reach() -> None:
    assert cd._reach({"is_placeless": True}, 8000) == "placeless"
    assert cd._reach({"distance_meters": 100.0}, 8000) == "nearby"
    assert cd._reach({"distance_meters": 9000.0}, 8000) == "far"
    # Located, but the caller has no origin: never claimed to be near.
    assert cd._reach({"distance_meters": None}, 8000) == "far"
    assert cd._reach({"distance_meters": 100.0}, None) is None


# ── Lana's answer ────────────────────────────────────────────────────────────────


def _turn(monkeypatch: Any, found: list[dict]) -> tuple[str, dict, dict]:
    seen: dict = {}

    def compose(*, goal: str, facts: list, fallback: str, **_: Any) -> str:
        seen.update(goal=goal, facts=facts)
        return fallback

    asked: dict = {}

    def anywhere(*a: Any, **k: Any) -> list[dict]:
        asked.update(k)
        return [dict(c) for c in found]

    monkeypatch.setattr("app.reply_compose.compose_reply", compose)
    monkeypatch.setattr(cd, "_my_communities", lambda uid: [])
    monkeypatch.setattr(cd, "discover_communities_anywhere", anywhere)
    monkeypatch.setattr(cd, "discover_communities", lambda *a, **k: [])
    ctx: dict[str, Any] = {}
    out = cd.communities_chat_turn(
        "u1", message="a club focused on AI ethics", session_ctx=ctx,
        community_topic="AI ethics",
    )
    seen["asked"] = asked
    return out, ctx, seen


def _card(pid: str, name: str, reach: str, **kw: Any) -> dict:
    return {"place_id": pid, "place_name": name, "is_member": False, "reach": reach,
            "status_line": "2 members", "member_count": 2, **kw}


_SJSU = {"place_id": "pSjsu", "place_name": "San Jose State University"}


def test_topic_ask_finds_the_local_chapter(monkeypatch: Any) -> None:
    out, ctx, seen = _turn(monkeypatch, [_card("pRcc", "RCC", "nearby", parent=_SJSU)])
    assert seen["asked"]["by_meaning"] is True and seen["asked"]["radius_m"]
    assert [c["place_id"] for c in ctx["community_discovery"]["communities"]] == ["pRcc"]
    assert ctx["community_join_pending"]["places"][0]["place_id"] == "pRcc"
    facts = " ".join(seen["facts"])
    assert "Nearby communities about it: 1" in facts
    assert "RCC is a chapter of San Jose State University" in facts


def test_far_one_is_offered_only_when_nothing_closer(monkeypatch: Any) -> None:
    far = _card("pRcc", "RCC", "far", area="San Jose")
    out, ctx, seen = _turn(monkeypatch, [far])
    assert [c["place_id"] for c in ctx["community_discovery"]["communities"]] == ["pRcc"]
    assert "None is near them" in seen["goal"]
    assert "in San Jose" in out

    near = _card("pNear", "AI Reading Group", "nearby")
    _, ctx, seen = _turn(monkeypatch, [far, near])
    assert [c["place_id"] for c in ctx["community_discovery"]["communities"]] == ["pNear"]
    assert "None is near them" not in seen["goal"]


def test_nothing_anywhere_still_offers_to_start_one(monkeypatch: Any) -> None:
    out, ctx, seen = _turn(monkeypatch, [])
    assert "no community about AI ethics yet" in out
    assert ctx["community_join_pending"] is None


# ── keeping vectors filled ───────────────────────────────────────────────────────


def test_embedding_text_is_name_then_blurb() -> None:
    assert ce.embedding_text("RCC", "AI ethics club.") == "RCC. AI ethics club."
    # A bare name is the name arm's job; a vector of it over-matches.
    assert ce.embedding_text("RCC", "  ") == ""
    assert ce.embedding_text(None, "AI ethics club.") == "AI ethics club."


class _Table:
    def __init__(self) -> None:
        self.ops: list[tuple] = []

    def table(self, name: str) -> "_Table":
        self.ops.append(("table", name))
        return self

    def update(self, data: dict) -> "_Table":
        self.ops.append(("update", data))
        return self

    def eq(self, col: str, val: Any) -> "_Table":
        self.ops.append(("eq", col, val))
        return self

    def is_(self, col: str, val: Any) -> "_Table":
        self.ops.append(("is", col, val))
        return self

    def execute(self) -> Any:
        return mock.Mock(data=[])


def test_write_is_conditional_on_the_words_embedded(monkeypatch: Any) -> None:
    """A rename between read and write must not store a vector of the old words."""
    t = _Table()
    monkeypatch.setattr(ce, "service_client", lambda: t)
    monkeypatch.setattr(ce, "_embed", lambda text: "[0.1]")
    assert ce._write({"id": "p1", "name": "RCC", "blurb": "AI ethics."}) is True
    assert ("eq", "blurb", "AI ethics.") in t.ops and ("eq", "name", "RCC") in t.ops
    assert ("update", {"blurb_embedding": "[0.1]"}) in t.ops
    # Nothing to embed: no write at all.
    t2 = _Table()
    monkeypatch.setattr(ce, "service_client", lambda: t2)
    assert ce._write({"id": "p1", "name": "RCC", "blurb": None}) is False
    assert t2.ops == []


def test_kick_runs_once_per_interval(monkeypatch: Any) -> None:
    runs: list[int] = []
    monkeypatch.setattr(ce, "embed_missing", lambda limit=25: runs.append(1) or 0)

    class _Sync:
        def __init__(self, target: Any, **_: Any) -> None:
            self.target = target

        def start(self) -> None:
            self.target()

    monkeypatch.setattr(ce.threading, "Thread", _Sync)
    monkeypatch.setattr(ce, "_last_kick", 0.0)
    monkeypatch.setattr(ce, "_kick_running", False)
    ce.kick()
    ce.kick()
    assert runs == [1]


def test_embed_missing_survives_one_bad_row(monkeypatch: Any) -> None:
    monkeypatch.setattr(ce, "missing", lambda limit: [{"id": "a"}, {"id": "b"}])

    def write(row: dict) -> bool:
        if row["id"] == "a":
            raise RuntimeError("vertex")
        return True

    monkeypatch.setattr(ce, "_write", write)
    assert ce.embed_missing() == 1
