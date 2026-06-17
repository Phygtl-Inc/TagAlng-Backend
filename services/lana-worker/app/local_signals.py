"""Local signals (LOOKING/SHARING) — Flash-routed capture + block-log reads."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from app.supabase_rpc import call_rpc

from app.signal_capture import clear_signal_draft

INTENT_SAVE_SIGNAL = "signal.capture"
INTENT_SHOW_BLOCK_LOG = "discovery.block_log"

_VALID_SIGNAL_INTENTS = frozenset({
    "swap_seek",
    "swap_offer",
    "meet_seek",
    "host_meet",
    "tip_seek",
    "tip_share",
})

_INTENT_LABELS: dict[str, str] = {
    "swap_seek": "looking for",
    "swap_offer": "offering",
    "meet_seek": "looking to meet neighbors",
    "host_meet": "hosting a meetup",
    "tip_seek": "looking for a recommendation",
    "tip_share": "sharing a recommendation",
}

_MATCH_TYPES_BY_SIGNAL_INTENT: dict[str, frozenset[str]] = {
    "swap_seek": frozenset({"inbound_for_my_seek"}),
    "swap_offer": frozenset({"inbound_for_my_offer"}),
    "meet_seek": frozenset({"meet_attendee_potential"}),
    "host_meet": frozenset({"meet_invite_potential"}),
    "tip_seek": frozenset({"tip_match"}),
    "tip_share": frozenset({"tip_match"}),
}


def normalize_signal_intent(raw: str | None) -> str | None:
    intent = str(raw or "").strip().lower()
    return intent if intent in _VALID_SIGNAL_INTENTS else None


def save_local_signal(
    user_jwt: str,
    *,
    intent: str,
    detail_text: str,
    category: str | None = None,
    block_id: str | None = None,
    zip_code: str | None = None,
    stage: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "p_intent": intent,
        "p_detail_text": detail_text,
    }
    if category:
        payload["p_category"] = category
    if block_id:
        payload["p_block_id"] = block_id
    if zip_code:
        payload["p_zip"] = zip_code
    if stage:
        payload["p_stage"] = stage
    try:
        raw = call_rpc(user_jwt, "save_local_signal", payload)
    except HTTPException as exc:
        detail = str(exc.detail or "").lower()
        # Backward-compat for older DB signatures that still use p_detail.
        if (
            exc.status_code == 502
            and "pgrst202" in detail
            and "p_detail_text" in detail
        ):
            legacy_payload = dict(payload)
            legacy_payload.pop("p_detail_text", None)
            legacy_payload["p_detail"] = detail_text
            raw = call_rpc(user_jwt, "save_local_signal", legacy_payload)
        else:
            raise
    return raw if isinstance(raw, dict) else {}


def refresh_my_signal_matches(user_jwt: str) -> int:
    """Re-run matcher for caller's listening signals (writes block_log_entries)."""
    try:
        raw = call_rpc(user_jwt, "refresh_my_signal_matches", {})
    except HTTPException:
        return 0
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


def fetch_my_block_log(user_jwt: str, *, refresh: bool = True) -> list[dict[str, Any]]:
    if refresh:
        refresh_my_signal_matches(user_jwt)
    raw = call_rpc(user_jwt, "get_my_block_log", {})
    if not raw:
        return []
    if isinstance(raw, list):
        return [r for r in raw if isinstance(r, dict)]
    if isinstance(raw, dict):
        return [raw]
    return []


def block_log_take_action(user_jwt: str, entry_id: str, action: str) -> dict[str, Any]:
    raw = call_rpc(
        user_jwt,
        "block_log_action",
        {"p_entry_id": entry_id, "p_action": action},
    )
    return raw if isinstance(raw, dict) else {}


def _signal_detail_phrase(intent: str, detail: str, *, role: str) -> str | None:
    text = str(detail or "").strip()
    if not text:
        return None
    intent = str(intent or "").strip().lower()
    if intent == "meet_seek":
        prefix = "They want to meet" if role == "peer" else "You're looking to meet"
        return f"{prefix}: {text}"
    if intent == "host_meet":
        prefix = "They offered to host" if role == "peer" else "You offered to host"
        return f"{prefix}: {text}"
    if intent == "swap_seek":
        prefix = "They're looking for" if role == "peer" else "You're looking for"
        return f"{prefix}: {text}"
    if intent == "swap_offer":
        prefix = "They're offering" if role == "peer" else "You're offering"
        return f"{prefix}: {text}"
    if intent == "tip_share":
        prefix = "They shared a tip" if role == "peer" else "You shared a tip"
        return f"{prefix}: {text}"
    if intent == "tip_seek":
        prefix = "They asked for a rec" if role == "peer" else "You asked for a rec"
        return f"{prefix}: {text}"
    return None


def block_log_match_summary(row: dict[str, Any]) -> str:
    parts: list[str] = []
    peer_bit = _signal_detail_phrase(
        str(row.get("peer_signal_intent") or ""),
        str(row.get("peer_signal_detail") or ""),
        role="peer",
    )
    my_bit = _signal_detail_phrase(
        str(row.get("my_signal_intent") or ""),
        str(row.get("my_signal_detail") or ""),
        role="my",
    )
    if peer_bit:
        parts.append(peer_bit)
    if my_bit:
        parts.append(my_bit)
    if parts:
        return " · ".join(parts)
    reasons = row.get("match_reasons")
    if isinstance(reasons, list):
        for raw in reasons:
            bit = str(raw or "").strip()
            if bit and bit.lower() != "same block neighbor":
                return bit
    return ""


def filter_block_log_for_signal(
    entries: list[dict[str, Any]],
    *,
    signal_intent: str | None,
) -> list[dict[str, Any]]:
    """Keep only block-log rows for the signal family just saved (swap vs meet vs tip)."""
    allowed = _MATCH_TYPES_BY_SIGNAL_INTENT.get(str(signal_intent or "").strip().lower())
    if not allowed:
        return entries
    return [
        row
        for row in entries
        if str(row.get("match_type") or "") in allowed
    ]


def normalize_block_log_row(row: dict[str, Any]) -> dict[str, Any]:
    reasons = row.get("match_reasons")
    if not isinstance(reasons, list):
        reasons = []
    summary = block_log_match_summary(row)
    normalized_reasons = [summary] if summary else [str(r) for r in reasons[:6] if str(r).strip()]
    return {
        "entry_id": row.get("id"),
        "match_type": row.get("match_type"),
        "peer_user_id": row.get("peer_user_id"),
        "peer_preview_label": row.get("peer_preview_label"),
        "match_strength": row.get("match_strength"),
        "match_reasons": normalized_reasons,
        "match_summary": summary or None,
        "my_signal_detail": row.get("my_signal_detail"),
        "peer_signal_detail": row.get("peer_signal_detail"),
        "my_signal_intent": row.get("my_signal_intent"),
        "peer_signal_intent": row.get("peer_signal_intent"),
        "created_at": row.get("created_at"),
        "expires_at": row.get("expires_at"),
        "notification_sent_to_peer": bool(row.get("notification_sent_to_peer")),
        "block_id": row.get("block_id"),
        "block_name": row.get("block_name"),
    }


def format_signal_saved_reply(
    result: dict[str, Any],
    *,
    detail: str,
    matches_shown: int | None = None,
) -> str:
    intent = str(result.get("intent") or "")
    label = _INTENT_LABELS.get(intent, "on your block")
    matches = (
        int(matches_shown)
        if matches_shown is not None
        else int(result.get("matches_created") or 0)
    )
    bit = f"Got it — I've noted you're {label}: {detail.strip()}."
    if matches > 0:
        bit += f" I found {matches} new match{'es' if matches != 1 else ''} on your block — check your block log."
    else:
        bit += " I'll let you know when a neighbor matches."
    return bit


def format_block_log_reply(entries: list[dict[str, Any]]) -> str:
    if not entries:
        return (
            "Your block log is quiet right now. When you ask for something or offer to help "
            "neighbors, matches will show up here."
        )
    lines = [f"You have {len(entries)} active match{'es' if len(entries) != 1 else ''} on your block:"]
    for idx, row in enumerate(entries[:6], start=1):
        nick = str(row.get("peer_preview_label") or "A neighbor").strip()
        reason = block_log_match_summary(row)
        if not reason:
            reasons = row.get("match_reasons") or []
            reason = str(reasons[0]).strip() if reasons else ""
        strength = row.get("match_strength")
        bit = f"{idx}. {nick}"
        if reason:
            bit += f" — {reason}"
        if strength is not None:
            try:
                pct = int(float(strength) * 100)
                bit += f" ({pct}% match)"
            except (TypeError, ValueError):
                pass
        lines.append(bit)
    if len(entries) > 6:
        lines.append(f"…and {len(entries) - 6} more.")
    return "\n".join(lines)


def stamp_signal_saved_ctx(
    ctx: dict[str, Any],
    result: dict[str, Any],
    *,
    active_intent: str | None = None,
) -> None:
    ctx["signal_saved"] = {
        "signal_id": result.get("signal_id"),
        "intent": result.get("intent"),
        "category": result.get("category"),
        "detail_text": result.get("detail_text"),
        "block_id": result.get("block_id"),
        "matches_created": result.get("matches_created"),
    }
    ctx["active_intent"] = active_intent or INTENT_SAVE_SIGNAL
    clear_signal_draft(ctx)


def stamp_block_log_ctx(ctx: dict[str, Any], entries: list[dict[str, Any]]) -> None:
    ctx["block_log_entries"] = [normalize_block_log_row(row) for row in entries]
    ctx["active_intent"] = INTENT_SHOW_BLOCK_LOG
