"""app/reco_kind_gate.py — neighbour recos must be the kind of thing asked for."""

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


def test_answers_are_cached_per_kind_and_category():
    calls = []

    def llm(**kw):
        calls.append(kw)
        return {"fits": {"barbershop": False}}

    with mock.patch("app.orchestrator.llm.llm_configured", return_value=True), \
            mock.patch("app.orchestrator.llm.llm_json", side_effect=llm):
        kg.keep_asked_kind([_tip("barbershop", "b")], "restaurant")
        kg.keep_asked_kind([_tip("barbershop", "b")], "restaurant")
    assert len(calls) == 1


def test_fails_open_and_does_nothing_without_a_kind():
    tips = [_tip("barbershop", "b")]
    assert kg.keep_asked_kind(tips, None) is tips
    with mock.patch("app.orchestrator.llm.llm_configured", return_value=True), \
            mock.patch("app.orchestrator.llm.llm_json", side_effect=RuntimeError("down")):
        assert kg.keep_asked_kind(tips, "restaurant") == tips
