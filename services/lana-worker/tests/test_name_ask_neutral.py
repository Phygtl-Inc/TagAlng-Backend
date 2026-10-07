"""The name ask is not about "neighbors" — Lana serves communities, not just blocks."""

from __future__ import annotations

import pathlib

import app.discovery_route as dr
import app.profile_intake as pi
from app.lana_dispatch import LANA_UNIFIED_OPENING_NEEDS_NAME


def test_opening_name_ask_is_neutral() -> None:
    assert "neighbor" not in LANA_UNIFIED_OPENING_NEEDS_NAME.lower()
    assert "call you" in LANA_UNIFIED_OPENING_NEEDS_NAME


def test_no_canned_name_ask_says_neighbors() -> None:
    for mod in (dr, pi):
        src = pathlib.Path(mod.__file__).read_text()
        # The canned asks end in "?"; comments and docstrings quoting old prod copy do not.
        code = [ln for ln in src.splitlines() if not ln.strip().startswith("#")]
        assert not [ln for ln in code if "should neighbors call you?" in ln.lower()], mod.__name__
