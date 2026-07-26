"""ZIP unlock state machine (Circles master §D) — worker side.

The count/transition itself is one atomic SQL function (recount_zip_unlock,
service-role only): "verified active" = verified by any method + a Lana session in
30 days + ≥1 confirmed thing (circle or accepted intro). This module wraps it with
the two worker concerns:

  * ZIP_OPENED (§D.4): when a recount crosses the threshold, push every member —
    the one celebration moment — and founding is stamped inside the SQL fn.
  * area_progress (§E.2): the warm goal-gradient read ("7 of 10 to come alive"),
    recounting on read (read-repair — no cron needed at pilot scale).

The state gates CONSUMPTION of others' supply only, never creation (§D.2) — no
create/host/invite path may ever check unlock_state.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from app.auth import service_client

logger = logging.getLogger(__name__)

_OPEN_PUSH_TITLE = "Your neighborhood just came alive"
_OPEN_PUSH_BODY = (
    "Enough neighbors have joined — you can now discover meets, people, "
    "and plans near you."
)


def recount_zip(zip5: str, *, notify_on_open: bool = True) -> dict[str, Any] | None:
    """Recount + transition one ZIP. Returns the state dict or None on failure."""
    z = str(zip5 or "").strip()
    if not z:
        return None
    try:
        res = service_client().rpc("recount_zip_unlock", {"p_zip5": z}).execute()
        state = res.data if isinstance(res.data, dict) else None
    except Exception:
        logger.exception("recount_zip_unlock_failed zip=%s", z)
        return None
    if state and state.get("opened") and notify_on_open:
        _notify_zip_opened_async(str(state.get("zip5") or z))
    return state


def _notify_zip_opened_async(zip5: str) -> None:
    """One push to every member of the newly-open ZIP. Fire-and-forget."""

    def _run() -> None:
        try:
            from app.notifications import send_push

            res = (
                service_client()
                .table("users")
                .select("id")
                .eq("home_zip", zip5)
                .not_.is_("phone_verified_at", "null")
                .limit(500)
                .execute()
            )
            sent = 0
            for row in res.data or []:
                uid = str(row.get("id") or "")
                if not uid:
                    continue
                try:
                    send_push(uid, title=_OPEN_PUSH_TITLE, body=_OPEN_PUSH_BODY, url="/")
                    sent += 1
                except Exception:
                    logger.exception("zip_opened_push_failed user=%s", uid)
            logger.info("zip_opened zip=%s pushed=%d", zip5, sent)
        except Exception:
            logger.exception("zip_opened_notify_failed zip=%s", zip5)

    threading.Thread(target=_run, daemon=True, name=f"zip-opened-{zip5}").start()


def _user_home_zip(user_id: str) -> str | None:
    try:
        res = (
            service_client()
            .table("users")
            .select("home_zip, founding_area, founding_earned_at, phone_verified_at")
            .eq("id", user_id)
            .limit(1)
            .execute()
        )
        return (res.data or [None])[0]
    except Exception:
        logger.exception("user_home_zip_failed user=%s", user_id)
        return None


def _has_confirmed_thing(user_id: str) -> bool:
    """≥1 confirmed circle or accepted intro — the quality half of U2/U4."""
    sb = service_client()
    try:
        circles = (
            sb.table("circle_affiliations")
            .select("id", count="exact")
            .eq("user_id", user_id)
            .eq("status", "confirmed")
            .is_("dismissed_at", "null")
            .limit(1)
            .execute()
        )
        if int(circles.count or 0) > 0:
            return True
        intros = (
            sb.table("intros")
            .select("id", count="exact")
            .eq("status", "accepted")
            .or_(f"initiator_id.eq.{user_id},candidate_id.eq.{user_id}")
            .limit(1)
            .execute()
        )
        return int(intros.count or 0) > 0
    except Exception:
        logger.exception("has_confirmed_thing_failed user=%s", user_id)
        return False


def area_progress(user_id: str, zip5: str | None = None) -> dict[str, Any]:
    """{state, count, threshold, is_founding_eligible, founding_earned} for the
    caller's area (§E.2 / Appendix B GET /area/:zip/progress). Recounts on read."""
    profile = _user_home_zip(user_id) or {}
    z = str(zip5 or profile.get("home_zip") or "").strip()
    if not z:
        return {"state": None, "count": 0, "threshold": None, "zip5": None,
                "is_founding_eligible": False, "founding_earned": False}
    state = recount_zip(z) or {}
    founding_earned = bool(profile.get("founding_earned_at"))
    eligible = founding_earned or (
        str(state.get("state") or "") in ("closed", "warming")
        and bool(profile.get("phone_verified_at"))
        and _has_confirmed_thing(user_id)
    )
    return {
        "zip5": state.get("zip5") or z,
        "state": state.get("state"),
        "count": int(state.get("count") or 0),
        "threshold": state.get("threshold"),
        "is_founding_eligible": bool(eligible),
        "founding_earned": founding_earned,
        "founding_area": profile.get("founding_area"),
    }
