"""Tests for the three-branch scope response.

The invariants here are the difference between a community that keeps someone and
one that shows them the door.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app import scope_response as sr


# ── banding ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "score,expected",
    [
        (0.91, "strong"),
        (0.62, "strong"),
        (0.61, "partial"),
        (0.38, "partial"),
        (0.37, "none"),
        (0.0, "none"),
        (None, "none"),
    ],
)
def test_bands(score, expected):
    assert sr.classify(score) == expected


def test_no_results_and_weak_results_are_treated_alike():
    """Both mean the scope cannot answer this. The user should not be able to tell
    whether we found nothing or found nothing good."""
    assert sr.classify(None) == sr.classify(0.05) == "none"


# ── the strong branch must not widen ────────────────────────────────────────

def test_strong_match_never_offers_to_widen():
    """Asking 'want me to look wider?' after a good answer is the same exit ramp,
    just better disguised."""
    out = sr.compose(scope_name="Etiqueta do Reino", question="formal dinner?", best_score=0.8)
    assert out["band"] == "strong"
    assert out["offers_widen"] is False
    assert out["reply"] is None


def test_strong_match_writes_nothing_to_the_radar():
    with patch.object(sr, "_record_gap") as rec:
        sr.compose(scope_name="X", question="q", best_score=0.9)
        rec.assert_not_called()


# ── the middle branch ───────────────────────────────────────────────────────

def test_partial_with_no_alternatives_degrades_to_none():
    """A partial band with nothing to actually offer is a 'none' band wearing a hat.
    Offering 'something similar' and then naming nothing is worse than widening."""
    with patch.object(sr, "_record_gap"), \
         patch.object(sr, "_generate", return_value=None):
        out = sr.compose(
            scope_name="Etiqueta do Reino", question="q", best_score=0.5, alternatives=[]
        )
    assert out["band"] == "none"
    assert out["offers_widen"] is True


def test_partial_does_not_offer_to_leave():
    """The whole point of this branch. If it widens, it is branch 3."""
    with patch.object(sr, "_record_gap"), \
         patch.object(sr, "_generate", return_value="Nothing on that — but there's a dinner Thursday."):
        out = sr.compose(
            scope_name="Etiqueta do Reino",
            question="Japanese clients?",
            best_score=0.5,
            alternatives=["a formal dining session Thursday"],
        )
    assert out["band"] == "partial"
    assert out["offers_widen"] is False


def test_partial_prompt_forbids_widening_and_demands_gap_first():
    captured = {}

    def _fake(system, payload, max_tokens=140):
        captured["system"] = system
        return "Nothing on that — but there's a dinner Thursday."

    with patch.object(sr, "_record_gap"), patch.object(sr, "_generate", _fake):
        sr.compose(
            scope_name="Etiqueta do Reino",
            question="Japanese clients?",
            best_score=0.5,
            alternatives=["a formal dining session Thursday"],
        )

    assert "ORDER IS NON-NEGOTIABLE" in captured["system"]
    assert "Do NOT offer to look outside" in captured["system"]


def test_partial_fallback_names_the_gap_before_the_offer():
    """Even when generation fails, the deterministic copy keeps the order."""
    with patch.object(sr, "_record_gap"), patch.object(sr, "_generate", return_value=None):
        out = sr.compose(
            scope_name="Etiqueta do Reino",
            question="q",
            best_score=0.5,
            alternatives=["a formal dining session Thursday"],
        )
    reply = out["reply"]
    assert reply.index("Nothing") < reply.index("but")


# ── the radar ───────────────────────────────────────────────────────────────

def test_partial_still_logs_the_gap():
    """Covering it in the moment does not make it covered. The creator still needs to
    know nobody has addressed this."""
    with patch.object(sr, "_record_gap") as rec, \
         patch.object(sr, "_generate", return_value="Nothing on that — but there's X."):
        sr.compose(
            scope_name="X", question="Japanese clients?", best_score=0.5,
            alternatives=["X"], place_id="p1",
        )
    rec.assert_called_once()
    assert rec.call_args.kwargs["widened"] is False


def test_none_logs_as_widened():
    with patch.object(sr, "_record_gap") as rec, \
         patch.object(sr, "_generate", return_value="Nobody here has covered this. Look nearby?"):
        sr.compose(scope_name="X", question="q", best_score=0.1, place_id="p1")
    assert rec.call_args.kwargs["widened"] is True


# ── widening names its destination ──────────────────────────────────────────

def test_sub_community_widens_to_its_parent_first():
    """A chapter should exhaust the parent community before leaving it entirely."""
    with patch.object(sr, "_record_gap"), patch.object(sr, "_generate", return_value=None):
        out = sr.compose(
            scope_name="Lisbon chapter", scope_kind="sub_community",
            question="q", best_score=None,
        )
    assert out["next_scope"] == "the whole community"
    assert "the whole community" in out["reply"]


def test_none_fallback_names_the_scope_and_ends_in_a_question():
    with patch.object(sr, "_record_gap"), patch.object(sr, "_generate", return_value=None):
        out = sr.compose(scope_name="Etiqueta do Reino", question="q", best_score=None)
    assert "Etiqueta do Reino" in out["reply"]
    assert out["reply"].rstrip().endswith("?")


# ── frontloading ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "I looked through the community and found a dinner on Thursday.",
    "Let me check what's available for you.",
    "Great question! There's a dinner Thursday.",
    "Here's what I found: a dinner Thursday.",
    "Sure, there's a dinner Thursday.",
    "It looks like there's a dinner Thursday.",
])
def test_preamble_openers_are_rejected(text):
    assert sr.frontloaded(text) is False


@pytest.mark.parametrize("text", [
    "There's a formal dining session Thursday at 7.",
    "Nothing on Japanese clients — but Ana covered cross-cultural table manners.",
    "Nobody in Etiqueta do Reino has covered this. Want me to look nearby?",
    "Belcanto's back room is quiet enough for that.",
])
def test_real_answers_pass(text):
    assert sr.frontloaded(text) is True


def test_preamble_triggers_one_retry_then_falls_back():
    """Two strikes and we use the deterministic copy, which is frontloaded by
    construction."""
    calls = []

    def _fake_json(**kwargs):
        calls.append(kwargs)
        return {"reply": "I looked everywhere and found nothing."}

    with patch("app.orchestrator.llm.llm_configured", return_value=True), \
         patch("app.orchestrator.llm.llm_json", _fake_json), \
         patch("app.orchestrator.llm.router_model", return_value="m"):
        assert sr._generate("sys", "q") is None

    assert len(calls) == 2
    assert "preamble" in calls[1]["system"]


def test_generation_failure_never_raises():
    with patch.object(sr, "_record_gap"), \
         patch("app.orchestrator.llm.llm_configured", side_effect=RuntimeError("down")):
        out = sr.compose(scope_name="X", question="q", best_score=None)
    assert out["reply"]
