"""Circles · Stage 2 grounding + profile surface — master spec §A.1/§G, Place Profile §3.

A suggested affiliation (parked by circles_capture) becomes a confirmed, grounded
circle here:

  ground_options()     -> 2-3 real nearby places for the "which spot?" chips
                          (Google search biased to the user's block, type-mapped)
  ground_affiliation() -> server-side upsert of the canonical places row + link.
                          The client sends ONLY the tapped google_place_id; every
                          place field comes from Google via places.place_details()
                          — a caller can never mint or rename a shared place.

Immediately after grounding, one warm enrichment question is queued on the rapport
tile ("What do you enjoy most at {place}?" — AI-authored, static fallback), whose
answer becomes a place-tagged affinity claim (§4.3): rapport_gaps.place_ref flows
onto the claim in the record-answer path.

Also the /lana/circles profile CRUD (§G): list / add / update / remove. Removal is
soft-delete (dismissed_at) — staleness is user-curated, no auto-decay.
"""

from __future__ import annotations

import logging
import os
import re
import unicodedata
from typing import Any

from app.auth import service_client
from app.circles_capture import CIRCLE_TYPES, upsert_place_feature

logger = logging.getLogger(__name__)

# circle_type -> (Places includedType, search keyword) for grounding offers (§A.1.2).
# includedType=None means keyword-only search (point_of_interest is not searchable
# as a strict type across categories).
_TYPE_SEARCH: dict[str, tuple[str | None, str]] = {
    "fitness": ("gym", "gym"),
    # includedType must be a Table-A type in Places API (New); "place_of_worship"
    # is Table-B and silently 400s the whole search. Faith spans several Table-A
    # types (church/mosque/synagogue/hindu_temple), so keyword-only.
    "faith": (None, "church mosque synagogue temple"),
    "school": ("school", "school"),
    "kids_activity": (None, "kids activity center"),
    "neighborhood": (None, "community center"),
    "hobby": (None, "club"),
    "support": (None, "community support group"),
    "heritage": (None, "cultural center"),
    "friends": (None, "cafe"),
    "other": (None, ""),
}

# Google place types -> our advisory place_type (O6: canonical type on the place,
# the user's own framing stays on their affiliation's circle_type).
_GOOGLE_TYPE_MAP: tuple[tuple[str, str], ...] = (
    ("gym", "fitness"),
    ("fitness_center", "fitness"),
    ("sports_complex", "fitness"),
    ("swimming_pool", "fitness"),
    ("church", "faith"),
    ("mosque", "faith"),
    ("synagogue", "faith"),
    ("hindu_temple", "faith"),
    ("place_of_worship", "faith"),
    ("school", "school"),
    ("preschool", "school"),
    ("primary_school", "school"),
    ("secondary_school", "school"),
    ("university", "school"),
    ("child_care_agency", "kids_activity"),
    ("playground", "kids_activity"),
    ("amusement_center", "kids_activity"),
    ("community_center", "neighborhood"),
    ("cultural_center", "heritage"),
)

_FEATURE_NOTE_RE = re.compile(r"\b([a-z][a-z0-9_]{1,63})=([^;]+)")

# Lingo §2/§14 backstop for AI-authored tile questions: the prompt forbids the
# backstage words, but models still leak them ("…the cycling activities on your
# block?", seen in dev) — a leaked question falls back to the clean template.
_BANNED_LEXICON_RE = re.compile(r"\b(moms?|mommy|mama|blocks?|circles?)\b", re.IGNORECASE)


def _lexicon_clean(*texts: str) -> bool:
    return not any(_BANNED_LEXICON_RE.search(t or "") for t in texts)

_PLACE_QUESTION_PROMPT = """You write ONE warm question for a neighborhood app user who \
just pinned a community place they belong to. Goal: learn what they personally value there \
— the answer becomes a matchable interest (e.g. "the Saturday long runs", "the pool", \
"the coffee after class").

Output ONLY JSON: {"question": "...", "teaser": "about <place>…"}

Rules:
- Name the concrete place. Short (<120 chars), warm, open — never yes/no.
- Ask what they enjoy / are into THERE (activity, program, rhythm) — a matchable facet, \
not an opinion poll or origin story.
- teaser: 2-5 word lead-in ending with "…".
- English only (rendered into the user's language downstream)."""


def _place_affinity_question(place_name: str) -> tuple[str, str]:
    """AI-authored per the lingo rules; the spec's own example line as fallback."""
    fallback = (
        f"What do you enjoy most at {place_name}?",
        f"about {place_name}…",
    )
    try:
        from app.orchestrator.llm import llm_configured, llm_json, router_model

        if not llm_configured():
            return fallback
        data = llm_json(
            model=router_model(),
            system=_PLACE_QUESTION_PROMPT,
            user_payload=f'place: "{place_name}"',
            max_tokens=120,
            temperature=0.4,
        )
        question = str((data or {}).get("question") or "").strip()
        teaser = str((data or {}).get("teaser") or "").strip()
        if question and _lexicon_clean(question, teaser):
            return question[:160], (teaser or fallback[1])[:80]
    except Exception:
        logger.exception("place_affinity_question_llm_failed")
    return fallback


def _advisory_place_type(google_types: list[str], circle_type_hint: str | None) -> str | None:
    types = set(google_types or [])
    for gtype, ours in _GOOGLE_TYPE_MAP:
        if gtype in types:
            return ours
    if circle_type_hint in CIRCLE_TYPES:
        return circle_type_hint
    return None


def upsert_canonical_place(
    details: dict[str, Any],
    *,
    circle_type_hint: str | None = None,
    created_by: str | None = None,
) -> str | None:
    """Idempotent on google_place_id. Refreshes Google-sourced fields on conflict but
    NEVER touches created_by / source / claimed_by / place_type of an existing row
    (first grounder seeds; owner columns are Phase 2). Returns places.id."""
    pid = str(details.get("place_id") or "").strip()
    name = str(details.get("name") or "").strip()
    if not pid or not name:
        return None
    sb = service_client()
    refresh = {
        "name": name,
        "address": details.get("address"),
        "lat": details.get("lat"),
        "lng": details.get("lng"),
        "zip": details.get("zip"),
    }
    existing = (
        sb.table("places").select("id").eq("google_place_id", pid).limit(1).execute()
    )
    if existing.data:
        place_id = str(existing.data[0]["id"])
        sb.table("places").update(refresh).eq("id", place_id).execute()
        return place_id
    row = {
        **refresh,
        "google_place_id": pid,
        "place_type": _advisory_place_type(details.get("types") or [], circle_type_hint),
        "source": "user_grounded",
        "created_by": created_by,
    }
    res = sb.table("places").insert(row).execute()
    if res.data:
        return str(res.data[0].get("id") or "") or None
    # Concurrent grounding of the same place — reselect.
    resel = sb.table("places").select("id").eq("google_place_id", pid).limit(1).execute()
    return str(resel.data[0]["id"]) if resel.data else None


def _own_affiliation(user_id: str, affiliation_id: str) -> dict[str, Any] | None:
    res = (
        service_client()
        .table("circle_affiliations")
        .select("id, circle_type, circle_key, detail, status, place_ref, place_name")
        .eq("id", affiliation_id)
        .eq("user_id", user_id)
        .is_("dismissed_at", "null")
        .limit(1)
        .execute()
    )
    return (res.data or [None])[0]


# How far past the block bias a NAMED search may reach (metres). Their gym or
# temple is often a couple of towns over; a nearby-only search then "fails" and
# we start offering strangers' places instead.
_WIDE_SEARCH_RADIUS_M = 60000.0

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def _norm_place_text(text: str) -> str:
    """Fold accents/punctuation/case for name comparison ("EōS Fitness" → "eos fitness").

    Apostrophes are DELETED rather than spaced, so the possessive a user types
    still matches how Google spells it ("St. Luke's" ≡ "St Lukes")."""
    stripped = (
        unicodedata.normalize("NFKD", str(text or ""))
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
        .replace("'", "")
    )
    return " ".join(_NON_ALNUM_RE.sub(" ", stripped).split())


def _name_hit(claimed: str, candidate: str) -> bool:
    """Does this Google result actually BEAR the name the user gave us?

    Containment either way on normalized text — the same certainty bar
    match_grounding_candidate() uses to accept a tap, so "orangetheory" matches
    "OrangeTheory Narcoossee" while "Fitness CF" does not match "Crunch Fitness".
    Deliberately not fuzzy: a near-miss quietly pins the user to the WRONG place,
    which is the §F trust failure this whole flow exists to prevent."""
    want, got = _norm_place_text(claimed), _norm_place_text(candidate)
    if len(want) < 3 or not got:
        return False
    return want in got or got in want


_PLACE_NAME_PROMPT = """You pull the venue name out of what a neighborhood-app user \
said about a community they attend.

Output ONLY JSON: {"place_name": "..."} — or {"place_name": null}

Rules:
- place_name = the business/organization/venue name they actually SAID, verbatim and \
nothing else ("I go to the gym at Fitness CF" -> "Fitness CF"; "our church is St. \
Luke's" -> "St. Luke's").
- null when they named only an activity or the KIND of place ("my gym", "we play futsal \
on Sundays", "my Tuesday spin class", "our church").
- NEVER invent, complete, or guess a name they did not say. Never answer with the \
activity word itself."""


def _resolve_place_name(user_id: str, affiliation: dict[str, Any]) -> str:
    """The venue name the user said for this community, "" when they named none.

    Normally the extractor supplies it at capture. This resolves (once, then
    persists) the rows it didn't: affiliations captured before place_name existed,
    and messages where the extractor missed it. '' is a REAL answer meaning "they
    named no venue" — stored so we never re-ask the model; null means unresolved."""
    stored = affiliation.get("place_name")
    if stored is not None:
        return str(stored).strip()

    phrase = _FEATURE_NOTE_RE.sub("", str(affiliation.get("detail") or "")).strip(" ;")
    name = ""
    if phrase:
        try:
            from app.orchestrator.llm import llm_configured, llm_json, router_model

            if not llm_configured():
                return ""  # unresolved — retried on the next serve, never persisted
            data = llm_json(
                model=router_model(),
                system=_PLACE_NAME_PROMPT,
                user_payload=f'they said: "{phrase}"',
                max_tokens=60,
                temperature=0.0,
            )
            name = str((data or {}).get("place_name") or "").strip()[:120]
            # A model that echoes the phrase back has named nothing.
            if name and _norm_place_text(name) == _norm_place_text(phrase):
                name = ""
        except Exception:
            logger.exception("place_name_resolve_failed aff=%s", affiliation.get("id"))
            return ""
    affiliation["place_name"] = name
    aff_id = str(affiliation.get("id") or "")
    if aff_id:
        try:
            service_client().table("circle_affiliations").update(
                {"place_name": name}
            ).eq("id", aff_id).eq("user_id", user_id).execute()
        except Exception:
            logger.exception("place_name_persist_failed aff=%s", aff_id)
    return name


def ground_options(
    user_id: str,
    affiliation: dict[str, Any],
    *,
    block_id: str | None,
    query: str | None = None,
) -> list[dict[str, Any]]:
    """Real places to offer as grounding chips, biased to the user's block.

    Returns three DIFFERENT kinds of row, and callers must not conflate them (the
    2026-08-03 "Fitness CF" bug was showing the third as if it were the first):

      · a MATCH (`suggested` False) — the user gave us a name (an explicit
        `query`, or the AI-extracted place_name) and this place actually BEARS
        it. Block-biased first, then widened once, since a named venue can sit a
        town over. Offer these as matches.
      · a SUGGESTION (`suggested` True) — they never named a venue ("my gym"), so
        this is the circle type's keyword search: a nearby place of the right
        KIND. Fair to offer as "which one is it?", never as theirs.
      · a CONSOLATION (`suggested` True + `unmatched_name` set) — they DID name a
        venue and we could not find it. Same rows as above, but the reply must
        lead with not having found what they said, and a surface that can't say
        that should not show them at all.

    An explicit `query` that matches nothing returns [] — a typed search is the
    user being specific, and answering it with arbitrary nearby spots is a lie."""
    from app.places import search_places

    circle_type = str(affiliation.get("circle_type") or "other")
    included_type, keyword = _TYPE_SEARCH.get(circle_type, (None, ""))
    typed = (query or "").strip()
    named = typed or _resolve_place_name(user_id, affiliation)

    def _search(text: str, *, radius: float | None = None) -> list[dict[str, Any]]:
        rows = search_places(
            query=text,
            block_id=block_id,
            user_id=user_id,
            limit=3,
            included_type=included_type,
            **({"radius": radius} if radius else {}),
        )
        return [
            {
                "name": r["name"],
                "address": r.get("address"),
                "google_place_id": r["place_id"],
                "suggested": False,
            }
            for r in rows
            if r.get("place_id")
        ]

    if named:
        hits = [o for o in _search(named) if _name_hit(named, o["name"])]
        if not hits:
            # Nothing by that name in the neighbourhood — widen once before
            # doubting them. Chains and clubs are routinely a few towns out.
            hits = [
                o
                for o in _search(named, radius=_WIDE_SEARCH_RADIUS_M)
                if _name_hit(named, o["name"])
            ]
        if hits or typed:
            return hits
        # Named, but not findable on the map. Keyword results ride along as
        # consolations, tagged with the name we failed to find so the caller can
        # lead with that — or drop them, if its surface can't say it.
        if not keyword:
            return []
        return [
            {**o, "suggested": True, "unmatched_name": named} for o in _search(keyword)
        ]

    if not keyword:
        return []
    return [{**o, "suggested": True} for o in _search(keyword)]


def _flush_parked_features(
    user_id: str, affiliation: dict[str, Any], place_id: str
) -> int:
    """Move feature notes captured pre-grounding ("has_pool=true" in detail) onto the
    now-canonical place, and strip them from the detail text."""
    detail = str(affiliation.get("detail") or "")
    if not detail:
        return 0
    flushed = 0
    for match in _FEATURE_NOTE_RE.finditer(detail):
        key, value = match.group(1), match.group(2).strip()
        try:
            if upsert_place_feature(
                place_id=place_id,
                key=key,
                value=value,
                confidence=0.6,
                source="rapport",
                contributed_by=user_id,
            ):
                flushed += 1
        except Exception:
            logger.exception("flush_parked_feature_failed key=%s", key)
    if flushed:
        cleaned = _FEATURE_NOTE_RE.sub("", detail).strip(" ;")
        service_client().table("circle_affiliations").update({"detail": cleaned or None}).eq(
            "id", affiliation["id"]
        ).execute()
    return flushed


def _close_grounding_gap(affiliation_id: str) -> None:
    """Close the "which spot?" ask for an affiliation that just got pinned.

    Grounding used to be closed only by the caller that owned the turn, so pinning
    through /lana/circles/ground (the tile's search box picks by place id, which no
    cached candidate can match) left the gap open and the ask re-showed for a place
    already on the profile (FE ask #3, issues #63). Closing it HERE covers every
    path into grounding, present and future. Idempotent: the chat path's own
    mark_answered on the same row is a no-op after this."""
    if not affiliation_id:
        return
    try:
        from app.rapport_gaps import mark_answered

        rows = (
            service_client()
            .table("rapport_gaps")
            .select("gap_row_id")
            .eq("affiliation_ref", affiliation_id)
            .in_("status", ["open", "asked"])
            .execute()
        ).data or []
        for row in rows:
            mark_answered(str(row.get("gap_row_id") or ""))
    except Exception:
        logger.exception("close_grounding_gap_failed aff=%s", affiliation_id)


def ground_affiliation(
    user_id: str,
    affiliation_id: str,
    google_place_id: str,
    *,
    open_enrichment_gap: bool = True,
) -> dict[str, Any]:
    """Pin an affiliation to a canonical place and confirm it (§3 upsert flow).

    Raises ValueError('affiliation_not_found') / ValueError('place_not_found') for
    the endpoint to map onto HTTP errors."""
    affiliation = _own_affiliation(user_id, affiliation_id)
    if not affiliation:
        raise ValueError("affiliation_not_found")

    from app.places import place_details

    details = place_details(google_place_id)
    if not details:
        raise ValueError("place_not_found")

    place_id = upsert_canonical_place(
        details,
        circle_type_hint=str(affiliation.get("circle_type") or "") or None,
        created_by=user_id,
    )
    if not place_id:
        raise ValueError("place_not_found")

    service_client().table("circle_affiliations").update(
        {"place_ref": place_id, "status": "confirmed"}
    ).eq("id", affiliation["id"]).execute()

    _flush_parked_features(user_id, affiliation, place_id)
    _close_grounding_gap(affiliation_id)

    if open_enrichment_gap:
        try:
            from app.rapport_gaps import open_semantic_gap

            question, teaser = _place_affinity_question(details["name"])
            open_semantic_gap(
                user_id,
                None,
                question,
                label=details["name"],
                bucket="interest",
                teaser=teaser,
                place_ref=place_id,
            )
        except Exception:
            logger.exception("place_affinity_gap_open_failed")

    return {
        "affiliation_id": str(affiliation["id"]),
        "place_id": place_id,
        "place_name": details["name"],
        "status": "confirmed",
    }


def tag_claim_place_from_gap(gap_row_id: str, claim_id: str) -> None:
    """§4.3: stamp the answering claim with the gap's place tag. Best-effort."""
    if not gap_row_id or not claim_id:
        return
    try:
        from app.rapport_gaps import get_gap_row

        gap = get_gap_row(gap_row_id) or {}
        place_ref = gap.get("place_ref")
        if not place_ref:
            return
        service_client().table("user_identity_claims").update(
            {"place_ref": str(place_ref)}
        ).eq("id", claim_id).execute()
    except Exception:
        logger.exception("tag_claim_place_failed gap=%s claim=%s", gap_row_id, claim_id)


# ── Grounding questions on the rapport tile ────────────────────────────────────
# A suggested affiliation with no place_ref is invisible to the onion matcher —
# only confirmed + grounded rows match. The "By the way…" tile is the one surface
# that reliably reaches every user, so each ungrounded affiliation opens ONE
# rapport gap ("You mentioned a gym — which spot is it?"); the ranker interleaves
# it with normal rapport questions (app/rapport_ranker.py) and the answer flows
# back through here to ground the affiliation. Never the word "circle" (§A.4 M7).

# At most this many open/asked grounding questions at a time — a chatty session
# that names five places must not turn the tile into a week-long interrogation.
_GROUND_GAP_MAX_OPEN_DEFAULT = 2
# ABOVE the semantic-gap default (0.8): grounding asks first. Deliberate — the
# affinity follow-up about the same topic is superseded by the §4.3 enrichment
# question that fires AFTER grounding (same ask, but place-tagged), so grounding
# first converts the thread into matcher data instead of burning it ungrounded.
# The ranker's cadence guard still paces circle asks to 1-in-N overall.
_GROUND_GAP_SCORE = 0.85

# circle_type -> the warm-neutral noun the fallback question uses (lingo-clean).
_GROUND_NOUN: dict[str, str] = {
    "fitness": "gym",
    "faith": "place of worship",
    "school": "school",
    "kids_activity": "kids' activity",
    "neighborhood": "neighborhood spot",
    "hobby": "hobby group",
    "support": "group",
    "heritage": "community",
    "friends": "go-to spot",
    "other": "spot",
}

def place_relation_noun(circle_type: str | None) -> str:
    """Caller-relative noun for a grounded place ("gym" → tag "your gym").
    Disclosure-safe by construction (§F / O7): names the RELATION, never the place."""
    return _GROUND_NOUN.get(str(circle_type or "other"), "spot")


_GROUNDING_QUESTION_PROMPT = """You write ONE warm question for a neighborhood app user who \
mentioned a community/place they're part of, but never named WHICH one. Goal: learn the \
specific local spot (so the app can connect them with the people there).

Output ONLY JSON: {"question": "...", "teaser": "about your <thing>…"}

Rules:
- Echo THEIR framing (their phrase is given) — ask which specific place/spot it is.
- Short (<120 chars), warm, direct — never yes/no, never an interrogation.
- NEVER the words "circle", "block", or "match". Say "spot", "place", or their own word.
- teaser: 2-5 word lead-in ending with "…".
- English only (rendered into the user's language downstream)."""


def _grounding_question(circle_type: str, detail: str | None) -> tuple[str, str]:
    """AI-authored per the lingo rules; a type-templated line as fallback."""
    noun = place_relation_noun(circle_type)
    phrase = _FEATURE_NOTE_RE.sub("", str(detail or "")).strip(" ;")
    fallback = (
        f"You mentioned your {noun} — which one is it, exactly?",
        f"about your {noun}…",
    )
    try:
        from app.orchestrator.llm import llm_configured, llm_json, router_model

        if not llm_configured():
            return fallback
        data = llm_json(
            model=router_model(),
            system=_GROUNDING_QUESTION_PROMPT,
            user_payload=f'their phrase: "{phrase or noun}" (kind: {noun})',
            max_tokens=120,
            temperature=0.4,
        )
        question = str((data or {}).get("question") or "").strip()
        teaser = str((data or {}).get("teaser") or "").strip()
        if question and _lexicon_clean(question, teaser):
            return question[:160], (teaser or fallback[1])[:80]
    except Exception:
        logger.exception("grounding_question_llm_failed")
    return fallback


def ensure_grounding_gaps(user_id: str, *, max_open: int | None = None) -> int:
    """Open grounding questions for ungrounded affiliations, newest first, capped.

    Idempotent per affiliation: the gap is keyed "ground:<affiliation_id>", so an
    affiliation whose question was already asked/answered/skipped never re-opens.
    Best-effort — called from capture (background) and the tile's buffer refill.
    """
    if not user_id:
        return 0
    if max_open is None:
        try:
            max_open = int(os.environ.get("LANA_CIRCLE_GAP_MAX_OPEN", _GROUND_GAP_MAX_OPEN_DEFAULT))
        except (TypeError, ValueError):
            max_open = _GROUND_GAP_MAX_OPEN_DEFAULT
    try:
        sb = service_client()
        existing = (
            sb.table("rapport_gaps")
            .select("gap_row_id, status, affiliation_ref")
            .eq("user_id", user_id)
            .not_.is_("affiliation_ref", "null")
            .execute()
        ).data or []
        already_asked = {str(r["affiliation_ref"]) for r in existing if r.get("affiliation_ref")}
        open_count = sum(1 for r in existing if r.get("status") in ("open", "asked"))
        need = max_open - open_count
        if need <= 0:
            return 0
        affs = (
            sb.table("circle_affiliations")
            .select("id, circle_type, detail")
            .eq("user_id", user_id)
            .is_("dismissed_at", "null")
            .is_("place_ref", "null")
            .order("created_at", desc=True)
            .limit(10)
            .execute()
        ).data or []
    except Exception:
        logger.exception("ensure_grounding_gaps_load_failed user=%s", user_id)
        return 0

    from app.rapport_gaps import open_semantic_gap

    opened = 0
    for aff in affs:
        if opened >= need:
            break
        aff_id = str(aff.get("id") or "")
        if not aff_id or aff_id in already_asked:
            continue
        question, teaser = _grounding_question(aff.get("circle_type"), aff.get("detail"))
        if open_semantic_gap(
            user_id,
            None,
            question,
            label=str(aff.get("detail") or aff.get("circle_type") or "place"),
            bucket="interest",
            teaser=teaser,
            affiliation_ref=aff_id,
            gap_id=f"ground:{aff_id}",
            unlock_score=_GROUND_GAP_SCORE,
        ):
            opened += 1
    return opened


def _home_block_id(user_id: str) -> str | None:
    try:
        res = (
            service_client()
            .table("users")
            .select("home_block_id")
            .eq("id", user_id)
            .limit(1)
            .execute()
        )
        return ((res.data or [{}])[0] or {}).get("home_block_id")
    except Exception:
        logger.exception("home_block_lookup_failed user=%s", user_id)
        return None


def _chip(option: dict[str, Any]) -> dict[str, Any]:
    """One grounding option in the shape both surfaces consume: the tile taps
    google_place_id straight into /lana/circles/ground; a chat chip posts `send`."""
    name = str(option.get("name") or "").strip()
    chip = {
        "label": name[:28],
        "address": option.get("address"),
        "google_place_id": option.get("google_place_id"),
        "send": f"It's {name}"[:120],
        # Carried so the reply can't call a nearby same-kind place a match.
        "suggested": bool(option.get("suggested")),
    }
    if option.get("unmatched_name"):
        # The name we looked for and missed — the reply has to lead with it.
        chip["unmatched_name"] = str(option["unmatched_name"])
    return chip


# The way out of a wrong list. Without it a user whose spot we failed to find has
# no move on the tile at all (its only affordance is these chips) — they either
# tap someone else's gym or drop the thread (Ankit, 2026-08-03). It carries no
# google_place_id on purpose: match_grounding_candidate skips id-less rows, so it
# can never be mistaken for a place, and both surfaces just post its `send`.
_ESCAPE_SEND = "none of those — it's somewhere else"


def _escape_chip() -> dict[str, Any]:
    return {
        "label": "Not these",
        "address": None,
        "google_place_id": None,
        "send": _ESCAPE_SEND,
        "suggested": False,
    }


def _with_escape(chips: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Offered places + the escape hatch. Never on an empty list (nothing to escape)."""
    return [*chips, _escape_chip()] if chips else []


def _is_escape(message: str) -> bool:
    return _norm_place_text(message) == _norm_place_text(_ESCAPE_SEND)


def _unmatched_name(rows: list[dict[str, Any]] | None) -> str:
    """The name we looked for and failed to find, when that's what these rows are."""
    for row in rows or []:
        if isinstance(row, dict) and row.get("unmatched_name"):
            return str(row["unmatched_name"])
    return ""


def _offers_are_suggestions(chips: list[dict[str, Any]] | None) -> bool:
    """True when the list is 'nearby places of this kind', not confirmed name matches."""
    rows = [c for c in (chips or []) if isinstance(c, dict) and c.get("google_place_id")]
    return bool(rows) and all(c.get("suggested") for c in rows)


def grounding_payload_for_gap(user_id: str, gap_row: dict[str, Any]) -> dict[str, Any]:
    """Serve-time extras for a grounding ask: kind + affiliation + place chips.

    Chips are fetched from Google ONCE (first serve) and cached on the row, so
    pending re-shows and React double-fires are pure lookups — the home render
    never pays a repeat Places call. An empty result is cached too (free-text
    still works); on a hard failure nothing is stored so the next serve retries.
    """
    affiliation_id = str(gap_row.get("affiliation_ref") or "")
    payload: dict[str, Any] = {
        "kind": "place_grounding",
        "affiliation_id": affiliation_id,
        "options": [],
    }
    try:
        affiliation = _own_affiliation(user_id, affiliation_id) or {}
    except Exception:
        logger.exception("grounding_affiliation_load_failed aff=%s", affiliation_id)
        affiliation = {}
    # What KIND of place this is and the user's own words for it, so the card can
    # show the right glyph and speak in their noun ("your gym") instead of a
    # neutral pin (FE ask #1, issues #63). Both absent-safe by contract.
    if affiliation.get("circle_type"):
        payload["circle_type"] = str(affiliation["circle_type"])
    detail = _FEATURE_NOTE_RE.sub("", str(affiliation.get("detail") or "")).strip(" ;")
    if detail:
        payload["detail"] = detail
    if affiliation.get("place_name"):
        # Their own name for the spot, when they gave one — truer than `detail`
        # for copy that wants to name it.
        payload["place_name"] = str(affiliation["place_name"])
    stored = gap_row.get("grounding_options")
    if isinstance(stored, list):
        payload["options"] = stored
        return payload
    try:
        if not affiliation:
            return payload
        # Tile rules differ from chat's, because PlaceGroundingCard renders options
        # as a pick-one place grid — with no room for "these are guesses", and with
        # a LONE option shown as "the place she mentioned — pin it?". So:
        #   · consolations are dropped. The question names the place ("which
        #     Fitness CF?"), and tiles that don't bear that name are the whole bug.
        #     Zero options opens the card's own search box, which is the honest move.
        #   · a lone suggestion is dropped too — "which gym?" answered with one
        #     nearby gym reads as a claim about her. Two or three read as a choice,
        #     which is what the grid is for, so those stay.
        #   · no escape chip: the card already ships "Search another" and a skip,
        #     and an id-less chip would render as a place tile.
        rows = [
            o
            for o in ground_options(user_id, affiliation, block_id=_home_block_id(user_id))
            if not o.get("unmatched_name")
        ]
        if len(rows) == 1 and rows[0].get("suggested"):
            rows = []
        options = [_chip(o) for o in rows]
        service_client().table("rapport_gaps").update(
            {"grounding_options": options}
        ).eq("gap_row_id", gap_row["gap_row_id"]).execute()
        payload["options"] = options
    except Exception:
        logger.exception("grounding_options_fetch_failed gap=%s", gap_row.get("gap_row_id"))
    return payload


def note_ungrounded_detail(user_id: str, affiliation_id: str, text: str) -> None:
    """Keep an un-matchable answer ("the little studio by Publix") as detail on the
    affiliation — still useful context, just not grounded yet. Never clobbers."""
    text = str(text or "").strip()[:120]
    if not text:
        return
    try:
        affiliation = _own_affiliation(user_id, affiliation_id)
        if not affiliation:
            return
        detail = str(affiliation.get("detail") or "")
        if text.lower() in detail.lower():
            return
        merged = f"{detail}; {text}".strip("; ")[:200]
        service_client().table("circle_affiliations").update({"detail": merged}).eq(
            "id", affiliation["id"]
        ).execute()
    except Exception:
        logger.exception("note_ungrounded_detail_failed aff=%s", affiliation_id)


def match_grounding_candidate(
    candidates: list[dict[str, Any]] | None, message: str
) -> dict[str, Any] | None:
    """The user picked one of the offered places — by tapping its chip (exact `send`
    echo) or naming it. Containment either way, so "orangetheory" hits
    "OrangeTheory Narcoossee". Deliberately NOT fuzzy beyond that: a wrong canonical
    place silently attached to a user is the §F trust failure, so anything less
    certain re-confirms via search instead."""
    msg = str(message or "").strip().lower()
    if not msg or not candidates:
        return None
    for cand in candidates:
        if not isinstance(cand, dict) or not cand.get("google_place_id"):
            continue
        name = str(cand.get("name") or cand.get("label") or "").strip().lower()
        send = str(cand.get("send") or "").strip().lower()
        if send and msg == send:
            return cand
        if len(name) >= 4 and (name in msg or msg in name):
            return cand
    return None


def _compose_grounding_reply(
    goal: str, facts: list[str], fallback: str, session_ctx: dict[str, Any] | None
) -> str:
    from app.reply_compose import compose_reply

    return compose_reply(
        goal=goal, facts=facts, fallback=fallback, session_ctx=session_ctx
    )


def _place_co_member_count(place_id: str, exclude_user: str) -> int:
    """Other users with a confirmed, non-dismissed affiliation at this place —
    the truth behind an intro offer. 0 on any error (fail toward create+invite,
    which is always-on)."""
    if not place_id:
        return 0
    try:
        res = (
            service_client()
            .table("circle_affiliations")
            .select("id", count="exact")
            .eq("place_ref", place_id)
            .eq("status", "confirmed")
            .is_("dismissed_at", "null")
            .neq("user_id", exclude_user)
            .limit(1)
            .execute()
        )
        return int(res.count or 0)
    except Exception:
        logger.exception("place_co_member_count_failed place=%s", place_id)
        return 0


def _unpinned_close(
    affiliation: dict[str, Any] | None,
    said: str,
    *,
    goal_head: str,
    fallback_head: str,
    session_ctx: dict[str, Any] | None,
    pending_action: str | None = None,
) -> dict[str, Any]:
    """Close a grounding thread that never got a pin — WITH the next step a pinned
    one ends on (ACKNOWLEDGE → OFFER, same bridge shape as ground_and_confirm).

    A failed pin used to dead-end on "I'll remember that one", which reads as a
    thread dropped (Ankit, 2026-08-03). But we still know WHAT the community is —
    their own words plus the circle key — and neighbours match on that, not on an
    address. So the close offers to LOOK for people into it. It can only ever be
    an offer to look: with no place there is no co-member count, so any claim
    about who is out there would be invented.

    An action the user had already asked for (pending_action) wins instead: their
    own request is dispatched, place-less, rather than re-offered to them."""
    aff = affiliation or {}
    noun = place_relation_noun(aff.get("circle_type"))
    topic = str(aff.get("circle_key") or "").replace("_", " ").strip() or noun
    facts = [f'They told you: "{said[:120]}"', f"Their community: {topic}."]
    offer: dict[str, Any] | None = None

    if pending_action in ("host_meet", "find_neighbors"):
        if session_ctx is not None:
            session_ctx["_grounding_offer_done"] = True
        send = (
            f"connect me with neighbors into {topic}"
            if pending_action == "find_neighbors"
            else f"help me host a {topic} meet"
        )
        goal = (
            f"{goal_head} ONE short warm sentence, no question and no offer — you "
            "are about to help with what they already asked for next. Never 'on my "
            "radar' / 'noted in my system'."
        )
        offer = {"kind": pending_action, "label": "", "send": send, "topic": topic, "auto": True}
        fallback = fallback_head
    elif session_ctx is not None and not session_ctx.get("_grounding_offer_done"):
        goal = (
            f"{goal_head} Then offer ONE next step: that you look for neighbors who "
            f"are into {topic} too. You have NOT looked yet and nobody is confirmed "
            "— so never imply people are waiting, and never promise an intro. End on "
            "the offer question; the chip below is the tap. Never 'on my radar'."
        )
        offer = {
            "kind": "find_neighbors",
            "label": "Yes, look",
            "send": f"connect me with neighbors into {topic}",
            "topic": topic,
        }
        fallback = f"{fallback_head} Want me to look for neighbors into {topic} too?"
        session_ctx["_grounding_offer_done"] = True
    else:
        goal = f"{goal_head} One sentence, no question."
        fallback = fallback_head

    return {
        "reply": _compose_grounding_reply(
            goal=goal, facts=facts, fallback=fallback, session_ctx=session_ctx
        ),
        "options": [],
        "pending": None,
        "grounded": False,
        "offer": offer,
    }


def ground_and_confirm(
    user_id: str,
    affiliation_id: str,
    google_place_id: str,
    *,
    session_ctx: dict[str, Any] | None = None,
    pending_action: str | None = None,
) -> dict[str, Any]:
    """Ground the affiliation, then ACKNOWLEDGE → OFFER (the rapport-bridge shape,
    LANA_RAPPORT_BRIDGE_SPEC_v1 §1/§3): one warm confirm plus exactly ONE
    state-aware CTA, never a bare acknowledgement.

      · co-members confirmed at the place → offer an intro (bridge rule 4 —
        an offer gated on a REAL count, never a vague "on my radar" promise);
      · nobody there yet → offer create+invite at the place (rule 5/6 —
        always-on per Circles master §D.2, and the act that seeds the area);
      · offer already made this session, or no chat ctx (tile endpoint) →
        today's plain warm close (the fallback, not the default).

    The returned `offer` {kind, label, send, topic} rides the existing rapport
    offer rails: the pipeline arms rapport_pending_action so a tap OR a typed
    "sure" dispatches deterministically (_forced_slots_for_kind), and a decline
    closes warmly. ground_affiliation also queues the §4.3 enrichment question,
    so the follow-up thread arms itself."""
    affiliation = _own_affiliation(user_id, affiliation_id) or {}
    try:
        result = ground_affiliation(user_id, affiliation_id, google_place_id)
    except ValueError:
        logger.exception("grounding_confirm_failed aff=%s", affiliation_id)
        return {
            "reply": "Hmm, I couldn't pin that spot just now — I'll ask again another time.",
            "options": [],
            "pending": None,
            "grounded": False,
        }
    place_name = str(result.get("place_name") or "that spot")
    place_id = str(result.get("place_id") or "")
    topic = str(affiliation.get("circle_key") or "").replace("_", " ").strip()

    # Fallback register (spec'd in docs/LANA_CIRCLES_BACKEND.md · grounding confirm):
    # warm close, no question, no promises — used when the bridge already fired.
    # EVERY variant must SAY the community was saved (2026-07-28 product decision:
    # a community is created only here, with its place, and never silently — the
    # user is always told it's now on their profile).
    saved_fact = (
        f"{place_name} is now saved as one of their communities on their profile "
        "— they're in it."
    )
    offer: dict[str, Any] | None = None
    goal = (
        "Tell them their community at this place is saved on their profile now — "
        "warm, one sentence, no follow-up question. Do not promise introductions "
        "or anything else — never 'on my radar' / 'noted in my system'; say it "
        "like 'that's on your profile now' or 'I'll keep an ear out'."
    )
    facts = [f"The place: {place_name}.", saved_fact]
    fallback = f"Done — {place_name} is saved to your communities now."

    if pending_action in ("host_meet", "find_neighbors"):
        # The grounding ran in service of an action the user ALREADY asked for
        # ("organize a meet for my squash group" → which club? → tap). The
        # ACKNOWLEDGE→OFFER shape would re-ask their own request here (QA
        # 2026-07-30, the squash/Life Time loop) — so the reply only announces
        # the community save (never silent, per the 2026-07-28 product rule)
        # and the caller dispatches the action itself, place pre-filled.
        if session_ctx is not None:
            session_ctx["_grounding_offer_done"] = True
        if pending_action == "find_neighbors":
            send = f"connect me with neighbors into {topic or place_name}"
        else:
            send = f"help me host a {topic or 'get-together'} meet at {place_name}"
        reply = _compose_grounding_reply(
            goal=(
                "Tell them their community at this place is saved on their "
                "profile now — ONE short warm sentence, no question and no "
                "offer (you are about to help with what they asked for next). "
                "Never 'on my radar' / 'noted in my system'."
            ),
            facts=facts,
            fallback=fallback,
            session_ctx=session_ctx,
        )
        return {
            "reply": reply,
            "options": [],
            "pending": None,
            "grounded": True,
            "offer": {
                "kind": pending_action,
                "label": "",
                "send": send,
                "topic": topic,
                "auto": True,
            },
        }

    if session_ctx is not None and not session_ctx.get("_grounding_offer_done"):
        others = _place_co_member_count(place_id, user_id)
        if others >= 1:
            offer = {
                "kind": "find_neighbors",
                "label": "Yes, introduce me",
                "send": f"connect me with neighbors into {topic or place_name}",
                "topic": topic,
            }
            noun = "neighbor" if others == 1 else "neighbors"
            goal = (
                "Tell them their community at this place is saved on their profile "
                "now — one warm sentence — then offer ONE next step: an "
                "introduction, grounded ONLY in the real count given. End on the "
                "'want an intro?' question — the chip below is the tap. Never "
                "promise anything beyond the offer, never 'on my radar'."
            )
            facts = [
                f"The place: {place_name}.",
                saved_fact,
                f"{others} other {noun} confirmed the same spot as theirs.",
            ]
            fallback = (
                f"Done — {place_name} is saved to your communities now. {others} of "
                f"your {noun} call it their spot too — want an intro?"
            )
        else:
            offer = {
                "kind": "host_meet",
                "label": "Set something up",
                "send": f"help me host a {topic or 'get-together'} meet at {place_name}",
                "topic": topic,
            }
            thing = f"a {topic} get-together" if topic else "a get-together"
            goal = (
                "Tell them their community at this place is saved on their profile "
                "now — one warm sentence — then offer ONE next step: setting up a "
                "small get-together there they can share with their own group. "
                "Nobody else is confirmed at this spot yet, so never claim or "
                "imply people are waiting — creating and inviting is how their area "
                "comes alive. End on the offer question; the chip below is the tap. "
                "Never 'on my radar'."
            )
            facts = [
                f"The place: {place_name}.",
                saved_fact,
                "Nobody else has confirmed this spot yet.",
                f"They could set up {thing} there and share it with their own people.",
            ]
            fallback = (
                f"Done — {place_name} is saved to your communities now. Want to set "
                f"up {thing} there you can share with your group?"
            )
        session_ctx["_grounding_offer_done"] = True

    reply = _compose_grounding_reply(
        goal=goal, facts=facts, fallback=fallback, session_ctx=session_ctx
    )
    return {"reply": reply, "options": [], "pending": None, "grounded": True, "offer": offer}


def handle_grounding_answer(
    user_id: str,
    gap_row: dict[str, Any],
    answer_text: str,
    *,
    session_ctx: dict[str, Any] | None = None,
    abandon: bool = False,
) -> dict[str, Any]:
    """First reply to a grounding question from the tile.

    Returns {reply, options, pending, grounded}: `pending` is the confirmation
    state to stash in session ctx (rapport_grounding) when chips were offered,
    None when the thread closed this turn. The gap itself is closed by the caller
    (she engaged — never re-ask), independent of whether grounding completed.
    NEVER auto-grounds from free text: their words drive a search, a tap confirms.
    `abandon` is the caller's classifier verdict that the reply DECLINES the ask
    ("none of these", "skip that") — close warmly, never search with those words.
    """
    affiliation_id = str(gap_row.get("affiliation_ref") or "")
    answer = str(answer_text or "").strip()

    # A tile chip tap (or typing exactly the offered name) IS the confirmation —
    # they chose a specific place we showed them.
    stored = gap_row.get("grounding_options")
    tapped = match_grounding_candidate(stored if isinstance(stored, list) else None, answer)
    if tapped:
        return ground_and_confirm(
            user_id, affiliation_id, str(tapped["google_place_id"]), session_ctx=session_ctx
        )

    affiliation = _own_affiliation(user_id, affiliation_id)
    if not affiliation or affiliation.get("place_ref"):
        # Dismissed or grounded through another surface since the question opened.
        return {
            "reply": "Got it — thanks for telling me.",
            "options": [],
            "pending": None,
            "grounded": False,
        }

    if abandon:
        # A rejection of the offered chips, not a place name: feeding it to Places
        # search returns arbitrary nearby spots ("none of these" → random cafés),
        # and it isn't detail worth keeping either.
        return _unpinned_close(
            affiliation,
            answer,
            goal_head=(
                "They passed on pinning down the spot — accept that warmly and "
                "never push for the place again."
            ),
            fallback_head="No worries — we can leave that one.",
            session_ctx=session_ctx,
        )

    if _is_escape(answer):
        # They tapped "not these" on the offered list. Their spot exists, we just
        # showed the wrong ones — ask what it's called instead of closing.
        return {
            "reply": _compose_grounding_reply(
                goal=(
                    "None of the places you offered were theirs. Say that plainly, "
                    "no apology spiral, and ask what their spot is called — or which "
                    "street or town it's in, if they'd rather. One short sentence."
                ),
                facts=[f"Their community: {place_relation_noun(affiliation.get('circle_type'))}."],
                fallback="My list was off — what's it called?",
                session_ctx=session_ctx,
            ),
            # No candidates: the next free-text turn drives a fresh search.
            "options": [],
            "pending": {
                "affiliation_id": affiliation_id,
                "candidates": [],
                "answer_text": "",
                "attempts": 1,
            },
            "grounded": False,
        }

    candidates = [
        {**_chip(o), "name": o.get("name")}
        for o in ground_options(
            user_id, affiliation, block_id=_home_block_id(user_id), query=answer
        )
    ]
    if not candidates:
        note_ungrounded_detail(user_id, affiliation_id, answer)
        return _unpinned_close(
            affiliation,
            answer,
            goal_head=(
                "Warmly acknowledge the spot they named — you could not find it on "
                "the map, so say you'll remember it the way they said it."
            ),
            fallback_head="Got it — I'll remember that one.",
            session_ctx=session_ctx,
        )

    names = [str(c.get("name") or "") for c in candidates]
    missing = _unmatched_name(candidates)
    if missing:
        # We could not find what they named, so these are merely nearby places of
        # the same kind. Presenting them as matches is the 2026-08-03 bug.
        goal = (
            "You could NOT find the place they named. Say so plainly, naming what "
            "they told you, then ask whether it's one of these nearby places or "
            "somewhere else — these are guesses of the right kind, NOT their spot, "
            "so never call them matches. One short sentence. The list renders as "
            "tappable chips below your message."
        )
        fallback = f"I couldn't find {missing} nearby — is it {names[0]}, or somewhere else?"
    elif _offers_are_suggestions(candidates):
        # They never named a venue, so nothing failed — these are simply the
        # nearby places of that kind, offered as a choice.
        goal = (
            "They haven't named their spot, so ask which of these nearby places it "
            "is — offer them as options, never as something you already know about "
            "them, and leave room for 'somewhere else'. One short warm sentence. "
            "The list renders as tappable chips below your message."
        )
        fallback = f"Is it {names[0]}, or somewhere else?"
    else:
        goal = (
            "They named their spot; you found places that really do carry that name. "
            "Ask them to confirm which one — one short warm sentence referencing the "
            "first match. The matches render as tappable chips below your message."
        )
        fallback = f"Nice — is that {names[0]}?"
    reply = _compose_grounding_reply(
        goal=goal,
        facts=[f'They said: "{answer[:120]}"', f"What you found: {', '.join(names[:3])}."],
        fallback=fallback,
        session_ctx=session_ctx,
    )
    return {
        "reply": reply,
        "options": [
            {"label": c["label"], "send": c["send"]} for c in _with_escape(candidates)
        ],
        "pending": {
            "affiliation_id": affiliation_id,
            "candidates": candidates,
            "answer_text": answer[:120],
            "attempts": 1,
        },
        "grounded": False,
    }


def handle_grounding_confirmation(
    user_id: str,
    state: dict[str, Any],
    message: str,
    *,
    session_ctx: dict[str, Any] | None = None,
    abandon: bool = False,
) -> dict[str, Any]:
    """A turn while grounding chips are pending. Same return shape as
    handle_grounding_answer. The caller has already ruled out a pivot/release."""
    affiliation_id = str(state.get("affiliation_id") or "")
    candidates = state.get("candidates") if isinstance(state.get("candidates"), list) else []
    attempts = int(state.get("attempts") or 1)
    # A live action this grounding serves (stamped by the policy's ground_place
    # decision) — carried so the confirmed place dispatches it instead of
    # re-offering it.
    pending_action = str(state.get("pending_action") or "").strip() or None
    msg = str(message or "").strip()

    matched = match_grounding_candidate(candidates, msg)
    if matched:
        return ground_and_confirm(
            user_id,
            affiliation_id,
            str(matched["google_place_id"]),
            session_ctx=session_ctx,
            pending_action=pending_action,
        )

    affiliation = _own_affiliation(user_id, affiliation_id)
    said = str(state.get("answer_text") or msg)

    escaped = _is_escape(msg)
    if escaped and attempts < 3:
        # "Not these" — the list was wrong, not the user. Ask for the name.
        return {
            "reply": _compose_grounding_reply(
                goal=(
                    "None of the places you offered were theirs. Say that plainly and "
                    "ask what their spot is called — or which street or town it's in, "
                    "if that's easier. One short sentence, no apology spiral."
                ),
                facts=[
                    f"Their community: "
                    f"{place_relation_noun((affiliation or {}).get('circle_type'))}."
                ],
                fallback="My list was off — what's it called?",
                session_ctx=session_ctx,
            ),
            "options": [],
            "pending": {
                "affiliation_id": affiliation_id,
                # Cleared so their next words drive a fresh search.
                "candidates": [],
                "answer_text": said[:120],
                "attempts": attempts + 1,
                "pending_action": pending_action,
            },
            "grounded": False,
        }

    if abandon or escaped or attempts >= 3:
        # "no" / "neither" / worn out — keep their words as detail, then close on the
        # bridge (an offer to look for their people, which needs no pin).
        note_ungrounded_detail(user_id, affiliation_id, said)
        return _unpinned_close(
            affiliation,
            said,
            goal_head=(
                "None of the map matches were their spot — accept that warmly and "
                "say you'll remember it the way they told you."
            ),
            fallback_head="No problem — I'll remember it the way you said it.",
            session_ctx=session_ctx,
            pending_action=pending_action,
        )

    # They typed a different name / correction — search once more with their words.
    if not affiliation or affiliation.get("place_ref"):
        return {"reply": "Got it — thanks!", "options": [], "pending": None, "grounded": False}
    fresh = [
        {**_chip(o), "name": o.get("name")}
        for o in ground_options(
            user_id, affiliation, block_id=_home_block_id(user_id), query=msg
        )
    ]
    if not fresh:
        note_ungrounded_detail(user_id, affiliation_id, msg)
        return _unpinned_close(
            affiliation,
            msg,
            goal_head=(
                "You could not find the place they named on the map — acknowledge it "
                "warmly and say you'll remember it as they said it."
            ),
            fallback_head="Got it — I'll remember that one.",
            session_ctx=session_ctx,
            pending_action=pending_action,
        )
    names = [str(c.get("name") or "") for c in fresh]
    return {
        "reply": _compose_grounding_reply(
            goal=(
                "They corrected which place they meant; you found places carrying that "
                "name. Ask them to confirm which one — short and warm; the matches "
                "render as tappable chips."
            ),
            facts=[f'They said: "{msg[:120]}"', f"What you found: {', '.join(names[:3])}."],
            fallback=f"Is it {names[0]}?",
            session_ctx=session_ctx,
        ),
        "options": [
            {"label": c["label"], "send": c["send"]} for c in _with_escape(fresh)
        ],
        "pending": {
            "affiliation_id": affiliation_id,
            "candidates": fresh,
            "answer_text": str(state.get("answer_text") or msg)[:120],
            "attempts": attempts + 1,
            "pending_action": pending_action,
        },
        "grounded": False,
    }


def _member_count(place_id: str) -> int:
    """Confirmed, non-dismissed members of a place (Place Profile §5.1)."""
    try:
        res = (
            service_client()
            .table("circle_affiliations")
            .select("id", count="exact")
            .eq("place_ref", place_id)
            .eq("status", "confirmed")
            .is_("dismissed_at", "null")
            .execute()
        )
        return int(res.count or 0)
    except Exception:
        logger.exception("member_count_failed place=%s", place_id)
        return 0


def list_my_circles(user_id: str) -> list[dict[str, Any]]:
    """The user's own communities for the profile surface (§G.1) — she always sees
    all of hers; what OTHERS see is tier-gated elsewhere. member_count/active power
    the '"active · 3 neighbors" / "just you so far"' status line.

    Grounded rows ONLY: a community without a place does not exist (2026-07-28
    product decision). Ungrounded rows are internal candidates — they surface
    exclusively through Lana's "which spot is it?" ask, never as communities."""
    sb = service_client()
    res = (
        sb.table("circle_affiliations")
        .select("id, circle_type, circle_key, detail, status, place_ref, created_at")
        .eq("user_id", user_id)
        .is_("dismissed_at", "null")
        .not_.is_("place_ref", "null")
        .order("created_at", desc=True)
        .limit(40)
        .execute()
    )
    rows = res.data or []
    place_ids = sorted({str(r["place_ref"]) for r in rows if r.get("place_ref")})
    places: dict[str, dict[str, Any]] = {}
    if place_ids:
        pres = (
            sb.table("places")
            .select("id, name, address")
            .in_("id", place_ids)
            .execute()
        )
        places = {str(p["id"]): p for p in (pres.data or [])}
    out: list[dict[str, Any]] = []
    for r in rows:
        place_ref = str(r.get("place_ref") or "") or None
        place = places.get(place_ref or "", {})
        count = _member_count(place_ref) if place_ref else 0
        detail = _FEATURE_NOTE_RE.sub("", str(r.get("detail") or "")).strip(" ;") or None
        out.append(
            {
                "id": str(r["id"]),
                "circle_type": r.get("circle_type"),
                "status": r.get("status"),
                "grounded": bool(place_ref),
                "place_name": place.get("name"),
                "place_address": place.get("address"),
                "detail": detail,
                "member_count": count,
                "active": count >= 2,
                "added_at": r.get("created_at"),
            }
        )
    return out


def add_circle(
    user_id: str,
    *,
    circle_type: str,
    detail: str | None = None,
    google_place_id: str | None = None,
    source: str = "profile_add",
    invited_by: str | None = None,
) -> dict[str, Any]:
    """Profile 'Add' (§G.2). A community's place is MANDATORY (2026-07-28 product
    decision): a profile add without a google_place_id is rejected — the created
    row grounds and confirms in one step. The invite self-confirm write
    (source='invite_confirmed', §A.2) is the one place-less entry left: it parks a
    CANDIDATE, not a community — the joiner grounds her own place right after via
    ground-options → ground, and until then the row is invisible everywhere."""
    if circle_type not in CIRCLE_TYPES:
        raise ValueError("invalid_circle_type")
    if source not in ("profile_add", "invite_confirmed"):
        raise ValueError("invalid_source")
    if source == "profile_add" and not (google_place_id or "").strip():
        raise ValueError("place_required")
    from app.circles_capture import _slugify

    key = _slugify(detail or "") or circle_type
    sb = service_client()
    existing = (
        sb.table("circle_affiliations")
        .select("id")
        .eq("user_id", user_id)
        .eq("circle_key", key)
        .is_("dismissed_at", "null")
        .limit(1)
        .execute()
    )
    if existing.data:
        affiliation_id = str(existing.data[0]["id"])
    else:
        row = {
            "user_id": user_id,
            "circle_type": circle_type,
            "circle_key": key,
            "detail": (detail or "").strip()[:200] or None,
            "status": "suggested",
            "source": source,
            "confidence": 1.0,  # self-stated, not inferred
        }
        if invited_by:
            row["invited_by"] = invited_by
        res = sb.table("circle_affiliations").insert(row).execute()
        affiliation_id = str(res.data[0]["id"]) if res.data else ""
        if not affiliation_id:
            raise ValueError("circle_create_failed")
    if google_place_id:
        return ground_affiliation(user_id, affiliation_id, google_place_id)
    return {"affiliation_id": affiliation_id, "status": "suggested", "grounded": False}


def update_circle(user_id: str, affiliation_id: str, *, detail: str | None) -> None:
    if not _own_affiliation(user_id, affiliation_id):
        raise ValueError("affiliation_not_found")
    service_client().table("circle_affiliations").update(
        {"detail": (detail or "").strip()[:200] or None}
    ).eq("id", affiliation_id).execute()


def remove_circle(user_id: str, affiliation_id: str) -> None:
    """Soft-delete (§G.3): the circle immediately stops generating matches — the
    onion and member_count both filter on dismissed_at is null."""
    from datetime import datetime, timezone

    if not _own_affiliation(user_id, affiliation_id):
        raise ValueError("affiliation_not_found")
    service_client().table("circle_affiliations").update(
        {"dismissed_at": datetime.now(timezone.utc).isoformat()}
    ).eq("id", affiliation_id).execute()
