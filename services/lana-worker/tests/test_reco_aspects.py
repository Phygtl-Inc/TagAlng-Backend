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
         patch.object(ra, "_resolve_key", side_effect=lambda s, k, l: (k, None)):
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
         patch.object(ra, "_resolve_key", side_effect=lambda s, k, l: (k, None)):
        qs = ra.open_aspect_questions(
            signal_id="s1", subject_ref=None, author_id="u1", statement="a" * 40)
    assert all(q["skippable"] for q in qs)


def test_question_echoes_their_words():
    """'You mentioned the owner' is answerable. 'Rate ownership' is a form."""
    fake = [{"aspect_key": "porcelain", "aspect_label": "the porcelain",
             "source_span": "x", "confidence": 0.9}]
    with patch.object(ra, "split_statement", return_value=fake), \
         patch.object(ra, "_resolve_key", side_effect=lambda s, k, l: (k, None)):
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


# ── regressions from the 2026-09-28 review ──────────────────────────────────

def _llm_returning(data):
    """Patch the model call; capture the system prompt it was given."""
    seen = {}

    def fake_llm_json(**kw):
        seen.update(kw)
        return data

    return seen, patch.multiple(
        "app.orchestrator.llm",
        llm_configured=lambda: True,
        llm_json=fake_llm_json,
        router_model=lambda: "m",
    )


def test_split_statement_actually_reaches_the_model():
    """The prompt is full of JSON braces; .format() on it raised KeyError, which the
    defensive except swallowed — so every statement split into []. Nothing else in this
    file exercised the real prompt, which is how it shipped."""
    seen, p = _llm_returning({"aspects": [
        {"label": "the wait", "key": "wait_time", "span": "waited an hour",
         "question": "You mentioned the wait — was it bad?", "confidence": 0.9},
    ]})
    with p:
        out = ra.split_statement("We waited an hour but Dr. Sarah was lovely with him.")
    assert [a["aspect_key"] for a in out] == ["wait_time"]
    assert f"At most {ra.MAX_ASPECTS}." in seen["system"]
    assert '"aspects"' in seen["system"]


def test_band_answer_actually_reaches_the_model():
    seen, p = _llm_returning({"sentiment": -2, "confidence": 0.9})
    with p:
        assert ra.band_answer("the owner", "he really sucked") == (-2, 0.9)
    assert "Aspect: the owner" in seen["system"]


def test_question_is_the_models_when_it_wrote_one():
    """AI-authored, in the statement's language; the template is only a fallback."""
    fake = [
        {"aspect_key": "espera", "aspect_label": "la espera", "source_span": "x",
         "question": "Mencionaste la espera — ¿fue mala?", "confidence": 0.9},
        {"aspect_key": "owner", "aspect_label": "the owner", "source_span": "x",
         "question": None, "confidence": 0.9},
    ]
    with patch.object(ra, "split_statement", return_value=fake), \
         patch.object(ra, "_resolve_key", side_effect=lambda s, k, l: (k, None)):
        qs = ra.open_aspect_questions(
            signal_id="s1", subject_ref=None, author_id="u1", statement="a" * 40,
            persist=False)
    assert qs[0]["question"] == "Mencionaste la espera — ¿fue mala?"
    assert "the owner" in qs[1]["question"]


class _Capture:
    def __init__(self):
        self.rows = []

    def table(self, _):
        return self

    def upsert(self, row, **_):
        self.rows.append(row)
        return self

    def execute(self):
        class R:
            data = [{}]
        return R()


def test_open_rows_carry_the_label_embedding():
    """Key matching compares label to label; the open row must carry its side."""
    cap = _Capture()
    fake = [{"aspect_key": "owner", "aspect_label": "the owner",
             "source_span": "x", "confidence": 0.9}]
    with patch.object(ra, "split_statement", return_value=fake), \
         patch.object(ra, "_resolve_key", return_value=("owner", "[0.1,0.2]")), \
         patch.object(ra, "service_client", return_value=cap):
        ra.open_aspect_questions(
            signal_id="s1", subject_ref="sub1", author_id="u1", statement="a" * 40)
    assert cap.rows[0][0]["label_embedding"] == "[0.1,0.2]"


def test_answer_keeps_the_key_it_was_asked_with():
    """Re-resolving at answer time can land on another key once the centroid moves; the
    upsert then inserts a second row and the open one is owed forever."""
    cap = _Capture()
    with patch.object(ra, "service_client", return_value=cap), \
         patch.object(ra, "band_answer", return_value=(1, 0.8)), \
         patch.object(ra, "_resolve_key", side_effect=AssertionError("re-resolved")):
        ra.record_aspect(
            signal_id="s1", subject_ref="sub1", author_id="u1",
            aspect_key="front_desk", aspect_label="the front desk", source_span=None,
            answer_verbatim="friendly enough", answer_source="voice")
    assert cap.rows[0]["aspect_key"] == "front_desk"


def test_find_runs_as_the_viewer_not_the_service_role():
    """Visibility is auth.uid() in SQL; a service-role call must not be how Find runs,
    and the viewer must not be an argument a client could forge."""
    calls = []
    with patch.object(ra, "split_query", return_value=[{"text": "owner speaks Italian"}]), \
         patch("app.vertex_extract.vertex_embed", return_value=[0.1, 0.2]), \
         patch("app.supabase_rpc.call_rpc",
               side_effect=lambda jwt, name, args: calls.append((jwt, name, args)) or []), \
         patch.object(ra, "service_client", side_effect=AssertionError("service role")):
        ra.find_by_aspects(request="owner speaks Italian", user_jwt="jwt-1")
    jwt, name, args = calls[0]
    assert (jwt, name) == ("jwt-1", "search_subjects_by_aspect")
    assert "p_viewer_id" not in args
    assert all(isinstance(v, str) for v in args["p_clauses"])


def test_an_aspect_they_never_said_is_dropped():
    """Observed live: the prompt's own "the wait" example came back as an aspect on a
    plumber nobody said kept them waiting. Every aspect must trace to their words."""
    statement = "He came the same day and fixed our leak fast, but he left a mess."
    out = ra._parse_aspects({"aspects": [
        {"label": "the mess", "key": "mess", "span": "he left a mess", "confidence": 0.9},
        {"label": "the wait", "key": "wait_time", "span": "the wait was bad", "confidence": 0.9},
        {"label": "nothing", "key": "nothing", "span": "", "confidence": 0.9},
    ]}, statement=statement)
    assert [a["aspect_key"] for a in out] == ["mess"]


def test_traceable_tolerates_light_rephrasing_and_other_languages():
    assert ra._traceable("fixed the leak fast", "He fixed our leak fast.")
    assert ra._traceable("la espera fue larguísima", "pero la espera fue larguísima y")
    assert not ra._traceable("the parking was hard", "Great croissants, long line.")


def test_the_subject_itself_is_never_an_aspect():
    """"What is Carlos the barber like to deal with?" — the subject is what is being
    recommended, not one of the things said about it."""
    out = ra._parse_aspects({"aspects": [
        {"label": "barber", "key": "barber", "span": "a barber", "confidence": 0.9},
        {"label": "the shop", "key": "shop", "span": "the shop is tidy", "confidence": 0.9},
    ]}, statement="a barber whose shop is tidy", subject_terms=["Carlos the barber", "barber"])
    assert [a["aspect_key"] for a in out] == ["shop"]



class _BackfillDB:
    """Just enough of the supabase query builder for backfill_embeddings."""

    def __init__(self, label_rows, content_rows):
        self.label_rows, self.content_rows, self.updates = label_rows, content_rows, []
        self._mode = None

    def table(self, _):
        return self

    def select(self, cols):
        self._mode = "label" if "aspect_key" in cols else "content"
        self._update = None
        return self

    def is_(self, *a):
        return self

    def in_(self, *a):
        return self

    @property
    def not_(self):
        return self

    def limit(self, _):
        return self

    def update(self, patch):
        self._update = patch
        return self

    def eq(self, _col, val):
        self.updates.append((val, self._update))
        return self

    def execute(self):
        class R:
            pass
        r = R()
        r.data = (self.label_rows if self._mode == "label" else self.content_rows) \
            if self._update is None else []
        return r


def test_backfill_fills_both_vectors():
    db = _BackfillDB([{"id": "a", "aspect_key": "wait_time", "aspect_label": "the wait"}],
                     [{"id": "b", "aspect_label": "the price", "answer_verbatim": "$20"}])
    with patch.object(ra, "service_client", return_value=db), \
         patch("app.vertex_extract.vertex_embed", return_value=[0.1, 0.2]):
        out = ra.backfill_embeddings()
    assert out == {"label": 1, "content": 1, "failed": 0}
    assert [(i, sorted(p)) for i, p in db.updates] == [("a", ["label_embedding"]),
                                                       ("b", ["embedding"])]


def test_backfill_stops_at_first_failed_embed():
    db = _BackfillDB([{"id": "a", "aspect_key": "k", "aspect_label": "l"},
                      {"id": "b", "aspect_key": "k2", "aspect_label": "l2"}], [])
    with patch.object(ra, "service_client", return_value=db), \
         patch("app.vertex_extract.vertex_embed", return_value=[]) as emb:
        out = ra.backfill_embeddings()
    assert out["failed"] == 1 and emb.call_count == 1 and db.updates == []
