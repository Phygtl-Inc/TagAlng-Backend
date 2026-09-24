"""Tests for reco_aspects.

These are the invariants. If one of them breaks, we have shipped a star rating with
extra steps.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app import reco_aspects as ra


# ── parsing ─────────────────────────────────────────────────────────────────

def test_parse_keeps_the_users_own_framing():
    out = ra._parse_aspects({
        "aspects": [
            {"label": "the owner", "key": "owner", "span": "the owner came over",
             "confidence": 0.9},
        ]
    })
    assert out[0]["aspect_label"] == "the owner"
    assert out[0]["source_span"] == "the owner came over"


def test_parse_drops_low_confidence():
    out = ra._parse_aspects({
        "aspects": [{"label": "maybe parking", "key": "parking", "confidence": 0.2}]
    })
    assert out == []


def test_parse_dedupes_keys():
    out = ra._parse_aspects({
        "aspects": [
            {"label": "the food", "key": "food", "confidence": 0.9},
            {"label": "food quality", "key": "food", "confidence": 0.9},
        ]
    })
    assert len(out) == 1


def test_parse_caps_at_max():
    out = ra._parse_aspects({
        "aspects": [
            {"label": f"thing {i}", "key": f"k{i}", "confidence": 0.9}
            for i in range(30)
        ]
    })
    assert len(out) == ra.MAX_ASPECTS


@pytest.mark.parametrize("bad", [None, {}, {"aspects": "nope"}, {"aspects": [1, 2]}])
def test_parse_never_raises_on_garbage(bad):
    assert ra._parse_aspects(bad) == []


# ── banding ─────────────────────────────────────────────────────────────────

def test_band_null_is_preserved_not_coerced_to_zero():
    """An answer that says nothing must not be recorded as neutral.

    0 means "I read this and it was mixed". None means "there was nothing to read".
    Collapsing the second into the first invents data.
    """
    assert ra._parse_band({"sentiment": None, "confidence": 0.9}) == (None, 0.0)


def test_band_rejects_out_of_range():
    assert ra._parse_band({"sentiment": 7}) == (None, 0.0)
    assert ra._parse_band({"sentiment": -9}) == (None, 0.0)


def test_band_empty_answer_is_not_banded():
    assert ra.band_answer("the owner", "   ") == (None, 0.0)


# ── the two rules ───────────────────────────────────────────────────────────

def test_questions_come_from_the_statement_not_a_template():
    """Six sections mentioned → six questions. Never more, never a category checklist."""
    statement = (
        "The atmosphere was compelling, I noticed the porcelain, the server spoke "
        "three languages, the owner came over, parking was easy, and it was quiet."
    )
    fake = [
        {"aspect_key": k, "aspect_label": lbl, "source_span": "x", "confidence": 0.9}
        for k, lbl in [
            ("atmosphere", "the atmosphere"), ("porcelain", "the porcelain"),
            ("server", "the server"), ("owner", "the owner"),
            ("parking", "parking"), ("noise", "how quiet it was"),
        ]
    ]
    with patch.object(ra, "split_statement", return_value=fake), \
         patch.object(ra, "canonical_key", side_effect=lambda s, k, l: k):
        qs = ra.open_aspect_questions(
            signal_id="s1", subject_ref="sub1", author_id="u1", statement=statement)

    assert len(qs) == 6
    assert [q["aspect_key"] for q in qs] == [
        "atmosphere", "porcelain", "server", "owner", "parking", "noise"]
    # No aspect exists that they did not raise.
    assert not any(q["aspect_key"] in {"delivery", "assembly", "used_for"} for q in qs)


def test_every_question_is_skippable():
    """Skip at question level. Flagged missing in the 2026-09-24 review."""
    fake = [{"aspect_key": "owner", "aspect_label": "the owner",
             "source_span": "x", "confidence": 0.9}]
    with patch.object(ra, "split_statement", return_value=fake), \
         patch.object(ra, "canonical_key", side_effect=lambda s, k, l: k):
        qs = ra.open_aspect_questions(
            signal_id="s1", subject_ref=None, author_id="u1", statement="a" * 40)
    assert all(q["skippable"] for q in qs)


def test_question_echoes_their_words():
    """'You mentioned the owner' is answerable. 'Rate ownership' is a form."""
    fake = [{"aspect_key": "porcelain", "aspect_label": "the porcelain",
             "source_span": "x", "confidence": 0.9}]
    with patch.object(ra, "split_statement", return_value=fake), \
         patch.object(ra, "canonical_key", side_effect=lambda s, k, l: k):
        qs = ra.open_aspect_questions(
            signal_id="s1", subject_ref=None, author_id="u1", statement="a" * 40)
    assert "the porcelain" in qs[0]["question"]


def test_skipped_aspect_is_stored_with_no_sentiment():
    """A skip is data: they raised it, then declined to grade it. Both facts matter."""
    captured = {}

    class _T:
        def upsert(self, row, on_conflict=None):
            captured.update(row)
            return self

        def execute(self):
            class R:
                data = [captured]
            return R()

    class _C:
        def table(self, _):
            return _T()

    with patch.object(ra, "service_client", return_value=_C()), \
         patch.object(ra, "canonical_key", side_effect=lambda s, k, l: k):
        ra.record_aspect(
            signal_id="s1", subject_ref=None, author_id="u1",
            aspect_key="owner", aspect_label="the owner", source_span=None,
            answer_verbatim=None, answer_source="skipped")

    assert captured["answer_source"] == "skipped"
    assert captured["sentiment"] is None


def test_short_statement_produces_no_questions():
    assert ra.split_statement("nice") == []


def test_canonical_key_falls_back_to_raw_on_failure():
    """Fragmenting is the safe failure. Merging two different things is not."""
    with patch.object(ra, "service_client", side_effect=RuntimeError("down")):
        assert ra.canonical_key("sub1", "front_desk", "the front desk") == "front_desk"
