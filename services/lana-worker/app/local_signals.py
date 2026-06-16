"""Local signals (LOOKING/SHARING) — Flash-routed capture + block-log reads."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from app.supabase_rpc import call_rpc

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
    "swap_seek": "looking to swap or borrow",
    "swap_offer": "offering to swap or give away",
    "meet_seek": "looking to meet neighbors",
    "host_meet": "hosting a meetup",
    "tip_seek": "looking for a recommendation",
    "tip_share": "sharing a recommendation",
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


def fetch_my_block_log(user_jwt: str) -> list[dict[str, Any]]:
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


def normalize_block_log_row(row: dict[str, Any]) -> dict[str, Any]:
    reasons = row.get("match_reasons")
    if not isinstance(reasons, list):
        reasons = []
    return {
        "entry_id": row.get("id"),
        "match_type": row.get("match_type"),
        "peer_user_id": row.get("peer_user_id"),
        "peer_preview_label": row.get("peer_preview_label"),
        "match_strength": row.get("match_strength"),
        "match_reasons": [str(r) for r in reasons[:6]],
        "created_at": row.get("created_at"),
        "expires_at": row.get("expires_at"),
        "notification_sent_to_peer": bool(row.get("notification_sent_to_peer")),
        "block_id": row.get("block_id"),
        "block_name": row.get("block_name"),
    }


def format_signal_saved_reply(result: dict[str, Any], *, detail: str) -> str:
    intent = str(result.get("intent") or "")
    label = _INTENT_LABELS.get(intent, "on your block")
    matches = int(result.get("matches_created") or 0)
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


def stamp_block_log_ctx(ctx: dict[str, Any], entries: list[dict[str, Any]]) -> None:
    ctx["block_log_entries"] = [normalize_block_log_row(row) for row in entries]
    ctx["active_intent"] = INTENT_SHOW_BLOCK_LOG
