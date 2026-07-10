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
    # Queued-for-launch variants: the capture completed but the swap/tip surface isn't
    # live on the block yet, so the contribution was parked in queued_contributions.
    "item_queued_now",
    "tip_queued_now",
    "look_meet_saved_now",
    # Tip-seek Google fallback cards + their refine chips. Ephemeral per-turn UI: without
    # this a tip turn's "From Google" restaurant cards (google_place_suggestions) and refine
    # chips (rec_chips / rec_widen_noun) survived the {**old, **new} merge and re-rendered on
    # the NEXT, unrelated turn (e.g. a meet save). A real tip turn re-stamps them, so clearing
    # here only drops stale ones. NOTE: rec_filter_asked is intentionally NOT here — it is
    # genuine cross-turn state (remembers we already asked the angle question), not a card.
    "google_place_suggestions",
    "rec_chips",
    "rec_widen_noun",
    # Tap-able answers for a clarify question — only valid on the turn that asked it.
    "clarify_options",
    # Structured goal payload from a tapped suggestion chip (kind + topic) — authoritative
    # for the turn it arrives on, meaningless afterwards. Stamped by main.py from the
    # request body; consumed by the pipeline / goal stack the same turn.
    "tapped_goal",
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
