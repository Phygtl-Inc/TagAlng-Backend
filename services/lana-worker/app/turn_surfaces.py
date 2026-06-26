"""Ephemeral per-turn UI payloads — must not persist across unrelated Lana turns."""

from __future__ import annotations

from typing import Any

# Cleared at the start of each routed turn unless the handler re-stamps them.
TURN_SCOPED_SURFACES = frozenset({
    "block_log_entries",
    "signal_saved",
    "identity_profile",
    "pending_intros",
    "recent_intro_duplicate",
    "event_published_now",
    "item_listed_now",
    "tip_listed_now",
    "look_meet_saved_now",
    # Tap-able answers for a clarify question — only valid on the turn that asked it.
    "clarify_options",
    # Per-turn signal to main.py's background claim extractor. Must NOT persist: a
    # prior peer-discovery turn (e.g. ZIP entry) set it True, and {**old, **new} merge
    # leaked that True into the identity turn — suppressing the claim-save so the user's
    # self-description never persisted. Re-stamped each turn by handlers that need it.
    "skip_claims_background_extract",
})


def clear_turn_surfaces(ctx: dict[str, Any]) -> None:
    """Mark turn surfaces absent so session merge drops stale cards."""
    for key in TURN_SCOPED_SURFACES:
        ctx[key] = None
