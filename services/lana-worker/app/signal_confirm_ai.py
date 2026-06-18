"""AI assist for signal confirm replies when short answers are ambiguous."""

from __future__ import annotations

import json
import logging
from typing import Any

from app.layer1_intents import SIGNAL_INTENT_BY_LINEAR
from app.orchestrator.llm import llm_configured, llm_json, router_model

_log = logging.getLogger(__name__)

_SYSTEM = (
    "You help Lana while she is collecting a neighborhood post (a swap / tip / meetup). "
    "She asked the user for ONE thing (pending_field). Classify what the user's reply means. "
    "Output only valid JSON: "
    '{"verdict":"answer"|"cancel"|"reroute",'
    '"field":"detail|stage|when_hint|where_hint|category|null",'
    '"value":"normalized string or null",'
    '"linear_intent":"looking.swap|sharing.swap|looking.meet|sharing.host|looking.tip|sharing.tip|null"}. '
    "verdict=answer: the reply actually answers the pending question — fill field + value. "
    "verdict=cancel: the user wants to STOP or drop the post "
    "(no, no don't, never mind, forget it, stop, cancel, not doing this). "
    "verdict=reroute: the reply is a DIFFERENT request or an unrelated question — NOT an answer and "
    "NOT a cancel (can you do this?, actually find me some moms, show my block log, what can you do, "
    "who matched with me). "
    "When unsure between answer and reroute, choose reroute — NEVER force an unrelated reply into the field. "
    "Field meanings: stage = kid vs adult / clothing size; when_hint = day/time (weekend, mornings); "
    "where_hint = a place, neighborhood, or cross-street; category = health|food|home|activities|education; "
    "detail = what they want or offer. "
    "Normalize size: adults/for adults/grown-up -> adult; 3T stays 3T. "
    "If user corrects from seeking to offering (give away my X) set linear_intent sharing.swap; "
    "to seeking (looking for X) set looking.swap. "
    "When pending_field is category and draft intent is tip_seek, keep tip_seek — never flip to sharing.tip."
)

_VALID_VERDICTS = {"answer", "cancel", "reroute"}


def interpret_signal_confirm_reply(
    draft: dict[str, Any],
    msg: str,
) -> dict[str, Any] | None:
    """Classify a confirm-phase reply.

    Returns None when the LLM is unavailable (caller uses the deterministic parser),
    otherwise a dict with "verdict" in {answer, cancel, reroute}. For "answer" it also
    carries field/value/linear_intent. An "answer" with no usable value falls back to
    None so the regex parser can still fill the slot.
    """
    if not llm_configured():
        return None
    text = str(msg or "").strip()
    if not text:
        return None
    payload = json.dumps(
        {
            "pending_field": draft.get("confirm_field"),
            "intent": draft.get("intent"),
            "linear_intent": draft.get("linear_intent"),
            "detail_so_far": draft.get("detail"),
            "user_reply": text,
        },
        ensure_ascii=False,
    )
    try:
        raw = llm_json(
            model=router_model(),
            system=_SYSTEM,
            user_payload=payload,
            max_tokens=256,
            temperature=0.1,
        )
    except Exception as exc:
        _log.warning("signal_confirm_ai failed: %s", exc)
        return None
    if not isinstance(raw, dict):
        return None
    verdict = str(raw.get("verdict") or "").strip().lower()
    if verdict not in _VALID_VERDICTS:
        return None
    if verdict in ("cancel", "reroute"):
        return {"verdict": verdict}
    # answer
    value = str(raw.get("value") or "").strip()
    if not value:
        return None  # claimed answer but nothing usable → let regex parser try
    raw["verdict"] = "answer"
    linear = str(raw.get("linear_intent") or "").strip() or None
    if linear and linear in SIGNAL_INTENT_BY_LINEAR:
        raw["signal_intent"] = SIGNAL_INTENT_BY_LINEAR[linear]
    return raw
