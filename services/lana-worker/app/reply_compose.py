"""Shared AI composer for deterministic-path replies (LANA_LINGO §1: train,
don't hardcode).

The state machines (discovery gates, swap/tip flows, verify gates…) keep
deciding WHAT the turn must accomplish; this module authors HOW it is said.
Every call site passes its old canned line as ``fallback`` — demoted from the
voice of the product to the no-LLM/failure floor — so a dead model never
breaks a turn (tests and local dev run fallback-only via llm_configured()).

Grounding rule: the model may only say what ``facts`` support. It never
invents counts, names, places, or promises — the composing prompt carries the
voice rules (lingo constitution + the C+D self-disclosure rule), so
role/gender-aware address, the banned lexicon, and the honesty constraint
apply to these turns exactly like chat turns.

Language: when the session has a language, the reply is authored directly in
it (better first-pass quality); main.py's final-mile localizer still renders
every outbound reply unconditionally — no opt-out — so an in-language compose
just makes that render a near no-op. Fallback strings stay English-canonical.
"""

from __future__ import annotations

import logging
import os
from typing import Any

_LOG = logging.getLogger(__name__)

# Kill switch: LANA_AI_REPLIES=0 forces every call straight to its fallback.
_ENABLED_ENV = "LANA_AI_REPLIES"

# Per-process cache for context-free lines (cache=True call sites only):
# (goal, lang) -> composed text. Facts-dependent sites must not opt in.
_STATIC_CACHE: dict[tuple[str, str], str] = {}

_MAX_FACTS = 12


def _enabled() -> bool:
    return os.getenv(_ENABLED_ENV, "1").strip().lower() not in ("0", "false", "off")


def compose_reply(
    *,
    goal: str,
    facts: list[str] | None = None,
    fallback: str,
    session_ctx: dict[str, Any] | None = None,
    user_message: str | None = None,
    max_sentences: int = 2,
    cache: bool = False,
) -> str:
    """One Lana turn AI-authored from true facts; the canned line is the floor.

    goal          — what this turn must accomplish, in imperative prose
                    ("Ask for their 5-digit ZIP so the swap can be posted…").
    facts         — the ONLY ground truth the model may use (user's words,
                    real counts, flow state). Keep them short and literal.
    fallback      — the previous hardcoded line; returned verbatim whenever
                    the LLM is unconfigured, disabled, or fails.
    session_ctx   — session context; used for the session language.
    user_message  — the user's current message when the reply should engage
                    with their words (optional; becomes a fact).
    max_sentences — soft length cap passed to the prompt.
    cache         — ONLY for context-free lines (no user-specific facts):
                    memoizes per (goal, language) like i18n's render cache.
    """
    try:
        from app.orchestrator.llm import llm_configured, llm_json, synthesizer_model

        if not _enabled() or not llm_configured():
            return fallback

        from app.i18n import lang_display_name, session_lang

        lang = session_lang(session_ctx) if isinstance(session_ctx, dict) else None
        lang_norm = (lang or "en").strip().lower()

        cache_key = (goal, lang_norm)
        if cache:
            hit = _STATIC_CACHE.get(cache_key)
            if hit:
                return hit

        fact_lines = [str(f).strip() for f in (facts or []) if str(f or "").strip()]
        fact_lines = fact_lines[:_MAX_FACTS]
        if user_message and str(user_message).strip():
            fact_lines.append(f'The user just said: "{str(user_message).strip()[:300]}"')

        lang_directive = (
            f"Write ENTIRELY in {lang_display_name(lang_norm)} (warm, natural informal register)."
            if lang_norm != "en"
            else "Write in English."
        )

        from app.context import voice_rules

        system = (
            "You are Lana, a warm local concierge, mid-conversation with a "
            "neighbor. Author ONE chat turn that accomplishes the GOAL.\n"
            f"- Ground every word ONLY in the FACTS given — never invent "
            "counts, names, places, events, or promises.\n"
            f"- At most {max_sentences} short sentences. Warm and specific, "
            "never robotic, never a form.\n"
            f"- {lang_directive}\n"
            '- Return JSON {"message": "..."}.\n\n'
            + voice_rules()
        )
        payload = (
            f"GOAL: {goal.strip()}\n\nFACTS:\n"
            + ("\n".join(f"- {f}" for f in fact_lines) if fact_lines else "- (none)")
        )
        data = llm_json(
            model=synthesizer_model(),
            system=system,
            user_payload=payload,
            max_tokens=200,
            temperature=0.5,
        )
        msg = str((data or {}).get("message") or "").strip() if isinstance(data, dict) else ""
        if not msg:
            return fallback
        if cache:
            _STATIC_CACHE[cache_key] = msg
        return msg
    except Exception:  # noqa: BLE001 — the static line beats a failed turn
        _LOG.exception("reply_compose_failed (goal=%s)", goal[:80])
        return fallback
