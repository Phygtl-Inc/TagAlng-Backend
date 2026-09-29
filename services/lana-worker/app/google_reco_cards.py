"""Google places in the recommendation card template — the empty-neighbourhood answer.

An ask nobody nearby has recommended for ("find good turkish places near me") falls back to
Google Places. That used to render as a bare list — name, address, Open. The neighbour
answer renders as subject cards: "Why Lana sees a fit", evidence lines, quotes. This builds
the same card for the top Google places, from Google REVIEWS instead of neighbours, marked
`source: "google"` so the surface can say "From Google · not community-verified".

Design: docs/superpowers/specs/2026-09-29-google-reco-cards-design.md. The rules that
shape every line here:

  * Evidence is what reviewers WROTE. One model call for the page proposes, per place, a
    fit line and up to three things reviewers bring up that bear on the ask; every quote it
    cites must appear verbatim in the review it names, or it is dropped (code, not trust).
    An aspect left with no quote is dropped with it.
  * No counts (Asjid, 2026-09-29): Google returns at most five reviews of possibly
    thousands, so "2 reviewers mentioned" would read as a tally it is not. Headlines say
    "Google reviewers mention …"; the rating and total go in the header as Google's own.
  * No time budget (Asjid, 2026-09-29): slow beats empty. Per-call network timeouts only,
    so a hung connection cannot freeze the chat. Any failure → no cards this turn; the
    plain Google list still renders.
  * Never stored. Google's terms forbid storing review content, and this ctx is merged
    into the session and written to the DB — main.py pops CTX_KEY before that write.
  * Rows from our own communities are never Google cards: they are ours, and they keep
    their own "From your circles" heading.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

logger = logging.getLogger(__name__)

CTX_KEY = "google_reco_cards"
# The wider set the fallback searches and enriches; the reader's claims pick the order and
# the top MAX_CARDS are shown (docs/superpowers/specs/2026-09-29-claims-rank-for-you-design.md).
POOL_KEY = "google_place_pool"
POOL_SIZE = 6
MAX_CARDS = 3
MAX_ASPECTS = 3
_DETAILS_URL = "https://places.googleapis.com/v1/places/"
_DETAILS_FIELDS = "id,displayName,rating,userRatingCount,googleMapsUri,primaryTypeDisplayName,reviews"
_DETAILS_TIMEOUT_S = 15.0

_PROMPT = """You write the "Why Lana sees a fit" panel for places found on GOOGLE, for a
neighbour who asked for a recommendation. Nobody in their neighbourhood has recommended
anything yet, so all you have is each place's Google reviews. Use ONLY what the reviews say.

For each place write:
1. "fit_line": ONE short sentence (max ~25 words) on why this place answers what they
   asked for, from the reviews — spoken to the reader ("you"). Never say neighbours,
   community or friends recommend it: these are Google reviewers. Never a rating or
   "great"/"best" in your own voice. Only what DOES fit; null if the reviews show nothing.
2. "aspects": up to 3 things reviewers bring up that bear on the ask (a dish, the owner,
   the atmosphere, authenticity, the wait). Each:
   - "label": 1-4 words, their noun ("the lamb adana").
   - "headline": one line, "Google reviewers mention …" / "Google reviewers say …" — NO
     numbers, never a count of people.
   - "quotes": 1-2 items {"review": <review number>, "excerpt": "<words copied EXACTLY,
     character for character, from that review — 4 to 30 words>"}. Never paraphrase an
     excerpt, never join two sentences, never cite a review for words it does not contain.

EVERY fit_line, label and headline MUST be in the "language" given. Excerpts stay exactly
as written in the review, whatever language that is.

{reader_rules}
For a Google place, a for_you quote is {"review": <n>, "excerpt": "<exact words>"}, the
same rule as aspect quotes.

Output ONLY JSON:
{"places": [{"id": "<place id>", "fit_line": "…",
             "aspects": [{"label": "…", "headline": "…",
                          "quotes": [{"review": 1, "excerpt": "…"}]}],
             "for_you": [{"claim": "c1", "line": "…",
                          "quotes": [{"review": 2, "excerpt": "…"}]}]}]}
"""

_LANG_NAMES = {"en": "English", "es": "Spanish", "pt": "Portuguese", "pt-br": "Portuguese (Brazil)"}
_WS = re.compile(r"\s+")
_DIGIT = re.compile(r"\d")


def google_cards_enabled() -> bool:
    return os.environ.get("LANA_GOOGLE_RECO_CARDS", "1").strip().lower() not in {
        "", "0", "false", "off",
    }


def _norm(text: str) -> str:
    return _WS.sub(" ", str(text or "")).strip().casefold()


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(text or "").casefold()).strip("_")[:48] or "aspect"


def _language(lang: str) -> str:
    code = (lang or "en").strip().lower()
    name = _LANG_NAMES.get(code) or _LANG_NAMES.get(code.split("-")[0])
    return f"{name} ({code})" if name else code


def place_reviews(place_id: str, *, lang: str = "en") -> dict[str, Any] | None:
    """Rating, total, maps link and up to 5 reviews for one Google place. None on failure.

    Reviews come back in the reader's language when Google has a translation (`text`);
    that is the text quotes are checked against and shown, so the two always agree."""
    import httpx

    api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
    pid = str(place_id or "").strip()
    if not api_key or not pid or "/" in pid:
        return None
    try:
        with httpx.Client(timeout=_DETAILS_TIMEOUT_S) as client:
            res = client.get(
                _DETAILS_URL + pid,
                params={"languageCode": (lang or "en")},
                headers={"X-Goog-Api-Key": api_key, "X-Goog-FieldMask": _DETAILS_FIELDS},
            )
        data = res.json() if res.status_code == 200 else {}
    except Exception:  # noqa: BLE001 — one place failing never costs the others
        logger.warning("google_reco_cards.details_failed place_id=%s", pid, exc_info=True)
        return None
    if not isinstance(data, dict) or not data.get("id"):
        logger.info("google_reco_cards.details_empty place_id=%s status=%s", pid,
                    getattr(res, "status_code", "?"))
        return None
    reviews: list[dict[str, Any]] = []
    for r in data.get("reviews") or []:
        if not isinstance(r, dict):
            continue
        text = str(((r.get("text") or {}).get("text")) or "").strip()
        if not text:
            continue
        author = r.get("authorAttribution") or {}
        reviews.append({
            "text": text[:1500],
            "author": str(author.get("displayName") or "").strip() or None,
            "author_url": str(author.get("uri") or "").strip() or None,
        })
    return {
        "place_id": str(data["id"]),
        "rating": data.get("rating"),
        "rating_count": data.get("userRatingCount"),
        "maps_url": str(data.get("googleMapsUri") or "").strip() or None,
        "type_label": str(((data.get("primaryTypeDisplayName") or {}).get("text")) or "").strip() or None,
        "reviews": reviews,
    }


def _prompt(has_claims: bool) -> str:
    # NOT str.format: the prompt is full of literal JSON braces.
    from app.reader_claims import COMPOSER_RULES

    return _PROMPT.replace(
        "{reader_rules}",
        COMPOSER_RULES if has_claims else 'There are no reader_claims: "for_you" is always [].',
    )


def _compose(
    places: list[dict[str, Any]], *, ask: str, chips: list[str], lang: str,
    claims: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """ONE model call for the page. {} on any failure."""
    from app.orchestrator.llm import llm_configured, llm_json, router_model

    if not llm_configured():
        return {}
    payload = {
        "language": _language(lang),
        "what_they_asked_for": ask,
        "their_ask_chips": chips,
        "reader_claims": [
            {"id": c["id"], "claim": c["label"], "sayable": c["sayable"], "about": c["about"]}
            for c in claims or []
        ],
        "places": [
            {
                "id": p["place_id"],
                "name": p["name"],
                "reviews": [
                    {"review": i + 1, "text": r["text"]} for i, r in enumerate(p["reviews"])
                ],
            }
            for p in places
        ],
    }
    try:
        data = llm_json(
            model=router_model(),
            system=_prompt(bool(claims)),
            user_payload=json.dumps(payload, ensure_ascii=False),
            max_tokens=700,
            temperature=0.2,
        )
    except Exception:  # noqa: BLE001
        logger.warning("google_reco_cards.compose_failed", exc_info=True)
        return {}
    return data if isinstance(data, dict) else {}


def _ground_quotes(raw: Any, reviews: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The cited excerpts that appear verbatim (case and spacing aside) in the review they
    name, as the review's own words with the reviewer attributed. At most two."""
    quotes: list[dict[str, Any]] = []
    for q in (raw if isinstance(raw, list) else [])[:2]:
        if not isinstance(q, dict):
            continue
        try:
            idx = int(q.get("review")) - 1
        except (TypeError, ValueError):
            continue
        excerpt = str(q.get("excerpt") or "").strip().strip('"“”').strip()
        if not (0 <= idx < len(reviews)) or len(excerpt) < 8:
            continue
        review = reviews[idx]
        if _norm(excerpt) not in _norm(review["text"]):
            continue
        if any(_norm(x["text"]) == _norm(excerpt) for x in quotes):
            continue
        quotes.append({
            "text": excerpt,
            "author": review.get("author"),
            "author_url": review.get("author_url"),
        })
    return quotes


def ground_for_you(
    raw: Any, claims: list[dict[str, Any]], quotes_of: Any,
) -> list[dict[str, Any]]:
    """The "For you" items the evidence supports: a claim the reader actually holds, a line,
    and at least one verified quote (`quotes_of(item_quotes)` does the verifying for the
    surface). `claim_label` is set ONLY for a sayable claim — a quiet one (faith,
    heritage) may order and explain the place, never be named."""
    by_id = {c["id"]: c for c in claims or []}
    out: list[dict[str, Any]] = []
    for f in (raw if isinstance(raw, list) else [])[:2]:
        if not isinstance(f, dict):
            continue
        claim = by_id.get(str(f.get("claim") or ""))
        line = str(f.get("line") or "").strip()
        if not claim or not line or len(line) > 240:
            continue
        quotes = quotes_of(f.get("quotes"))
        if not quotes:
            continue
        out.append({
            "claim_id": claim["id"],
            "claim_label": claim["label"] if claim["sayable"] else None,
            "line": line,
            "quotes": quotes,
        })
    return out


def _compose_page(
    places: list[dict[str, Any]], *, ask: str, chips: list[str], lang: str,
    claims: list[dict[str, Any]] | None,
) -> dict[str, dict[str, Any]]:
    """One model call PER PLACE, all at once, then one judge call for the page's "For you".

    It was one call for the whole page, and it was the whole wait: 28.7s of a 33s turn for
    six places (live, 2026-09-29) — one long answer is written token by token. Six short
    ones in parallel take about as long as one. Grounded per place; any place whose call
    fails simply has no fit line or evidence."""
    if not places:
        return {}
    with ThreadPoolExecutor(max_workers=len(places), thread_name_prefix="gcompose") as pool:
        parsed = list(pool.map(
            lambda p: _compose([p], ask=ask, chips=chips, lang=lang, claims=claims), places
        ))
    items: list[dict[str, Any]] = []
    for p in parsed:
        items.extend(i for i in (p.get("places") or []) if isinstance(i, dict))
    grounded = ground({"places": items}, places, claims)
    if claims:
        from app.reader_claims import keep_judged

        keep_judged(list(grounded.values()), claims)
    return grounded


def ground(
    parsed: dict[str, Any], places: list[dict[str, Any]],
    claims: list[dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Keep only what the reviews support. {place id: {"fit_line", "aspects"}}.

    A quote survives only when its excerpt appears verbatim (case and spacing aside) in the
    review it cites; it is shown as the review's own words with the reviewer attributed.
    An aspect with no surviving quote, or a headline carrying a number, is dropped."""
    by_id = {p["place_id"]: p for p in places}
    out: dict[str, dict[str, Any]] = {}
    items = parsed.get("places") if isinstance(parsed, dict) else None
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        place = by_id.get(str(item.get("id") or ""))
        if not place:
            continue
        reviews = place["reviews"]
        aspects: list[dict[str, Any]] = []
        for a in (item.get("aspects") or [])[:MAX_ASPECTS]:
            if not isinstance(a, dict):
                continue
            label = str(a.get("label") or "").strip()[:60]
            headline = str(a.get("headline") or "").strip()[:160]
            # No counts, by decision: five reviews of thousands is not a tally.
            if not label or not headline or _DIGIT.search(headline):
                continue
            quotes = _ground_quotes(a.get("quotes"), reviews)
            if quotes:
                aspects.append({"label": label, "headline": headline, "quotes": quotes})
        line = str(item.get("fit_line") or "").strip()
        out[place["place_id"]] = {
            "fit_line": line if 0 < len(line) <= 240 else None,
            "aspects": aspects,
            "for_you": ground_for_you(
                item.get("for_you"), claims or [], lambda raw, _r=reviews: _ground_quotes(raw, _r)
            ),
        }
    return out


def _distance(origin: tuple[float, float] | None, p: dict[str, Any]) -> tuple[str | None, float | None]:
    try:
        lat, lng = float(p["lat"]), float(p["lng"])
    except (KeyError, TypeError, ValueError):
        return None, None
    if not origin:
        return None, None
    la1, lo1, la2, lo2 = map(math.radians, (origin[0], origin[1], lat, lng))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    meters = 2 * 6371000 * math.asin(math.sqrt(h))
    miles = meters / 1609.344
    text = f"{miles:.0f} mi" if miles >= 10 else f"{max(miles, 0.1):.1f} mi"
    return text, round(meters, 1)


def build_cards(
    places: list[dict[str, Any]],
    *,
    ask: str,
    chips: list[str],
    lang: str = "en",
    origin: tuple[float, float] | None = None,
    category: str | None = None,
    claims: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Google search rows in, RecoCardRow-shaped dicts out. [] on any failure.

    The whole pool is enriched; places a reader claim is PROVEN to serve lead (Google's
    order among equals), and the top MAX_CARDS are returned."""
    candidates = [
        p for p in places
        if isinstance(p, dict) and p.get("place_id") and not (p.get("community") or {}).get("member_count")
    ][:POOL_SIZE]
    if not candidates:
        return []
    with ThreadPoolExecutor(max_workers=POOL_SIZE, thread_name_prefix="gplace") as pool:
        details = list(pool.map(lambda p: place_reviews(str(p["place_id"]), lang=lang), candidates))
    enriched = []
    for p, d in zip(candidates, details):
        if d:
            enriched.append({**d, "name": str(p.get("name") or "").strip(), "row": p})
    if not enriched:
        return []
    with_reviews = [e for e in enriched if e["reviews"]]
    grounded = _compose_page(with_reviews, ask=ask, chips=chips, lang=lang, claims=claims)
    # Claims RANK, never filter: a place the reader's claims are proven to fit leads.
    # Stable, so Google's own order holds among equals.
    enriched.sort(key=lambda e: -len((grounded.get(e["place_id"]) or {}).get("for_you") or []))
    enriched = enriched[:MAX_CARDS]

    cards: list[dict[str, Any]] = []
    for e in enriched:
        g = grounded.get(e["place_id"], {})
        dist_text, dist_m = _distance(origin, e["row"])
        cards.append({
            "subject_ref": f"google:{e['place_id']}",
            "title": e["name"] or str(e["row"].get("name") or ""),
            "category": e.get("type_label") or category,
            "locality": str(e["row"].get("address") or "").strip() or None,
            "distance_text": dist_text,
            "distance_is_subject": dist_text is not None,
            "distance_meters": dist_m,
            "vouch_count": 0,
            "match_strength": 0.0,
            "group_kind": "google",
            "group_key": "google",
            "group_label": None,
            "contributors": [],
            "fit_chips": list(chips),
            "fit_line": g.get("fit_line"),
            "aspects": [
                {
                    "aspect_key": _slug(a["label"]),
                    "label": a["label"],
                    "n_people": 0,
                    "headline": a["headline"],
                    "quotes": [q["text"] for q in a["quotes"]],
                    "review_quotes": a["quotes"],
                }
                for a in g.get("aspects") or []
            ] or None,
            "for_you": [
                {
                    "line": f["line"],
                    "claim_label": f["claim_label"],
                    "quotes": [q["text"] for q in f["quotes"]],
                    "review_quotes": f["quotes"],
                }
                for f in g.get("for_you") or []
            ],
            "source": "google",
            "google": {
                "rating": e.get("rating"),
                "rating_count": e.get("rating_count"),
                "maps_url": e.get("maps_url"),
            },
            "tip_rec": False,
        })
    logger.info(
        "google_reco_cards pool=%d with_reviews=%d grounded=%d shown=%d aspects=%d "
        "claims=%d for_you=%d",
        len(candidates), len(with_reviews), len(grounded), len(cards),
        sum(len(c["aspects"] or []) for c in cards), len(claims or []),
        sum(len(c["for_you"]) for c in cards),
    )
    return cards


def stamp_google_cards(
    ctx: dict[str, Any],
    *,
    ask: str,
    chips: list[str],
    lang: str,
    origin: tuple[float, float] | None,
    category: str | None,
    claims: list[dict[str, Any]] | None = None,
) -> None:
    """Build the Google cards for this turn's fallback list onto ctx[CTX_KEY]. Never
    raises; clears the key when there is nothing to show, so a stale set never renders."""
    ctx[CTX_KEY] = None
    if not google_cards_enabled():
        return
    places = ctx.get(POOL_KEY) or ctx.get("google_place_suggestions")
    if not isinstance(places, list) or not places:
        return
    try:
        cards = build_cards(
            places, ask=ask, chips=chips, lang=lang, origin=origin, category=category,
            claims=claims,
        )
    except Exception:  # noqa: BLE001 — the plain list is still the answer
        logger.warning("google_reco_cards.build_failed", exc_info=True)
        return
    ctx[CTX_KEY] = cards or None


def hold_out_of_storage(*ctxs: dict[str, Any]) -> Any:
    """Remove the Google cards from every context about to be written to the DB and return
    them, so the caller can put them back for this turn's response only. Google's terms
    forbid storing review content; lana_sessions.context is where ctx is written."""
    held = None
    for c in ctxs:
        if isinstance(c, dict):
            val = c.pop(CTX_KEY, None)
            # The pool is Google place content too, and only ever needed this turn.
            c.pop(POOL_KEY, None)
            held = held or val
    return held
