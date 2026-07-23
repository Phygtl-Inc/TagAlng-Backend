"""Identity for peer matching: seed from saved claims; AI-authored ask when unknown.

A signed-in user's public identity claims already answer "who are you?" — the
discovery funnel must never re-interrogate them (QA 2026-07-16: verified users
asked "Tell me one thing about you" on "show me nearby people"). Only when the
account truly has no claims do we ask, and that ask is composed by the model,
grounded in what the user actually said — the static line is a fallback only.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.auth import service_client

logger = logging.getLogger(__name__)

FALLBACK_ASKS = {
    "match": (
        "Tell me one thing about you — life stage, heritage, or what you're looking for — "
        "so I can match you better."
    ),
    "intro": (
        "Tell me one thing about you — life stage, heritage, or what you're looking for — "
        "then I can introduce you to a neighbor nearby."
    ),
}

_ASK_PROMPT = (
    "You are Lana, a warm neighborhood concierge. The user wants to meet nearby neighbors, "
    "but their profile has no identity claims yet, so you need ONE short question that draws "
    "out something matchable: life stage, heritage, language, interests, or what they're "
    "looking for.\n"
    "Ground the question in the user's actual message — never invent facts about them, never "
    "imply you already know them. If purpose is 'intro', the question should lead toward an "
    "introduction to a nearby neighbor.\n"
    'Return JSON only: {"ask": "<one warm sentence, under 160 characters>"}'
)


def identity_from_saved_claims(user_id: str | None, *, max_claims: int = 4) -> str | None:
    """Top public claim labels as an identity snippet ("Speaks Urdu · Mother"), or None."""
    if not user_id:
        return None
    try:
        res = (
            service_client()
            .table("user_identity_claims")
            .select("label")
            .eq("user_id", user_id)
            .eq("disclosure", "public")
            .is_("dismissed_at", "null")
            .order("confidence", desc=True)
            .limit(max_claims)
            .execute()
        )
        labels: list[str] = []
        for row in res.data or []:
            label = str(row.get("label") or "").strip()
            if label and label not in labels:
                labels.append(label)
        return " · ".join(labels) or None
    except Exception:
        logger.exception("identity_from_saved_claims_failed user_id=%s", user_id)
        return None


def compose_identity_ask(*, msg: str, purpose: str = "match") -> str:
    """One AI-authored question to learn something matchable; canned line only on failure."""
    fallback = FALLBACK_ASKS.get(purpose, FALLBACK_ASKS["match"])
    try:
        from app.orchestrator.llm import llm_configured, llm_json, router_model

        if not llm_configured():
            return fallback
        data = llm_json(
            model=router_model(),
            system=_ASK_PROMPT,
            user_payload=json.dumps(
                {"user_message": str(msg or "")[:400], "purpose": purpose},
                ensure_ascii=False,
            ),
            max_tokens=200,
            temperature=0.5,
        )
        ask = str((data or {}).get("ask") or "").strip() if isinstance(data, dict) else ""
        if 10 <= len(ask) <= 220:
            return ask
    except Exception:
        logger.exception("identity_ask_compose_failed")
    return fallback
