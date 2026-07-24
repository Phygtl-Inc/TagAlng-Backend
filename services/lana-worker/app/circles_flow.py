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
import re
from typing import Any

from app.auth import service_client
from app.circles_capture import CIRCLE_TYPES, upsert_place_feature

logger = logging.getLogger(__name__)

# circle_type -> (Places includedType, search keyword) for grounding offers (§A.1.2).
# includedType=None means keyword-only search (point_of_interest is not searchable
# as a strict type across categories).
_TYPE_SEARCH: dict[str, tuple[str | None, str]] = {
    "fitness": ("gym", "gym"),
    "faith": ("place_of_worship", "church mosque synagogue temple"),
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
        if question:
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
        .select("id, circle_type, circle_key, detail, status, place_ref")
        .eq("id", affiliation_id)
        .eq("user_id", user_id)
        .is_("dismissed_at", "null")
        .limit(1)
        .execute()
    )
    return (res.data or [None])[0]


def ground_options(
    user_id: str,
    affiliation: dict[str, Any],
    *,
    block_id: str | None,
    query: str | None = None,
) -> list[dict[str, Any]]:
    """2-3 real nearby places to offer as grounding chips, biased to the user's block.

    Uses the user's own phrase (affiliation.detail) as the search text, falling back
    to the circle type's keyword; `query` overrides ("search for another")."""
    from app.places import search_places

    circle_type = str(affiliation.get("circle_type") or "other")
    included_type, keyword = _TYPE_SEARCH.get(circle_type, (None, ""))
    detail = str(affiliation.get("detail") or "")
    # Strip parked feature notes ("; has_pool=true") from the search phrase.
    phrase = _FEATURE_NOTE_RE.sub("", detail).strip(" ;")
    q = (query or "").strip() or phrase or keyword
    if not q:
        return []
    rows = search_places(
        query=q,
        block_id=block_id,
        user_id=user_id,
        limit=3,
        included_type=included_type,
    )
    return [
        {
            "name": r["name"],
            "address": r.get("address"),
            "google_place_id": r["place_id"],
        }
        for r in rows
        if r.get("place_id")
    ]


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
    """The user's own circles for the profile surface (§G.1) — she always sees all
    of hers; what OTHERS see is tier-gated elsewhere. member_count/active power the
    '"active · 3 neighbors" / "just you so far"' status line."""
    sb = service_client()
    res = (
        sb.table("circle_affiliations")
        .select("id, circle_type, circle_key, detail, status, place_ref, created_at")
        .eq("user_id", user_id)
        .is_("dismissed_at", "null")
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
    """Profile 'Add' (§G.2) — same grounding semantics as chat; grounding optional.
    Also the invite self-confirm write (source='invite_confirmed', §A.2)."""
    if circle_type not in CIRCLE_TYPES:
        raise ValueError("invalid_circle_type")
    if source not in ("profile_add", "invite_confirmed"):
        raise ValueError("invalid_source")
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
