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
import os
import threading
from typing import Any

from app.auth import service_client

logger = logging.getLogger(__name__)

# Discovery gating rollout knob (§D.2 — the consumption side):
#   off  — no gating anywhere (pre-Circles behavior).
#   soft — DEFAULT. Empty discovery states gain the seed-forward framing
#          ("N of M neighbors — host something / bring your people in")
#          instead of a bare "nothing found".
#   hard — soft, plus find-peers intros require an OPEN area (sparse-area intros
#          are junk-quality and privacy-risky; peers is the true density product).
# Browse (others' events) is gated pre-open in BOTH soft and hard since the
# rapport-bridge alignment (2026-07-30) — see discovery_zip_gate's docstring.
# The original "supply is never hidden" soft-mode stance is retired for browse.
_GATE_MODES = ("off", "soft", "hard")

# The open-transition copy lives in the i18n catalog as notify.area_open.*
# (2026-08-03) — it reaches up to 500 people who each read in their own
# language, so it cannot be a module constant.


def recount_zip(zip5: str, *, notify_on_open: bool = True) -> dict[str, Any] | None:
    """Recount + transition one ZIP. Returns the state dict or None on failure.

    notify_on_open gates ONLY the open-transition push fan-out — the recount, the
    state write and the founding stamps happen in SQL regardless. Every caller
    passes False today: a count that a *read* triggers must never broadcast to a
    whole ZIP (2026-08-02 — `area_progress` served GET /lana/area/progress and
    could push "your neighborhood just came alive" to up to 500 users with nobody
    pressing anything). The announcement needs a deliberate owner; until one
    exists, `opened` is consumed silently and the push never fires.
    """
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
            from app.i18n import t
            from app.notifications import send_push

            res = (
                service_client()
                .table("users")
                .select("id, locale")
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
                # locale rides along on the roster query — 500 recipients must
                # never mean 500 extra lookups.
                lang = (row.get("locale") or "").strip() or None
                try:
                    send_push(
                        uid,
                        title=t("notify.area_open.title", lang),
                        body=t("notify.area_open.body", lang),
                        url="/",
                    )
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


def gate_mode() -> str:
    mode = str(os.environ.get("LANA_ZIP_UNLOCK_GATE", "soft")).strip().lower()
    return mode if mode in _GATE_MODES else "soft"


def _unlock_snapshot(zip5: str) -> dict[str, Any] | None:
    """Stored unlock row for a ZIP — no recount on the hot path. A missing row
    (nobody ever counted this ZIP) recounts once, read-repair style."""
    z = str(zip5 or "").strip()
    if not z:
        return None
    try:
        res = (
            service_client()
            .table("zip_unlock")
            .select("zip5, unlock_state, verified_active_count, unlock_threshold")
            .eq("zip5", z)
            .limit(1)
            .execute()
        )
        row = (res.data or [None])[0]
    except Exception:
        logger.exception("unlock_snapshot_failed zip=%s", z)
        return None
    if row:
        return {
            "zip5": row.get("zip5"),
            "state": row.get("unlock_state"),
            "count": row.get("verified_active_count"),
            "threshold": row.get("unlock_threshold"),
        }
    state = recount_zip(z, notify_on_open=False)
    return (
        {
            "zip5": state.get("zip5") or z,
            "state": state.get("state"),
            "count": state.get("count"),
            "threshold": state.get("threshold"),
        }
        if state
        else None
    )


def discovery_zip_gate(user_id: str | None, *, surface: str) -> dict[str, Any] | None:
    """§D.2 consumption gate for one discovery surface ('peers' | 'browse').

    None → proceed normally (mode off, area open, or unknown ZIP — fail OPEN:
    a gating error must never lock a user out of discovery). Otherwise a framing
    dict {mode, blocked, zip5, state, count, threshold}.

    `blocked` (§D.2 as amended by LANA_RAPPORT_BRIDGE_SPEC_v1, 2026-07-30):
      · peers  — blocked in hard mode (sparse-area intros are junk-quality and
        privacy-risky);
      · browse — blocked in every gating mode while the area is not open.
        Others'-events discovery before unlock undercuts the bridge (QA: a lone
        off-topic event card answered "meet other runners" in a waitlist ZIP) —
        pre-open, the product move is always create+invite, so consumers route
        to that instead of listing supply. The original soft-mode "supply is
        never hidden" stance is retired for browse; copy must stay honest
        (the area isn't open yet) and must NOT claim nothing exists.
    """
    mode = gate_mode()
    if mode == "off" or not user_id:
        return None
    try:
        profile = _user_home_zip(user_id) or {}
        snap = _unlock_snapshot(str(profile.get("home_zip") or ""))
    except Exception:
        logger.exception("discovery_zip_gate_failed user=%s", user_id)
        return None
    if not snap or str(snap.get("state") or "") == "open":
        return None
    return {
        "mode": mode,
        "blocked": surface == "browse" or (mode == "hard" and surface == "peers"),
        "zip5": snap.get("zip5"),
        "state": snap.get("state"),
        "count": int(snap.get("count") or 0),
        "threshold": snap.get("threshold"),
    }


def gate_framing_facts(frame: dict[str, Any]) -> list[str]:
    """Compose-ready facts about the user's not-yet-open area, for AI-authored
    empty states. Lingo-clean: 'your area', never 'block'/'ZIP <n>' in copy."""
    count = int(frame.get("count") or 0)
    threshold = frame.get("threshold")
    facts = [
        "Their area is still coming alive — not enough verified neighbors have "
        "joined yet for full discovery.",
    ]
    if threshold:
        facts.append(
            f"Progress: {count} of {threshold} neighbors so far. Never call it a "
            "waitlist or quota — frame it as their area waking up."
        )
    facts.append(
        "The genuinely useful move: they can host something or bring their own "
        "people in — creating is always available and is exactly what brings "
        "their area to life."
    )
    return facts


def area_progress(user_id: str, zip5: str | None = None) -> dict[str, Any]:
    """{state, count, threshold, is_founding_eligible, founding_earned} for the
    caller's area (§E.2 / Appendix B GET /area/:zip/progress). Recounts on read."""
    profile = _user_home_zip(user_id) or {}
    z = str(zip5 or profile.get("home_zip") or "").strip()
    if not z:
        return {"state": None, "count": 0, "threshold": None, "zip5": None,
                "is_founding_eligible": False, "founding_earned": False}
    state = recount_zip(z, notify_on_open=False) or {}
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
