"""Who among these recommenders is like the reader — "4 of these 8 have toddlers, like you".

A merged card says how many neighbours stand behind a subject. That number is social proof
from strangers. The cohort turns some of them into people whose opinion the reader has a
specific reason to weigh: eight recommended this dentist, and four of them are parents of
toddlers, which is what she is.

This is the single capability screens 07 and 08 ask for three separate times:

    "8 Spanish-speaking parents said gentle with kids"   card level
    "4 of these 8 have toddlers, like you"               inside one theme
    "Sam · toddler parent"                               on one contributor

WHAT IT MAY CLAIM, AND WHAT IT MAY NOT

A cohort is a COUNT OF PROVEN OVERLAP and nothing else. It is built by intersecting the
reader's own claims with each contributor's, on `concept` — the resolved concept id, not
the free-text label, so "has a toddler" and "mum to a 2-year-old" count as one thing
exactly when the concept resolver already said they were.

It never asserts anything about the SUBJECT. Dr. Sarah does not become "good with toddlers"
because four toddler parents recommended her; four toddler parents recommended her, and
that is all the card is allowed to say ([[truthful-peer-match-model]], and the same rule
app/tip_rec_cascade.py states for peer rows).

PRIVATE CLAIMS NEVER FORM A COHORT. `user_identity_claims.disclosure` marks what a
neighbour is willing to be known by. A cohort is a public statement about its members —
"4 of these 8 have toddlers" tells the reader something about four identifiable people —
so a private claim is excluded even when both sides hold it. The overlap would be real and
disclosing it would not be ours to do.

A COHORT OF ONE IS NOT A COHORT. One person sharing a claim is that person, not a pattern,
and rendering "1 of these 8" invites a reader to work out who. Two is the floor.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Below this a "cohort" is an individual, and naming them is the reader's inference to
# make from the contributor list, not ours to hand over as a statistic.
MIN_COHORT = 2

# A card shows one or two of these; past that it is a demographic readout of the people
# who helped, which is not what anyone came for.
MAX_COHORTS = 2

# Claim kinds that may form a cohort. `public` only — see the header.
_PUBLIC = "public"


def _claims_by_concept(user_id: str) -> dict[str, str]:
    """{concept_id: label} for one user's PUBLIC claims.

    Keyed on the resolved concept rather than the label so two neighbours who wrote "has a
    toddler" and "mum to a 2-year-old" count as one shared thing exactly when the concept
    resolver already decided they were — this module does no matching of its own.
    """
    from app.db import service_client

    try:
        rows = (
            service_client()
            .table("user_identity_claims")
            .select("label, concept, disclosure")
            .eq("user_id", user_id)
            .is_("dismissed_at", "null")
            .limit(200)
            .execute()
        ).data or []
    except Exception:  # noqa: BLE001 — no cohort is a fine outcome; a 500 is not
        logger.info("reco_cohort.claims_read_failed user=%s", user_id)
        return {}

    out: dict[str, str] = {}
    for r in rows:
        concept = str(r.get("concept") or "").strip()
        label = " ".join(str(r.get("label") or "").split())
        # Absent disclosure is treated as private: a claim whose sharing rule we cannot
        # read is not one to broadcast a count about.
        if not concept or not label or str(r.get("disclosure") or "") != _PUBLIC:
            continue
        out.setdefault(concept, label)
    return out


def cohorts_for(
    reader_id: str,
    contributor_ids: list[str],
    *,
    limit: int = MAX_COHORTS,
) -> list[dict[str, Any]]:
    """Claims the reader shares with SEVERAL contributors, most-shared first.

    Each entry: {concept, label, n, total, peer_user_ids}. `n` of `total` is the whole
    claim — "4 of these 8" — so both travel together and a client must not render one
    without the other.
    """
    ids = [str(u) for u in dict.fromkeys(contributor_ids) if str(u or "").strip()]
    if not reader_id or len(ids) < MIN_COHORT:
        return []

    mine = _claims_by_concept(reader_id)
    if not mine:
        return []

    holders: dict[str, list[str]] = {}
    for peer_id in ids:
        if peer_id == reader_id:
            continue
        for concept in _claims_by_concept(peer_id):
            if concept in mine:
                holders.setdefault(concept, []).append(peer_id)

    out = [
        {
            "concept": concept,
            # The READER's wording for the shared claim. Both sides proved it; using her
            # own label keeps the line in words she already recognises, and avoids
            # publishing a stranger's phrasing of something personal.
            "label": mine[concept],
            "n": len(peers),
            "total": len(ids),
            "peer_user_ids": sorted(peers),
        }
        for concept, peers in holders.items()
        if len(peers) >= MIN_COHORT
    ]
    # Most-shared first; label as the tiebreak so the order is total and two reads of one
    # card cannot disagree.
    out.sort(key=lambda c: (-c["n"], c["label"].casefold()))
    return out[:limit]


def cohort_within(
    cohort: dict[str, Any], peer_ids: list[str]
) -> dict[str, Any] | None:
    """The same cohort, narrowed to a subset — "4 of these 8 have toddlers, like you".

    Screen 08 renders this inside a theme: of the people who said one thing, how many are
    like the reader. Pure set intersection over ids the card already holds, so it costs no
    extra read and cannot disagree with the card-level number it was derived from.
    """
    if not cohort:
        return None
    subset = {str(p) for p in peer_ids if str(p or "").strip()}
    if not subset:
        return None
    inside = sorted(set(cohort.get("peer_user_ids") or []) & subset)
    if len(inside) < MIN_COHORT:
        return None
    return {
        "concept": cohort.get("concept"),
        "label": cohort.get("label"),
        "n": len(inside),
        # Out of the people in THIS theme, not out of everyone on the card.
        "total": len(subset),
        "peer_user_ids": inside,
    }
