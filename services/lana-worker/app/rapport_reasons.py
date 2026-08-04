"""The "why am I being asked this?" line under a rapport ask.

The tile used to show only the teaser (``why_frame`` — "about FIT 407 Lake Nona…"),
which names the topic but never says what answering *does* for the user. This module
authors the honest reason in Lana's voice, grounded in the actual question:

    Q: "What do you enjoy most about your time at FIT 407 Lake Nona?"
    → "Knowing what you go for there lets me introduce you to neighbors who show up
       for the same thing."

Written per gap, never templated (AI-authored copy rule), and composed OFF the turn:
``open_semantic_gap`` fires this into a background thread after the row lands, and the
ranker kicks the same call when it serves a row that has no reason yet (legacy rows,
or a compose that failed). A miss is never fatal — readers fall back to the teaser.

English-canonical like the rest of the gap text; the translation rides along in
``question_i18n[lang].why_reason`` (see app/rapport_i18n.py).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

from app.auth import service_client
from app.lingo_guard import enforce, find_violations

logger = logging.getLogger(__name__)

# Hard ceiling — the card renders this as a single small italic line under a divider.
_MAX_LEN = 140

# The ranker re-attempts a missing reason on every home render, so a gap whose compose
# keeps failing would fire an LLM call per reload. One attempt per gap per window
# (in-process, best-effort across workers) — the row is written once and never retried
# after that, so this only ever throttles failures.
_ATTEMPT_COOLDOWN_S = 300.0
_last_attempt: dict[str, float] = {}


def _cooling_down(gap_row_id: str) -> bool:
    now = time.monotonic()
    last = _last_attempt.get(gap_row_id)
    if last is not None and (now - last) < _ATTEMPT_COOLDOWN_S:
        return True
    if len(_last_attempt) > 5000:  # keep the map bounded on a long-lived worker
        for key in [k for k, t in _last_attempt.items() if now - t >= _ATTEMPT_COOLDOWN_S]:
            _last_attempt.pop(key, None)
    _last_attempt[gap_row_id] = now
    return False

_REASON_PROMPT = """You write ONE line for a neighborhood app where a warm local concierge \
(Lana) helps neighbors meet the people and plans near them. She has just asked the user a \
question; your line goes right under it and answers the user's fair question: "why are you \
asking me this?"

Output ONLY JSON: {"reason": "..."}

What the answer actually lets her do — say one of these, whichever fits the question, and \
never promise anything else:
- introduce them to neighbors nearby who share that same thing
- suggest (or help them start) a small local get-together that fits it
- know which things happening near them are actually worth mentioning
- for a named place: find the other people who go there too

Rules:
- ONE sentence, under 120 characters. No question mark. No "By the way", no greeting.
- Speak TO the user, first person ("so I can…", "that way I can…", "it helps me…").
- Be SPECIFIC to THIS question — name the thing it's about. \
Weak: "It helps me get to know you better." \
Strong: "Knowing which nights you play lets me point you to neighbors who hit the courts then."
- Truthful and modest: it is about connecting them with people and plans nearby. NEVER claim \
it unlocks features, feeds ads, is shared with anyone, or is required.
- Never the words "circle", "block", "match", "mom", or "profile".
- English only — it is rendered into the user's language downstream."""


def _clean(reason: str) -> str:
    """Trim, de-quote, and lexicon-clean one composed reason (§14 guardrail).

    The line ships to a user's screen, so a leaked banned word ("on your block",
    "match you with moms") goes through the same enforce() rewrite as any reply —
    it only costs a call when the model actually leaked one."""
    out = " ".join(str(reason or "").split()).strip().strip('"').strip()
    if find_violations(out):
        out = enforce(out).text
    return out[:_MAX_LEN]


def compose_ask_reason(
    question: str,
    *,
    label: str | None = None,
    why_frame: str | None = None,
    grounding: bool = False,
) -> str | None:
    """AI-author the why-line for one ask. None when it can't be written.

    Returning None is deliberate: nothing is stored, so the card falls back to the
    teaser and the next read retries. A canned "it helps me know you better" line
    would be worse than the teaser it replaced.
    """
    q = str(question or "").strip()
    if not q:
        return None
    payload: dict[str, Any] = {"question": q}
    if label:
        payload["topic"] = str(label)[:80]
    if why_frame:
        payload["teaser_shown"] = str(why_frame)[:80]
    if grounding:
        # A grounding ask wants the specific local spot, so the honest reason is about
        # reaching the people who go there — not about the topic in the abstract.
        payload["note"] = (
            "This question asks WHICH specific local place they mean. Answering lets her "
            "find the other people who go there."
        )
    try:
        from app.orchestrator.llm import llm_configured, llm_json, router_model

        if not llm_configured():
            return None
        data = llm_json(
            model=router_model(),
            system=_REASON_PROMPT,
            user_payload=json.dumps(payload, ensure_ascii=False),
            max_tokens=120,
            temperature=0.4,
        )
    except Exception:  # noqa: BLE001 — a missing why-line must never break a gap
        logger.exception("rapport-reason: compose failed")
        return None
    reason = _clean(str((data or {}).get("reason") or ""))
    return reason or None


def _row_i18n(gap_row_id: str) -> dict[str, Any] | None:
    """The row's current question_i18n, so a later merge can't clobber other languages."""
    try:
        res = (
            service_client()
            .table("rapport_gaps")
            .select("question_i18n")
            .eq("gap_row_id", gap_row_id)
            .limit(1)
            .execute()
        )
        existing = ((res.data or [{}])[0] or {}).get("question_i18n")
        return existing if isinstance(existing, dict) else None
    except Exception:  # noqa: BLE001
        logger.debug("rapport-reason: i18n read failed for %s", gap_row_id)
        return None


def attach_ask_reason(
    gap_row_id: str,
    question: str,
    *,
    user_id: str | None = None,
    label: str | None = None,
    why_frame: str | None = None,
    grounding: bool = False,
) -> str | None:
    """Compose the why-line, store it on the gap, and render it into the user's
    language. Returns the stored reason, or None when nothing was written."""
    if not gap_row_id:
        return None
    reason = compose_ask_reason(
        question, label=label, why_frame=why_frame, grounding=grounding
    )
    if not reason:
        return None
    try:
        service_client().table("rapport_gaps").update({"why_reason": reason}).eq(
            "gap_row_id", gap_row_id
        ).execute()
    except Exception:
        # Pre-20260930 environments have no why_reason column — the card keeps
        # showing the teaser, exactly as it did before. Warn, never raise.
        logger.warning(
            "rapport-reason: store failed for %s", gap_row_id, exc_info=True
        )
        return None
    if user_id:
        _localize(gap_row_id, user_id, question, why_frame, reason)
    return reason


def _localize(
    gap_row_id: str,
    user_id: str,
    question: str,
    why_frame: str | None,
    reason: str,
) -> None:
    """Re-render this gap's texts (now including the reason) into the user's language."""
    try:
        from app.lang_pref import get_user_preferred_language
        from app.rapport_i18n import localize_gap_row

        lang = get_user_preferred_language(user_id)
        if not lang or lang == "en":
            return
        localize_gap_row(
            gap_row_id,
            question,
            why_frame,
            lang,
            _row_i18n(gap_row_id),
            why_reason=reason,
        )
    except Exception:  # noqa: BLE001 — localization is an upgrade, never a blocker
        logger.exception("rapport-reason: i18n render failed for %s", gap_row_id)


def attach_ask_reason_async(
    gap_row_id: str,
    question: str,
    *,
    user_id: str | None = None,
    label: str | None = None,
    why_frame: str | None = None,
    grounding: bool = False,
) -> None:
    """Fire-and-forget ``attach_ask_reason`` — every caller sits on a turn or a
    home render, and neither may wait on an LLM for a subtitle."""
    if not gap_row_id or not str(question or "").strip():
        return
    if _cooling_down(gap_row_id):
        return
    threading.Thread(
        target=attach_ask_reason,
        args=(gap_row_id, question),
        kwargs={
            "user_id": user_id,
            "label": label,
            "why_frame": why_frame,
            "grounding": grounding,
        },
        daemon=True,
        name="rapport-reason",
    ).start()
