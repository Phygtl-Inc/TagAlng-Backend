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

Fails OPEN: no kind, a generic kind ("place", "spot"), an "unsure" answer, or any error keeps
every row — today's behaviour. Only a sure "no" drops one.
"""

from __future__ import annotations

import json
import logging
import threading
from collections import OrderedDict
from typing import Any

logger = logging.getLogger(__name__)

_CACHE_MAX = 2000
_cache: "OrderedDict[tuple[str, str, str], bool]" = OrderedDict()
_lock = threading.Lock()

_PROMPT = """Someone asked their neighbours for a recommendation. You get their whole
REQUEST, a short KIND extracted from it, and the CATEGORIES of the recommendations neighbours
shared, with the NAMES of what was recommended under each when known. The KIND is a shorthand
and can be trimmed too far; the REQUEST is the authority on what they want. The names tell you
what a category actually holds.

For each category, decide whether a recommendation in it answers the request: would the
asker accept it as the thing they asked for?

- Judge the thing asked for only. Ignore qualifiers about quality, price, distance, audience,
  experience level or timing.
- A category answers when it is that thing, a more specific form of it, or the same thing
  under another name. When the thing is something to get rather than a place, a category
  also answers when it is where you would get it.
- A category does not answer when it is a different kind of thing that merely shares a
  qualifier or a setting with the request.
- When the request names no particular kind of thing, every category answers.
- Say "no" only when you are sure the category is a different kind of thing. Say "yes" when it
  could reasonably be what they mean, and "unsure" when you cannot tell.

Output ONLY JSON: {"fits": {"<category>": "yes" | "no" | "unsure"}}
"""


def _norm(text: Any) -> str:
    return " ".join(str(text or "").split()).casefold()


def _row_category(row: dict[str, Any]) -> str:
    return _norm(row.get("subject_category") or row.get("category") or row.get("reco_type"))


def _verdict(value: Any) -> bool | None:
    """The model's answer as keep (True) / drop (False) / no answer (None). Only a sure
    "no" drops a row; "unsure", anything unreadable and a missing answer all keep it."""
    if isinstance(value, bool):
        return value
    v = _norm(value)
    if v in ("yes", "true"):
        return True
    if v in ("no", "false"):
        return False
    return None


def fitting_categories(
    kind: str,
    categories: list[str],
    request: str | None = None,
    names: dict[str, list[str]] | None = None,
) -> dict[str, bool]:
    """{category: fits} for the categories the model (or the cache) could answer.
    Categories it could not answer are absent — the caller keeps those rows.

    Only YES is cached. A wrong "no" hides a real neighbour's recommendation, and a cached
    one hid it from every later identical ask on that worker until it restarted: one bad
    call, "project program" judged not to be a "program", made Pouya's RCC tip vanish for
    the rest of the evening (prod 2026-10-07). A "no" is asked again next time."""
    k = _norm(kind)
    r = _norm(request) if request and _norm(request) != k else ""
    named = {c: sorted(names[c])[:3] for c in categories if names and names.get(c)}

    def _key(c: str) -> tuple[str, str, str]:
        return (k, r, c + "|" + "|".join(named.get(c, [])))

    out: dict[str, bool] = {}
    todo: list[str] = []
    with _lock:
        for c in categories:
            hit = _cache.get(_key(c))
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
        payload: dict[str, Any] = {"kind": kind, "categories": todo}
        if r:
            payload["request"] = str(request)
        todo_named = {c: named[c] for c in todo if c in named}
        if todo_named:
            payload["names"] = todo_named
        data = llm_json(
            model=router_model(),
            system=_PROMPT,
            user_payload=json.dumps(payload, ensure_ascii=False),
            max_tokens=40 + 16 * len(todo),
            temperature=0.0,
        )
    except Exception:  # noqa: BLE001 — fail open: today's rows
        logger.warning("reco_kind_gate.failed kind=%r", kind, exc_info=True)
        return out
    fits = (data or {}).get("fits") if isinstance(data, dict) else None
    if not isinstance(fits, dict):
        return out
    by_norm = {_norm(c): _verdict(v) for c, v in fits.items()}
    with _lock:
        for c in todo:
            v = by_norm.get(c)
            if v is None:
                continue
            out[c] = v
            if v:
                _cache[_key(c)] = True
                _cache.move_to_end(_key(c))
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)
    return out


def keep_asked_kind(
    tips: list[dict[str, Any]], kind: str | None, request: str | None = None
) -> list[dict[str, Any]]:
    """The rows whose category IS the asked kind. A row with no category, or one the model
    could not answer for, is kept. `request` is the whole ask the kind was cut from; the
    model reads it so an over-trimmed kind cannot reject the right answer. Never raises."""
    if not tips or not str(kind or "").strip():
        return tips
    cats = sorted({c for c in (_row_category(t) for t in tips) if c})
    if not cats:
        return tips
    names: dict[str, set[str]] = {}
    for t in tips:
        n = _norm(t.get("subject_name") or t.get("reco_name"))
        if n:
            names.setdefault(_row_category(t), set()).add(n)
    fits = fitting_categories(
        str(kind), cats, request, {c: sorted(v) for c, v in names.items()}
    )
    kept = [t for t in tips if fits.get(_row_category(t), True)]
    if len(kept) != len(tips):
        logger.info(
            "reco_kind_gate kind=%r dropped=%s kept=%d",
            kind, sorted({_row_category(t) for t in tips if not fits.get(_row_category(t), True)}),
            len(kept),
        )
    return kept
