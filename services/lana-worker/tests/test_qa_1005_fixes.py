"""QA run 2026-10-05: "which communities am I in?" never named theirs, "neighbors" on
surfaces the final-mile guard never sees, and typed community answers left untagged."""

from __future__ import annotations

from typing import Any
from unittest import mock

_MINE = [
    {"id": "a1", "place_id": "pS", "place_name": "San Jose State University", "member_count": 1},
    {"id": "a2", "place_id": "pR", "place_name": "Responsible Computing Club", "member_count": 1},
    {"id": "a3", "place_id": "pB", "place_name": "Bay Club Fitness", "member_count": 3},
]
_NEARBY = [{"place_id": "pX", "place_name": "QA Pausa", "status_line": "2 people", "is_member": False}]


def _turn(ask: str, mine=_MINE, nearby=_NEARBY) -> dict[str, Any]:
    from app.community_discovery import communities_chat_turn

    with mock.patch("app.community_discovery._my_communities", return_value=list(mine)), mock.patch(
        "app.community_discovery.discover_communities", return_value=list(nearby)
    ), mock.patch("app.community_affinity.attach_affinity"), mock.patch(
        "app.community_surface.communities_card", return_value=None
    ), mock.patch("app.reply_compose.compose_reply", side_effect=lambda **kw: kw):
        return communities_chat_turn("u1", message="what communities am I in?", session_ctx={},
                                     community_ask=ask)


def test_which_am_i_in_names_every_one_of_theirs() -> None:
    out = _turn("mine")
    facts = " ".join(out["facts"])
    for c in _MINE:
        assert c["place_name"] in facts
    assert "which communities they are in" in out["goal"]
    assert "never a list" not in out["goal"]
    for c in _MINE:
        assert c["place_name"] in out["fallback"]


def test_communities_near_me_keeps_the_short_nearby_answer() -> None:
    out = _turn("about")
    assert "communities near them" in out["goal"]
    assert "Responsible Computing Club" not in " ".join(out["facts"])


def test_mine_with_no_communities_says_so() -> None:
    out = _turn("mine", mine=[])
    assert "They are not in any community yet" in out["facts"]


def test_a_long_membership_list_is_capped_with_a_count() -> None:
    many = [dict(_MINE[0], place_id=f"p{i}", place_name=f"Club {i}") for i in range(9)]
    out = _turn("mine", mine=many, nearby=[])
    facts = " ".join(out["facts"])
    assert "Club 5" in facts and "Club 6" not in facts and "and 3 more" in facts


def test_mine_survives_the_slot_reader() -> None:
    from app.discovery_slots import slots_community_ask

    assert slots_community_ask({"community_ask": "mine"}) == "mine"


# ── lingo on surfaces the final-mile guard never sees ─────────────────────────────────


def test_a_tile_question_is_lexicon_cleaned_before_it_is_stored() -> None:
    from app import rapport_gaps as rg

    seen: dict[str, Any] = {}
    with mock.patch.object(rg, "_question_is_servable", side_effect=lambda q: seen.setdefault("q", q) and None), \
            mock.patch("app.lingo_guard.enforce", return_value=mock.Mock(text="Which language do you chat with others in?")), \
            mock.patch.object(rg, "_question_embedding", return_value=None), \
            mock.patch.object(rg, "_is_semantic_duplicate", return_value=True):
        rg.open_semantic_gap("u1", None, "Which language do you feel comfortable chatting in with neighbors?",
                             teaser="about languages", unlock_score=0.5)
    assert "neighbor" not in seen["q"]


def test_cards_never_say_neighbors() -> None:
    from app.community_question_sets import community_steps_for
    from app.lingo_guard import find_violations

    from app.circles_capture import CIRCLE_TYPES

    for t in CIRCLE_TYPES:
        for s in community_steps_for(t):
            assert not find_violations(s["question"]), s["question"]


def test_hosting_card_copy_says_people() -> None:
    import inspect

    from app import hosting_cta, hosting_surface, peer_discovery_surface, tip_surface

    for mod in (hosting_cta, hosting_surface, peer_discovery_surface, tip_surface):
        src = inspect.getsource(mod)
        assert "neighbor{'s'" not in src, mod.__name__
        assert "Open to neighbors nearby" not in src


# ── typed community answers are tagged like tapped ones ───────────────────────────────


def test_a_typed_answer_to_a_community_question_is_tagged() -> None:
    import inspect

    from app import lana_unified_pipeline as lup

    src = inspect.getsource(lup)
    i = src.index("res = try_upsert_claims_from_message(")
    block = src[i:i + 2500]
    assert "tag_claim_place_from_gap(gap_row_id, _cid)" in block
