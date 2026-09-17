"""Domain standing — the read side (SPEC_RECOMMENDER_AUTHORITY §4/A2).

The two things that must hold, because both were silently broken in the spec as written:
the concept search must search EVERY bucket, and a score must never leave the server.
"""

from types import SimpleNamespace
from unittest.mock import patch

from app.authority import authority_for, best_authority, concepts_for_ask


def _client(rows, capture=None):
    """A supabase stub whose .rpc(name, args) records args and returns `rows`."""

    def rpc(name, args):
        if capture is not None:
            capture.append((name, args))
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=rows))

    return SimpleNamespace(rpc=rpc)


# ── A2 · concepts_for_ask ────────────────────────────────────────────────────


def test_concept_search_spans_every_bucket():
    """p_bucket must be null.

    The ask can land in any bucket — "from someone from Turkey" is heritage, "who has run
    a marathon" is activity. Pinning a bucket answers half the requirement and silently
    drops the other half.
    """
    calls: list = []
    with patch("app.authority.service_client", return_value=_client([{"id": "c1"}], calls)):
        assert concepts_for_ask("turkish restaurant", embedding=[0.1] * 768) == ["c1"]

    name, args = calls[0]
    assert name == "match_concepts_by_embedding"
    assert args["p_bucket"] is None, "a bucketed search answers only half the spec"
    assert args["p_limit"] == 3 and args["p_min_similarity"] == 0.55


def test_caller_supplied_embedding_is_not_recomputed():
    """Contract v2 Part 0: no new LLM call on the request path. The tip lane has already
    embedded the ask, so asking Vertex again is a second charge for the same vector."""
    with patch("app.authority.service_client", return_value=_client([])):
        with patch("app.layer1_handlers._embed_attr_filter") as embed:
            concepts_for_ask("anything", embedding=[0.2] * 768)
            embed.assert_not_called()


def test_no_text_and_no_embedding_resolve_to_nothing():
    with patch("app.authority.service_client", return_value=_client([{"id": "c1"}])):
        assert concepts_for_ask("   ") == []


def test_rpc_failure_costs_ordering_not_the_answer():
    """Authority refines an order. It must never be able to fail an ask."""

    def boom():
        raise RuntimeError("db down")

    with patch("app.authority.service_client", side_effect=boom):
        assert concepts_for_ask("x", embedding=[0.1] * 768) == []
        assert authority_for("u1", ["c1"]) == {}
        assert best_authority("u1", ["c1"]) is None


# ── A1 · authority_for / best_authority ──────────────────────────────────────


def test_as_of_is_forwarded_because_it_is_the_anti_gaming_rule():
    """A claim written after the recommendation must not raise that recommendation's
    authority. The caller passes the attestation's moment; if we drop it the function
    defaults to now() and the rule silently stops applying."""
    calls: list = []
    with patch("app.authority.service_client", return_value=_client([], calls)):
        authority_for("u1", ["c1"], as_of="2026-09-01T00:00:00Z")

    _, args = calls[0]
    assert args["p_as_of"] == "2026-09-01T00:00:00Z"
    assert args["p_include_relations"] is False, (
        "a parent's or child's claim is their standing, not the attester's"
    )


def test_best_authority_picks_the_winning_concept_with_its_own_quote():
    """The reason must explain the concept that actually moved the row. Taking the max
    score from one concept and the quote from another describes a different fact."""
    rows = [
        {"concept_id": "c1", "authority": 0.35, "evidence_kinds": ["specific"],
         "evidence_quote": "I cook a lot"},
        {"concept_id": "c2", "authority": 0.90, "evidence_kinds": ["behavioural", "specific"],
         "evidence_quote": "I grew up in Gaziantep and my grandmother ran a kebab place"},
    ]
    with patch("app.authority.service_client", return_value=_client(rows)):
        best = best_authority("u1", ["c1", "c2"])

    assert best["concept_id"] == "c2"
    assert best["score"] == 0.90
    assert "Gaziantep" in best["quote"]


def test_zero_authority_is_no_authority():
    """0.0 must read as "no standing", not as a ranked row with an empty reason. This is
    the Python half of the LEAST/NULL guard in the migration: an attester with no claims
    scores 0.0, and 0.0 must not surface a reason line."""
    rows = [{"concept_id": "c1", "authority": 0.0, "evidence_kinds": [],
             "evidence_quote": None}]
    with patch("app.authority.service_client", return_value=_client(rows)):
        assert best_authority("u1", ["c1"]) is None


def test_score_is_carried_internally_but_named_as_internal():
    """§7: never render the number, render the evidence. The score exists for P1's sort
    and must be stripped before the payload — this pins the shape callers rely on."""
    rows = [{"concept_id": "c1", "authority": 0.8, "evidence_kinds": ["behavioural"],
             "evidence_quote": "ran the Berlin marathon in 2024"}]
    with patch("app.authority.service_client", return_value=_client(rows)):
        got = authority_for("u1", ["c1"])

    assert got["c1"] == {
        "score": 0.8,
        "evidence": ["behavioural"],
        "quote": "ran the Berlin marathon in 2024",
    }
