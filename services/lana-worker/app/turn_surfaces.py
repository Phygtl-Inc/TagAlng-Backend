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
    # "I just introduced you to X" — the intro card owns the turn it was created on and
    # nothing after it. derive_ui_intent checks this FIRST, ahead of hosting and every
    # other surface, so leaving it set stranded the whole session: once Lana proposed an
    # intro, every later turn returned propose_neighbor_intro and the host setup carousel
    # (and every other card) silently never rendered. The persistent `intro_proposal`
    # payload is deliberately NOT here — later turns still read it to avoid re-proposing.
    "intro_proposed_now",
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
    # The "which spot is it?" card. Owns the turn that asked; the ANSWER turn must not
    # re-render it (the pending state in rapport_grounding is what survives, not the UI).
    "grounding_card",
    # decide_turn policy chips — one-turn CTAs authored by the policy call.
    # (policy_chip_msgs is NOT here: the next turn reads it to recognize a tap
    # on the policy's own chip, then the next policy turn re-stamps it.)
    "policy_chips",
    # Empty peers-search offer pills ("Yes, notify me" / "Show everyone nearby").
    # (peer_seek_offer_pending is NOT here: the next turn reads it to interpret
    # the tap, then clears it with None — same split as policy_chip_msgs.)
    "peer_seek_offer",
    # Recommendation-ask offer pills ("Yes, ask my neighbors" / "No, just the list") and the
    # post-posting manage pills ("Take it down"). Same split as above: the *_pending twins
    # are NOT here, because the next turn reads them to interpret the answer and then
    # clears them with None.
    "tip_ask_offer",
    "posting_manage",
    # The seek-side ask card ("Gentle pediatric dentist" + its chips). Same split again:
    # ask_draft_pending is NOT here, because the next turn reads it to recognize
    # "Looks good" / "Let me tweak that" and then clears it with None.
    "ask_draft",
    # Per-turn signal to main.py's background claim extractor. Must NOT persist: a
    # prior peer-discovery turn (e.g. ZIP entry) set it True, and {**old, **new} merge
    # leaked that True into the identity turn — suppressing the claim-save so the user's
    # self-description never persisted. Re-stamped each turn by handlers that need it.
    "skip_claims_background_extract",
    # The look screen's "YOUR COMMUNITIES" card. Stamped only by the looking-open turn
    # (activity_browse's P1 ask); without this the {**old, **new} merge would keep
    # re-rendering it under every later reply in the session.
    "communities_card",
    # Nearby joinable communities listed by a discovery.communities turn — the cards
    # belong to the turn that answered the ask, not to every later reply.
    "community_discovery",
    # A one-shot Supabase instruction the FE executes (send/verify an OTP, logout).
    # Without this a login turn's send_login_otp survived the merge and kept
    # re-announcing an auth stage on unrelated later turns.
    "auth_action",
})


def clear_turn_surfaces(ctx: dict[str, Any]) -> None:
    """Mark turn surfaces absent so session merge drops stale cards.

    Records what it actually took away under `_wiped_turn_surfaces`. Lanes that stamp a
    turn surface and then build their outgoing ctx through this have to re-attach it by
    name, and forgetting one is silent: the "did you mean?" clarifier shipped its question
    with its answer buttons nulled a line after they were set (2026-08-21). The list lets
    the serialization boundary say so out loud — see main._warn_surface_dropped.
    """
    wiped = [k for k in TURN_SCOPED_SURFACES if ctx.get(k)]
    for key in TURN_SCOPED_SURFACES:
        ctx[key] = None
    # Not itself a surface, and re-stamped on every call, so it never goes stale.
    ctx["_wiped_turn_surfaces"] = wiped
