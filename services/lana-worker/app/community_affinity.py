"""How well the caller fits a community she is not in — one number, and the evidence.

/lana/circles/discover answered "what is near me and how alive is it" and nothing about
the reader: every row looked equally relevant, so a gym she has nothing to do with sat
above the one full of people who describe themselves exactly as she does.

`affinity` is that answer on a 0-1 scale, blended here from the three un-weighted arms
score_community_affinity_for_user returns — the peer matcher's arms (Circles §C), scored
one level up through the community's MEMBERS because a place holds no claims of its own:

    concepts  0.50  public concepts the caller and a member BOTH hold, capped at three
                    (three proven overlaps is already "PERFECT FIT" on a peer card)
    semantic  0.35  best cosine between her public self-claims and the members', rescaled
                    from the 0.55 floor discover_communities_semantic uses
    type      0.15  she already belongs to a community of this kind

WHY THE BLEND LIVES HERE. The SQL returns components precisely so the weights can move
without a migration — the same reason score_onion_candidates_for_user returns its bonuses
separately. One place to re-tune, one place to test.

WHY IT LEANS ON PROVEN OVERLAP. A raw cosine shipped as a percentage is the failure this
repo has already had in prod (2026-09-10: one shared word scored 0.76 for every pair and
three unrelated rows rendered "76%"). So a cosine alone can reach 0.35 here and no
further; the top of the scale is reserved for overlap that has a label on both sides and
can be named out loud — which is exactly what the "why Lana sees a fit" line is written
from ([[truthful-peer-match-model]]).

`None` means UNSCORED (the read failed) and 0.0 means genuinely nothing in common. A
panel must be able to tell those apart.
"""

from __future__ import annotations

import logging
from typing import Any

from app.auth import service_client
from app.circles_flow import place_relation_noun

logger = logging.getLogger(__name__)

# The blend. Re-tune here; the SQL stays put.
_W_CONCEPTS = 0.50
_W_SEMANTIC = 0.35
_W_TYPE = 0.15

# Three proven shared concepts is the ceiling, matching the peer card's badge ladder
# (peer_discovery_surface.match_badge: 3+ shared = PERFECT FIT). A fourth says nothing
# the reader cannot already see.
_CONCEPT_FULL = 3

# Below the floor a cosine is noise — the same 0.55 discover_communities_semantic
# defaults to. Above the ceiling it has said everything it can.
_SEM_FLOOR = 0.55
_SEM_CEIL = 0.90

# Labels fed to one authored line. More than this and the composer reaches past the
# strongest threads for filler.
_MAX_BASIS_LABELS = 4
# Places scored in one call. The endpoint caps its own limit at 40; this is the guard
# for any future caller that does not.
_MAX_PLACES = 40


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _labels(value: Any) -> list[str]:
    out: list[str] = []
    for item in value if isinstance(value, list) else []:
        label = " ".join(str(item or "").split()).strip()
        if label:
            out.append(label[:80])
    return out


def _split_by_subject(labels: list[str], subjects: Any) -> tuple[list[str], list[str]]:
    """(self labels, child labels). subjects[i] describes labels[i] — the SQL aggregates
    both arrays in the same order so a missing entry can only mean 'self'."""
    subs = [str(s or "self").strip().lower() for s in (subjects if isinstance(subjects, list) else [])]
    mine: list[str] = []
    kids: list[str] = []
    for i, label in enumerate(labels):
        (kids if (subs[i] if i < len(subs) else "self") == "child" else mine).append(label)
    return mine, kids


def score_row(scored: dict[str, Any]) -> float:
    """One RPC row -> affinity in [0, 1], rounded to the two places a card can show."""
    shared = int(scored.get("shared_concept_count") or 0)
    concepts = min(shared, _CONCEPT_FULL) / _CONCEPT_FULL

    try:
        sim = float(scored.get("semantic_similarity"))
    except (TypeError, ValueError):
        sim = 0.0
    semantic = _clamp01((sim - _SEM_FLOOR) / (_SEM_CEIL - _SEM_FLOOR))

    same_type = 1.0 if scored.get("same_type") else 0.0
    return round(
        _W_CONCEPTS * concepts + _W_SEMANTIC * semantic + _W_TYPE * same_type, 2
    )


def basis_for(scored: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    """The evidence one "why Lana sees a fit" line may stand on, in plain labels.

    Proven overlap first: a shared concept's label is the caller's OWN word as much as the
    member's, so it discloses nothing about who is there. The fuzzy pair (her claim, a
    member's claim) is the fallback the peer card uses for the same case, and the bare
    "you already have one of these" is offered ONLY when nothing else is true — as one
    reason among several it is filler, and chips built from filler all read alike.
    """
    labels = _labels(scored.get("shared_concept_labels"))
    mine, kids = _split_by_subject(labels, scored.get("shared_concept_subjects"))
    basis: dict[str, Any] = {}
    if mine:
        basis["shared"] = mine[:_MAX_BASIS_LABELS]
    if kids:
        basis["kids_shared"] = kids[:_MAX_BASIS_LABELS]
    if not basis:
        you = " ".join(str(scored.get("my_label") or "").split()).strip()
        them = " ".join(str(scored.get("member_label") or "").split()).strip()
        if you and them:
            basis["you_said"] = you[:80]
            basis["members_say"] = them[:80]
    if not basis and scored.get("same_type"):
        kind = place_relation_noun(str(scored.get("matched_type") or "")) or None
        if kind:
            basis["you_already_have"] = kind
    if basis:
        # Never the place's name or address — the card already shows both, and a name in
        # the evidence is what tempts a composer to assert things about the place itself.
        basis["members"] = int(row.get("member_count") or scored.get("member_count") or 0)
    return basis


def _fetch(user_id: str, place_ids: list[str]) -> dict[str, dict[str, Any]]:
    """{place_id: scored row} from the RPC. {} on any failure — an unscored panel still
    lists real communities, which is the whole point of the surface."""
    if not user_id or not place_ids:
        return {}
    try:
        res = service_client().rpc(
            "score_community_affinity_for_user",
            {"p_user_id": user_id, "p_place_ids": place_ids[:_MAX_PLACES]},
        ).execute()
        rows = res.data if isinstance(res.data, list) else []
    except Exception:  # noqa: BLE001 — no score is a plain list, never a 500
        logger.exception("community_affinity_failed user=%s", user_id)
        return {}
    return {
        str(r.get("place_id")): r
        for r in rows
        if isinstance(r, dict) and str(r.get("place_id") or "").strip()
    }


def attach_affinity(user_id: str, rows: list[dict[str, Any]]) -> None:
    """Set `affinity` on each discovery row, in place, and stash its evidence.

    The evidence rides on the row as `_fit_basis` for app/community_fit_line.py to pop —
    the two are computed from ONE read on purpose, so the number and the sentence can
    never be built from different facts.
    """
    for row in rows:
        row.setdefault("affinity", None)
        row.setdefault("_fit_basis", None)
    if not user_id or not rows:
        return
    scored = _fetch(user_id, [str(r.get("place_id") or "") for r in rows if r.get("place_id")])
    if not scored:
        return
    for row in rows:
        hit = scored.get(str(row.get("place_id") or ""))
        if hit is None:
            continue
        row["affinity"] = score_row(hit)
        row["_fit_basis"] = basis_for(hit, row) or None
