"""Claim-personalized recommendations for the tip_seek fallback.

When a mom asks Lana for a recommendation and no neighbor has vouched a match, the
tip_seek path falls back to a generic Google Places search. This module lets that
fallback lean on what Lana already knows about her (her own identity claims): the LLM
turns the request + relevant claims into a sharper Places query and a one-line reframe
that names the reasoning ("since you're vegetarian…").

Design notes / guardrails (see docs plan):
- OpenAI-only via the shared `llm_json` helper — no Gemini, no new dependency, Lana keeps one voice.
- The LLM shapes the QUERY STRING and PHRASING only. It never invents venues; place names come
  solely from Google Places (app/places.search_places).
- Self-only: `claims` are the requesting mom's own, so there is no cross-user disclosure leak.
- The reframe references the ANGLE ("veg-friendly"), not a private claim's raw text.
- Best-effort: returns None on empty claims, no LLM, a not-relevant verdict, or any bad output,
  so the caller always degrades cleanly to today's generic behavior.
"""

from __future__ import annotations

import json
import logging
from typing import Any

_log = logging.getLogger(__name__)

# Cap the claims we hand the model — a mom rarely has more than a handful of active
# claims, and this keeps the prompt small and the shaping call cheap.
_MAX_CLAIMS = 20

_SYSTEM = """You help a neighborhood concierge personalize a LOCAL PLACES recommendation using \
what she already knows about the user (their identity claims: heritage, diet, interests, kids, etc.).

You are given the user's recommendation REQUEST (e.g. "a pizza place", "a good dentist") and a list \
of their CLAIMS. Decide whether any claim genuinely changes what places would fit, then return ONE \
compact JSON object with exactly these keys:
{"relevant","places_query","reframe"}

- relevant: true ONLY if at least one claim meaningfully shapes this specific request
  (e.g. "vegetarian" for a restaurant, "toddler"/"has kids" for a place to eat or visit,
  "triathlete"/"into fitness" for food or activities). false for claims that don't affect the pick
  (e.g. heritage has no bearing on finding a dentist). When false, personalization is skipped.
- places_query: a short natural-language Google Places search string that folds in the relevant
  angle, e.g. "vegetarian-friendly pizza restaurant" or "kid-friendly restaurant with high chairs".
  When relevant is false, echo the plain request as the query.
- reframe: ONE short, warm first-person line that names the angle and invites widening, e.g.
  "Since you mentioned you're vegetarian, I leaned toward veg-friendly spots." Reference the ANGLE,
  never read back sensitive personal details verbatim. null when relevant is false.

Never invent place names. Return only the JSON object."""


def _clean_claims(claims: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    """Keep concept/label only (drop disclosure/embeddings); skip empty rows."""
    out: list[dict[str, str]] = []
    for c in claims or []:
        if not isinstance(c, dict):
            continue
        label = str(c.get("label") or "").strip()
        concept = str(c.get("concept") or "").strip()
        if not (label or concept):
            continue
        out.append({"concept": concept, "label": label or concept})
        if len(out) >= _MAX_CLAIMS:
            break
    return out


def personalize_tip_query(
    *,
    request: str,
    category: str | None,
    claims: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    """Return {"relevant","places_query","reframe"} or None.

    None means "fall back to the generic behavior": no claims, LLM not configured, the model
    judged nothing relevant, or the output was malformed. Never raises.
    """
    req = str(request or "").strip()
    trimmed = _clean_claims(claims)
    _log.info(
        "rec_personalize.begin request=%r category=%r raw_claims=%d usable_claims=%d labels=%s",
        req, category, len(claims or []), len(trimmed),
        [c.get("label") for c in trimmed],
    )
    if not req or not trimmed:
        _log.info("rec_personalize.skip reason=%s", "no_request" if not req else "no_claims")
        return None
    try:
        from app.orchestrator.llm import llm_configured, llm_json, router_model

        if not llm_configured():
            _log.info("rec_personalize.skip reason=llm_not_configured")
            return None
        payload = "\n\n".join(
            [
                f"REQUEST:\n{req}"
                + (f"\n(category hint: {str(category).strip()})" if category else ""),
                "CLAIMS:\n" + json.dumps(trimmed, ensure_ascii=False),
            ]
        )
        model = router_model()
        _log.info("rec_personalize.llm_call model=%s", model)
        data = llm_json(
            model=model,
            system=_SYSTEM,
            user_payload=payload,
            max_tokens=200,
            temperature=0.2,
        )
        _log.info("rec_personalize.llm_result data=%r", data)
    except Exception:  # noqa: BLE001 — best-effort; caller falls back to generic
        _log.exception("rec_personalize.llm_error")
        return None

    if not isinstance(data, dict):
        _log.info("rec_personalize.skip reason=bad_shape type=%s", type(data).__name__)
        return None
    if not bool(data.get("relevant")):
        _log.info("rec_personalize.skip reason=not_relevant")
        return None
    places_query = str(data.get("places_query") or "").strip()
    if not places_query:
        _log.info("rec_personalize.skip reason=empty_places_query")
        return None
    reframe_raw = data.get("reframe")
    reframe = str(reframe_raw or "").strip() or None
    _log.info("rec_personalize.ok places_query=%r reframe=%r", places_query, reframe)
    return {"relevant": True, "places_query": places_query, "reframe": reframe}
