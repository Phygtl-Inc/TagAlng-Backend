"""Queued contributions — swap items and tips captured before the surface is live.

QA (2026-07-08): the pass-along capture ran beautifully ("3T rain boots · free") and
then dead-ended — swaps are "Coming soon" in the UI, so "it's listed on your block"
was a promise the product couldn't keep. A dentist tip ask from an unverified guest
got a bare "Verify your email first" wall.

This module is the honest alternative: park the finished capture in
`queued_contributions` (see migration) and close with a promise we CAN keep —
"I'll hold your listing — swaps open on your block soon and yours will be first up."
`notify` is true only when the user has a verified contact to text when the surface
opens; anonymous/unverified users queue with notify=false.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

QUEUED_KIND_SWAP = "swap_item"
QUEUED_KIND_TIP = "tip"

# local_signals intent → queued_contributions kind (only the not-yet-live families).
_KIND_BY_SIGNAL_INTENT: dict[str, str] = {
    "swap_seek": QUEUED_KIND_SWAP,
    "swap_offer": QUEUED_KIND_SWAP,
    "tip_seek": QUEUED_KIND_TIP,
    "tip_share": QUEUED_KIND_TIP,
}


def kind_for_signal_intent(intent: str | None) -> str | None:
    """The queue kind for a signal intent, or None when that family is live (meets)."""
    return _KIND_BY_SIGNAL_INTENT.get(str(intent or "").strip().lower())


def queue_contribution(
    *,
    user_id: str | None,
    block_id: str | None,
    kind: str,
    payload: dict[str, Any],
    notify: bool = True,
) -> bool:
    """Insert one queued contribution (service-role; best-effort — a queue failure must
    never break the turn). Returns True when the row landed."""
    if not user_id or kind not in (QUEUED_KIND_SWAP, QUEUED_KIND_TIP):
        return False
    try:
        from app.auth import service_client

        service_client().table("queued_contributions").insert(
            {
                "user_id": user_id,
                "block_id": block_id or None,
                "kind": kind,
                "payload": payload if isinstance(payload, dict) else {},
                "status": "queued",
                "notify": bool(notify),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        ).execute()
        return True
    except Exception:  # noqa: BLE001 - queueing is best-effort
        logging.getLogger(__name__).exception(
            "queue_contribution_failed: kind=%s user_id=%s", kind, user_id
        )
        return False


def _notify_tail(notify: bool) -> str:
    """The contact promise — reuses the existing verify gate as an invitation, never a wall."""
    if notify:
        return " I'll text you."
    return " Verify your email and I'll text you the moment it opens."


def queued_close_line(*, kind: str, summary: str, notify: bool) -> str:
    """The honest close after a completed capture — hold + first-up + contact promise."""
    what = str(summary or "").strip() or ("your item" if kind == QUEUED_KIND_SWAP else "your tip")
    if kind == QUEUED_KIND_SWAP:
        return (
            f"I'll hold your **{what}** listing — swaps open on your block soon "
            f"and yours will be first up.{_notify_tail(notify)}"
        )
    return (
        f"I'll hold your **{what}** tip — tips open on your block soon "
        f"and yours will be first up.{_notify_tail(notify)}"
    )


def unverified_queue_reply(
    *,
    signal_intent: str | None,
    detail: str,
    user_id: str | None,
    block_id: str | None,
    zip_code: str | None = None,
    category: str | None = None,
) -> str | None:
    """The honest replacement for the bare "Verify your email first" wall on a swap/tip
    ask from an unverified user: acknowledge the ask, explain the surface is almost here,
    and queue it (notify=false — no verified contact yet). Returns None when the intent
    isn't in the swap/tip family, so live features keep their existing gate."""
    intent = str(signal_intent or "").strip().lower()
    kind = kind_for_signal_intent(intent)
    if not kind:
        return None
    what = str(detail or "").strip()
    seeking = intent.endswith("_seek")
    queued = queue_contribution(
        user_id=user_id,
        block_id=block_id,
        kind=kind,
        payload={
            "intent": intent,
            "detail_text": what,
            "category": str(category or "").strip() or None,
            "zip": str(zip_code or "").strip() or None,
        },
        notify=False,
    )
    surface = "Swaps" if kind == QUEUED_KIND_SWAP else "Tips from neighbors"
    ack = f"Heard you — **{what}**." if what else "Heard you."
    if queued:
        held = (
            "I've queued your ask so it's first in line when they open"
            if seeking
            else "I'm holding yours so it's first up when they open"
        )
    else:
        held = "yours will be first up when they open"
    return (
        f"{ack} {surface} are almost here on your block — {held}. "
        "Verify your email and I'll text you the moment it's live."
    )
