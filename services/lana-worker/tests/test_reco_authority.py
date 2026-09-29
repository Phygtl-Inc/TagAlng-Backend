"""Recommender standing on the tip search — app/reco_authority.py and its readers."""

from __future__ import annotations

from typing import Any

import pytest

import app.reco_authority as ra


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv("LANA_RECO_AUTHORITY", "1")
    monkeypatch.setattr("app.authority.concepts_for_ask", lambda text, **_: ["c-spain"])


def _tip(sig: str, peer: str, **over: Any) -> dict[str, Any]:
    row = {
        "signal_id": sig,
        "peer_user_id": peer,
        "neighbor_label": f"n-{peer}",
        "created_at": "2026-09-01T10:00:00+00:00",
        "subject_ref": f"sub-{sig}",
        "subject_name": f"Barber {sig}",
        "reco_name": f"Barber {sig}",
        "match_strength": 0.8,
    }
    row.update(over)
    return row


def _scores(by_peer: dict[str, tuple[float, str | None]]):
    """A score_rows stand-in: standing by author, as the batch would return it."""
    def fake(rows, concept_ids):
        out = []
        for r in rows:
            s = by_peer.get(r["peer_user_id"])
            out.append({"score": s[0], "quote": s[1]} if s else None)
        return out
    return fake


PARSED = {"clauses": [], "subject_kind": "barber", "recommender_trait": "from Spain"}


# ── the tier ────────────────────────────────────────────────────────────────


def test_strong_then_thin_then_none_and_stable_inside_a_tier(on, monkeypatch):
    monkeypatch.setattr(ra, "score_rows", _scores({
        "thin": (0.10, None), "strong1": (0.35, "I grew up in Madrid and came here in 2019"),
        "strong2": (0.50, "Born in Sevilla, my whole family still lives there"),
    }))
    tips = [_tip("a", "none"), _tip("b", "thin"), _tip("c", "strong1"), _tip("d", "strong2")]
    out = ra.recall_and_rank_by_standing(tips, parsed=PARSED, fetch=lambda k, n: [])
    # Search order stands within the strong tier (c before d), nothing pass 1 found is dropped.
    assert [t["signal_id"] for t in out] == ["c", "d", "b", "a"]
    assert out[0]["_standing"] == {
        "tier": "strong", "quote": "I grew up in Madrid and came here in 2019", "trait": "from Spain",
    }
    assert "_standing" not in out[3]


def test_a_thin_claim_never_carries_a_quote(on, monkeypatch):
    monkeypatch.setattr(ra, "score_rows", _scores({"p": (0.25, "a quote that must not show")}))
    out = ra.recall_and_rank_by_standing([_tip("a", "p")], parsed=PARSED, fetch=lambda k, n: [])
    assert out[0]["_standing"]["tier"] == "thin"
    assert out[0]["_standing"]["quote"] is None


def test_floor_is_min_explicit_score():
    from app.authority import MIN_EXPLICIT_SCORE

    assert ra.tier_of(MIN_EXPLICIT_SCORE) == "strong"
    assert ra.tier_of(MIN_EXPLICIT_SCORE - 0.01) == "thin"
    assert ra.tier_of(0.0) is None


# ── recall ──────────────────────────────────────────────────────────────────


def test_second_pass_keeps_only_rows_whose_author_has_standing(on, monkeypatch):
    monkeypatch.setattr(ra, "score_rows", _scores({"tony": (0.40, "Soy de Valencia, crecí allí")}))
    asked: list[tuple[str, int]] = []

    def fetch(kind, n):
        asked.append((kind, n))
        return [_tip("a", "carlos"), _tip("t", "tony"), _tip("x", "nobody")]

    out = ra.recall_and_rank_by_standing([_tip("a", "carlos")], parsed=PARSED, fetch=fetch)
    assert asked == [("barber", ra.RECALL_POOL)]
    # Tony found by who he is; "nobody" is not padded in; Carlos (pass 1) stays, once.
    assert [t["signal_id"] for t in out] == ["t", "a"]


def test_second_pass_failure_keeps_the_first(on, monkeypatch):
    monkeypatch.setattr(ra, "score_rows", _scores({}))

    def boom(kind, n):
        raise RuntimeError("rpc down")

    tips = [_tip("a", "p")]
    assert ra.recall_and_rank_by_standing(tips, parsed=PARSED, fetch=boom) == tips


# ── inert paths ─────────────────────────────────────────────────────────────


def _recorder(calls: list, value: Any = None):
    # Records instead of raising: the module swallows exceptions, so a raising stand-in
    # would let an inert-path test pass even when the path is not inert.
    def rec(*a, **k):
        calls.append(a)
        return value
    return rec


def test_off_does_nothing(monkeypatch):
    monkeypatch.setenv("LANA_RECO_AUTHORITY", "0")
    calls: list = []
    monkeypatch.setattr("app.authority.concepts_for_ask", _recorder(calls, ["c"]))
    monkeypatch.setattr(ra, "score_rows", _recorder(calls, [None]))
    tips = [_tip("a", "p")]
    assert ra.recall_and_rank_by_standing(tips, parsed=PARSED, fetch=_recorder(calls, [])) is tips
    assert calls == []


def test_no_recommender_trait_does_nothing(on, monkeypatch):
    # "a Spanish barber" is about the barber — aspects own it, not standing.
    calls: list = []
    monkeypatch.setattr("app.authority.concepts_for_ask", _recorder(calls, ["c"]))
    monkeypatch.setattr(ra, "score_rows", _recorder(calls, [None]))
    tips = [_tip("a", "p")]
    parsed = {"clauses": [{"text": "Spanish"}], "subject_kind": "barber", "recommender_trait": None}
    assert ra.recall_and_rank_by_standing(tips, parsed=parsed, fetch=_recorder(calls, [])) is tips
    assert calls == []


def test_trait_with_no_concept_does_nothing(on, monkeypatch):
    calls: list = []
    monkeypatch.setattr("app.authority.concepts_for_ask", lambda text, **_: [])
    monkeypatch.setattr(ra, "score_rows", _recorder(calls, [None]))
    tips = [_tip("a", "p")]
    assert ra.recall_and_rank_by_standing(tips, parsed=PARSED, fetch=_recorder(calls, [])) is tips
    assert calls == []


# ── the batched read ────────────────────────────────────────────────────────


class _Client:
    def __init__(self, data=None, raises=False):
        self.data, self.raises, self.calls = data, raises, []

    def rpc(self, name, args):
        self.calls.append((name, args))
        return self

    def execute(self):
        if self.raises:
            raise RuntimeError("function attester_authority_many does not exist")
        return type("R", (), {"data": self.data})()


def test_batch_maps_idx_back_per_row_and_scores_as_of_each_post(monkeypatch):
    client = _Client(data=[
        {"idx": 2, "concept_id": "c1", "authority": 0.10, "evidence_quote": None},
        {"idx": 2, "concept_id": "c2", "authority": 0.45, "evidence_quote": "grew up in Madrid"},
        {"idx": 3, "concept_id": "c1", "authority": 0.0, "evidence_quote": None},
    ])
    monkeypatch.setattr("app.auth.service_client", lambda: client)
    rows = [
        _tip("a", "p1", created_at="2026-01-01T00:00:00Z"),
        _tip("b", "p2", created_at="2026-02-01T00:00:00Z"),
        _tip("c", "p2", created_at="2026-03-01T00:00:00Z"),  # same author, later post
    ]
    out = ra.score_rows(rows, ["c1", "c2"])
    assert out == [None, {"score": 0.45, "quote": "grew up in Madrid"}, None]
    name, args = client.calls[0]
    assert name == "attester_authority_many"
    assert args["p_user_ids"] == ["p1", "p2", "p2"]
    assert args["p_as_of"] == [
        "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z", "2026-03-01T00:00:00Z",
    ]


def test_batch_missing_falls_back_per_row(monkeypatch):
    monkeypatch.setattr("app.auth.service_client", lambda: _Client(raises=True))
    seen = []

    def per_row(uid, concept_ids, *, as_of=None, public_only=False, **_):
        assert public_only, "a results page must never score on private claims"
        seen.append((uid, as_of))
        return {"c1": {"score": 0.5, "quote": "q", "evidence": []}} if uid == "p2" else {}

    monkeypatch.setattr("app.authority.authority_for", per_row)
    out = ra.score_rows([_tip("a", "p1"), _tip("b", "p2")], ["c1"])
    assert out == [None, {"score": 0.5, "quote": "q"}]
    assert seen == [("p1", "2026-09-01T10:00:00+00:00"), ("p2", "2026-09-01T10:00:00+00:00")]


# ── the card ────────────────────────────────────────────────────────────────


def test_card_counts_people_not_rows_and_carries_strong_quotes_only():
    rows = [
        {"signal_id": "s1", "_standing": {"tier": "strong", "quote": "Born in Madrid", "trait": "from Spain"}},
        {"signal_id": "s2", "_standing": {"tier": "strong", "quote": "Born in Madrid", "trait": "from Spain"}},
        {"signal_id": "s3", "_standing": {"tier": "thin", "quote": None, "trait": "from Spain"}},
        {"signal_id": "s4"},
    ]
    contributors = [
        {"signal_id": "s1", "peer_user_id": "ana", "nickname": "Ana"},
        {"signal_id": "s2", "peer_user_id": "ana", "nickname": "Ana"},  # her second tip
        {"signal_id": "s3", "peer_user_id": "bo", "nickname": "Bo"},
        {"signal_id": "s4", "peer_user_id": "cy", "nickname": "Cy"},
    ]
    tier, st = ra.card_standing(rows, contributors)
    assert tier == "strong"
    assert st["n_people"] == 1 and st["of_people"] == 3
    assert contributors[0]["standing_quote"] == "Born in Madrid"
    assert "standing_quote" not in contributors[2]


def test_thin_only_card_sorts_but_puts_nothing_on_the_wire():
    rows = [{"signal_id": "s1", "_standing": {"tier": "thin", "quote": None, "trait": "from Spain"}}]
    tier, st = ra.card_standing(rows, [{"signal_id": "s1", "peer_user_id": "p", "nickname": "P"}])
    assert (tier, st) == ("thin", None)


def test_strong_standing_outranks_provenance_on_the_page():
    from app.reco_cards import subject_cards_from_tips

    block = _tip("b", "neighbour", same_block=True)
    spaniard = _tip("s", "ana", _standing={
        "tier": "strong", "quote": "I grew up in Madrid and moved here in 2019", "trait": "from Spain",
    })
    cards = subject_cards_from_tips([block, spaniard])
    assert [c["title"] for c in cards] == ["Barber s", "Barber b"]
    assert cards[0]["recommender_standing"]["quotes"] == [
        {"nickname": "n-ana", "quote": "I grew up in Madrid and moved here in 2019"},
    ]


def test_without_standing_the_page_order_is_unchanged():
    from app.reco_cards import subject_cards_from_tips

    block = _tip("b", "neighbour", same_block=True)
    other = _tip("s", "ana")
    assert [c["title"] for c in subject_cards_from_tips([other, block])] == ["Barber b", "Barber s"]


def test_wire_model_carries_quotes_never_a_score_or_tier():
    from app.models import RecoCardRow

    row = RecoCardRow(
        title="Barber",
        _standing_tier="strong",
        recommender_standing={
            "trait": "from Spain", "n_people": 1, "of_people": 2,
            "quotes": [{"nickname": "Ana", "quote": "Born in Madrid"}],
        },
        contributors=[{"signal_id": "s", "peer_user_id": "p", "nickname": "Ana",
                       "standing_quote": "Born in Madrid"}],
    )
    dumped = row.model_dump()
    assert dumped["recommender_standing"]["n_people"] == 1
    assert dumped["contributors"][0]["standing_quote"] == "Born in Madrid"
    blob = str(dumped)
    assert "score" not in blob and "_standing_tier" not in blob and "strong" not in blob


# ── the fit line ────────────────────────────────────────────────────────────


def test_fit_gets_recommended_by_as_a_reason():
    from app.reco_fit import _facts, _has_reason

    card = {"title": "Barber", "contributors": [], "recommender_standing": {
        "trait": "from Spain", "n_people": 2, "of_people": 3,
        "quotes": [{"nickname": "Ana", "quote": "Born in Madrid"}],
    }}
    facts = _facts(card, [])
    assert facts["recommended_by"] == {
        "trait": "from Spain", "people": 2, "of": 3, "quotes": ["Born in Madrid"],
    }
    assert _has_reason(facts)


def test_fit_without_standing_has_no_recommended_by():
    from app.reco_fit import _facts, _has_reason

    facts = _facts({"title": "Barber", "contributors": []}, [])
    assert facts["recommended_by"] is None
    assert not _has_reason(facts)


# ── the split ───────────────────────────────────────────────────────────────


def test_split_reads_the_recommender_trait(monkeypatch):
    import app.orchestrator.llm as llm
    from app.reco_aspects import split_query_full

    monkeypatch.setattr(llm, "llm_configured", lambda: True)
    monkeypatch.setattr(llm, "llm_json", lambda **kw: {
        "clauses": [], "subject_kind": "barber", "recommender_trait": "  from Spain ",
    })
    assert split_query_full("a barber recommended by someone from Spain") == {
        "clauses": [], "subject_kind": "barber", "recommender_trait": "from Spain",
    }


def test_split_failure_has_no_trait(monkeypatch):
    import app.orchestrator.llm as llm
    from app.reco_aspects import split_query_full

    monkeypatch.setattr(llm, "llm_configured", lambda: True)
    monkeypatch.setattr(llm, "llm_json", lambda **kw: "not json")
    assert split_query_full("a barber recommended by someone from Spain")["recommender_trait"] is None


def test_strong_without_their_own_words_orders_but_is_never_named():
    # Behavioural standing (recs near the concept) can reach strong with no quote: fine
    # for order, never for "recommended by someone from Spain".
    rows = [{"signal_id": "s1", "_standing": {"tier": "strong", "quote": None, "trait": "from Spain"}}]
    contributors = [{"signal_id": "s1", "peer_user_id": "p", "nickname": "P"}]
    assert ra.card_standing(rows, contributors) == ("strong", None)
    assert "standing_quote" not in contributors[0]


def test_authority_for_sends_public_only_only_when_asked(monkeypatch):
    import app.authority as authority

    client = _Client(data=[])
    monkeypatch.setattr(authority, "service_client", lambda: client)
    authority.authority_for("u", ["c"])
    authority.authority_for("u", ["c"], public_only=True)
    assert "p_public_only" not in client.calls[0][1]
    assert client.calls[1][1]["p_public_only"] is True
