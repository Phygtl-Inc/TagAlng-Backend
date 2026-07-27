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
    # decide_turn policy chips — one-turn CTAs authored by the policy call.
    # (policy_chip_msgs is NOT here: the next turn reads it to recognize a tap
    # on the policy's own chip, then the next policy turn re-stamps it.)
    "policy_chips",
    # Per-turn signal to main.py's background claim extractor. Must NOT persist: a
    # prior peer-discovery turn (e.g. ZIP entry) set it True, and {**old, **new} merge
    # leaked that True into the identity turn — suppressing the claim-save so the user's
    # self-description never persisted. Re-stamped each turn by handlers that need it.
    "skip_claims_background_extract",
    # A one-shot Supabase instruction the FE executes (send/verify an OTP, logout).
    # Without this a login turn's send_login_otp survived the merge and kept
    # re-announcing an auth stage on unrelated later turns.
    "auth_action",
})


def clear_turn_surfaces(ctx: dict[str, Any]) -> None:
    """Mark turn surfaces absent so session merge drops stale cards."""
    for key in TURN_SCOPED_SURFACES:
        ctx[key] = None
