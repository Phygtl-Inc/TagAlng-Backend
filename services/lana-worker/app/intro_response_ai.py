"""AI interpreter for a neighbor's reply to a pending introduction.

Accept / decline / block is a high-stakes decision (it connects two people or
blocks someone), so the meaning of the reply is read by the model first; the
regex parser in layer1_tier is only the offline/fallback path.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.orchestrator.llm import llm_configured, llm_json, router_model

_log = logging.getLogger(__name__)

_VALID = {"accept", "decline", "block", "unclear"}

_SYSTEM = (
    "Lana offered to introduce a neighbor and is reading the user's reply. "
    "Decide what the reply means by MEANING, not keywords. "
    'Output only JSON: {"action": "accept"|"decline"|"block"|"unclear"}. '
    "accept = they want the introduction to happen (yes, sure, go ahead, connect us, "
    "introduce us, sounds good, please do). "
    "decline = they do not want it now (no, not now, not yet, maybe later, pass, skip). "
    "block = they want to block/report this person or never be contacted by them. "
    "unclear = ambiguous, a question, or a different topic — when unsure use unclear, "
    "never guess accept or decline."
)


def interpret_nudge_response(msg: str, *, nickname: str | None = None) -> str | None:
    """Return 'accept'|'decline'|'block'|'unknown', or None if the model is unavailable.

    'unclear' from the model maps to 'unknown' so callers re-prompt instead of acting.
    """
    if not llm_configured():
        return None
    text = str(msg or "").strip()
    if not text:
        return None
    payload = json.dumps(
        {"neighbor": nickname or "a neighbor", "user_reply": text},
        ensure_ascii=False,
    )
    try:
        raw: Any = llm_json(
            model=router_model(),
            system=_SYSTEM,
            user_payload=payload,
            max_tokens=32,
            temperature=0.0,
        )
    except Exception as exc:  # pragma: no cover - network/LLM failure path
        _log.warning("intro_response_ai failed: %s", exc)
        return None
    if not isinstance(raw, dict):
        return None
    action = str(raw.get("action") or "").strip().lower()
    if action not in _VALID:
        return None
    return "unknown" if action == "unclear" else action
