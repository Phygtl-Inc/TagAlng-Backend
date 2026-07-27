"""Rolling conversation summary — memory hygiene for long sessions.

Today every turn loads the full session history and only trims at prompt-format
time; nothing carries the meaning of older turns forward (the context-rot risk
in the engineering doc §E). This module maintains a small
``lana_sessions.context.rolling_summary``:

  * runs as a FastAPI background task after the reply is sent (never blocks a turn);
  * fires only once the session passes _MIN_HISTORY messages, then re-summarizes
    every _STRIDE new messages — a quiet session costs nothing;
  * folds the previous summary + the next slice of older turns into ≤120 words,
    keeping names, stated facts, preferences, and decisions;
  * the last _KEEP_VERBATIM messages always stay verbatim — the summary only
    covers what the recent window no longer shows.

decide_turn reads ``rolling_summary`` straight off session context. The write
is a read-modify-write against the freshest stored context; it can race the
next turn's own persist, which is acceptable for advisory memory (the keys are
namespaced and re-derived on the following pass).
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

_MIN_HISTORY = 30   # don't bother below this many messages
_KEEP_VERBATIM = 12  # the recent window decide_turn sees verbatim
_STRIDE = 12         # re-summarize only after this many new older messages
_MAX_WORDS = 120
_SLICE_CHARS = 6000  # cap on the older-turns text fed to the summarizer


def _render_older(messages: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for m in messages:
        role = "user" if str(m.get("role") or "") == "user" else "lana"
        content = str(m.get("content") or "").strip().replace("\n", " ")
        if content:
            lines.append(f"{role}: {content[:300]}")
    text = "\n".join(lines)
    return text[-_SLICE_CHARS:]


def maybe_update_rolling_summary(session_id: str, user_id: str) -> None:
    """Background task entry point — safe to call after every lana turn."""
    try:
        from app.db import get_session_for_user, list_messages, update_session_context
        from app.orchestrator.llm import llm_configured, llm_json, router_model

        if not llm_configured():
            return
        history = list_messages(session_id)
        if len(history) < _MIN_HISTORY:
            return
        session = get_session_for_user(session_id, user_id)
        ctx = dict(session.get("context") or {})
        upto = int(ctx.get("rolling_summary_upto") or 0)
        cut = len(history) - _KEEP_VERBATIM
        if cut - upto < _STRIDE:
            return  # not enough new older material to be worth a call

        prev_summary = str(ctx.get("rolling_summary") or "").strip()
        older_slice = _render_older(history[max(upto, 0):cut])
        if not older_slice:
            return
        payload = {
            "previous_summary": prev_summary or None,
            "older_turns": older_slice,
        }
        data = llm_json(
            model=router_model(),
            system=(
                "You maintain a running memory of one chat between a user and "
                "Lana, a neighborhood concierge. Fold previous_summary and "
                "older_turns into ONE plain-English summary, max "
                f"{_MAX_WORDS} words. Keep only what matters later: the user's "
                "name, stated facts about them, preferences, places and "
                "communities they mentioned, things built or decided (events, "
                "listings, intros), and anything left unresolved. Drop "
                "greetings and small talk. Third person ('they'). Return JSON "
                '{"summary": "..."}'
            ),
            user_payload=json.dumps(payload, ensure_ascii=False),
            max_tokens=300,
            temperature=0.1,
        )
        summary = str((data or {}).get("summary") or "").strip()
        if not summary:
            return
        words = summary.split()
        if len(words) > _MAX_WORDS:
            summary = " ".join(words[:_MAX_WORDS])
        ctx["rolling_summary"] = summary
        ctx["rolling_summary_upto"] = cut
        update_session_context(session_id, ctx)
        logger.info(
            "rolling_summary_updated session=%s upto=%d words=%d",
            session_id, cut, min(len(words), _MAX_WORDS),
        )
    except Exception:
        logger.exception("rolling_summary_failed session=%s", session_id)
