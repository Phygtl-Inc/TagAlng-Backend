"""app/reco_kind_gate.py — neighbour recos must be the kind of thing asked for."""

import json
from unittest import mock

import app.reco_kind_gate as kg


def _tip(cat, sig):
    return {"signal_id": sig, "category": cat}


def setup_function(_):
    kg._cache.clear()


def test_a_barbershop_is_not_a_restaurant_even_when_tagged_kid_friendly():
    tips = [_tip("barbershop", "b"), _tip("italian restaurant", "r"), _tip(None, "n")]
    with mock.patch("app.orchestrator.llm.llm_configured", return_value=True), \
            mock.patch("app.orchestrator.llm.llm_json",
                       return_value={"fits": {"barbershop": False, "italian restaurant": True}}):
        kept = kg.keep_asked_kind(tips, "restaurant")
    # The uncategorised row stays: the gate only drops what the model says is not the kind.
    assert [t["signal_id"] for t in kept] == ["r", "n"]


def test_a_furniture_store_does_not_answer_a_gaming_laptop_ask():
    tips = [_tip("furniture store", "f"), _tip("electronics store", "e")]
    with mock.patch("app.orchestrator.llm.llm_configured", return_value=True), \
            mock.patch("app.orchestrator.llm.llm_json",
                       return_value={"fits": {"furniture store": False,
                                              "electronics store": True}}) as llm:
        kept = kg.keep_asked_kind(tips, "gaming laptop")
    assert [t["signal_id"] for t in kept] == ["e"]
    # The prompt has to cover things you buy, not only places: an electronics store is not
    # itself "a gaming laptop", and a place-only prompt would reject the right answer too.
    assert "where you would get it" in llm.call_args.kwargs["system"]


def test_a_yes_is_cached_per_kind_and_category():
    calls = []

    def llm(**kw):
        calls.append(kw)
        return {"fits": {"trattoria": "yes"}}

    with mock.patch("app.orchestrator.llm.llm_configured", return_value=True), \
            mock.patch("app.orchestrator.llm.llm_json", side_effect=llm):
        kg.keep_asked_kind([_tip("trattoria", "t")], "restaurant")
        kg.keep_asked_kind([_tip("trattoria", "t")], "restaurant")
    assert len(calls) == 1


def test_fails_open_and_does_nothing_without_a_kind():
    tips = [_tip("barbershop", "b")]
    assert kg.keep_asked_kind(tips, None) is tips
    with mock.patch("app.orchestrator.llm.llm_configured", return_value=True), \
            mock.patch("app.orchestrator.llm.llm_json", side_effect=RuntimeError("down")):
        assert kg.keep_asked_kind(tips, "restaurant") == tips


def test_the_whole_request_reaches_the_model_beside_an_over_trimmed_kind():
    # Prod 2026-10-07: "beginner-friendly project programs" was parsed to kind "programs",
    # and the gate judged a "project program" tip not to be one. The request must be read.
    ask = "any beginner-friendly project programs at San Jose State I can get involved in?"
    with mock.patch("app.orchestrator.llm.llm_configured", return_value=True), \
            mock.patch("app.orchestrator.llm.llm_json",
                       return_value={"fits": {"project program": "yes"}}) as llm:
        kept = kg.keep_asked_kind([_tip("project program", "p")], "programs", ask)
    assert [t["signal_id"] for t in kept] == ["p"]
    payload = json.loads(llm.call_args.kwargs["user_payload"])
    assert payload["request"] == ask and payload["kind"] == "programs"


def test_only_a_sure_no_drops_a_row():
    tips = [_tip("barbershop", "b"), _tip("student club space", "s"), _tip("cafe", "c")]
    with mock.patch("app.orchestrator.llm.llm_configured", return_value=True), \
            mock.patch("app.orchestrator.llm.llm_json",
                       return_value={"fits": {"barbershop": "no",
                                              "student club space": "unsure",
                                              "cafe": "yes"}}):
        kept = kg.keep_asked_kind(tips, "study space", "somewhere to study on campus")
    assert [t["signal_id"] for t in kept] == ["s", "c"]


def test_a_no_is_never_cached_so_one_bad_call_cannot_hide_a_tip_for_good():
    answers = iter([{"fits": {"project program": "no"}},
                    {"fits": {"project program": "yes"}}])
    with mock.patch("app.orchestrator.llm.llm_configured", return_value=True), \
            mock.patch("app.orchestrator.llm.llm_json",
                       side_effect=lambda **_k: next(answers)) as llm:
        first = kg.keep_asked_kind([_tip("project program", "p")], "programs")
        second = kg.keep_asked_kind([_tip("project program", "p")], "programs")
    assert first == [] and [t["signal_id"] for t in second] == ["p"]
    assert llm.call_count == 2


def test_what_was_recommended_is_shown_beside_its_category():
    # "a beginner friendly club for project experience" vs category "project program": the
    # category alone hides that the thing recommended IS a club's program.
    tip = {"signal_id": "p", "category": "project program",
           "reco_name": "Responsible computing club consulting project program"}
    with mock.patch("app.orchestrator.llm.llm_configured", return_value=True), \
            mock.patch("app.orchestrator.llm.llm_json",
                       return_value={"fits": {"project program": "yes"}}) as llm:
        kg.keep_asked_kind([tip], "club", "a beginner friendly club for project experience")
    payload = json.loads(llm.call_args.kwargs["user_payload"])
    assert payload["names"] == {
        "project program": ["responsible computing club consulting project program"]}
