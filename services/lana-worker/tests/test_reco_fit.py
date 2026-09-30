"""The "Why Lana sees a fit" line + proof headlines (app/reco_fit.py).

These make claims about identifiable neighbours, so what matters is what they may NOT say:
a headline with the wrong count, a cohort label on people who are not in it, a relevance
line made of what is missing. And that a slow model never costs the reader the card.
"""

from __future__ import annotations

import time
from unittest import mock

import pytest

from app import reco_fit as rf


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setenv("LANA_RECO_FIT", "1")
    rf._cache.clear()
    rf._pending.clear()


def _card(**kw):
    base = {
        "subject_ref": "p", "title": "Prestige", "category": "barbershop",
        "distance_text": "1 min walk", "group_kind": "circle", "group_label": "Mizu",
        "vouch_count": 2,
        "contributors": [{"signal_id": "a", "shared_circles": [{"name": "Mizu"}]},
                         {"signal_id": "b", "shared_circles": []}],
        "aspects": [{"aspect_key": "spanish", "label": "barbers speak Spanish", "n_people": 2,
                     "quotes": ["they switch to Spanish", "my barber cuts in Spanish"]}],
    }
    base.update(kw)
    return base


# ── what the facts say ──────────────────────────────────────────────────────

def test_cohort_label_only_when_every_recommender_is_in_it():
    full = rf._facts(_card(cohorts=[{"label": "Spanish speakers", "n": 2, "total": 2}]), [])
    part = rf._facts(_card(cohorts=[{"label": "Spanish speakers", "n": 1, "total": 2}]), [])
    assert full["cohort_label"] == "Spanish speakers"
    assert part["cohort_label"] is None


def test_community_count_is_how_many_recommenders_share_it():
    """Prod QA: "2 neighbours in your community" when only 1 of the 2 was."""
    assert rf._facts(_card(), [])["recommenders_in_your_community"] == 1


def test_no_reason_no_line():
    """A far card with nothing that fits got "33 miles away and not in your community"."""
    far = rf._facts(_card(group_kind="nearby", group_label=None), [])
    assert rf._has_reason(far) is False
    assert rf._has_reason(rf._facts(_card(), [])) is True


# ── validation of what the model wrote ──────────────────────────────────────

def _facts_by_id(card):
    f = rf._facts(card, [])
    return {f["id"]: f}


def test_headline_must_carry_the_real_count():
    fb = _facts_by_id(_card())
    ok = rf._valid({"cards": [{"id": "p", "fit_line": "x", "aspects": [
        {"key": "spanish", "headline": "2 neighbours said they cut in Spanish"}]}]}, fb)
    wrong = rf._valid({"cards": [{"id": "p", "fit_line": "x", "aspects": [
        {"key": "spanish", "headline": "3 neighbours said they cut in Spanish"}]}]}, fb)
    none = rf._valid({"cards": [{"id": "p", "fit_line": "x", "aspects": [
        {"key": "spanish", "headline": "Neighbours said they cut in Spanish"}]}]}, fb)
    assert ok["p"]["headlines"] == {"spanish": "2 neighbours said they cut in Spanish"}
    assert wrong["p"]["headlines"] == {} and none["p"]["headlines"] == {}


def test_headline_for_an_aspect_the_card_does_not_have_is_dropped():
    out = rf._valid({"cards": [{"id": "p", "aspects": [
        {"key": "parking", "headline": "1 neighbour said parking is easy"}]}]}, _facts_by_id(_card()))
    assert out["p"]["headlines"] == {}


def test_fit_line_dropped_without_a_positive_reason():
    far = _card(group_kind="nearby", group_label=None)
    out = rf._valid({"cards": [{"id": "p", "fit_line": "It is 33 miles away."}]},
                    _facts_by_id(far))
    assert out["p"]["fit_line"] is None


@pytest.mark.parametrize("bad", [None, [], {"cards": "x"}, {"cards": [1, {"id": "nope"}]}])
def test_garbage_never_raises(bad):
    assert rf._valid(bad, _facts_by_id(_card())) == {}


# ── the page: one call, cache, bounded wait ─────────────────────────────────

GOOD = {"p": {"fit_line": "1 person in your Mizu community vouches for it.",
              "headlines": {"spanish": "2 neighbours said they cut in Spanish"}}}


def test_applies_line_and_headline():
    cards = [_card()]
    with mock.patch.object(rf, "_compose", return_value=GOOD) as comp:
        rf.attach_fit(cards, lang="en", reader_id="r")
    assert comp.call_count == 1
    assert cards[0]["fit_line"].startswith("1 person")
    assert cards[0]["aspects"][0]["headline"] == "2 neighbours said they cut in Spanish"


def test_one_call_for_the_whole_page():
    cards = [_card(subject_ref=f"s{i}", title=f"T{i}") for i in range(4)]
    with mock.patch.object(rf, "_compose", return_value={}) as comp:
        rf.attach_fit(cards, reader_id="r")
    assert comp.call_count == 1
    assert len(comp.call_args.args[0]) == 4


def test_second_look_is_served_from_cache():
    with mock.patch.object(rf, "_compose", return_value=GOOD) as comp:
        rf.attach_fit([_card()], reader_id="r")
        again = [_card()]
        rf.attach_fit(again, reader_id="r")
    assert comp.call_count == 1
    assert again[0]["fit_line"]


def test_changed_facts_are_rewritten():
    with mock.patch.object(rf, "_compose", return_value=GOOD) as comp:
        rf.attach_fit([_card()], reader_id="r")
        rf.attach_fit([_card(vouch_count=3)], reader_id="r")
    assert comp.call_count == 2


def test_slow_model_leaves_the_card_and_fills_the_cache_later():
    def slow(facts, lang, claims=None):
        time.sleep(0.4)
        return GOOD

    cards = [_card()]
    with mock.patch.object(rf, "_compose", side_effect=slow):
        rf.attach_fit(cards, reader_id="r", timeout_s=0.05)
        assert "fit_line" not in cards[0]
        time.sleep(0.6)
        later = [_card()]
        rf.attach_fit(later, reader_id="r")
    assert later[0]["fit_line"]


def test_start_and_finish_overlap_other_work():
    def compose(facts, lang, claims=None):
        time.sleep(0.2)
        return GOOD

    cards = [_card()]
    with mock.patch.object(rf, "_compose", side_effect=compose):
        t0 = time.monotonic()
        rf.start_fit(cards, reader_id="r")
        time.sleep(0.25)             # the reply compose, running meanwhile
        rf.finish_fit(cards)
        elapsed = time.monotonic() - t0
    assert cards[0]["fit_line"] and elapsed < 0.4


def test_failed_compose_is_todays_card():
    cards = [_card()]
    with mock.patch.object(rf, "_compose", side_effect=RuntimeError("down")):
        rf.attach_fit(cards, reader_id="r")
    assert "fit_line" not in cards[0]


def test_off_switch(monkeypatch):
    monkeypatch.setenv("LANA_RECO_FIT", "0")
    with mock.patch.object(rf, "_compose") as comp:
        rf.attach_fit([_card()], reader_id="r")
    comp.assert_not_called()


def test_language_goes_by_name():
    assert rf._language_name("es") == "Spanish (es)"
    assert rf._language_name("pt-BR") == "Portuguese (Brazil) (pt-br)"
    assert rf._language_name("xx") == "xx"


# ── For you: the reader's claims (2026-09-29 claims-rank spec) ───────────────

CLAIMS = [{"id": "c1", "label": "Has toddlers", "bucket": "stage", "sayable": True, "about": "self"}]


def _fy_card(**kw):
    card = _card(**kw)
    card["contributors"] = [{"description": "so gentle with the toddlers, they loved her", "peer_user_id": "p"}]
    return card


def test_for_you_quote_must_be_the_neighbours_own_words():
    facts = rf._facts(_fy_card(), [])
    good = {"id": facts["id"], "fit_line": "x", "for_you": [
        {"claim": "c1", "line": "You've mentioned your toddlers — a neighbour says she's gentle with them.",
         "quotes": ["so gentle with the toddlers"]}]}
    out = rf._valid({"cards": [good]}, {facts["id"]: facts}, CLAIMS)[facts["id"]]
    assert out["for_you"][0]["quotes"] == ["so gentle with the toddlers"]
    assert out["for_you"][0]["claim_label"] == "Has toddlers"
    bad = {**good, "for_you": [{**good["for_you"][0], "quotes": ["the best pediatric dentist in town"]}]}
    assert rf._valid({"cards": [bad]}, {facts["id"]: facts}, CLAIMS)[facts["id"]]["for_you"] == []


def test_order_for_you_moves_up_only_within_tier_and_group():
    cards = [
        {"title": "circle-plain", "group_kind": "circle"},
        {"title": "block-plain", "group_kind": "block"},
        {"title": "block-fits", "group_kind": "block", "for_you": [{"line": "x"}]},
        {"title": "strong-plain", "group_kind": "nearby", "_standing_tier": "strong"},
    ]
    rf.order_for_you(cards)
    assert [c["title"] for c in cards] == ["strong-plain", "circle-plain", "block-fits", "block-plain"]


def test_no_claims_keeps_todays_order_and_prompt():
    cards = [{"title": "a", "group_kind": "block"}, {"title": "b", "group_kind": "block"}]
    rf.order_for_you(cards)
    assert [c["title"] for c in cards] == ["a", "b"]
    assert "no reader_claims" in rf._prompt(False)


def test_start_fit_hands_the_readers_claims_to_the_one_call():
    seen = {}

    def compose(facts, lang, claims=None):
        seen["claims"] = claims
        return {facts[0]["id"]: {"fit_line": "Fits.", "headlines": {},
                                 "for_you": [{"line": "For your toddlers.", "claim_label": "Has toddlers",
                                              "quotes": ["so gentle with the toddlers"]}]}}

    cards = [_fy_card(), _card(title="Other", subject_ref="s2")]
    looked = [{**CLAIMS[0], "look_for": "gentle with toddlers", "because": ""}]
    with mock.patch("app.reader_claims.load_reader_claims", return_value=CLAIMS), \
            mock.patch("app.reader_claims.expand_for_ask", return_value=looked), \
            mock.patch.object(rf, "_compose", side_effect=compose):
        rf.attach_fit(cards, reader_id="r-claims")
    # The writer gets the claims as expanded for this ask, not the raw list.
    assert seen["claims"] == looked
    fitted = [c for c in cards if c.get("for_you")]
    assert fitted and fitted[0]["for_you"][0]["claim_label"] == "Has toddlers"


def test_two_neighbour_cards_never_give_the_same_reason():
    claims = CLAIMS + [{"id": "c2", "label": "Plays guitar", "bucket": "hobby", "sayable": True, "about": "self"}]
    words = "so gentle with the toddlers, they loved her. live music on fridays"
    a = rf._facts(_fy_card(subject_ref="a"), [])
    b = rf._facts(_fy_card(subject_ref="b"), [])
    for f in (a, b):
        f["their_words"] = [words]
    kids = {"claim": "c1", "line": "kids", "quotes": ["so gentle with the toddlers"]}
    music = {"claim": "c2", "line": "music", "quotes": ["live music on fridays"]}
    parsed = {"cards": [{"id": f["id"], "fit_line": "x", "for_you": [kids, music]} for f in (a, b)]}
    with mock.patch("app.orchestrator.llm.llm_configured", return_value=True), \
            mock.patch("app.orchestrator.llm.llm_json", return_value=parsed), \
            mock.patch("app.reader_claims.judge_for_you", side_effect=lambda pairs: {p["key"] for p in pairs}):
        out = rf._compose([a, b], "en", claims)
    assert [[x["line"] for x in out[f["id"]]["for_you"]] for f in (a, b)] == [["Kids."], ["Music."]]
