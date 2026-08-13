"""Every pending flow is classified: carried across a login, or dropped on purpose.

Three flows (event draft, meet seek, signal ask) each got a bespoke cross-account
stash, added one incident at a time. `tip_seek_pending` never got one, so prod
2026-08-06 lost a confirmed "Italian restaurants" ask at the login boundary — the
person had it on screen as "YOUR ASK", logged in, and was greeted with "how can I
help you today?".

Listing flows by hand guarantees the next one repeats it. So the test below scans
the app for pending-state keys and fails on any that app/login_carry.py has not
classified. Adding a flow forces a choice; forgetting defaults to a FAILURE rather
than to silent data loss.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.login_carry import (
    LOGIN_CARRY_EXCLUDED,
    LOGIN_CARRY_KEYS,
    collect,
    describe,
)

APP = Path(__file__).resolve().parents[1] / "app"

# session_ctx["…pending…"] = / ctx_base["…pending…"] = — where a flow parks state.
_ASSIGN = re.compile(
    r"""(?:session_ctx|ctx_base|ctx)\[["']([a-z_]*pending[a-z_]*)["']\]\s*=""",
)


def _discovered_keys() -> set[str]:
    found: set[str] = set()
    for path in APP.rglob("*.py"):
        found.update(_ASSIGN.findall(path.read_text()))
    return found


def test_every_pending_key_is_classified() -> None:
    classified = set(LOGIN_CARRY_KEYS) | set(LOGIN_CARRY_EXCLUDED)
    unclassified = sorted(_discovered_keys() - classified)
    assert not unclassified, (
        "These pending keys are neither carried across a login nor explicitly "
        "excluded, so they are silently lost when a guest logs into an existing "
        f"account: {unclassified}. Add each to LOGIN_CARRY_KEYS (real work the "
        "person started) or to LOGIN_CARRY_EXCLUDED with the reason (turn-scoped "
        "state that must not be replayed) in app/login_carry.py."
    )


def test_the_reported_bug_key_is_carried() -> None:
    assert "tip_seek_pending" in LOGIN_CARRY_KEYS


def test_carry_and_exclude_do_not_overlap() -> None:
    both = set(LOGIN_CARRY_KEYS) & set(LOGIN_CARRY_EXCLUDED)
    assert not both, f"a key cannot be both carried and dropped: {sorted(both)}"


def test_every_exclusion_states_a_reason() -> None:
    for key, reason in LOGIN_CARRY_EXCLUDED.items():
        assert len(reason.strip()) > 10, f"{key} is excluded without a real reason"


def test_collect_takes_only_real_work() -> None:
    ctx = {
        "tip_seek_pending": {"detail": "Italian restaurants"},
        "clarify_pending": {"options": ["a", "b"]},   # excluded
        "ask_draft_pending": None,                     # nothing in flight
        "community_join_pending": {},                  # empty is nothing
        "unrelated": "x",
    }
    assert collect(ctx) == {"tip_seek_pending": {"detail": "Italian restaurants"}}


def test_collect_survives_junk() -> None:
    assert collect(None) == {}
    assert collect({}) == {}


def test_describe_uses_their_own_words() -> None:
    """The greeting should say "Italian restaurants", never "your pending request"."""
    assert describe({"tip_seek_pending": {"detail": "Italian restaurants"}}) == (
        "Italian restaurants"
    )
    assert describe({"community_join_pending": {"title": "Lp Fit"}}) == "Lp Fit"
    assert describe({"tip_seek_pending": {}}) is None
    assert describe({}) is None
    assert describe(None) is None
