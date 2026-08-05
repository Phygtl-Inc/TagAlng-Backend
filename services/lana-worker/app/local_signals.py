"""Local signals (LOOKING/SHARING) — Flash-routed capture + block-log reads."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from app.reply_compose import compose_reply
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

# find_neighbor_tips v1 (20261001120000) took only these. Used to retry against a DB that
# has not yet applied the v2 migration.
_V1_TIP_ARGS = frozenset({"p_block_id", "p_query", "p_category", "p_limit"})

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
    photo_url: str | None = None,
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
    if photo_url:
        payload["p_photo_url"] = photo_url
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
    result = raw if isinstance(raw, dict) else {}

    # The matcher ran inside that insert and found the other side of somebody's open ask —
    # tell them NOW, in this turn. Before this the match rows piled up in
    # match_notifications with no consumer, so "I'll text you when a neighbor recommends
    # one" only came true for people who opened the radar themselves. Fire-and-forget on a
    # daemon thread: the poster's turn never waits on someone else's push. A reused row
    # (dedupe) creates no new matches, hence the matches_created guard.
    try:
        if int(result.get("matches_created") or 0) > 0:
            from app.signal_match_notify import notify_new_signal_matches

            notify_new_signal_matches(user_jwt, signal_id=str(result.get("signal_id") or ""))
    except Exception:  # noqa: BLE001 — a notification must never break a save
        pass
    return result


def find_neighbor_tips(
    user_jwt: str,
    *,
    block_id: str,
    query: str,
    category: str | None = None,
    limit: int = 3,
    locale: str = "en",
    radius_meters: float | None = None,
) -> list[dict[str, Any]]:
    """Neighbors' tip_share posts that match this ask — READ-ONLY (no signal written).

    This is how a recommendation ask gets a real neighbor answer without posting a
    tip_seek first: before find_neighbor_tips the only matcher ran inside
    save_local_signal, so asking a question was the same act as broadcasting it.
    Best-effort: [] whenever the RPC is missing (older DB) or errors.

    v2 (20261002120000) adds the author, the distance and the tip's own tags to each row,
    plus radius_meters to widen past the caller's block. A DB still on v1 rejects the two
    new arguments, so those are dropped and the call is retried — the caller reads every v2
    field with .get() and simply shows a thinner row.
    """
    if not (str(query or "").strip()):
        return []
    if not block_id and radius_meters is None:
        return []
    payload: dict[str, Any] = {
        "p_block_id": block_id,
        "p_query": str(query).strip(),
        "p_category": (str(category).strip() or None) if category else None,
        "p_limit": int(limit),
        "p_locale": str(locale or "en"),
    }
    if radius_meters is not None:
        payload["p_radius_meters"] = float(radius_meters)
    try:
        raw = call_rpc(user_jwt, "find_neighbor_tips", payload)
    except HTTPException as exc:
        if "pgrst202" not in str(exc.detail or "").lower():
            return []
        if not block_id:
            return []  # v1 has no radius mode and no block to fall back to
        legacy = {k: v for k, v in payload.items() if k in _V1_TIP_ARGS}
        try:
            raw = call_rpc(user_jwt, "find_neighbor_tips", legacy)
        except HTTPException:
            return []
    if isinstance(raw, list):
        return [r for r in raw if isinstance(r, dict)]
    if isinstance(raw, dict):
        return [raw]
    return []


def close_local_signal(user_jwt: str, *, signal_id: str | None = None) -> dict[str, Any]:
    """Withdraw a posting (default: the caller's most recent open one).

    Returns the RPC's verdict dict: {"closed": bool, ...}. `closed: False` with
    reason not_found/already_closed is a normal outcome the caller must speak to —
    never claim a removal that did not happen.
    """
    payload: dict[str, Any] = {}
    if signal_id:
        payload["p_signal_id"] = signal_id
    try:
        raw = call_rpc(user_jwt, "close_local_signal", payload)
    except HTTPException as exc:
        detail = str(exc.detail or "").lower()
        # Migration not applied in this environment yet — the caller apologises rather
        # than pretending the posting is gone.
        if "pgrst202" in detail or "close_local_signal" in detail:
            return {"closed": False, "reason": "unavailable"}
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


def _first_match_reason(row: dict[str, Any]) -> str:
    reasons = row.get("match_reasons")
    if isinstance(reasons, list):
        for raw in reasons:
            bit = str(raw or "").strip()
            if bit and bit.lower() != "same block neighbor":
                return bit
    return ""


def block_log_match_summary(row: dict[str, Any], *, user_facing: bool = True) -> str:
    """Summarize a block-log row for chat — lead with what the neighbor has/does."""
    match_type = str(row.get("match_type") or "").strip()
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
    reason = _first_match_reason(row)

    if user_facing:
        if match_type == "inbound_for_my_seek":
            if peer_bit:
                return peer_bit
            if reason:
                return reason
            return "A neighbor may have something that fits your ask."
        if match_type == "inbound_for_my_offer":
            if peer_bit:
                return peer_bit
            if reason:
                return reason
            return "A neighbor is looking for something you offered."
        if match_type in ("meet_attendee_potential", "meet_invite_potential"):
            if peer_bit and my_bit:
                return f"{peer_bit} · {my_bit}"
            if peer_bit:
                return peer_bit
            if reason:
                return reason
        if match_type == "tip_match":
            if peer_bit:
                return peer_bit
            if reason:
                return reason

    parts: list[str] = []
    if peer_bit:
        parts.append(peer_bit)
    if my_bit and (not user_facing or not peer_bit):
        parts.append(my_bit)
    if parts:
        return " · ".join(parts)
    return reason or my_bit or peer_bit or ""


def filter_block_log_for_signal(
    entries: list[dict[str, Any]],
    *,
    signal_intent: str | None,
    signal_id: str | None = None,
    detail_text: str | None = None,
) -> list[dict[str, Any]]:
    """Keep only block-log rows for the signal family just saved (swap vs meet vs tip)."""
    allowed = _MATCH_TYPES_BY_SIGNAL_INTENT.get(str(signal_intent or "").strip().lower())
    if not allowed:
        filtered = entries
    else:
        filtered = [
            row
            for row in entries
            if str(row.get("match_type") or "") in allowed
        ]
    sid = str(signal_id or "").strip()
    if sid:
        filtered = [
            row
            for row in filtered
            if str(row.get("my_signal_id") or row.get("signal_id") or "") == sid
        ]
    detail = str(detail_text or "").strip().lower()
    if detail:
        filtered = [
            row
            for row in filtered
            if str(row.get("my_signal_detail") or "").strip().lower() == detail
        ]
    return filtered


def normalize_block_log_row(row: dict[str, Any]) -> dict[str, Any]:
    reasons = row.get("match_reasons")
    if not isinstance(reasons, list):
        reasons = []
    summary = block_log_match_summary(row)
    normalized_reasons = [summary] if summary else [str(r) for r in reasons[:6] if str(r).strip()]
    return {
        "entry_id": row.get("entry_id") or row.get("id"),
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
    entries: list[dict[str, Any]] | None = None,
) -> str:
    intent = str(result.get("intent") or "")
    label = _INTENT_LABELS.get(intent, "posting nearby")
    matches = (
        int(matches_shown)
        if matches_shown is not None
        else int(result.get("matches_created") or 0)
    )
    bit = f"Got it — I've noted you're {label}: {detail.strip()}."
    if matches > 0 and entries:
        bit += f" I found {matches} neighbor match{'es' if matches != 1 else ''} near you:"
        lines = [bit]
        for row in entries[:1]:
            nick = str(row.get("peer_preview_label") or "A neighbor").strip()
            summary = block_log_match_summary(row)
            if summary:
                lines.append(f"• {nick} — {summary}")
            else:
                lines.append(f"• {nick}")
        if len(entries) > 1:
            lines.append(f"…and {len(entries) - 1} more in your neighborhood log.")
        lines.append("Say show my neighborhood log for the full list.")
        return "\n".join(lines)
    if matches > 0:
        bit += f" I found {matches} match{'es' if matches != 1 else ''} near you — check your neighborhood log."
    else:
        bit += " I'll let you know when a neighbor nearby matches."
    return bit


def format_block_log_reply(entries: list[dict[str, Any]]) -> str:
    if not entries:
        return compose_reply(
            goal=(
                "Tell the user their neighborhood log is quiet right now, and that "
                "when they ask for something or offer to help neighbors, matches "
                "will show up there."
            ),
            fallback=(
                "Your neighborhood log is quiet right now. When you ask for something or offer to help "
                "neighbors, matches will show up here."
            ),
            cache=True,
        )
    lines = [f"You have {len(entries)} active match{'es' if len(entries) != 1 else ''} near you:"]
    for idx, row in enumerate(entries[:1], start=1):
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
    if len(entries) > 1:
        lines.append(f"…and {len(entries) - 1} more below.")
    lines.append("Say introduce me to #1 to nudge a neighbor about a swap or meetup.")
    return "\n".join(lines)


def _clear_stale_intro_ctx(ctx: dict[str, Any]) -> None:
    ctx["pending_intro_respond"] = None
    ctx["pending_intro_offer"] = None
    ctx["pending_intros"] = None
    ctx.pop("intro_proposal", None)


def stamp_signal_saved_ctx(
    ctx: dict[str, Any],
    result: dict[str, Any],
    *,
    active_intent: str | None = None,
    when_hint: str | None = None,
    where_hint: str | None = None,
    block_name: str | None = None,
) -> None:
    _clear_stale_intro_ctx(ctx)
    ctx.pop("block_log_intro_list", None)
    ctx.pop("pending_hosting_offer", None)
    ctx["signal_saved"] = {
        "signal_id": result.get("signal_id"),
        "intent": result.get("intent"),
        "category": result.get("category"),
        "detail_text": result.get("detail_text"),
        "block_id": result.get("block_id"),
        "matches_created": result.get("matches_created"),
    }
    from app.hosting_surface import attach_hosting_to_signal_saved
    from app.tip_surface import attach_tip_to_signal_saved

    attach_hosting_to_signal_saved(
        ctx["signal_saved"],
        ctx,
        when_hint=when_hint,
        block_name=block_name,
    )
    attach_tip_to_signal_saved(
        ctx["signal_saved"],
        where_hint=where_hint,
    )
    ctx["active_intent"] = active_intent or INTENT_SAVE_SIGNAL
    clear_signal_draft(ctx)


def stamp_block_log_ctx(ctx: dict[str, Any], entries: list[dict[str, Any]]) -> None:
    _clear_stale_intro_ctx(ctx)
    ctx.pop("signal_saved", None)
    normalized = [normalize_block_log_row(row) for row in entries]
    ctx["block_log_entries"] = normalized
    # Persist numbered list for "introduce me to #N" on the next turn (same order user saw).
    ctx["block_log_intro_list"] = list(normalized)
    ctx["active_intent"] = INTENT_SHOW_BLOCK_LOG
