"""C-4-EVENT-P3 bubble CTAs — open meetup vs invite neighbors (distinct turns)."""

from __future__ import annotations

from typing import Any

from app.local_signals import (
    fetch_my_block_log,
    filter_block_log_for_signal,
    normalize_block_log_row,
    refresh_my_signal_matches,
    stamp_signal_saved_ctx,
)
from app.reply_compose import compose_reply

_HOSTING_OPEN_MESSAGES = frozenset({
    "open the meet up",
    "open the meetup",
})
_HOSTING_SEND_MOM_MESSAGES = frozenset({
    # Legacy payload — chips rendered before the 2026-07 lexicon scrub still post it.
    "send to a mom",
    # Current label AND payload ("Send to a parent") post this form.
    "send to a parent",
})


def is_hosting_open_cta(msg: str) -> bool:
    return str(msg or "").strip().lower() in _HOSTING_OPEN_MESSAGES


def is_hosting_send_mom_cta(msg: str) -> bool:
    return str(msg or "").strip().lower() in _HOSTING_SEND_MOM_MESSAGES


def is_hosting_ui_cta(msg: str) -> bool:
    return is_hosting_open_cta(msg) or is_hosting_send_mom_cta(msg)


def session_has_hosting_offer(session_ctx: dict[str, Any]) -> bool:
    saved = session_ctx.get("signal_saved")
    if isinstance(saved, dict) and str(saved.get("intent") or "") == "host_meet":
        return True
    if str(session_ctx.get("active_intent") or "") == "sharing.host":
        pending = session_ctx.get("pending_hosting_offer")
        return isinstance(pending, dict) and bool(pending.get("detail_text"))
    return False


def _hosting_detail_from_session(session_ctx: dict[str, Any]) -> str:
    saved = session_ctx.get("signal_saved")
    if isinstance(saved, dict):
        detail = str(saved.get("detail_text") or "").strip()
        if detail:
            return detail
    pending = session_ctx.get("pending_hosting_offer")
    if isinstance(pending, dict):
        return str(pending.get("detail_text") or "").strip()
    return ""


def stamp_pending_hosting_offer(ctx: dict[str, Any], saved: dict[str, Any]) -> None:
    """Remember last hosting card so CTAs work on the next turn."""
    if str(saved.get("intent") or "") != "host_meet":
        return
    ctx["pending_hosting_offer"] = {
        "signal_id": saved.get("signal_id"),
        "detail_text": saved.get("detail_text"),
        "hosting": saved.get("hosting"),
    }


def handle_hosting_open_turn(
    *,
    session_ctx: dict[str, Any],
    user_jwt: str,
    phone_verified: bool,
    phase: str,
) -> tuple[str, dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    ctx = dict(session_ctx)
    detail = _hosting_detail_from_session(ctx)
    title = detail.split(" — ", 1)[0].strip() or "Your meetup"

    refresh_my_signal_matches(user_jwt)
    entries: list[dict[str, Any]] = []
    try:
        all_entries = fetch_my_block_log(user_jwt, refresh=False)
        entries = filter_block_log_for_signal(
            all_entries,
            signal_intent="host_meet",
            signal_id=str((ctx.get("signal_saved") or {}).get("signal_id") or "") or None,
            detail_text=detail or None,
        )
    except Exception:
        entries = []

    saved_raw = ctx.get("signal_saved") if isinstance(ctx.get("signal_saved"), dict) else {}
    result = {
        "signal_id": saved_raw.get("signal_id"),
        "intent": "host_meet",
        "detail_text": detail or saved_raw.get("detail_text"),
        "matches_created": len(entries),
    }
    stamp_signal_saved_ctx(ctx, result, active_intent="sharing.host")
    saved = ctx["signal_saved"]
    saved["hosting_opened"] = True
    hosting = saved.get("hosting") if isinstance(saved.get("hosting"), dict) else {}
    hosting = dict(hosting)
    hosting["status_label"] = "Open to neighbors nearby"
    hosting["outreach_copy"] = (
        f"{len(entries)} neighbor{'s' if len(entries) != 1 else ''} may want to join."
        if entries
        else "Neighbors nearby can see it — I'll notify you when someone fits."
    )
    saved["hosting"] = hosting
    ctx["signal_saved"] = saved
    stamp_pending_hosting_offer(ctx, saved)

    if entries:
        ctx["block_log_entries"] = [normalize_block_log_row(r) for r in entries[:8]]
        reply = compose_reply(
            goal=(
                "The host just opened their meetup to neighbors nearby and you "
                "found real potential joiners. Confirm it is open, give the real "
                "count, and point them to the list shown below the message."
            ),
            facts=[
                f"Meetup title: {title}",
                f"Neighbors who might join (shown below the message): {len(entries)}",
            ],
            fallback=(
                f"Done — **{title}** is open to neighbors nearby. "
                f"I found {len(entries)} neighbor{'s' if len(entries) != 1 else ''} who might join — see below."
            ),
        )
    else:
        ctx.pop("block_log_entries", None)
        reply = compose_reply(
            goal=(
                "The host just opened their meetup to neighbors nearby but no "
                "potential joiners were found yet. Confirm it is open and promise "
                "to let them know when a neighbor wants to join."
            ),
            facts=[f"Meetup title: {title}"],
            fallback=(
                f"Done — **{title}** is open to neighbors nearby. "
                "I'll let you know when a neighbor wants to join."
            ),
        )

    ctx["active_intent"] = "sharing.host"
    ctx["routing_phase"] = phase or "preview"
    ctx["last_routing"] = {"outcome": "A", "intent_class": "sharing", "tool_called": "hosting_open"}
    _ = phone_verified
    return reply, ctx, ctx["last_routing"], []


def handle_hosting_send_mom_turn(
    *,
    session_ctx: dict[str, Any],
    user_jwt: str,
    phone_verified: bool,
    phase: str,
) -> tuple[str, dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    from app.auth import jwt_user_id
    from app.guest_capabilities import fetch_peer_matches
    from app.layer1_handlers import peers_to_match_rows

    ctx = dict(session_ctx)
    detail = _hosting_detail_from_session(ctx)
    title = detail.split(" — ", 1)[0].strip() or "your meetup"

    peers: list[dict[str, Any]] = []
    try:
        # Who to invite obeys the top-of-app filter too: with a community selected,
        # the people worth inviting to this meet are the ones at that community.
        from app.community_scope import active_community, peers_in_community

        comm = active_community(session_ctx)
        user_id = jwt_user_id(user_jwt)
        peers = (
            peers_in_community(user_id, str(comm["place_id"]), limit=5)
            if comm and user_id
            else []
        )
        peers = peers or fetch_peer_matches(user_jwt, limit=5)
    except Exception:
        peers = []

    peer_rows = peers_to_match_rows(peers, phone_verified=phone_verified) if peers else []
    ctx.pop("signal_saved", None)
    ctx.pop("pending_hosting_offer", None)
    ctx["peer_matches"] = peer_rows
    ctx["active_intent"] = "discovery.find_peers"
    ctx["routing_phase"] = phase or "preview"
    ctx["last_routing"] = {"outcome": "A", "intent_class": "discovery", "tool_called": "hosting_invite"}

    if peer_rows:
        reply = compose_reply(
            goal=(
                "The host asked to invite a neighbor to their meetup and cards of "
                "neighbors they could invite are shown below the message. Tell them "
                "to tap the **Nudge** button (exact button name) on someone who fits."
            ),
            facts=[
                f"Meetup title: {title}",
                f"Neighbor cards shown below the message: {len(peer_rows)}",
            ],
            fallback=(
                f"Here are neighbors you could invite to **{title}** — "
                "tap **Nudge** on someone who fits."
            ),
        )
    else:
        reply = compose_reply(
            goal=(
                "The host asked to invite a neighbor to their meetup but no strong "
                "fits were found near them yet. Say so honestly, and suggest "
                "broadening who they are hosting for, or saying exactly "
                "'find people like me' (keep that phrase verbatim)."
            ),
            facts=[f"Meetup title: {title}"],
            fallback=(
                f"I don't see strong fits near you yet for **{title}**. "
                "Try broadening who you're hosting for, or say find people like me."
            ),
        )
    return reply, ctx, ctx["last_routing"], peer_rows
