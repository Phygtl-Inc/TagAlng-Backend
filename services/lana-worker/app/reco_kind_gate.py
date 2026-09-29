"""Keep only neighbour recommendations of the KIND the reader asked for.

Prod QA 2026-09-29: "find kid friendly restaurants near me" answered with Prestige
Barbershop. The tip search scores on the ask's words; the barbershop reco carries the tag
"kid friendly", nobody nearby had recommended a restaurant, so the qualifier alone won —
and because a neighbour row existed, the Google fallback (which would have found real
kid-friendly restaurants) never ran.

The ask decides what is IN the list (docs/superpowers/specs/2026-09-29-claims-rank-for-
you-design.md): a qualifier cannot stand in for the thing asked for. The kind comes from
reco_aspects.split_query_full's `subject_kind` ("restaurant"); each row carries its own
category ("barbershop"). Whether one IS a kind of the other is the model's call — one
small call for the page, cached per (kind, category) — never a word match: a "trattoria"
is a restaurant, a "barbershop" is not, and no list of words would know both.

Fails OPEN: no kind, a generic kind ("place", "spot"), or any error keeps every row —
today's behaviour.
"""

from __future__ import annotations

import json
import logging
import threading
from collections import OrderedDict
from typing import Any

logger = logging.getLogger(__name__)

_CACHE_MAX = 2000
_cache: "OrderedDict[tuple[str, str], bool]" = OrderedDict()
_lock = threading.Lock()

_PROMPT = """A neighbour asked for a recommendation of a certain KIND of thing. For each
category below, answer whether something in that category IS that kind — what they would
accept as an answer.

- "restaurant": "italian restaurant", "trattoria", "pizzeria", "diner", "steakhouse",
  "cafe" (food served) → true; "barbershop", "dentist", "bakery supply", "gym" → false.
- "dentist": "pediatric dentist", "orthodontist" → true; "pediatrician" → false.
- If the asked kind is generic ("place", "spot", "somewhere", "anything"), everything → true.
- Judge the kind only — never whether it is good, nearby or kid friendly.

Output ONLY JSON: {"fits": {"<category>": true|false}}
"""


def _norm(text: Any) -> str:
    return " ".join(str(text or "").split()).casefold()


def _row_category(row: dict[str, Any]) -> str:
    return _norm(row.get("subject_category") or row.get("category") or row.get("reco_type"))


def fitting_categories(kind: str, categories: list[str]) -> dict[str, bool]:
    """{category: fits} for the categories the model (or the cache) could answer.
    Categories it could not answer are absent — the caller keeps those rows."""
    k = _norm(kind)
    out: dict[str, bool] = {}
    todo: list[str] = []
    with _lock:
        for c in categories:
            hit = _cache.get((k, c))
            if hit is None:
                todo.append(c)
            else:
                out[c] = hit
    if not todo:
        return out
    try:
        from app.orchestrator.llm import llm_configured, llm_json, router_model

        if not llm_configured():
            return out
        data = llm_json(
            model=router_model(),
            system=_PROMPT,
            user_payload=json.dumps({"kind": kind, "categories": todo}, ensure_ascii=False),
            max_tokens=40 + 16 * len(todo),
            temperature=0.0,
        )
    except Exception:  # noqa: BLE001 — fail open: today's rows
        logger.warning("reco_kind_gate.failed kind=%r", kind, exc_info=True)
        return out
    fits = (data or {}).get("fits") if isinstance(data, dict) else None
    if not isinstance(fits, dict):
        return out
    by_norm = {_norm(c): v for c, v in fits.items() if isinstance(v, bool)}
    with _lock:
        for c in todo:
            if c in by_norm:
                out[c] = by_norm[c]
                _cache[(k, c)] = by_norm[c]
                _cache.move_to_end((k, c))
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)
    return out


def keep_asked_kind(tips: list[dict[str, Any]], kind: str | None) -> list[dict[str, Any]]:
    """The rows whose category IS the asked kind. A row with no category, or one the model
    could not answer for, is kept. Never raises."""
    if not tips or not str(kind or "").strip():
        return tips
    cats = sorted({c for c in (_row_category(t) for t in tips) if c})
    if not cats:
        return tips
    fits = fitting_categories(str(kind), cats)
    kept = [t for t in tips if fits.get(_row_category(t), True)]
    if len(kept) != len(tips):
        logger.info(
            "reco_kind_gate kind=%r dropped=%s kept=%d",
            kind, sorted({_row_category(t) for t in tips if not fits.get(_row_category(t), True)}),
            len(kept),
        )
    return kept
