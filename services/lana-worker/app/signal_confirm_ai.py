"""AI assist for signal confirm replies when short answers are ambiguous."""

from __future__ import annotations

import json
import logging
from typing import Any

from app.layer1_intents import SIGNAL_INTENT_BY_LINEAR
from app.orchestrator.llm import llm_configured, llm_json, router_model

_log = logging.getLogger(__name__)

_SYSTEM = (
    "You help Lana understand a neighbor's short reply while collecting a block post. "
    "Output only valid JSON with keys: "
    "understood (bool), field (detail|stage|when_hint|category|null), "
    "value (string to store, normalized), "
    "linear_intent (looking.swap|sharing.swap|looking.meet|sharing.host|looking.tip|sharing.tip|null). "
    "Normalize size: adults/for adults/grown-up -> adult; 3T stays 3T. "
    "If user corrects from seeking to offering (give away my X, swap my X), set linear_intent sharing.swap. "
    "If user corrects to seeking (looking for X), set linear_intent looking.swap. "
    "When pending_field is category and draft intent is tip_seek, keep tip_seek — do not set sharing.tip. "
    "Category answers (food, health, home) are field=category only, not intent flips. "
    "Rain coat, boots, jacket = swap not tip. "
    "If the reply clearly answers the pending question, set understood true."
)


def interpret_signal_confirm_reply(
    draft: dict[str, Any],
    msg: str,
) -> dict[str, Any] | None:
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
    if not isinstance(raw, dict) or not raw.get("understood"):
        return None
    value = str(raw.get("value") or "").strip()
    if not value:
        return None
    linear = str(raw.get("linear_intent") or "").strip() or None
    if linear and linear in SIGNAL_INTENT_BY_LINEAR:
        raw["signal_intent"] = SIGNAL_INTENT_BY_LINEAR[linear]
    return raw
