"""What survives a guest → existing-account login.

Logging into an account the user already has swaps the JWT and forces a new
session, so anything the guest had in flight is dropped unless it was stashed
against the destination account first. Three flows were stashed by hand (event
draft, meet seek, signal ask) and every other pending key was silently lost —
prod 2026-08-06: a guest asked for Italian restaurants, confirmed it with a chip,
saw it on screen as "YOUR ASK: Italian restaurants", logged in, and was greeted
with "how can I help you today?". `tip_seek_pending` had no stash.

Enumerating flows one at a time guarantees the next one breaks the same way, so
the decision lives here instead: every pending key in the codebase must be listed
as CARRY or EXCLUDED, and tests/test_login_carry_covers_pending_keys.py fails on
any key that is neither. Adding a flow therefore forces the choice rather than
defaulting to data loss.
"""

from __future__ import annotations

from typing import Any

# Work the person actually started. Losing it makes them repeat themselves, so it
# rides across the login.
LOGIN_CARRY_KEYS: tuple[str, ...] = (
    # A recommendation ask parked at the verify gate. The post-verify path already
    # ANSWERS this (discovery_route: "the moment they're verified, ANSWER it") —
    # it just never survived the session reset.
    "tip_seek_pending",
    "tip_pending_ask",
    "tip_pending_question",
    # A looking/offering ask captured mid-flow.
    "look_pending_ask",
    "pass_along_pending_ask",
    # A drafted ask awaiting their go-ahead.
    "ask_draft_pending",
    # They chose a community to join and then had to verify.
    "community_join_pending",
    # A community they were CREATING when the login interrupted. Real work — the place is
    # pinned and Lana's questions are half-answered — and the draft itself
    # (community_draft) rides in the session context, so dropping only the pending ask
    # would strand it mid-set with nothing marking which step was open.
    "community_pending_ask",
    "community_pending_question",
)

# Deliberately dropped, with the reason. These describe the STATE OF A TURN, not
# work: replaying them onto a fresh session with a different identity re-arms an
# offer for a conversation that no longer exists, or gates a person who has
# already passed the gate.
LOGIN_CARRY_EXCLUDED: dict[str, str] = {
    "look_seek_pending": "own cross-account stash (stash_pending_meet_seek)",
    "signal_pending": "own cross-account stash (stash_pending_signal_ask)",
    "clarify_pending": "a clarifier for a question asked in the old session",
    "browse_or_meet_pending": "same — a clarifier tied to the previous turn",
    "lang_nudge_pending": "language is re-derived per session from users.locale",
    "out_of_scope_pending": "a decline for a request already answered",
    "peer_seek_offer_pending": "offer pills for a search the new session has not run",
    "rapport_offer_pending": "rapport offers are re-queued from the account's own gaps",
    "rapport_pending_action": "same — belongs to the guest's tile, not this account",
    "tip_ask_offer_pending": "consent prompt for a post; must be re-asked, never assumed",
    "tip_tweak_pending": "a refinement of results the new session has not shown",
    "posting_manage_pending": "acts on the guest's postings, not this account's",
    "pending_lane_switch": "turn-scoped routing state",
    "pending_signup_gate": "the login just satisfied it",
    "pending_zip": "re-derived from the account's own home_zip",
    "host_publish_pending": "own cross-account stash (stash_pending_event_draft)",
    "pending_cohost_id": "belongs to the host draft, carried by its own stash",
    "pending_cohost_invite_id": "same — part of the host draft, not standalone work",
    "pending_post_verify": "the flag means 'mid verification'; the login satisfied it",
    "pending_confirmation": "a yes/no for a question the old session asked; must be re-asked",
    "pending_heritage_change": (
        "a heritage-conflict confirmation. Replaying it would rewrite identity from a "
        "yes given in another session — this one must always be re-asked"
    ),
    "pending_hosting_offer": "an offer made in the guest conversation, not this one",
    "pending_intro_offer": "intros belong to the account's own peers, never a guest's",
    "pending_intro_respond": "same — responds to an intro this account may not have",
    "pending_intros": "a list read fresh from this account's own intros",
}


def collect(session_ctx: dict[str, Any]) -> dict[str, Any]:
    """The carry-worthy slice of a guest's context — only keys with real values."""
    if not isinstance(session_ctx, dict):
        return {}
    out: dict[str, Any] = {}
    for key in LOGIN_CARRY_KEYS:
        val = session_ctx.get(key)
        if val:  # None / {} / "" is nothing in flight
            out[key] = val
    return out


def describe(carry: dict[str, Any]) -> str | None:
    """A short human phrase for what is waiting, for the greeting that resumes it.

    Prefers the person's own words (the ask detail) over a key name, so the
    greeting can say "Italian restaurants" rather than "your pending request".
    """
    if not isinstance(carry, dict):
        return None
    for key in LOGIN_CARRY_KEYS:
        val = carry.get(key)
        if not isinstance(val, dict):
            continue
        for field in ("detail", "detail_text", "question", "label", "title"):
            text = str(val.get(field) or "").strip()
            if text:
                return text[:120]
    return None
