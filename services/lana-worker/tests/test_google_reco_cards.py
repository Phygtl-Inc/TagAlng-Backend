"""Google places in the recommendation card template — app/google_reco_cards.py."""

from __future__ import annotations

from typing import Any

import pytest

import app.google_reco_cards as g

REVIEWS = [
    {"text": "The lamb adana was the best I've had outside Istanbul. Staff are so warm.",
     "author": "Mehmet K.", "author_url": "https://maps.google.com/u/1"},
    {"text": "Authentic Turkish breakfast on weekends, get the menemen!",
     "author": "Sara L.", "author_url": "https://maps.google.com/u/2"},
]
PLACE = {"place_id": "p1", "name": "Cafe 34 Istanbul", "reviews": REVIEWS}


def _parsed(**over: Any) -> dict[str, Any]:
    item = {
        "id": "p1",
        "fit_line": "Reviewers say it tastes like Istanbul.",
        "aspects": [{
            "label": "the lamb adana",
            "headline": "Google reviewers mention the lamb adana",
            "quotes": [{"review": 1, "excerpt": "The lamb adana was the best I've had outside Istanbul"}],
        }],
    }
    item.update(over)
    return {"places": [item]}


# ── grounding ───────────────────────────────────────────────────────────────


def test_a_verbatim_quote_survives_with_its_reviewer():
    out = g.ground(_parsed(), [PLACE])["p1"]
    q = out["aspects"][0]["quotes"][0]
    assert q == {"text": "The lamb adana was the best I've had outside Istanbul",
                 "author": "Mehmet K.", "author_url": "https://maps.google.com/u/1"}
    assert out["fit_line"] == "Reviewers say it tastes like Istanbul."


def test_spacing_and_case_do_not_matter_but_words_do():
    p = _parsed(aspects=[{"label": "breakfast", "headline": "Google reviewers mention breakfast",
                          "quotes": [{"review": 2, "excerpt": "authentic   turkish BREAKFAST on weekends"}]}])
    assert g.ground(p, [PLACE])["p1"]["aspects"]


def test_an_invented_quote_is_dropped_with_its_aspect():
    p = _parsed(aspects=[{"label": "the baklava", "headline": "Google reviewers mention the baklava",
                          "quotes": [{"review": 1, "excerpt": "the baklava is flaky and fresh"}]}])
    assert g.ground(p, [PLACE])["p1"]["aspects"] == []


def test_real_words_cited_to_the_wrong_reviewer_are_dropped():
    p = _parsed(aspects=[{"label": "breakfast", "headline": "Google reviewers mention breakfast",
                          "quotes": [{"review": 1, "excerpt": "Authentic Turkish breakfast on weekends"}]}])
    assert g.ground(p, [PLACE])["p1"]["aspects"] == []


def test_a_headline_with_a_count_is_dropped():
    p = _parsed(aspects=[{"label": "the lamb", "headline": "2 Google reviewers mention the lamb",
                          "quotes": [{"review": 1, "excerpt": "The lamb adana was the best"}]}])
    assert g.ground(p, [PLACE])["p1"]["aspects"] == []


def test_out_of_range_review_and_unknown_place_are_ignored():
    p = _parsed(aspects=[{"label": "x y", "headline": "Google reviewers mention x",
                          "quotes": [{"review": 9, "excerpt": "The lamb adana was the best"}]}])
    assert g.ground(p, [PLACE])["p1"]["aspects"] == []
    assert g.ground({"places": [{"id": "nope"}]}, [PLACE]) == {}


@pytest.mark.parametrize("bad", [None, "x", {"places": "x"}, {"places": [1, None]}])
def test_garbage_never_raises(bad):
    assert g.ground(bad, [PLACE]) == {}


# ── building cards ──────────────────────────────────────────────────────────


def _row(pid: str, **over: Any) -> dict[str, Any]:
    r = {"place_id": pid, "name": f"Place {pid}", "address": "1 Main St", "lat": 28.4, "lng": -81.25}
    r.update(over)
    return r


def _details(pid: str, reviews=REVIEWS):
    return {"place_id": pid, "rating": 4.6, "rating_count": 1240,
            "maps_url": f"https://maps.google.com/?cid={pid}", "type_label": "Turkish restaurant",
            "reviews": list(reviews)}


def test_cards_are_google_cards_top_three_and_never_community_rows(monkeypatch):
    fetched: list[str] = []

    def fake_details(pid, *, lang="en"):
        fetched.append(pid)
        return _details(pid)

    monkeypatch.setattr(g, "place_reviews", fake_details)
    monkeypatch.setattr(g, "_compose", lambda places, **k: {"places": [
        {"id": p["place_id"], "fit_line": "Reviewers love it.", "aspects": _parsed()["places"][0]["aspects"]}
        for p in places]})
    rows = [_row("c", community={"member_count": 3}), _row("a"), _row("b"), _row("d"), _row("e")]
    cards = g.build_cards(rows, ask="turkish places", chips=["turkish restaurant"],
                          origin=(28.4, -81.25))
    # The whole pool is enriched (our community row never is); three are shown.
    assert sorted(fetched) == ["a", "b", "d", "e"]
    assert [x["subject_ref"] for x in cards] == ["google:a", "google:b", "google:d"]
    c = cards[0]
    assert c["source"] == "google" and c["group_kind"] == "google" and c["contributors"] == []
    assert c["google"] == {"rating": 4.6, "rating_count": 1240, "maps_url": "https://maps.google.com/?cid=a"}
    assert c["fit_line"] == "Reviewers love it."
    assert c["aspects"][0]["review_quotes"][0]["author"] == "Mehmet K."
    assert c["aspects"][0]["n_people"] == 0
    assert c["distance_text"] == "0.1 mi"
    assert c["fit_chips"] == ["turkish restaurant"]


def test_a_place_whose_details_fail_is_skipped_and_the_rest_still_build(monkeypatch):
    monkeypatch.setattr(g, "place_reviews", lambda pid, **k: None if pid == "a" else _details(pid))
    monkeypatch.setattr(g, "_compose", lambda places, **k: {})
    cards = g.build_cards([_row("a"), _row("b")], ask="x", chips=[])
    assert [c["subject_ref"] for c in cards] == ["google:b"]
    # The model failing leaves the card without a fit line or evidence — never invented.
    assert cards[0]["fit_line"] is None and cards[0]["aspects"] is None


def test_a_place_with_no_reviews_is_not_sent_to_the_model(monkeypatch):
    monkeypatch.setattr(g, "place_reviews", lambda pid, **k: _details(pid, reviews=[]))

    def boom(*a, **k):
        raise AssertionError("no reviews → no model call")

    monkeypatch.setattr(g, "_compose", boom)
    assert g.build_cards([_row("a")], ask="x", chips=[])[0]["aspects"] is None


# ── stamping + storage ──────────────────────────────────────────────────────


def test_stamp_off_clears_the_key(monkeypatch):
    monkeypatch.setenv("LANA_GOOGLE_RECO_CARDS", "0")
    ctx = {"google_place_suggestions": [_row("a")], g.CTX_KEY: ["stale"]}
    g.stamp_google_cards(ctx, ask="x", chips=[], lang="en", origin=None, category=None)
    assert ctx[g.CTX_KEY] is None


def test_stamp_survives_a_build_failure(monkeypatch):
    monkeypatch.setattr(g, "build_cards", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    ctx = {"google_place_suggestions": [_row("a")]}
    g.stamp_google_cards(ctx, ask="x", chips=[], lang="en", origin=None, category=None)
    assert ctx[g.CTX_KEY] is None


def test_hold_out_of_storage_removes_cards_from_every_context():
    merged, session = {g.CTX_KEY: ["cards"], "keep": 1}, {g.CTX_KEY: ["cards"]}
    assert g.hold_out_of_storage(merged, session) == ["cards"]
    assert g.CTX_KEY not in merged and g.CTX_KEY not in session and merged["keep"] == 1


def test_fallback_wrapper_stamps_cards_without_the_recommender_chip(monkeypatch):
    import app.discovery_route as dr

    seen: dict[str, Any] = {}

    def core(**kw):
        kw["ctx"]["google_place_suggestions"] = [_row("a")]
        return "reply"

    monkeypatch.setattr(dr, "_tip_seek_fallback_core", core)
    monkeypatch.setattr("app.places._centroid", lambda *a: (28.4, -81.25))
    monkeypatch.setattr(g, "stamp_google_cards", lambda ctx, **kw: seen.update(kw))
    monkeypatch.setattr("app.reader_claims.load_reader_claims", lambda uid: [{"id": "c1", "for": uid}])
    ctx = {"ask_draft": {"chips": [
        {"label": "turkish restaurant", "field": "category"},
        {"label": "from a Turkish person", "field": "recommended_by"},
    ]}}
    out = dr._tip_seek_fallback_reply(ctx=ctx, msg="m", detail="turkish places", category=None,
                                      block_id="b", session_ctx={"preferred_lang": "es"}, user_id="u")
    assert out == "reply"
    assert seen["chips"] == ["turkish restaurant"] and seen["lang"] == "es"
    assert seen["origin"] == (28.4, -81.25)
    assert seen["claims"] == [{"id": "c1", "for": "u"}]


def test_response_carries_google_cards_outside_the_peer_gate():
    from app.main import _reco_cards_from_ctx

    ctx = {g.CTX_KEY: [{"title": "Cafe 34", "source": "google", "google": {"rating": 4.6}}]}
    rows = _reco_cards_from_ctx(ctx, g.CTX_KEY)
    assert rows[0].source == "google" and rows[0].google.rating == 4.6


def test_turn_payload_ships_google_cards_on_a_non_peer_turn():
    from app.auth import AuthSession
    from app.main import _onboarding_fields

    ctx = {"routing_phase": "listening",
           g.CTX_KEY: [{"title": "Cafe 34 Istanbul", "source": "google"}]}
    payload = _onboarding_fields(ctx, AuthSession(user_id="u", is_anonymous=False,
                                                  phone_verified=True, home_block_id="b"))
    assert [c.title for c in payload["google_reco_cards"]] == ["Cafe 34 Istanbul"]
    assert payload["reco_cards"] == []  # the neighbour surface stays gated



# ── For you: the reader's claims ─────────────────────────────────────────────

CLAIMS = [
    {"id": "c1", "label": "Eats out with their kids", "bucket": "stage", "sayable": True, "about": "self"},
    {"id": "c2", "label": "Muslim", "bucket": "faith", "sayable": False, "about": "self"},
]
FAMILY = [{"text": "So patient with our two toddlers, and they have a kids menu.",
           "author": "Maria G.", "author_url": "https://maps.google.com/u/3"}]


def test_for_you_needs_a_real_claim_and_a_verbatim_quote():
    quotes = lambda raw: g._ground_quotes(raw, FAMILY)  # noqa: E731
    good = {"claim": "c1", "line": "You've mentioned your kids — reviewers say it's great for families.",
            "quotes": [{"review": 1, "excerpt": "So patient with our two toddlers"}]}
    assert g.ground_for_you([good], CLAIMS, quotes)[0]["claim_label"] == "Eats out with their kids"
    invented = {**good, "quotes": [{"review": 1, "excerpt": "best place for kids in Orlando"}]}
    not_theirs = {**good, "claim": "c9"}
    no_quote = {**good, "quotes": []}
    assert g.ground_for_you([invented, not_theirs, no_quote], CLAIMS, quotes) == []


def test_a_quiet_claim_orders_but_is_never_named():
    halal = [{"text": "Everything here is halal and freshly grilled.", "author": "A", "author_url": None}]
    item = {"claim": "c2", "line": "Reviewers note everything here is halal.",
            "quotes": [{"review": 1, "excerpt": "Everything here is halal"}]}
    out = g.ground_for_you([item], CLAIMS, lambda raw: g._ground_quotes(raw, halal))
    assert out and out[0]["claim_label"] is None


def test_places_the_reader_fits_lead_and_nothing_is_dropped(monkeypatch):
    monkeypatch.setattr(g, "place_reviews", lambda pid, **k: _details(pid, reviews=FAMILY))

    def compose(places, **k):
        assert k["claims"] == CLAIMS
        return {"places": [
            {"id": p["place_id"], "fit_line": "x",
             "for_you": [{"claim": "c1", "line": "Great for your kids.",
                          "quotes": [{"review": 1, "excerpt": "So patient with our two toddlers"}]}]
             if p["place_id"] == "d" else []}
            for p in places]}

    monkeypatch.setattr(g, "_compose", compose)
    # The independent relevance check confirms everything it is shown here.
    monkeypatch.setattr("app.reader_claims.judge_for_you", lambda pairs: {p["key"] for p in pairs})
    monkeypatch.setattr(g, "_expand", lambda claims, ask, lang="en": claims)
    cards = g.build_cards([_row("a"), _row("b"), _row("d"), _row("e")], ask="restaurants",
                          chips=[], claims=CLAIMS)
    assert [c["subject_ref"] for c in cards] == ["google:d", "google:a", "google:b"]
    assert cards[0]["for_you"][0]["review_quotes"][0]["author"] == "Maria G."
    assert cards[1]["for_you"] == []


def test_no_claims_is_todays_page(monkeypatch):
    monkeypatch.setattr(g, "place_reviews", lambda pid, **k: _details(pid))
    seen = {}

    def compose(places, **k):
        seen["claims"] = k.get("claims")
        return {}

    monkeypatch.setattr(g, "_compose", compose)
    cards = g.build_cards([_row("a"), _row("b")], ask="x", chips=[])
    assert [c["subject_ref"] for c in cards] == ["google:a", "google:b"]
    assert all(c["for_you"] == [] for c in cards) and not seen["claims"]
    assert "no reader_claims" in g._prompt(False) and "reader_claims" in g._prompt(True)


def test_pool_is_used_and_held_out_of_storage_too():
    ctx = {g.CTX_KEY: ["cards"], g.POOL_KEY: [{"place_id": "a"}]}
    g.hold_out_of_storage(ctx)
    assert g.POOL_KEY not in ctx



def test_the_page_is_composed_one_call_per_place(monkeypatch):
    calls = []
    monkeypatch.setattr(g, "place_reviews", lambda pid, **k: _details(pid))
    monkeypatch.setattr(g, "_compose", lambda places, **k: calls.append([p["place_id"] for p in places]) or {})
    g.build_cards([_row("a"), _row("b"), _row("d")], ask="x", chips=[])
    assert sorted(calls) == [["a"], ["b"], ["d"]]


def test_an_opening_hours_line_proves_a_when_claim_attributed_to_google_maps():
    hours = ["Monday: 5:00 AM – 10:00 PM", "Tuesday: 5:00 AM – 10:00 PM"]
    got = g._ground_for_you_quotes([{"hours": "Monday: 5:00 AM - 10:00 PM"}], [], hours, "https://maps/x")
    assert got == [{"text": hours[0], "author": "Google Maps", "author_url": "https://maps/x"}]
    # Hours Google never gave, or none at all, prove nothing.
    assert g._ground_for_you_quotes([{"hours": "Monday: 4:00 AM - 10:00 PM"}], [], hours, None) == []
    assert g._ground_for_you_quotes([{"hours": "Monday: 5:00 AM - 10:00 PM"}], [], [], None) == []
    assert g._ground_for_you_quotes([{"hours": ""}], [], hours, None) == []


def test_hours_reach_the_for_you_but_never_an_aspect():
    place = {"place_id": "p1", "name": "Track", "reviews": [{"text": "a great track"}],
             "hours": ["Monday: Open 24 hours"], "maps_url": None}
    claims = [{"id": "c1", "label": "Morning Runner", "sayable": True, "by_hours": True}]
    parsed = {"places": [{"id": "p1", "fit_line": "x",
                          "aspects": [{"label": "hours", "headline": "Google says open all day",
                                       "quotes": [{"hours": "Monday: Open 24 hours"}]}],
                          "for_you": [{"claim": "c1", "line": "You run mornings — it's open 24 hours.",
                                       "quotes": [{"hours": "Monday: Open 24 hours"}]}]}]}
    out = g.ground(parsed, [place], claims)["p1"]
    assert out["aspects"] == []
    assert out["for_you"][0]["quotes"][0]["text"] == "Monday: Open 24 hours"


def test_a_claim_is_offered_once_per_card():
    halal = [{"text": "Everything here is halal and freshly grilled.", "author": "A", "author_url": None}]
    quotes = lambda raw: g._ground_quotes(raw, halal)  # noqa: E731
    item = {"claim": "c2", "line": "Reviewers note it's halal.",
            "quotes": [{"review": 1, "excerpt": "Everything here is halal"}]}
    assert len(g.ground_for_you([item, item], CLAIMS, quotes)) == 1


def test_three_places_fitting_the_same_claim_each_say_something_different(monkeypatch):
    reviews = FAMILY + [{"text": "A jazz trio plays on Friday nights.", "author": "B", "author_url": None}]
    monkeypatch.setattr(g, "place_reviews", lambda pid, **k: _details(pid, reviews=reviews))
    guitar = [{"id": "c1", "label": "Eats out with their kids", "bucket": "family", "sayable": True, "about": "child"},
              {"id": "c3", "label": "Plays guitar", "bucket": "hobby", "sayable": True, "about": "self"}]
    kids = {"claim": "c1", "line": "kids", "quotes": [{"review": 1, "excerpt": "So patient with our two toddlers"}]}
    music = {"claim": "c3", "line": "music",
             "quotes": [{"review": 2, "excerpt": "A jazz trio plays on Friday nights"}]}
    monkeypatch.setattr(g, "_compose", lambda places, **k: {"places": [
        {"id": p["place_id"], "fit_line": "x", "for_you": [kids, music]} for p in places]})
    monkeypatch.setattr("app.reader_claims.judge_for_you", lambda pairs: {p["key"] for p in pairs})
    monkeypatch.setattr(g, "_expand", lambda claims, ask, lang="en": claims)
    cards = g.build_cards([_row("a"), _row("b"), _row("d")], ask="steakhouses", chips=[], claims=guitar)
    said = [[f["line"] for f in c["for_you"]] for c in cards]
    assert said == [["Kids."], ["Music."], []]


def test_the_writer_only_sees_claims_the_expansion_kept(monkeypatch):
    monkeypatch.setattr(g, "place_reviews", lambda pid, **k: _details(pid, reviews=FAMILY))
    seen = {}
    monkeypatch.setattr(g, "_compose", lambda places, **k: seen.update(claims=k["claims"]) or {})
    kept = [{**CLAIMS[0], "look_for": "kid-friendly", "because": ""}]
    monkeypatch.setattr(g, "_expand", lambda claims, ask, lang="en": seen.update(ask=ask) or kept)
    g.build_cards([_row("a")], ask="pizza near me", chips=[], claims=CLAIMS)
    assert seen["ask"] == "pizza near me" and seen["claims"] == kept
    # And what the writer is shown of them: the one thing to look for.
    from app.reader_claims import claims_payload

    assert claims_payload(kept)[0]["look_for"] == "kid-friendly"


def test_opening_hours_prove_only_a_claim_the_expansion_marked_by_hours():
    hours = ["Friday: 11:00 AM – 11:00 PM"]
    quotes = lambda raw: g._ground_for_you_quotes(raw, [], hours, None)  # noqa: E731
    item = {"claim": "c1", "line": "You're a night owl — it's open until 11 PM on Fridays.",
            "quotes": [{"hours": "Friday: 11:00 AM - 11:00 PM"}]}
    late = [{"id": "c1", "label": "Usually late", "sayable": True, "by_hours": False}]
    owl = [{"id": "c1", "label": "Night owl", "sayable": True, "by_hours": True}]
    assert g.ground_for_you([item], late, quotes) == []
    assert g.ground_for_you([item], owl, quotes)[0]["quotes"][0]["text"] == hours[0]
