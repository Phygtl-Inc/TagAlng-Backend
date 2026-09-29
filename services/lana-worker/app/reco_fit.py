"""Why Lana sees a fit — the relevance line and the proof headlines on a results card.

Screen 07/08 (C-FIND-V2.3, Ankit 2026-09-28): the panel under the ask's chips reads in two
parts —

  1. ONE short paragraph on why this is relevant to THIS reader:
       "Parents in your Church circle and 3 toddler moms you follow all vouch for her —
        she's close by in Lake Nona, ORL."
  2. PROOF lines: who said what, then their own words.
       "2 Spanish-speaking neighbours said they cut in Spanish with every customer"
       + the quotes behind it.

Both are written by the model from the card's own facts and nothing else. Two guards keep
them honest, because both make claims about identifiable neighbours:
  · a proof headline must carry the real count of people behind it, or it is dropped and
    the card keeps its plain "N people mentioned X";
  · a cohort phrase ("Spanish-speaking neighbours") is only offered to the model when
    EVERY recommender of that subject is in the cohort — then any subset of them is, and
    "2 Spanish-speaking neighbours said…" cannot mislabel someone who is not.
Tenure ("going here for 6 years") is never invented: nothing records it, so it can only
appear when a neighbour's own quote says it.

Cost: ONE model call for the whole results page, never one per card, bounded by
FIT_TIMEOUT_S — a slow call leaves the cards exactly as they were, and keeps running in
the background so the cache has it for the next look. Cached per reader + subject + the
facts it was written from, so a changed card is rewritten and an unchanged one is free.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Any

logger = logging.getLogger(__name__)

# Total wait allowed from start to finish; the reply compose runs inside this window.
FIT_TIMEOUT_S = 3.0
FIT_CARDS = 5
_CACHE_MAX = 1000
_cache: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
_cache_lock = threading.Lock()
_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="reco-fit")

_PROMPT = """You write the "Why Lana sees a fit" text on neighbourhood recommendation cards.

For each card you get FACTS only. EVERY fit_line and headline MUST be written in the
"language" given — Spanish means Spanish, even though the facts and quotes are in English.
Write:

1. "fit_line": ONE short sentence (max ~25 words) on why this is relevant to THE READER,
   spoken TO them ("you", "your"): who around them vouches — their shared community by name
   ("people in your Mizu Sushi & Steakhouse community"), how many — never say more of them
   are in the reader's community than "recommenders_in_your_community" — what of their ask it
   answers, how close it is. Only what DOES fit: never list what is missing or not
   mentioned; if little fits, say the little that does. Never a rating, never "great" /
   "best" in your own voice — only what the facts show neighbours did or said.
2. "aspects": for each aspect, a "headline": who said what, in one line, STARTING with the
   exact number of people given ("2 neighbours said …"). Summarise what THEIR quotes say
   (e.g. "said they cut in Spanish with every customer"), never add anything not in them.
   If "cohort_label" is given for the card you MAY call them that ("2 Spanish-speaking
   parents said …"); otherwise say "neighbours". Never invent how long anyone has gone
   there, their background, or anything else not in the facts.

If a card has "recommended_by", the reader asked for a recommendation from a certain kind
of person and that many of the recommenders are one — the fit_line SHOULD lead with it:
exactly "people" of them (never more, never "all" unless people equals "of"), described by
"trait" and grounded in their "quotes" ("2 neighbours who grew up in Spain recommend him").
Never apply that trait to anyone else, and never to the place itself.

When a card's "write_fit_line" is false, set its "fit_line" to null.

{reader_rules}
For a neighbour card, a for_you quote is the EXACT words copied from that card's
"their_words" or an aspect's "quotes" — a plain string, never paraphrased.

Output ONLY JSON:
{"cards": [{"id": "<card id>", "fit_line": "...",
            "aspects": [{"key": "<aspect_key>", "headline": "..."}],
            "for_you": [{"claim": "c1", "line": "...", "quotes": ["<exact words>"]}]}]}
"""


def _prompt(has_claims: bool) -> str:
    # NOT str.format: the prompt is full of literal JSON braces.
    from app.reader_claims import COMPOSER_RULES

    return _PROMPT.replace(
        "{reader_rules}",
        COMPOSER_RULES if has_claims else 'There are no reader_claims: "for_you" is always [].',
    )


def fit_enabled() -> bool:
    return os.environ.get("LANA_RECO_FIT", "1").strip().lower() not in {"", "0", "false", "off"}


def _facts(card: dict[str, Any], ask_chips: list[str]) -> dict[str, Any]:
    contributors = card.get("contributors") or []
    vouch = int(card.get("vouch_count") or len(contributors) or 1)
    cohort = (card.get("cohorts") or [None])[0] or None
    # Only a cohort that covers EVERY recommender may name the people behind an aspect.
    cohort_label = (
        cohort.get("label")
        if isinstance(cohort, dict)
        and int(cohort.get("total") or 0) > 0
        and int(cohort.get("n") or 0) >= int(cohort.get("total") or 0)
        else None
    )
    return {
        "id": str(card.get("subject_ref") or card.get("title")),
        "name": card.get("title"),
        "category": card.get("category"),
        "distance": card.get("distance_text"),
        "shared_community": card.get("group_label") if card.get("group_kind") == "circle" else None,
        # How many of the recommenders actually share a community with the reader — the
        # line may never put more of them "in your community" than this.
        "recommenders_in_your_community": sum(
            1 for c in contributors if isinstance(c, dict) and c.get("shared_circles")
        ),
        "same_block": card.get("group_kind") == "block",
        "vouched_by": vouch,
        "reader_cohort": (
            {"label": cohort.get("label"), "n": cohort.get("n"), "of": cohort.get("total")}
            if isinstance(cohort, dict) else None
        ),
        "cohort_label": cohort_label,
        # The card's OWN chips when it has them — the recommender requirement is removed
        # from cards that do not meet it, so the line cannot claim it either.
        "asked_for": card["fit_chips"] if "fit_chips" in card else ask_chips,
        "recommended_by": _recommended_by(card),
        "matches_of_ask": (card.get("aspect_match") or {}).get("clauses_matched"),
        "parts_of_ask": (card.get("aspect_match") or {}).get("clauses_total"),
        # The neighbours' own words on this card — what a "For you" quote must come from.
        "their_words": [
            w for w in (
                str(c.get("description") or c.get("body") or "").strip()
                for c in contributors if isinstance(c, dict)
            ) if w
        ][:4],
        "aspects": [
            {
                "key": a.get("aspect_key"),
                "label": a.get("label"),
                "people": int(a.get("n_people") or 1),
                "from_reader_communities": int(a.get("n_shared_community") or 0),
                "quotes": (a.get("quotes") or [])[:3],
            }
            for a in (card.get("aspects") or [])
        ],
    }


def _recommended_by(card: dict[str, Any]) -> dict[str, Any] | None:
    """The recommender-standing fact (app/reco_authority.py): strong standing only, as
    people-counts and their own quotes. Null when the ask named no such requirement."""
    st = card.get("recommender_standing")
    if not isinstance(st, dict) or int(st.get("n_people") or 0) <= 0:
        return None
    return {
        "trait": st.get("trait"),
        "people": int(st["n_people"]),
        "of": int(st.get("of_people") or st["n_people"]),
        "quotes": [q.get("quote") for q in (st.get("quotes") or []) if isinstance(q, dict)][:3],
    }


def _has_reason(facts: dict[str, Any]) -> bool:
    """A relevance line needs something that DOES fit. Without one, the model fills the
    sentence with what is missing ("33 miles away and not in your community") — a card
    is better with no line than with that one."""
    return bool(
        facts.get("shared_community")
        or facts.get("same_block")
        or (facts.get("matches_of_ask") or 0) > 0
        or facts.get("reader_cohort")
        or facts.get("recommended_by")
    )


def _key(reader_id: str | None, lang: str, facts: dict[str, Any]) -> str:
    blob = json.dumps([reader_id, lang, facts], sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(blob.encode()).hexdigest()


def _cache_get(k: str) -> dict[str, Any] | None:
    with _cache_lock:
        v = _cache.get(k)
        if v is not None:
            _cache.move_to_end(k)
        return v


def _cache_put(k: str, v: dict[str, Any]) -> None:
    with _cache_lock:
        _cache[k] = v
        _cache.move_to_end(k)
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)


_DIGITS = re.compile(r"\d+")


def _norm(text: str) -> str:
    return " ".join(str(text or "").split()).casefold()


def _neighbour_quotes(raw: Any, facts: dict[str, Any]) -> list[dict[str, Any]]:
    """For-you quotes that appear verbatim (case and spacing aside) in this card's own
    words — a neighbour's description or an aspect quote. At most two."""
    words = [str(w) for w in facts.get("their_words") or []]
    for a in facts.get("aspects") or []:
        words.extend(str(q) for q in a.get("quotes") or [])
    haystack = [_norm(w) for w in words if w]
    out: list[dict[str, Any]] = []
    for q in (raw if isinstance(raw, list) else [])[:2]:
        text = str(q.get("text") if isinstance(q, dict) else q or "").strip().strip('"“”').strip()
        if len(text) < 8 or not any(_norm(text) in h for h in haystack):
            continue
        if any(_norm(x["text"]) == _norm(text) for x in out):
            continue
        out.append({"text": text})
    return out


def _valid(
    parsed: Any, facts_by_id: dict[str, dict[str, Any]],
    claims: list[dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Keep only what the facts support. {card id: {"fit_line", "headlines", "for_you"}}."""
    from app.google_reco_cards import ground_for_you

    out: dict[str, dict[str, Any]] = {}
    if not isinstance(parsed, dict):
        return out
    for c in parsed.get("cards") or []:
        if not isinstance(c, dict):
            continue
        cid = str(c.get("id") or "")
        facts = facts_by_id.get(cid)
        if not facts:
            continue
        line = str(c.get("fit_line") or "").strip()
        people = {a["key"]: a["people"] for a in facts["aspects"]}
        heads: dict[str, str] = {}
        for a in c.get("aspects") or []:
            if not isinstance(a, dict):
                continue
            key, text = str(a.get("key") or ""), str(a.get("headline") or "").strip()
            if key not in people or not text or len(text) > 160:
                continue
            # The count is the claim. A headline that does not carry the real number of
            # people behind it — or carries a different one — is dropped, not trusted.
            nums = [int(n) for n in _DIGITS.findall(text)]
            if not nums or nums[0] != people[key]:
                continue
            heads[key] = text
        for_you = ground_for_you(
            c.get("for_you"), claims or [], lambda raw, _f=facts: _neighbour_quotes(raw, _f)
        )
        out[cid] = {
            # A proven "For you" is a reason in its own right.
            "fit_line": line if 0 < len(line) <= 240 and (_has_reason(facts) or for_you) else None,
            "headlines": heads,
            "for_you": [
                {"claim_id": f["claim_id"], "line": f["line"], "claim_label": f["claim_label"],
                 "quotes": [q["text"] for q in f["quotes"]]}
                for f in for_you
            ],
        }
    return out


_LANG_NAMES = {"en": "English", "es": "Spanish", "pt": "Portuguese", "pt-br": "Portuguese (Brazil)"}


def _language_name(lang: str) -> str:
    code = (lang or "en").strip().lower()
    name = _LANG_NAMES.get(code) or _LANG_NAMES.get(code.split("-")[0])
    return f"{name} ({code})" if name else code


def _compose(
    facts_list: list[dict[str, Any]], lang: str, claims: list[dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    from app.orchestrator.llm import llm_configured, llm_json, router_model

    if not llm_configured():
        return {}
    data = llm_json(
        model=router_model(),
        system=_prompt(bool(claims)),
        user_payload=json.dumps(
            {
                "language": _language_name(lang),
                "reader_claims": [
                    {"id": c["id"], "claim": c["label"], "sayable": c["sayable"], "about": c["about"]}
                    for c in claims or []
                ],
                "cards": facts_list,
            },
            ensure_ascii=False,
        ),
        max_tokens=1200 if claims else 900,
        temperature=0.3,
    )
    out = _valid(data, {f["id"]: f for f in facts_list}, claims)
    if claims:
        # The same independent check as the Google cards: a real quote must also be ON
        # POINT — "the kids were hungry" never backs "has kids" (app/reader_claims.py).
        from app.reader_claims import keep_judged

        judged = [
            {"for_you": [{"claim_id": f.get("claim_id"), "quotes":
                          [{"text": q} for q in f.get("quotes") or []], "_f": f}
                         for f in r.get("for_you") or []]}
            for r in out.values()
        ]
        # The ask is the page's chips — what the reader just said, which a "For you" must
        # go beyond (asked for Italian, "loves Italian food" is not a reason).
        _ask = ", ".join(sorted({c for f in facts_list for c in f.get("asked_for") or []}))
        keep_judged(judged, claims, _ask)
        for r, j in zip(out.values(), judged):
            r["for_you"] = [
                {**x["_f"], "quotes": [q["text"] for q in x["quotes"]]} for x in j["for_you"]
            ]
    return out



def _apply(card: dict[str, Any], result: dict[str, Any]) -> None:
    if result.get("fit_line") and result.get("reason", True):
        card["fit_line"] = result["fit_line"]
    heads = result.get("headlines") or {}
    for a in card.get("aspects") or []:
        if a.get("aspect_key") in heads:
            a["headline"] = heads[a["aspect_key"]]
    if result.get("for_you"):
        card["for_you"] = list(result["for_you"])


def order_for_you(cards: list[dict[str, Any]]) -> None:
    """Claims RANK: within the same standing tier and the same group (circle / block /
    nearby), a card the reader's claims are PROVEN to fit moves up. In place and stable,
    so every other order the page already had — provenance, "from someone X", coverage —
    holds, and a reader with no claims keeps today's page exactly."""
    from app.reco_authority import NO_TIER_RANK, TIER_RANK
    from app.reco_cards import _GROUP_RANK

    cards.sort(key=lambda c: (
        TIER_RANK.get(str(c.get("_standing_tier")), NO_TIER_RANK),
        _GROUP_RANK.get(str(c.get("group_kind")), 3),
        0 if c.get("for_you") else 1,
    ))


class _Pending:
    """A compose in flight for one results page: started when the cards are built, joined
    after Lana's reply is written, so the two model calls overlap instead of stacking."""

    def __init__(self, todo, future, started):
        self.todo, self.future, self.started = todo, future, started


# Keyed by the id() of the cards list the caller stamped. Popped on finish; bounded so a
# path that never finishes (an exception between start and finish) cannot grow it.
_pending: dict[int, _Pending] = {}
_pending_lock = threading.Lock()


def start_fit(
    cards: list[dict[str, Any]],
    *,
    lang: str = "en",
    ask_chips: list[str] | None = None,
    reader_id: str | None = None,
) -> None:
    """Fill cached cards now; start ONE background compose for the rest. Never raises."""
    import time

    if not fit_enabled() or not cards:
        return
    from app.reader_claims import load_reader_claims

    claims = load_reader_claims(reader_id)
    chips = [str(c) for c in (ask_chips or []) if str(c or "").strip()][:6]
    todo: list[tuple[dict[str, Any], dict[str, Any], str]] = []
    for card in cards[:FIT_CARDS]:
        facts = _facts(card, chips)
        facts["write_fit_line"] = _has_reason(facts) or bool(claims)
        # The claims are part of what the line was written from: a changed profile writes
        # a new line rather than serving one made for who the reader used to be.
        k = _key(reader_id, lang, {**facts, "_claims": [c["label"] for c in claims]})
        hit = _cache_get(k)
        if hit is not None:
            _apply(card, hit)
        else:
            todo.append((card, facts, k))
    if not todo:
        return

    def run() -> dict[str, dict[str, Any]]:
        res = _compose([f for _, f, _ in todo], lang, claims)
        for _, f, k in todo:
            if f["id"] in res:
                _cache_put(k, res[f["id"]])
        return res

    future = _pool.submit(run)
    with _pending_lock:
        if len(_pending) > 64:
            _pending.clear()
        _pending[id(cards)] = _Pending(todo, future, time.monotonic())


def finish_fit(cards: list[dict[str, Any]] | None, *, budget_s: float = FIT_TIMEOUT_S) -> None:
    """Apply the compose started for these cards, waiting at most what is left of
    `budget_s` since it started. A late compose still lands in the cache. Never raises."""
    import time

    if not cards:
        return
    try:
        _finish(cards, budget_s)
    finally:
        # Cached results were applied at start, fresh ones just now — order by both.
        order_for_you(cards)


def _finish(cards: list[dict[str, Any]], budget_s: float) -> None:
    import time

    with _pending_lock:
        pend = _pending.pop(id(cards), None)
    if pend is None:
        return
    left = max(0.05, budget_s - (time.monotonic() - pend.started))
    try:
        res = pend.future.result(timeout=left)
    except FutureTimeout:
        logger.info("reco_fit: compose not back within %.1fs, cards shown without it", budget_s)
        return
    except Exception:  # noqa: BLE001 — a card without the line is today's card
        logger.warning("reco_fit: compose failed", exc_info=True)
        return
    for card, f, _ in pend.todo:
        if f["id"] in res:
            _apply(card, res[f["id"]])


def attach_fit(
    cards: list[dict[str, Any]],
    *,
    lang: str = "en",
    ask_chips: list[str] | None = None,
    reader_id: str | None = None,
    timeout_s: float = FIT_TIMEOUT_S,
) -> None:
    """start_fit + finish_fit back to back, for callers with nothing to overlap."""
    start_fit(cards, lang=lang, ask_chips=ask_chips, reader_id=reader_id)
    finish_fit(cards, budget_s=timeout_s)
