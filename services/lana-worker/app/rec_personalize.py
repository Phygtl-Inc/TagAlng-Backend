"""Claim-personalized recommendations for the tip_seek fallback.

When a user asks Lana for a recommendation and no neighbor has vouched a match, the
tip_seek path falls back to a Google Places search. This module lets that fallback lean on
what Lana already knows about them (their identity claims) AND on the request itself: the
LLM turns the request + relevant claims into one or more candidate FILTERS, each a sharper
Places query plus the structured levers Google supports — a place `included_type`
(e.g. italian_restaurant), per-place boolean `required_attrs` to VERIFY (e.g.
servesVegetarianFood), and a one-line reframe naming the angle.

Design notes / guardrails:
- OpenAI-only via the shared `llm_json` helper — no new dependency, Lana keeps one voice.
- The LLM shapes QUERY STRINGS, a TYPE, ATTRIBUTES and PHRASING only. It never invents
  venues; place names come solely from Google Places (app/places.search_places).
- included_type + required_attrs are validated against app/place_types whitelists here, so a
  hallucinated enum can't reach (and break) the Places request.
- VERIFY, don't assert: required_attrs let the caller keep only places Google confirms match
  ("kid-friendly" ⇐ goodForChildren=true), instead of claiming it blindly.
- Self-only: `claims` are the requesting user's own — no cross-user disclosure leak. The
  reframe references the ANGLE ("veg-friendly"), not a private claim's raw text.
- Best-effort: returns None on empty claims/request, no LLM, a not-relevant verdict, or bad
  output, so the caller degrades cleanly to a plain nearby search.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.place_types import valid_attrs, valid_included_type

_log = logging.getLogger(__name__)

# Cap the claims we hand the model — keeps the prompt small and the shaping call cheap.
_MAX_CLAIMS = 20
# Cap candidate filters — hybrid UX asks the user to choose among at most a few angles.
_MAX_FILTERS = 4

_SYSTEM = """You help a neighborhood concierge personalize a LOCAL PLACES recommendation using \
the user's REQUEST and what she already knows about them (identity claims: diet, kids, \
interests, heritage, etc.).

Decide which claims (and the request itself) genuinely change which places would fit, then \
return ONE compact JSON object with exactly these keys:
{"relevant","base_query","filters"}

- base_query: a plain Google Places search string for the raw request with NO claim bias
  (e.g. request "nice restaurants" -> "restaurant"; "good coffee" -> "coffee shop").
- filters: an array (0-4) of DISTINCT candidate angles, each an object:
    {"label","query","included_type","required_attrs","reframe"}
  Include a filter ONLY when a claim (or the request) meaningfully narrows the pick. Return
  [] when nothing relevant applies (e.g. heritage has no bearing on finding a dentist).
    - label: 1-3 word chip text, e.g. "Vegetarian", "Kid-friendly", "Italian", "Top-rated".
    - query: Places text query folding in the angle, e.g. "vegetarian restaurant".
    - included_type: a valid Google Places (New) place TYPE that captures the angle, or null.
      Use one ONLY when a real type fits (e.g. "italian_restaurant","vegetarian_restaurant",
      "vegan_restaurant","mexican_restaurant","pizza_restaurant","cafe","bakery"). null if unsure.
    - required_attrs: array of Google per-place BOOLEAN fields that must be true for the place
      to genuinely match, chosen ONLY from: goodForChildren, menuForChildren,
      servesVegetarianFood, servesBreakfast, servesLunch, servesDinner, servesBrunch,
      servesCoffee, servesDessert, dineIn, takeout, delivery, outdoorSeating, reservable,
      goodForGroups, allowsDogs. [] if none apply. (E.g. a dad/kids -> ["goodForChildren"];
      vegetarian -> ["servesVegetarianFood"]. A cuisine like Italian needs no attr — the type
      carries it.)
    - reframe: ONE short warm first-person line naming the angle, e.g. "Since you're
      vegetarian, I kept these veg-friendly." Reference the ANGLE, never sensitive details.
- relevant: true if filters is non-empty, else false.

Prefer ONE filter when a single angle clearly dominates. Offer 2+ ONLY when genuinely distinct \
angles compete (e.g. a parent who is also vegetarian -> a "Kid-friendly" AND a "Vegetarian" \
filter). Never invent place names. Return only the JSON object."""


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


def _clean_filter(f: Any) -> dict[str, Any] | None:
    """Validate one LLM filter into a safe, ready-to-use shape (or None to drop it).
    included_type + required_attrs are whitelisted so a bad enum never reaches Places."""
    if not isinstance(f, dict):
        return None
    label = str(f.get("label") or "").strip()
    query = str(f.get("query") or "").strip()
    if not label or not query:
        return None
    return {
        "label": label[:24],
        "query": query,
        "included_type": valid_included_type(f.get("included_type")),
        "required_attrs": valid_attrs(f.get("required_attrs")),
        "reframe": (str(f.get("reframe") or "").strip() or None),
    }


def personalize_tip_query(
    *,
    request: str,
    category: str | None,
    claims: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    """Return {"relevant","base_query","filters":[{label,query,included_type,required_attrs,
    reframe}]} or None.

    None means "fall back to a plain search": no request/claims, LLM not configured, nothing
    relevant, or malformed output. Never raises. All included_type/required_attrs are already
    validated against the Places whitelists, so the caller can pass them straight through.
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
            max_tokens=400,
            temperature=0.2,
        )
        _log.info("rec_personalize.llm_result data=%r", data)
    except Exception:  # noqa: BLE001 — best-effort; caller falls back to plain search
        _log.exception("rec_personalize.llm_error")
        return None

    if not isinstance(data, dict):
        _log.info("rec_personalize.skip reason=bad_shape type=%s", type(data).__name__)
        return None
    raw_filters = data.get("filters")
    filters: list[dict[str, Any]] = []
    if isinstance(raw_filters, list):
        for f in raw_filters:
            cleaned = _clean_filter(f)
            if cleaned:
                filters.append(cleaned)
            if len(filters) >= _MAX_FILTERS:
                break
    base_query = str(data.get("base_query") or req).strip() or req
    if not filters:
        _log.info("rec_personalize.skip reason=no_relevant_filters")
        return None
    _log.info(
        "rec_personalize.ok base_query=%r filters=%s",
        base_query, [(f["label"], f["included_type"], f["required_attrs"]) for f in filters],
    )
    return {"relevant": True, "base_query": base_query, "filters": filters}
