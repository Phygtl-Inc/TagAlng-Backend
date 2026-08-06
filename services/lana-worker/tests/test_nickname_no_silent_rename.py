"""A name on file is never silently replaced.

Regression cases are the real transcript of user db98744e (dev), whose name went
Tommaso -> Orlando -> Not across three turns without ever being asked:

  1. "Orlando" answering "which Lagoinha location?" -> extractor called it a
     nickname, and the write had no guard, so it replaced "Tommaso".
  2. "My name is not Orlando but Tom" -> the intro pattern captured the
     NEGATION, storing "Not", while the reply said "Tom".

Names are meaning, not format: negation, contrast, and antecedent resolution are
needed to reach "Tom", so the extractor owns renames and the pattern match is a
fill-only fallback.
"""

from __future__ import annotations

import pytest

from app import claims_persist
from app.claims_persist import (
    extract_nickname_from_message,
    nickname_rename_is_stated,
)


# --- the fallback pattern match may never yield a negation ------------------


@pytest.mark.parametrize(
    "message",
    [
        "My name is not Orlando but Tom",
        "my name is not Orlando",
        "I am Not",
        "my name's not Joe",
    ],
)
def test_fallback_never_captures_a_negation_as_the_name(message: str) -> None:
    """"My name is not X" is a CORRECTION. The pattern can only see slot 2, so it
    must decline rather than store "Not" — the extractor resolves these."""
    assert extract_nickname_from_message(message) != "Not"


def test_fallback_still_reads_a_plain_introduction() -> None:
    assert extract_nickname_from_message("my name is Tommaso") == "Tommaso"
    assert extract_nickname_from_message("call me Sam") == "Sam"


# --- the rename gate -------------------------------------------------------


def test_rename_needs_all_three_signals() -> None:
    data = {"nickname_is_rename": True, "nickname_quote": "but Tom"}
    assert nickname_rename_is_stated(data, "My name is not Orlando but Tom", "Orlando")


def test_no_rename_without_a_saved_name() -> None:
    """With nothing on file this is a first fill, not a rename."""
    data = {"nickname_is_rename": True, "nickname_quote": "but Tom"}
    assert not nickname_rename_is_stated(data, "My name is not Orlando but Tom", None)


def test_no_rename_when_the_model_did_not_claim_one() -> None:
    """The "Orlando" turn: a name-shaped word the user never asked to be called."""
    data = {"nickname_is_rename": False, "nickname_quote": "Orlando"}
    assert not nickname_rename_is_stated(data, "Orlando", "Tommaso")


def test_no_rename_when_the_quote_is_absent_from_the_message() -> None:
    """A quote that isn't in the message means the name was inferred, not stated."""
    data = {"nickname_is_rename": True, "nickname_quote": "call me Orlando"}
    assert not nickname_rename_is_stated(data, "Lagoinha in Orlando", "Tommaso")


def test_no_rename_without_a_quote() -> None:
    for quote in (None, "", "x"):
        data = {"nickname_is_rename": True, "nickname_quote": quote}
        assert not nickname_rename_is_stated(data, "anything at all", "Tommaso")


def test_rename_gate_survives_junk_extractor_output() -> None:
    assert not nickname_rename_is_stated(None, "hi", "Tommaso")
    assert not nickname_rename_is_stated("not a dict", "hi", "Tommaso")


# --- the write itself is fill-only ----------------------------------------


class _FakeTable:
    def __init__(self, sink: dict[str, object]) -> None:
        self.sink = sink

    def update(self, row: dict[str, object]) -> "_FakeTable":
        self.sink["updated"] = row
        return self

    def eq(self, *_a: object, **_k: object) -> "_FakeTable":
        return self

    def execute(self) -> None:
        return None


def _patch_write(monkeypatch: pytest.MonkeyPatch, saved: str | None) -> dict[str, object]:
    sink: dict[str, object] = {}
    monkeypatch.setattr(claims_persist, "current_nickname", lambda _uid: saved)
    monkeypatch.setattr(
        claims_persist,
        "service_client",
        lambda: type("_C", (), {"table": lambda _s, _n: _FakeTable(sink)})(),
    )
    return sink


def test_patch_refuses_to_overwrite_a_saved_name(monkeypatch: pytest.MonkeyPatch) -> None:
    sink = _patch_write(monkeypatch, "Tommaso")
    claims_persist.persist_profile_patch("u1", {"nickname": "Orlando"})
    assert "updated" not in sink, "a saved name must not be replaced without allow_rename"


def test_patch_fills_an_empty_name(monkeypatch: pytest.MonkeyPatch) -> None:
    sink = _patch_write(monkeypatch, None)
    claims_persist.persist_profile_patch("u1", {"nickname": "Tommaso"})
    assert sink["updated"] == {"nickname": "Tommaso"}


def test_patch_allows_a_vouched_rename(monkeypatch: pytest.MonkeyPatch) -> None:
    sink = _patch_write(monkeypatch, "Orlando")
    claims_persist.persist_profile_patch("u1", {"nickname": "Tom"}, allow_rename=True)
    assert sink["updated"] == {"nickname": "Tom"}


def test_fallback_persist_is_inert_once_a_name_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    """main.py runs this on EVERY turn — it must never be able to rename."""
    sink = _patch_write(monkeypatch, "Tommaso")
    assert claims_persist.persist_nickname_if_stated("u1", "call me Orlando") is None
    assert "updated" not in sink


# --- the reply must say a rename happened --------------------------------


def test_name_change_signal_only_fires_on_a_real_change() -> None:
    from app.policy.decide import name_change_signal

    assert name_change_signal({}) is None
    assert name_change_signal({"nickname_changed": None}) is None
    assert name_change_signal({"nickname_changed": {"from": "Orlando", "to": ""}}) is None
    assert name_change_signal({"nickname_changed": {"from": "Orlando", "to": "Tom"}}) == {
        "from": "Orlando",
        "to": "Tom",
    }


def test_full_name_is_not_gated_by_the_nickname_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the spoken name is protected; full_name is a separate field."""
    sink = _patch_write(monkeypatch, "Tommaso")
    claims_persist.persist_profile_patch("u1", {"nickname": "Orlando", "full_name": "T. Rossi"})
    assert sink["updated"] == {"full_name": "T. Rossi"}
