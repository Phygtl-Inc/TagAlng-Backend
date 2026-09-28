"""The "Help Lana learn more" round, wired into the capture flow (app/aspect_round.py).

What must hold:
  · Off unless LANA_ASPECTS — the migrations are not applied everywhere yet.
  · The round opens on the posting turn from the author's OWN words, and is carried on
    that turn only (never sticky).
  · The endpoint accepts only keys from this session's round, writes as the caller, and
    clears a finished round with None.
  · Nothing that reaches a client carries a sentiment band.
"""

from __future__ import annotations

import os
from unittest import mock

import pytest
from fastapi import HTTPException

from app import aspect_round as ar

_QS = [
    {"aspect_key": "wait_time", "aspect_label": "the wait", "source_span": "an hour",
     "question": "You mentioned the wait — was it bad?"},
    {"aspect_key": "front_desk", "aspect_label": "the front desk", "source_span": "rude",
     "question": "You mentioned the front desk — how were they?"},
]

_DRAFT = {
    "name": "Dr. Sarah",
    "category": "pediatrician",
    "trait": "amazing with my toddler, but the wait was over an hour",
    "details": ["front desk was kind of rude"],
    "reco_type": "professional",
    "step_set": [
        {"field": "profession", "label": "Profession", "question": "What does she do?",
         "kind": "text", "required": True},
        {"field": "ask_ok", "label": "Neighbours", "question": "Can neighbours ask?",
         "kind": "toggle", "options": ["Let them ask", "Keep it to the card"]},
    ],
    "answers": {"profession": "pediatrician", "ask_ok": "Let them ask"},
}


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv("LANA_ASPECTS", "1")


def _open(ctx=None, **kw):
    ctx = {} if ctx is None else ctx
    with mock.patch("app.reco_aspects.open_aspect_questions", return_value=list(_QS)) as m:
        rnd = ar.open_after_post(
            ctx, draft=dict(_DRAFT), signal_id="sig-1", subject_ref="sub-1",
            user_id="u-1", **kw)
    return ctx, rnd, m


# ── flag ────────────────────────────────────────────────────────────────────

def test_off_by_default(monkeypatch):
    monkeypatch.delenv("LANA_ASPECTS", raising=False)
    ctx, rnd, m = _open()
    assert rnd is None and ctx == {}
    m.assert_not_called()


# ── opening ─────────────────────────────────────────────────────────────────

def test_round_opens_from_their_own_words(on):
    ctx, rnd, m = _open()
    stmt = m.call_args.kwargs["statement"]
    assert "the wait was over an hour" in stmt and "front desk was kind of rude" in stmt
    # Card answers are the template's facts (decision A) — never split into questions.
    # "Profession: pediatrician" became "What is Dr. Sarah the pediatrician like?".
    assert "pediatrician" not in stmt
    assert "Let them ask" not in stmt
    assert m.call_args.kwargs["author_id"] == "u-1"
    assert m.call_args.kwargs["subject_ref"] == "sub-1"
    assert m.call_args.kwargs["subject_terms"] == ["Dr. Sarah", "pediatrician"]
    assert ctx[ar.CTX_KEY]["status"] == "offered"
    assert [i["aspect_key"] for i in rnd["items"]] == ["wait_time", "front_desk"]


def test_their_verbatim_message_is_what_gets_split(on):
    """The extracted trait keeps one clause; their message keeps all of them."""
    draft = {**_DRAFT, "trait": "shop is always tidy", "details": [],
             "statement": "a spanish barber whose pricing is good but the shop is tidy"}
    with mock.patch("app.reco_aspects.open_aspect_questions", return_value=list(_QS)) as m:
        ar.open_after_post({}, draft=draft, signal_id="s", subject_ref=None, user_id="u")
    stmt = m.call_args.kwargs["statement"]
    assert "pricing is good" in stmt and "spanish" in stmt


def test_nothing_specific_said_means_no_round(on):
    ctx = {}
    with mock.patch("app.reco_aspects.open_aspect_questions", return_value=[]):
        assert ar.open_after_post(ctx, draft=dict(_DRAFT), signal_id="s", subject_ref=None,
                                  user_id="u") is None
    assert ar.CTX_KEY not in ctx


def test_public_round_hides_internals_and_has_no_band(on):
    _, rnd, _ = _open()
    pub = ar.public_round(rnd)
    flat = repr(pub)
    for leaked in ("signal_id", "subject_ref", "source_span", "sentiment"):
        assert leaked not in flat
    assert pub["current"] == 0 and pub["total"] == 2 and pub["answered"] == 0


# ── actions ─────────────────────────────────────────────────────────────────

def _act(ctx, **kw):
    with mock.patch("app.reco_aspects.record_aspect") as rec:
        rnd = ar.apply_action(ctx, author_id="u-1", **kw)
    return rnd, rec


def test_answer_writes_as_the_caller_under_the_opened_key(on):
    ctx, _, _ = _open()
    rnd, rec = _act(ctx, action="answer", aspect_key="wait_time",
                    answer="  really was — long wait ", source="voice")
    kw = rec.call_args.kwargs
    assert (kw["author_id"], kw["signal_id"], kw["aspect_key"]) == ("u-1", "sig-1", "wait_time")
    assert kw["answer_verbatim"] == "really was — long wait"
    assert kw["answer_source"] == "voice"
    assert rnd["status"] == "active"
    assert ar.public_round(rnd)["current"] == 1


def test_unknown_key_is_refused(on):
    ctx, _, _ = _open()
    with pytest.raises(ar.RoundError):
        _act(ctx, action="answer", aspect_key="porcelain", answer="x")


def test_empty_answer_is_refused(on):
    ctx, _, _ = _open()
    with pytest.raises(ar.RoundError):
        _act(ctx, action="answer", aspect_key="wait_time", answer="   ")


def test_skip_one_then_answer_last_finishes(on):
    ctx, _, _ = _open()
    _, rec = _act(ctx, action="skip", aspect_key="wait_time")
    assert rec.call_args.kwargs["answer_source"] == "skipped"
    assert rec.call_args.kwargs["answer_verbatim"] is None
    rnd, _ = _act(ctx, action="answer", aspect_key="front_desk", answer="short with me")
    assert rnd["status"] == "done"
    assert ar.public_round(rnd)["current"] is None


def test_skip_all_stores_only_what_was_still_owed(on):
    ctx, _, _ = _open()
    _act(ctx, action="answer", aspect_key="wait_time", answer="long")
    rnd, rec = _act(ctx, action="skip_all")
    assert [c.kwargs["aspect_key"] for c in rec.call_args_list] == ["front_desk"]
    # A decision, stored as one — an 'open' row would be re-offered after they said no.
    assert [c.kwargs["answer_source"] for c in rec.call_args_list] == ["skipped"]
    assert rnd["status"] == "skipped"
    assert rnd["items"][0]["state"] == "answered"


def test_closed_round_refuses_more(on):
    ctx, _, _ = _open()
    _act(ctx, action="skip_all")
    with pytest.raises(ar.RoundError):
        _act(ctx, action="start")


def test_settle_clears_with_none_not_pop(on):
    ctx, _, _ = _open()
    _act(ctx, action="skip_all")
    ar.settle(ctx)
    assert ar.CTX_KEY in ctx and ctx[ar.CTX_KEY] is None


# ── reoffer ─────────────────────────────────────────────────────────────────

def test_reoffer_is_one_recommendation_and_localized(on):
    rows = [
        {"signal_id": "a", "aspect_key": "wait_time", "aspect_label": "the wait",
         "question": "Earlier you mentioned the wait — how was that?"},
        {"signal_id": "b", "aspect_key": "crust", "aspect_label": "the crust",
         "question": "Earlier you mentioned the crust — how was that?"},
    ]
    ctx = {}
    with mock.patch("app.reco_aspects.reoffer_open_aspects", return_value=rows), \
         mock.patch.object(ar, "_subject_name", return_value="Dr. Sarah"), \
         mock.patch("app.i18n.localize_text", side_effect=lambda t, l: f"[{l}] {t}"):
        rnd = ar.reoffer_round(ctx, user_id="u-1", lang="es")
    assert [i["aspect_key"] for i in rnd["items"]] == ["wait_time"]
    assert rnd["mode"] == "reoffer" and rnd["subject_name"] == "Dr. Sarah"
    assert rnd["items"][0]["question"].startswith("[es] ")


# ── reader side ─────────────────────────────────────────────────────────────

def test_attach_aspects_words_and_counts_only(on):
    cards = [{"subject_ref": "sub-1"}, {"subject_ref": None}]
    rows = [
        {"aspect_key": "wait_time", "aspect_label": "the wait", "n_people": 5,
         "n_shared_community": 2, "sample_quotes": ["over an hour", "long"]},
        {"aspect_key": "parking", "aspect_label": "parking", "n_people": 1,
         "n_shared_community": 0, "sample_quotes": None},   # only ever skipped
    ]
    with mock.patch("app.supabase_rpc.call_rpc", return_value=rows) as rpc:
        ar.attach_aspects(cards, user_jwt="jwt-r")
    assert rpc.call_args.args[:2] == ("jwt-r", "subject_aspects")
    assert cards[0]["aspects"] == [{
        "aspect_key": "wait_time", "label": "the wait", "n_people": 5,
        "n_shared_community": 2, "quotes": ["over an hour", "long"]}]
    assert "aspects" not in cards[1]


def test_rerank_promotes_coverage_and_is_stable(on):
    tips = [{"signal_id": "1", "subject_ref": "A"}, {"signal_id": "2", "subject_ref": "B"},
            {"signal_id": "3", "subject_ref": None}]
    hits = [{"subject_ref": "B", "clauses_matched": 2, "clauses_total": 2,
             "matched_aspects": [{"quote": "no wait at all"}]}]
    with mock.patch("app.reco_aspects.find_by_aspects", return_value=hits) as f:
        out = ar.rerank_tips_by_aspects(tips, request="toddlers and no wait", user_jwt="j")
    assert f.call_args.kwargs["subject_scope"] == ["A", "B"]
    assert [t["signal_id"] for t in out] == ["2", "1", "3"]
    assert out[0]["_aspect_match"]["quotes"] == ["no wait at all"]


def test_rerank_off_is_a_noop(monkeypatch):
    monkeypatch.delenv("LANA_ASPECTS", raising=False)
    tips = [{"subject_ref": "A"}]
    with mock.patch("app.reco_aspects.find_by_aspects") as f:
        assert ar.rerank_tips_by_aspects(tips, request="x", user_jwt="j") is tips
    f.assert_not_called()


# ── tip_share posting turn ──────────────────────────────────────────────────

def test_posting_turn_opens_the_round(on):
    from app.tip_share import run_tip_share_turn

    ctx = {"tip_share_active": True, "tip_ready": True, "tip_draft": dict(_DRAFT)}
    with mock.patch("app.tip_share._save_tip",
                    return_value=({"signal_id": "sig-9", "subject_ref": "sub-9",
                                   "matches_created": 0}, "")), \
         mock.patch("app.auth.jwt_user_id", return_value="u-7"), \
         mock.patch("app.reco_aspects.open_aspect_questions", return_value=list(_QS)) as m:
        run_tip_share_turn(user_message="Pass the tip along", session_ctx=ctx, history=[],
                           user_jwt="jwt", home_block_id="blk")
    assert ctx["tip_listed_now"]
    assert m.call_args.kwargs["signal_id"] == "sig-9"
    assert m.call_args.kwargs["subject_ref"] == "sub-9"
    assert m.call_args.kwargs["author_id"] == "u-7"
    assert ctx[ar.CTX_KEY]["round_id"] == "sig-9"


def test_new_post_with_nothing_to_ask_drops_a_stale_round(on):
    from app.tip_share import run_tip_share_turn

    ctx = {"tip_share_active": True, "tip_ready": True, "tip_draft": dict(_DRAFT),
           ar.CTX_KEY: {"round_id": "old", "items": [{"aspect_key": "x"}]}}
    with mock.patch("app.tip_share._save_tip",
                    return_value=({"signal_id": "sig-2", "matches_created": 0}, "")), \
         mock.patch("app.auth.jwt_user_id", return_value="u-7"), \
         mock.patch("app.reco_aspects.open_aspect_questions", return_value=[]):
        run_tip_share_turn(user_message="Pass the tip along", session_ctx=ctx, history=[],
                           user_jwt="jwt", home_block_id="blk")
    assert ctx[ar.CTX_KEY] is None


# ── the turn payload + endpoint ─────────────────────────────────────────────

def test_round_rides_only_the_posting_turn(on):
    from app.main import _aspect_round_for_turn

    _, rnd, _ = _open()
    assert _aspect_round_for_turn({"tip_listed_now": True, ar.CTX_KEY: rnd}) is not None
    assert _aspect_round_for_turn({"tip_listed_now": False, ar.CTX_KEY: rnd}) is None
    dumped = _aspect_round_for_turn({"tip_listed_now": True, ar.CTX_KEY: rnd}).model_dump()
    assert "sentiment" not in repr(dumped) and "signal_id" not in repr(dumped)


def _endpoint(ctx, body):
    from app.auth import AuthSession
    from app.main import set_aspect_answer

    written: dict = {}
    auth = AuthSession(user_id="u-1", is_anonymous=False, phone_verified=True,
                       home_block_id="b1")
    with mock.patch("app.main.verify_auth", return_value=auth), \
         mock.patch("app.main.get_session_for_user", return_value={"context": ctx}), \
         mock.patch("app.main.update_session_context",
                    side_effect=lambda sid, c: written.update(c)), \
         mock.patch("app.reco_aspects.record_aspect") as rec:
        res = set_aspect_answer("s-1", body, authorization="Bearer t")
    return res, written, rec


def test_endpoint_disabled_is_404(monkeypatch):
    from app.models import AspectAnswerRequest

    monkeypatch.delenv("LANA_ASPECTS", raising=False)
    with pytest.raises(HTTPException) as e:
        _endpoint({}, AspectAnswerRequest(action="start"))
    assert e.value.status_code == 404


def test_endpoint_answer_then_finish_clears_the_session(on):
    from app.models import AspectAnswerRequest

    ctx, _, _ = _open()
    res, written, rec = _endpoint(
        dict(ctx), AspectAnswerRequest(action="answer", aspect_key="wait_time",
                                       answer="long wait"))
    assert res.aspect_round.status == "active" and res.aspect_round.answered == 1
    assert rec.call_args.kwargs["author_id"] == "u-1"
    res, written, _ = _endpoint(
        dict(written), AspectAnswerRequest(action="skip", aspect_key="front_desk"))
    assert res.aspect_round.status == "done"
    assert written[ar.CTX_KEY] is None


def test_endpoint_unknown_key_is_409(on):
    from app.models import AspectAnswerRequest

    ctx, _, _ = _open()
    with pytest.raises(HTTPException) as e:
        _endpoint(dict(ctx), AspectAnswerRequest(action="answer", aspect_key="nope",
                                                 answer="x"))
    assert e.value.status_code == 409


def test_endpoint_without_a_round_is_409(on):
    from app.models import AspectAnswerRequest

    with pytest.raises(HTTPException) as e:
        _endpoint({}, AspectAnswerRequest(action="start"))
    assert e.value.status_code == 409


# ── reco card ordering ──────────────────────────────────────────────────────

def test_subject_cards_rank_aspect_coverage_after_provenance(on):
    from app.reco_cards import subject_cards_from_tips

    def tip(sig, ref, strength, match=None):
        t = {"signal_id": sig, "subject_ref": ref, "peer_user_id": f"p{sig}",
             "detail_text": "x", "reco_name": f"Dr {sig}", "match_strength": strength,
             "subject_vouch_count": 1, "subject_merge_mode": "aggregate"}
        if match:
            t["_aspect_match"] = match
        return t

    tips = [tip("1", "A", 0.9), tip("2", "B", 0.4,
                                    {"clauses_matched": 2, "clauses_total": 2, "quotes": []})]
    with mock.patch("app.aspect_round.attach_aspects"):
        cards = subject_cards_from_tips(tips, user_jwt="j")
    assert [c["subject_ref"] for c in cards][:2] == ["B", "A"]
    assert cards[0]["aspect_match"]["clauses_matched"] == 2


def test_capture_keeps_their_opening_words_on_the_draft(on):
    from app.tip_share import run_tip_share_turn

    ctx: dict = {}
    with mock.patch("app.tip_share.llm_configured", return_value=False, create=True):
        try:
            run_tip_share_turn(
                user_message="I want to recommend Carlos, pricing is good and the shop is tidy",
                session_ctx=ctx, history=[], user_jwt="jwt", home_block_id="blk")
        except Exception:  # noqa: BLE001 — only the stamp is under test
            pass
    said = (ctx.get("tip_draft") or {}).get("statement") or ""
    assert "pricing is good" in said


def test_control_lines_never_replace_the_statement():
    from app.tip_share import posting_cta

    assert posting_cta("pass the tip along") and posting_cta("Looks good")
    assert posting_cta("fix:profession")
    assert not posting_cta("Carlos at Fade Factory, great fades")
