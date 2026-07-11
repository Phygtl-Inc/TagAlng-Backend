"""User language preference — seed, persist, and the divergence nudge.

The model (product decision, 2026-07-11):
- ``users.preferred_language`` decides how a conversation STARTS (the opening
  message); live per-turn detection (the classifier's ``lang`` verdict) decides
  how it CONTINUES. The DB stays English-canonical throughout.
- When the observed language keeps diverging from the saved preference for
  ``_DIVERGENCE_TURNS`` consecutive turns, Lana offers ONCE to switch the
  preference — never every turn (that's irritating), at most once per session,
  with a cross-session cooldown (``users.lang_nudge_at``).
- The offer and the saved-confirmation are AI-authored in Lana's voice from
  the true facts (which language she's seeing, what the setting says) — the
  ``t()`` strings are only the LLM-down fallback.
"""

from __future__ import annotations

import logging
from typing import Any

from app.auth import service_client
from app.i18n import lang_display_name, normalize_lang_code, t

_LOG = logging.getLogger(__name__)

# Consecutive turns the observed language must diverge from the preference
# before Lana offers to switch it.
_DIVERGENCE_TURNS = 3
# Days before the nudge may be offered again in a later session.
_NUDGE_COOLDOWN_DAYS = 14


# ── users table ──────────────────────────────────────────────────────────────

def get_user_preferred_language(user_id: str) -> str | None:
    """The user's preferred language — users.locale (single source of truth;
    the same column drives event translations and the profile dashboard)."""
    try:
        row = (
            service_client()
            .table("users")
            .select("locale")
            .eq("id", user_id)
            .limit(1)
            .execute()
        )
        data = row.data[0] if row.data else {}
        return normalize_lang_code(data.get("locale"))
    except Exception:  # noqa: BLE001
        _LOG.exception("preferred_language_read_failed")
        return None


def set_user_preferred_language(user_id: str, lang: str) -> bool:
    code = normalize_lang_code(lang)
    if not code:
        return False
    try:
        service_client().table("users").update({"locale": code}).eq(
            "id", user_id
        ).execute()
        return True
    except Exception:  # noqa: BLE001
        _LOG.exception("preferred_language_write_failed")
        return False


def _nudge_allowed_by_cooldown(user_id: str) -> bool:
    """True when the cross-session cooldown has passed (or never nudged)."""
    try:
        from datetime import datetime, timedelta, timezone

        row = (
            service_client()
            .table("users")
            .select("lang_nudge_at")
            .eq("id", user_id)
            .limit(1)
            .execute()
        )
        data = row.data[0] if row.data else {}
        raw = data.get("lang_nudge_at")
        if not raw:
            return True
        last = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return datetime.now(timezone.utc) - last > timedelta(days=_NUDGE_COOLDOWN_DAYS)
    except Exception:  # noqa: BLE001
        _LOG.exception("lang_nudge_cooldown_read_failed")
        return False  # fail closed — a missed nudge is better than a nagging one


def _mark_nudged(user_id: str) -> None:
    try:
        from datetime import datetime, timezone

        service_client().table("users").update(
            {"lang_nudge_at": datetime.now(timezone.utc).isoformat()}
        ).eq("id", user_id).execute()
    except Exception:  # noqa: BLE001
        _LOG.exception("lang_nudge_mark_failed")


# ── session seeding ──────────────────────────────────────────────────────────

def seed_session_language(session_ctx: dict[str, Any], preferred: str | None) -> None:
    """Start the session in the user's preferred language (opening included).

    Live detection takes over from turn one — the preference only decides how
    the conversation starts, never pins it."""
    code = normalize_lang_code(preferred)
    if not code:
        return
    session_ctx["preferred_lang"] = code
    if code != "en":
        session_ctx.setdefault("lang", code)


# ── AI-authored copy (fallback: t() strings) ─────────────────────────────────

def _compose(system_goal: str, facts: list[str], fallback: str, lang: str | None) -> str:
    """One short Lana line AI-authored from true facts; fallback when no LLM."""
    try:
        from app.i18n import synth_language_directive
        from app.orchestrator.llm import llm_configured, llm_json, synthesizer_model

        if not llm_configured():
            return fallback
        lang_line = synth_language_directive(lang) if lang else None
        data = llm_json(
            model=synthesizer_model(),
            system=(
                "You are Lana, a warm neighborhood concierge. "
                f"{system_goal} Ground it ONLY in the facts given — one or two short "
                "sentences, warm and casual, never robotic. "
                + (f"{lang_line} " if lang_line else "")
                + 'Return JSON {"message": "..."}.'
            ),
            user_payload="\n".join(f"- {f}" for f in facts),
            max_tokens=160,
            temperature=0.4,
        )
        msg = str((data or {}).get("message") or "").strip() if isinstance(data, dict) else ""
        return msg or fallback
    except Exception:  # noqa: BLE001
        _LOG.exception("lang_pref_compose_failed")
        return fallback


def _compose_nudge_offer(observed: str, preferred: str) -> str:
    new_name = lang_display_name(observed)
    old_name = lang_display_name(preferred)
    return _compose(
        "Casually offer — as a by-the-way, not a demand — to switch the user's "
        "default app language, since they keep writing in a different language "
        "than their setting. Make clear it's optional and nothing changes if "
        "they say no.",
        [
            f"The user's saved app language is {old_name}",
            f"For the last few messages they have been writing in {new_name}",
            f"You are offering to make {new_name} their default",
        ],
        t("lang.nudge_offer", observed, new_name=new_name, old_name=old_name),
        observed,
    )


def _compose_pref_saved(new_pref: str, reply_lang: str | None) -> str:
    lang_name = lang_display_name(new_pref)
    return _compose(
        "Confirm you just saved the user's new default language. Mention they "
        "can change it anytime in Settings or by telling you.",
        [f"Their default language is now {lang_name} (already saved — state it as done)"],
        t("lang.pref_saved", reply_lang, lang_name=lang_name),
        reply_lang,
    )


# ── post-turn hook ───────────────────────────────────────────────────────────

def language_preference_post_turn(
    *,
    user_id: str | None,
    user_message: str,
    session_ctx: dict[str, Any],
    reply: str,
    is_anonymous: bool,
) -> str:
    """Runs once after the pipeline reply is composed. Two jobs:

    1. Apply a ``set_preferred_lang`` classifier verdict (an explicit "make
       Urdu my default" or an accept of the pending nudge) — persist it and
       confirm deterministically.
    2. Track preference↔observed divergence and append the one-time nudge
       offer when it has held for ``_DIVERGENCE_TURNS`` turns.

    Anonymous guests have no users row — mirroring still works for them via
    the session language; only the preference machinery is skipped."""
    if is_anonymous or not user_id:
        return reply
    try:
        slots = session_ctx.get("_discovery_slots")
        slots_for = str(session_ctx.get("_discovery_slots_for") or "")
        fresh = isinstance(slots, dict) and slots_for == str(user_message or "").strip()
        observed = normalize_lang_code(session_ctx.get("lang")) or "en"

        new_pref = normalize_lang_code(slots.get("set_preferred_lang")) if fresh else None
        nudge_pending = normalize_lang_code(session_ctx.get("lang_nudge_pending"))

        if new_pref:
            if set_user_preferred_language(user_id, new_pref):
                session_ctx["preferred_lang"] = new_pref
                session_ctx["lang_divergence_count"] = 0
                session_ctx["lang_nudge_pending"] = None
                session_ctx["lang_nudge_done"] = True
                confirm = _compose_pref_saved(new_pref, observed)
                # The synthesizer already answered the turn conversationally;
                # the deterministic confirm states the SAVE actually happened.
                return f"{reply}\n\n{confirm}" if reply else confirm
            return reply

        if nudge_pending:
            # Offer was out and this turn didn't accept it — a decline or a
            # topic change either way. Drop it for good this session.
            session_ctx["lang_nudge_pending"] = None
            session_ctx["lang_nudge_done"] = True
            return reply

        preferred = normalize_lang_code(session_ctx.get("preferred_lang"))
        if not preferred:
            return reply
        count = int(session_ctx.get("lang_divergence_count") or 0)
        count = count + 1 if observed != preferred else 0
        session_ctx["lang_divergence_count"] = count
        if (
            count >= _DIVERGENCE_TURNS
            and not session_ctx.get("lang_nudge_done")
            and _nudge_allowed_by_cooldown(user_id)
        ):
            session_ctx["lang_nudge_pending"] = observed
            session_ctx["lang_divergence_count"] = 0
            _mark_nudged(user_id)
            offer = _compose_nudge_offer(observed, preferred)
            return f"{reply}\n\n{offer}" if reply else offer
        return reply
    except Exception:  # noqa: BLE001
        _LOG.exception("language_preference_post_turn_failed")
        return reply
