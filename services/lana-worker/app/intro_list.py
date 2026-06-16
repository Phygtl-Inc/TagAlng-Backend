"""List pending neighbor intros for Lana + FE."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.supabase_rpc import call_rpc

INTENT_LIST_INTROS = "social.list_intros"


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


def stamp_pending_intros_ctx(ctx: dict[str, Any], intros: list[dict[str, Any]]) -> None:
    ctx["pending_intros"] = [normalize_intro_row(row) for row in intros]
    ctx["active_intent"] = INTENT_LIST_INTROS


def infer_intro_direction(msg: str, slots: dict[str, Any] | None = None) -> str:
    lower = str(msg or "").lower()
    if any(
        phrase in lower
        for phrase in (
            "show my intros",
            "show intros",
            "my intros",
            "pending intros",
            "list intros",
            "intro inbox",
            "any intros",
        )
    ):
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
) -> str:
    nick = str(peer.get("nickname") or peer.get("matching_peer_label") or "them").strip()
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
            reply = f"{nick} already introduced you — it's waiting on you to respond."
        else:
            reply = f"You already sent an intro to {nick} — give them a little time to respond."
        if reason:
            reply += f" I matched you on: {reason}."
        return reply
    return (
        f"There's already a recent intro between you and {nick}. "
        "Say 'show my intros' to see what's pending."
    )
