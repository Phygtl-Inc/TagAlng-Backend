"""Write-only verification gates — single source of truth for WHEN Lana may ask a
mom to verify her email, and WHAT she says when she does.

QA (2026-07-08): the bare "Verify your email first — …" imperative ended 17% of all
turns (40/233) — it fired on search refinements, on a dentist question, right after
an insult, and in sessions where NO block was even resolved. The RSVP gate in the
web UI converts because it is benefit-framed at a real commitment moment. Policy:

1. Only WRITE actions may gate (`WRITE_ACTIONS` allowlist). Read / browse / refine /
   explain turns NEVER demand verification — previews redact instead.
2. A block-scoped gate must not fire when the session has no resolved block. Those
   turns route to the normal no-coverage / ask-ZIP path; the copy here references
   "your block", which is a lie until a block exists.
3. Copy is benefit-framed per action: lead with what Lana will do for her, then the
   email ask — never a bare imperative.

Every call site must route through `gate_reply()` (or `gate_copy()` for gates
enforced reactively by the backend, e.g. an RPC returning `phone_not_verified`).
`tests/test_gates.py` greps `app/` to keep the old copy from creeping back in.
"""

from __future__ import annotations

from typing import Any

from app.analytics import track

# Actions that WRITE on the user's behalf — the only actions allowed to gate.
WRITE_ACTIONS = frozenset(
    {
        "rsvp",  # commit a spot at a neighbor's gathering
        "publish_event",  # post a gathering to the block
        "send_intro",  # send a nudge / intro request to a neighbor
        "save_signal",  # post an ask/offer (local signal) to the block log
        "profile_photo_save",  # persist a profile photo to the account
    }
)

# Write actions whose outcome (and copy) lives on a block — these additionally
# require a resolved block before they may gate (rule 2 above).
BLOCK_SCOPED_ACTIONS = frozenset({"rsvp", "publish_event", "save_signal"})

# Benefit-framed copy per action: what Lana will DO, then the email ask.
GATE_COPY: dict[str, str] = {
    "rsvp": (
        "I can hold your spot and let the host know you're coming — I just need "
        "your email so she knows you're a real neighbor. Ok?"
    ),
    "publish_event": (
        "I can post it so neighbors on your block can see it and RSVP — I just "
        "need your email first so their replies reach you. Ok?"
    ),
    "send_intro": (
        "Quick email check so she knows you're a real neighbor — then I'll send "
        "your note."
    ),
    "save_signal": (
        "I can hold that ask and ping you when a neighbor matches — I just need "
        "your email so I can reach you. Ok?"
    ),
    "profile_photo_save": (
        "I can put that on your profile so neighbors recognize you — I just need "
        "your email first so the photo sticks to your account. Ok?"
    ),
}


def block_resolved(session_ctx: dict[str, Any], home_block_id: str | None = None) -> bool:
    """True when this session has a concrete block to write to."""
    if home_block_id:
        return True
    ctx = session_ctx or {}
    return bool(ctx.get("preview_block_id") or ctx.get("home_block_id"))


def _is_verified(session_ctx: dict[str, Any], verified: bool | None) -> bool:
    if verified is not None:
        return bool(verified)
    return bool((session_ctx or {}).get("phone_verified"))


def needs_verification(
    action: str,
    session_ctx: dict[str, Any],
    *,
    verified: bool | None = None,
    home_block_id: str | None = None,
) -> bool:
    """Should this turn stop and ask for email verification?

    False for anything outside the WRITE_ACTIONS allowlist, for verified users,
    and for block-scoped writes when no block is resolved (those turns must take
    the no-coverage / ask-ZIP path instead of a gate that references "your block").
    """
    if action not in WRITE_ACTIONS:
        return False
    if _is_verified(session_ctx, verified):
        return False
    if action in BLOCK_SCOPED_ACTIONS and not block_resolved(session_ctx, home_block_id):
        return False
    return True


def gate_copy(action: str) -> str:
    """Benefit-framed gate copy for a write action (KeyError on non-write actions —
    read actions have no gate copy on purpose)."""
    return GATE_COPY[action]


def gate_shown(action: str, user_id: str | None = None) -> None:
    """Analytics: a verification gate was shown for `action` (fire-and-forget)."""
    track("gate_shown", user_id=user_id, event_properties={"action": action})


def gate_passed(action: str, user_id: str | None = None) -> None:
    """Analytics: a write action proceeded past the gate check (fire-and-forget)."""
    track("gate_passed", user_id=user_id, event_properties={"action": action})


def gate_reply(
    action: str,
    session_ctx: dict[str, Any],
    *,
    verified: bool | None = None,
    home_block_id: str | None = None,
    user_id: str | None = None,
) -> str | None:
    """The gate reply to send for this action, or None when the turn may proceed.

    Emits `gate_shown {action}` when the gate fires and `gate_passed {action}` when
    a verified user clears a write-action check. Read actions always return None.
    """
    if action not in WRITE_ACTIONS:
        return None
    if needs_verification(action, session_ctx, verified=verified, home_block_id=home_block_id):
        gate_shown(action, user_id)
        return GATE_COPY[action]
    if _is_verified(session_ctx, verified):
        gate_passed(action, user_id)
    return None
