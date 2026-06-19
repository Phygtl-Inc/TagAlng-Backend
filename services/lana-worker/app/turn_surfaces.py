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
})


def clear_turn_surfaces(ctx: dict[str, Any]) -> None:
    """Mark turn surfaces absent so session merge drops stale cards."""
    for key in TURN_SCOPED_SURFACES:
        ctx[key] = None
