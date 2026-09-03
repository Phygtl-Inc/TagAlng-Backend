"""Deliver the "a neighbor matched your ask" notification — the missing half of
"Tell her once. She keeps looking."

The matcher (_match_local_signal) has always found these matches the instant the second
side posts, and queued rows into match_notifications at strength >= 0.75. Nothing ever read
that queue, so the promise was only kept for people who happened to open the radar
themselves. This module drains the queue and rings the existing push+email rail
(app.notifications.notify_user), which already localizes to each recipient.

No scheduler: the drain runs inside the turn that CREATED the match — the neighbor's own
chat turn — so the person waiting hears about it in the same second. The stale sweeper
(drain_stale_match_notifications, exposed as POST /hooks/signal-matches) covers a turn that
died before delivering, and is correct whether or not anything ever calls it on a timer.

Everything here is best-effort and runs on a daemon thread: a notification must never add
latency to, or break, the turn of the person who posted.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

_LOG = logging.getLogger(__name__)

# What the recipient originally asked for → how to describe what just showed up.
# Keyed on the RECIPIENT's own intent, because the notification speaks to their ask.
_TITLE_KEY = {
    "tip_seek": "notify.signal_match.tip_title",
    "swap_seek": "notify.signal_match.item_title",
    "swap_offer": "notify.signal_match.item_wanted_title",
    "meet_seek": "notify.signal_match.meet_title",
    "host_meet": "notify.signal_match.meet_title",
    "tip_share": "notify.signal_match.tip_wanted_title",
}


def _deliver(rows: list[dict[str, Any]]) -> int:
    """push + email one drained batch. Returns how many recipients were rung."""
    from app.analytics import track
    from app.i18n import localize_text, t
    from app.notifications import email_html, notify_user, recipient_langs

    rows = [r for r in rows if isinstance(r, dict) and r.get("recipient_user_id")]
    if not rows:
        return 0
    langs = recipient_langs([str(r["recipient_user_id"]) for r in rows])
    sent = 0
    for row in rows:
        uid = str(row.get("recipient_user_id") or "")
        lang = langs.get(uid)
        their_intent = str(row.get("recipient_intent") or "").strip().lower()
        ask = str(row.get("recipient_ask") or "").strip()
        what = str(row.get("match_detail") or "").strip()
        title = t(_TITLE_KEY.get(their_intent, "notify.signal_match.title"), lang)
        # The neighbor's own words are the whole value of this notification, so they are
        # passed through (localized as free text, never re-worded into a claim) and the
        # generic line is only the floor when the row carries no detail.
        body = (
            localize_text(f"“{what[:140]}” — for your ask: {ask[:60]}", lang)
            if what and ask
            else localize_text(f"“{what[:140]}”", lang)
            if what
            else t("notify.signal_match.body_generic", lang)
        )
        try:
            notify_user(
                uid,
                title=title,
                body=body,
                url="/chat?panel=radar",
                # Email is the channel that actually lands (push is opt-in and rare), so it
                # gets its own emoji-free subject; the heading inside the mail keeps the
                # warmer push title.
                email_subject=t("notify.signal_match.subject", lang),
                email_html=email_html(
                    title, body, t("notify.signal_match.cta", lang), "/chat?panel=radar"
                ),
            )
            sent += 1
            track(
                "match_notified",
                user_id=uid,
                event_properties={
                    "recipient_intent": their_intent or None,
                    "strength": row.get("match_strength"),
                    "match_intent": str(row.get("match_intent") or "") or None,
                },
            )
        except Exception:  # noqa: BLE001 — one bad recipient must not stop the batch
            _LOG.debug("signal_match_notify_one_failed", exc_info=True)
    return sent


def _drain_and_deliver(user_jwt: str, signal_id: str) -> None:
    from app.supabase_rpc import call_rpc

    try:
        raw = call_rpc(
            user_jwt, "drain_signal_match_notifications", {"p_signal_id": signal_id}
        )
    except Exception:  # noqa: BLE001 — includes the pre-migration PGRST202 (RPC absent)
        _LOG.debug("signal_match_drain_failed", exc_info=True)
        return
    rows = raw if isinstance(raw, list) else ([raw] if isinstance(raw, dict) else [])
    if not rows:
        return
    count = _deliver(rows)
    _LOG.info("signal_match_notify signal=%s drained=%d notified=%d", signal_id, len(rows), count)


def notify_new_signal_matches(user_jwt: str, *, signal_id: str | None) -> None:
    """Tell the OTHER side that what they asked for just showed up.

    Called right after a successful save_local_signal that created matches. Fire-and-forget
    on a daemon thread — the poster's turn never waits on somebody else's push.
    """
    sid = str(signal_id or "").strip()
    if not sid:
        return
    try:
        threading.Thread(
            target=_drain_and_deliver, args=(user_jwt, sid), daemon=True
        ).start()
    except Exception:  # noqa: BLE001
        _LOG.debug("signal_match_notify_spawn_failed", exc_info=True)


def sweep_stale_signal_matches(*, older_than_minutes: int = 10, limit: int = 200) -> int:
    """Deliver queued matches no live turn carried. Service-role; used by the hook."""
    from app.auth import service_client

    sb = service_client()
    if sb is None:
        return 0
    try:
        res = sb.rpc(
            "drain_stale_match_notifications",
            {"p_older_than_minutes": int(older_than_minutes), "p_limit": int(limit)},
        ).execute()
        rows = res.data if isinstance(res.data, list) else []
    except Exception:  # noqa: BLE001
        _LOG.debug("signal_match_sweep_failed", exc_info=True)
        return 0
    if not rows:
        return 0
    count = _deliver(rows)
    _LOG.info("signal_match_sweep drained=%d notified=%d", len(rows), count)
    return count
