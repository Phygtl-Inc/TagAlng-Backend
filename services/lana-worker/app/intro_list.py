"""List pending neighbor intros for Lana + FE."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from app.supabase_rpc import call_rpc
from app.ui_actions import attach_intro_row_actions

INTENT_LIST_INTROS = "social.list_intros"

_LIST_INTROS_PHRASES = (
    "show my intros",
    "show intros",
    "my intros",
    "pending intros",
    "list intros",
    "intro inbox",
    "any intros",
)


def wants_list_intros_phrase(msg: str) -> bool:
    lower = str(msg or "").lower()
    return any(phrase in lower for phrase in _LIST_INTROS_PHRASES)


def fetch_my_intros(
    user_jwt: str,
    *,
    direction: str = "all",
) -> list[dict[str, Any]]:
    raw = call_rpc(user_jwt, "get_my_intros", {"p_direction": direction})
    if not raw:
        return []
    if isinstance(raw, list):
        return [r for r in raw if isinstance(r, dict)]
    if isinstance(raw, dict):
        return [raw]
    return []


def normalize_intro_row(row: dict[str, Any]) -> dict[str, Any]:
    dims = row.get("shared_dimensions")
    if not isinstance(dims, list):
        dims = []
    return {
        "intro_id": row.get("id"),
        "other_user_id": row.get("other_user_id"),
        "nickname": row.get("nickname"),
        "avatar_url": row.get("avatar_url"),
        "created_at": row.get("created_at"),
        "expires_at": row.get("expires_at"),
        "status": row.get("status") or "proposed",
        "match_reason": row.get("match_reason"),
        "shared_dimensions": [str(d) for d in dims[:8]],
        "direction": row.get("direction"),
    }


def _expires_label(expires_at: Any) -> str:
    if not expires_at:
        return ""
    try:
        text = str(expires_at).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        days = max(0, (dt - now).days)
        if days == 0:
            return "expires today"
        if days == 1:
            return "expires tomorrow"
        return f"expires in {days} days"
    except (TypeError, ValueError):
        return ""


def format_intros_list_reply(intros: list[dict[str, Any]]) -> str:
    if not intros:
        return (
            "You don't have any pending intros right now. When I introduce you to a neighbor "
            "or someone introduces you, they'll show up here until they respond."
        )

    lines = [f"You have {len(intros)} pending intro{'s' if len(intros) != 1 else ''}:"]
    for idx, row in enumerate(intros[:8], start=1):
        nick = str(row.get("nickname") or "a neighbor").strip()
        direction = str(row.get("direction") or "").strip()
        dir_label = "you sent" if direction == "sent" else "waiting on you" if direction == "received" else "pending"
        reason = str(row.get("match_reason") or "").strip()
        expiry = _expires_label(row.get("expires_at"))
        bit = f"{idx}. {nick} ({dir_label})"
        if reason:
            bit += f" — {reason}"
        if expiry:
            bit += f" ({expiry})"
        lines.append(bit)
    if len(intros) > 8:
        lines.append(f"…and {len(intros) - 8} more.")
    return "\n".join(lines)


def intro_row_from_proposal(
    intro: dict[str, Any],
    peer: dict[str, Any],
    *,
    direction: str = "sent",
) -> dict[str, Any]:
    """Synthetic inbox row when get_my_intros lags or RLS blocked (fallback)."""
    nick = str(peer.get("nickname") or peer.get("matching_peer_label") or "A neighbor").strip()
    return {
        "id": intro.get("intro_id"),
        "intro_id": intro.get("intro_id"),
        "other_user_id": intro.get("candidate_user_id") or peer.get("peer_user_id"),
        "nickname": nick,
        "avatar_url": peer.get("avatar_url"),
        "status": intro.get("status") or "proposed",
        "match_reason": intro.get("match_reason") or peer.get("matching_peer_label"),
        "shared_dimensions": intro.get("shared_dimensions") or [],
        "direction": direction,
    }


def attach_pending_intros_after_propose(
    ctx: dict[str, Any],
    *,
    user_jwt: str,
    intro: dict[str, Any],
    peer: dict[str, Any],
) -> None:
    """Show sent intro on propose turn — fetch DB first, else use just-created intro."""
    rows: list[dict[str, Any]] = []
    try:
        rows = fetch_my_intros(user_jwt, direction="sent")
    except HTTPException:
        rows = []
    if not rows and intro.get("intro_id"):
        rows = [intro_row_from_proposal(intro, peer, direction="sent")]
    if rows:
        ctx["pending_intros"] = [normalize_intro_row(r) for r in rows]


def stamp_pending_intros_ctx(ctx: dict[str, Any], intros: list[dict[str, Any]]) -> None:
    # Null (not pop) so merge_session_context drops stale respond/offer from prior turns.
    ctx["pending_intro_respond"] = None
    ctx["pending_intro_offer"] = None
    ctx["pending_intros"] = [
        attach_intro_row_actions(normalize_intro_row(row)) for row in intros
    ]
    ctx["active_intent"] = INTENT_LIST_INTROS



def stamp_duplicate_intro_sent(
    ctx: dict[str, Any],
    *,
    peer: dict[str, Any],
    match_reason: str | None = None,
) -> None:
    """Already sent intro — steer FE to inbox, not another nudge."""
    nick = str(peer.get("nickname") or peer.get("matching_peer_label") or "").strip()
    ctx["recent_intro_duplicate"] = {
        "candidate_user_id": peer.get("peer_user_id"),
        "candidate_nickname": nick or "that neighbor",
        "match_reason": str(match_reason or peer.get("matching_peer_label") or "").strip(),
    }
    ctx.pop("pending_intro_offer", None)
    ctx.pop("intro_offer_shown", None)
    ctx.pop("pending_intro_respond", None)


def stamp_intro_respond_from_peer(
    ctx: dict[str, Any],
    *,
    user_jwt: str,
    peer: dict[str, Any],
) -> bool:
    """Duplicate / waiting-on-you intro — surface respond CTAs for FE."""
    peer_id = str(peer.get("peer_user_id") or "").strip()
    try:
        intros = fetch_my_intros(user_jwt, direction="all")
    except HTTPException:
        return False
    for row in intros:
        if peer_id and str(row.get("other_user_id") or "") != peer_id:
            continue
        if str(row.get("direction") or "") != "received":
            return False
        norm = normalize_intro_row(row)
        ctx["pending_intro_respond"] = {
            "intro_id": norm.get("intro_id") or norm.get("id"),
            "other_user_id": norm.get("other_user_id"),
            "nickname": norm.get("nickname"),
        }
        ctx["pending_intros"] = [norm]
        ctx["active_intent"] = "tier.respond_nudge"
        ctx.pop("pending_intro_offer", None)
        ctx.pop("intro_offer_shown", None)
        ctx.pop("peer_matches", None)
        return True
    return False


def infer_intro_direction(msg: str, slots: dict[str, Any] | None = None) -> str:
    lower = str(msg or "").lower()
    if wants_list_intros_phrase(lower):
        return "all"
    if any(w in lower for w in ("i sent", "outgoing", "waiting on them", "they respond")):
        return "sent"
    if any(
        phrase in lower
        for phrase in (
            "what did you send",
            "what intro did you send",
            "who did i introduce",
            "intros i sent",
        )
    ):
        return "sent"
    if any(w in lower for w in ("received", "waiting on me", "someone introduced", "for me to accept")):
        return "received"
    slot_dir = str((slots or {}).get("intro_direction") or "").lower()
    if slot_dir in ("sent", "received", "all"):
        return slot_dir
    return "all"


def format_duplicate_intro_reply(
    *,
    peer: dict[str, Any],
    user_jwt: str,
    attempt_summary: str | None = None,
) -> str:
    raw_nick = str(peer.get("nickname") or "").strip()
    if raw_nick and raw_nick.lower() not in ("a neighbor", "them"):
        nick = raw_nick
    else:
        nick = "that neighbor"
    peer_id = str(peer.get("peer_user_id") or "")
    try:
        intros = fetch_my_intros(user_jwt, direction="all")
    except HTTPException:
        intros = []
    for row in intros:
        if peer_id and str(row.get("other_user_id") or "") != peer_id:
            continue
        direction = str(row.get("direction") or "")
        reason = str(row.get("match_reason") or "").strip()
        if direction == "received":
            reply = (
                f"{nick} already introduced you — it's waiting on you to respond. "
                "Tap below to accept or pass."
            )
        else:
            reply = f"You already sent an intro to {nick} — give them a little time to respond."
        if reason:
            reply += f" I matched you on: {reason}."
        return reply
    attempt = str(attempt_summary or "").strip()
    if attempt:
        return (
            f"You already nudged {nick} in the last 30 days"
            f" (your last intro wasn't about this match: {attempt}). "
            "Pick another block-log row — e.g. introduce me to #2 — or say show my intros."
        )
    return (
        f"There's already a recent intro between you and {nick} in the last 30 days. "
        "Your inbox only shows pending intros — if it's empty, that one was likely "
        "accepted, expired, or declined. Say show my intros to check, or pick another "
        "block-log match (e.g. introduce me to #2)."
    )
