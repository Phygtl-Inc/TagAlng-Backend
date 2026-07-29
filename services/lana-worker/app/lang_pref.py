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
import re
import threading
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
    except Exception:  # noqa: BLE001
        _LOG.exception("preferred_language_write_failed")
        return False
    # Re-render the user's queued rapport-tile questions into the new language in the
    # background (2–5 short renders), so the "By the way…" backlog is already in-language
    # by her next home render — no stale old-language questions after a switch.
    try:
        from app.rapport_i18n import localize_user_gaps_async

        localize_user_gaps_async(user_id, code)
    except Exception:  # noqa: BLE001 — tile i18n must never block saving the preference
        _LOG.exception("lang_pref_gap_rerender_kickoff_failed")
    return True


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


def _compose_guest_confirm(new_pref: str) -> str:
    """Guest (pre-signup) accept — one short warm confirm that the chat now
    continues in their language. No Settings mention (no account yet)."""
    lang_name = lang_display_name(new_pref)
    return _compose(
        "Confirm in ONE short warm sentence that you'll keep chatting in the "
        "user's language from here on. They don't have an account yet, so do "
        "NOT mention settings or saving anything.",
        [f"You will keep chatting in {lang_name}"],
        t("lang.guest_confirm", new_pref),
        new_pref,
    )


# ── identity claim from an accepted language ─────────────────────────────────

# The extractor stores languages spoken as ONE thread under this slug
# (vertex prompt: 'speak 7 languages' → concept "multilingual").
_LANGUAGE_THREAD_CONCEPT = "multilingual"
# Older/free-form language threads the extractor may have slugged differently.
_LANGUAGE_THREAD_HINT_RE = re.compile(r"\b(?:speak\w*|language\w*|lingual|polyglot)\b", re.I)


def _remember_language_claim(user_id: str, lang: str, source_quote: str | None) -> None:
    """Persist "speaks <language>" as an identity claim when the user ASKS for that
    language as their default — the same statement we already trust enough to flip
    their whole app language. Without this the fact never reached the profile: Lana
    would happily chat in German yet the user could never match on it unless they
    separately said "I speak German". Deterministic enrich, never clobber: an
    existing languages thread keeps its extractor-authored label and gains the new
    language in synonyms + details; a missing thread is created under the
    extractor's own slug so a later tile answer merges into it. English is skipped
    (the default — no matching signal), as is a language already on the thread."""
    code = normalize_lang_code(lang)
    if not user_id or not code or code == "en":
        return
    name = lang_display_name(code)
    if not name or name.lower() == code:
        return  # no display name for this code — never persist "Speaks xx"
    try:
        from app.claims_persist import upsert_claims
        from app.models import ExtractedClaim

        rows = (
            service_client()
            .table("user_identity_claims")
            .select("concept, label, synonyms, details")
            .eq("user_id", user_id)
            .is_("dismissed_at", "null")
            .limit(40)
            .execute()
        ).data or []
        thread: dict[str, Any] | None = None
        for row in rows:
            if not isinstance(row, dict):
                continue
            concept = str(row.get("concept") or "").strip()
            label = str(row.get("label") or "").strip()
            if concept.lower() == _LANGUAGE_THREAD_CONCEPT or _LANGUAGE_THREAD_HINT_RE.search(
                f"{concept.replace('_', ' ')} {label}"
            ):
                thread = row
                break
        if thread is not None:
            blob = " ".join(
                [
                    str(thread.get("label") or ""),
                    " ".join(str(s) for s in (thread.get("synonyms") or [])),
                    " ".join(str(d) for d in (thread.get("details") or [])),
                ]
            )
            if re.search(rf"\b{re.escape(name)}\b", blob, re.I):
                return  # already on the thread — nothing new to remember
        claim = ExtractedClaim(
            concept=str(thread.get("concept")) if thread else _LANGUAGE_THREAD_CONCEPT,
            # The upsert merge keeps the INCOMING label — hand back the stored one
            # so this deterministic write never clobbers the extractor's phrasing
            # ("Speaks Urdu and English" must not collapse to "Speaks German").
            label=str(thread.get("label")) if thread else f"Speaks {name}",
            confidence=1.0,
            synonyms=[name],
            details=[f"Speaks {name}"] if thread else [],
            source_quote=(source_quote or "").strip()[:120] or f"Asked Lana to chat in {name}",
            bucket="interest",
        )
        upsert_claims(user_id, [claim])
    except Exception:  # noqa: BLE001 — remembering is best-effort, never the turn's problem
        _LOG.exception("language_claim_upsert_failed")


def remember_language_claim_async(
    user_id: str, lang: str, source_quote: str | None = None
) -> None:
    """Fire-and-forget ``_remember_language_claim`` — callers sit on the reply path
    and the write embeds the claim (a model call)."""
    code = normalize_lang_code(lang)
    if not user_id or not code or code == "en":
        return
    threading.Thread(
        target=_remember_language_claim,
        args=(user_id, code, source_quote),
        daemon=True,
        name="lang-claim",
    ).start()


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

    Anonymous guests have no users row, so nothing persists for them — but the
    session-level accept still works: the offer they said yes to flips the
    session language and clears the offer state. Before this, a guest accept
    was a full no-op (nothing saved, the armed offer never expired because the
    TTL decrement lives here too), so every "sí, hablemos en español" produced
    another warm ack forever — the endless language loop in the signup chats.
    The accepted code is stashed as ``guest_locale`` and written to the users
    row on the first post-signup turn of the same session."""
    try:
        slots = session_ctx.get("_discovery_slots")
        slots_for = str(session_ctx.get("_discovery_slots_for") or "")
        fresh = isinstance(slots, dict) and slots_for == str(user_message or "").strip()
        observed = normalize_lang_code(session_ctx.get("lang")) or "en"

        new_pref = normalize_lang_code(slots.get("set_preferred_lang")) if fresh else None
        nudge_pending = normalize_lang_code(session_ctx.get("lang_nudge_pending"))

        if new_pref and (is_anonymous or not user_id):
            # Session-only accept for guests: speak the language now, remember
            # it for signup, and disarm the offer so it can't loop. No DB row
            # to write. The FIRST accept gets an explicit confirm PREPENDED to
            # the turn's reply — on funnel turns the reply is a deterministic
            # step question, so without the confirm the accept lands silently
            # and the user keeps repeating it (QA 2026-07-23, transcript #3).
            # Repeats stay silent: the funnel question alone re-anchors.
            already_settled = (
                normalize_lang_code(session_ctx.get("guest_locale")) == new_pref
            )
            session_ctx["preferred_lang"] = new_pref
            session_ctx["lang"] = new_pref
            session_ctx["guest_locale"] = new_pref
            session_ctx["lang_divergence_count"] = 0
            session_ctx["lang_nudge_pending"] = None
            session_ctx["lang_nudge_done"] = True
            session_ctx["lang_offer_langs"] = None  # None, not pop — a popped key resurrects from the stored ctx on merge
            session_ctx["lang_offer_ttl"] = None
            if already_settled:
                return reply
            confirm = _compose_guest_confirm(new_pref)
            return f"{confirm}\n\n{reply}" if reply else confirm

        if new_pref:
            if set_user_preferred_language(user_id, new_pref):
                session_ctx["preferred_lang"] = new_pref
                session_ctx["guest_locale"] = None
                session_ctx["lang_divergence_count"] = 0
                session_ctx["lang_nudge_pending"] = None
                session_ctx["lang_nudge_done"] = True
                session_ctx["lang_offer_langs"] = None  # None, not pop — a popped key resurrects from the stored ctx on merge
                session_ctx["lang_offer_ttl"] = None
                # Speak the chosen language IMMEDIATELY — the accept itself is often
                # typed in the OLD language ("lets talk in urdu" is English), so the
                # session flips to the new preference rather than mirroring the accept.
                session_ctx["lang"] = new_pref
                # Asking for a language IS stating you speak it — remember it as an
                # identity claim so it becomes matchable, not just a locale.
                remember_language_claim_async(user_id, new_pref, user_message)
                confirm = _compose_pref_saved(new_pref, new_pref)
                # The synthesizer already answered the turn conversationally;
                # the deterministic confirm states the SAVE actually happened.
                return f"{reply}\n\n{confirm}" if reply else confirm
            return reply

        # A rapport-concierge language offer stays live for a few turns (the accept is
        # often a short negotiation: "yes" → "which one?" → "urdu"), then expires.
        # Runs for guests too — an offer must never stay armed forever.
        # Unlike the divergence nudge, letting it lapse does NOT mark the nudge done.
        if session_ctx.get("lang_offer_langs"):
            ttl = int(session_ctx.get("lang_offer_ttl") or 0) - 1
            if ttl <= 0:
                session_ctx["lang_offer_langs"] = None  # None, not pop — a popped key resurrects from the stored ctx on merge
                session_ctx["lang_offer_ttl"] = None
            else:
                session_ctx["lang_offer_ttl"] = ttl

        if is_anonymous or not user_id:
            # No users row — the divergence nudge and preference persistence
            # below need one. Session mirroring already happened upstream.
            return reply

        # A language accepted while still a guest, carried across signup in the
        # same session: persist it onto the fresh account once, unless the user
        # has since picked something themselves.
        guest_locale = normalize_lang_code(session_ctx.get("guest_locale"))
        if guest_locale:
            session_ctx["guest_locale"] = None
            # The guest accepted this language pre-signup — now that a users row
            # exists, remember the claim regardless of whether the locale write
            # below is skipped (a self-chosen pref doesn't unsay "I speak X").
            remember_language_claim_async(user_id, guest_locale)
            saved = get_user_preferred_language(user_id)
            if guest_locale != saved and saved in (None, "en"):
                set_user_preferred_language(user_id, guest_locale)
                session_ctx["preferred_lang"] = guest_locale

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
