"""Tests for community_intro and community_blurb.

The invariants here are the difference between a contextual opening and a greeting with
a name substituted into it.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app import community_blurb as cb
from app import community_intro as ci


class _Result:
    def __init__(self, data=None, count=None):
        self.data = data
        self.count = count


class _Table:
    def __init__(self, data=None, count=None):
        self._r = _Result(data, count)

    def select(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def neq(self, *a, **k):
        return self

    def is_(self, *a, **k):
        return self

    def or_(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def update(self, patch):
        self.patched = patch
        return self

    def execute(self):
        return self._r


# ── purpose resolution ──────────────────────────────────────────────────────

def test_first_action_beats_blurb():
    """The operator's own words always win. The blurb is the floor, not the ceiling."""
    place = {
        "id": "p1", "name": "Etiqueta do Reino",
        "blurb": "Etiqueta do Reino is a spot focused on social and dining etiquette.",
        "first_action": "Ask me which fork to use before your next dinner.",
    }
    with patch.object(ci, "service_client") as sc:
        sc.return_value.rpc.side_effect = RuntimeError("no such rpc")
        sc.return_value.table.return_value = _Table([place])
        out = ci.community_purpose("p1")

    assert out["source"] == "first_action"
    assert "fork" in out["purpose"]


def test_blurb_used_when_no_first_action():
    place = {"id": "p1", "name": "X", "blurb": "X is about etiquette.", "first_action": None}
    with patch.object(ci, "service_client") as sc:
        sc.return_value.rpc.side_effect = RuntimeError("no such rpc")
        sc.return_value.table.return_value = _Table([place])
        out = ci.community_purpose("p1")
    assert out["source"] == "blurb"


def test_no_purpose_means_no_question_not_an_invented_one():
    """A community nothing describes gets the generic opening.

    Generating a question from an empty purpose means grounding it in fiction, which is
    worse than being generic.
    """
    place = {"id": "p1", "name": "X", "blurb": None, "first_action": None}
    with patch.object(ci, "service_client") as sc:
        sc.return_value.rpc.side_effect = RuntimeError("no such rpc")
        sc.return_value.table.return_value = _Table([place])
        assert ci.community_purpose("p1") is None


# ── maturity ────────────────────────────────────────────────────────────────

def test_anonymous_is_always_a_visitor():
    """No lookup, no assumptions. An anonymous arrival is a stranger by definition."""
    assert ci.user_maturity("u1", is_anonymous=True) == "visitor"
    assert ci.user_maturity(None) == "visitor"


@pytest.mark.parametrize("n,expected", [(0, "visitor"), (1, "new"), (7, "new"), (8, "engaged"), (40, "engaged")])
def test_maturity_bands(n, expected):
    with patch.object(ci, "service_client") as sc:
        sc.return_value.table.return_value = _Table([], count=n)
        assert ci.user_maturity("u1") == expected


def test_maturity_failure_degrades_to_new_not_engaged():
    """A failed lookup must not grant assumed familiarity."""
    with patch.object(ci, "service_client", side_effect=RuntimeError("down")):
        assert ci.user_maturity("u1") == "new"


# ── the opening ─────────────────────────────────────────────────────────────

def test_question_generation_passes_purpose_and_audience():
    captured = {}

    def _fake_json(**kwargs):
        captured.update(kwargs)
        return {"question": "Which table rule do people get wrong most often?"}

    with patch.object(ci, "community_purpose",
                      return_value={"purpose": "social and dining etiquette",
                                    "source": "blurb", "place_id": "p1", "name": "E"}), \
         patch.object(ci, "user_maturity", return_value="visitor"), \
         patch("app.orchestrator.llm.llm_configured", return_value=True), \
         patch("app.orchestrator.llm.llm_json", _fake_json), \
         patch("app.orchestrator.llm.router_model", return_value="m"):
        out = ci.first_question(place_id="p1", user_id=None, is_anonymous=True)

    assert out["maturity"] == "visitor"
    # Both inputs reach the prompt — this is the whole model.
    assert "etiquette" in captured["system"]
    assert "stranger" in captured["system"].lower()


def test_generation_failure_returns_none_not_an_error():
    """None means 'use the existing generic opening'. It must never raise into the path."""
    with patch.object(ci, "community_purpose", return_value={"purpose": "x", "source": "blurb",
                                                            "place_id": "p1", "name": "E"}), \
         patch.object(ci, "user_maturity", return_value="new"), \
         patch("app.orchestrator.llm.llm_configured", side_effect=RuntimeError("boom")):
        assert ci.first_question(place_id="p1", user_id="u1") is None


def test_overlong_question_is_rejected():
    with patch.object(ci, "community_purpose", return_value={"purpose": "x", "source": "blurb",
                                                            "place_id": "p1", "name": "E"}), \
         patch.object(ci, "user_maturity", return_value="new"), \
         patch("app.orchestrator.llm.llm_configured", return_value=True), \
         patch("app.orchestrator.llm.llm_json", return_value={"question": "q" * 400}), \
         patch("app.orchestrator.llm.router_model", return_value="m"):
        assert ci.first_question(place_id="p1", user_id="u1") is None


# ── widening ────────────────────────────────────────────────────────────────

def test_widen_fallback_still_names_the_community_and_still_asks():
    """The boundary has to be felt even when generation fails."""
    with patch("app.orchestrator.llm.llm_configured", side_effect=RuntimeError("down")):
        ask = ci.widen_ask(community_name="Etiqueta do Reino", question="best tailor?")
    assert "Etiqueta do Reino" in ask
    assert ask.rstrip().endswith("?")


# ── blurbs ──────────────────────────────────────────────────────────────────

def test_unembedded_blurb_stays_stale_for_the_next_pass():
    """A blurb written without its embedding is invisible while looking complete."""
    tbl = _Table([])
    with patch.object(cb, "generate_blurb", return_value="X is about etiquette."), \
         patch.object(cb, "_embed", return_value=None), \
         patch.object(cb, "service_client") as sc:
        sc.return_value.table.return_value = tbl
        cb.refresh_one({"id": "p1", "name": "X", "blurb": None, "blurb_stale": False})

    assert tbl.patched["blurb_stale"] is True
    assert "blurb_embedding" not in tbl.patched


def test_embedded_blurb_clears_stale():
    tbl = _Table([])
    with patch.object(cb, "generate_blurb", return_value="X is about etiquette."), \
         patch.object(cb, "_embed", return_value="[0.1]"), \
         patch.object(cb, "service_client") as sc:
        sc.return_value.table.return_value = tbl
        cb.refresh_one({"id": "p1", "name": "X", "blurb": None, "blurb_stale": True})

    assert tbl.patched["blurb_stale"] is False
    assert tbl.patched["blurb_embedding"] == "[0.1]"


def test_generation_failure_writes_nothing():
    with patch.object(cb, "generate_blurb", return_value=None), \
         patch.object(cb, "service_client") as sc:
        assert cb.refresh_one({"id": "p1", "name": "X", "blurb": None, "blurb_stale": True}) is False
        sc.return_value.table.assert_not_called()
