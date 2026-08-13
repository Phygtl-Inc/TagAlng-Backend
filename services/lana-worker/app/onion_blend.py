"""Onion scores → the find-peers turn (Circles §C, consumption side).

app/onion.py ranks introduction candidates but nothing served them to a user
until now. This module folds that ranking into the verified peer-match list the
find-peers turn already shows, without claiming anything the matcher didn't
prove (truthful-match model):

  * A vector-matched peer who also overlaps on circles gets the proof attached —
    shared onion concepts join `shared_labels`, and a proven same-place overlap
    adds a caller-relative tag ("your gym"). The existing proven-shared badge
    ladder (peer_discovery_surface) then ranks them honestly higher; no second
    scoring path.
  * A same-place peer the block-scoped vector matcher can never see (communities
    reach beyond the block) is appended as a new candidate — ONLY on a proven
    place overlap (same_place_bonus > 0). Type-only or concept-only strangers
    outside the block stay invisible: that reach is junk-quality and the privacy
    cost is real.

Disclosure (§F, open question O7): the place is never named. The tag names the
RELATION via the caller's OWN circle_type ("your gym") — the viewer learns only
that this peer goes where the viewer already knows they themselves go.

Fail-open everywhere: any error returns the vector list unchanged — the onion
must never cost a user the matches they already had. The §D.2 peers gate is
enforced inside app/onion.py; a gated result blends nothing (the gate *turn*
itself is composed upstream by _zip_gate_peers_turn).
"""

from __future__ import annotations

import logging
import os
from typing import Any

from app.auth import service_client
from app.circles_flow import place_relation_noun
from app.onion import score_onion_candidates

logger = logging.getLogger(__name__)

# Concept labels merged per row — the "You both:" reason line stays scannable.
_MAX_ONION_LABELS = 3


def onion_blend_enabled() -> bool:
    return os.environ.get("LANA_ONION_MATCHER", "on").strip().lower() not in (
        "0",
        "off",
        "false",
        "no",
    )


def _caller_place_tags(user_id: str) -> dict[str, str]:
    """place id -> "your gym" for the caller's own confirmed grounded circles."""
    try:
        res = (
            service_client()
            .table("circle_affiliations")
            .select("place_ref, circle_type, circle_key, noun")
            .eq("user_id", user_id)
            .eq("status", "confirmed")
            .is_("dismissed_at", "null")
            .not_.is_("place_ref", "null")
            .limit(40)
            .execute()
        )
        rows = res.data if isinstance(res.data, list) else []
    except Exception:
        logger.exception("onion_blend_place_tags_failed user=%s", user_id)
        return {}
    tags: dict[str, str] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        pid = str(r.get("place_ref") or "")
        if pid and pid not in tags:
            tags[pid] = (
                "your "
                f"{place_relation_noun(r.get('circle_type'), r.get('noun'), r.get('circle_key'))}"
            )
    return tags


def _merged_labels(
    existing: Any,
    place_tag: str | None,
    concept_labels: list[str],
) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()

    def _add(label: Any) -> None:
        s = str(label or "").strip()
        if s and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)

    _add(place_tag)  # the strongest proof reads first
    if isinstance(existing, list):
        for s in existing:
            _add(s)
    for s in concept_labels[:_MAX_ONION_LABELS]:
        _add(s)
    return out


def _proof_count(row: dict[str, Any]) -> int:
    labels = row.get("shared_labels")
    return len(labels) if isinstance(labels, list) else 0


def blend_onion_matches(
    peers: list[dict[str, Any]],
    *,
    user_id: str | None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Blend onion candidates into the vector `peers` list.

    Returns `peers` untouched when disabled, unauthenticated, gated, empty, or
    on any error. Otherwise returns at most `limit` rows ordered by proven
    overlap first, then onion score, then the vector order the list arrived in
    (display gets its final truthful re-rank in enrich_peer_match_rows).
    """
    if not user_id or not onion_blend_enabled():
        return peers
    try:
        result = score_onion_candidates(user_id, limit=max(20, limit * 4))
    except Exception:
        logger.exception("onion_blend_score_failed user=%s", user_id)
        return peers
    candidates = [c for c in (result.get("candidates") or []) if isinstance(c, dict)]
    if result.get("gated") or not candidates:
        return peers

    place_tags = _caller_place_tags(user_id)
    blended: list[dict[str, Any]] = [dict(p) for p in peers if isinstance(p, dict)]
    by_uid = {str(r.get("peer_user_id") or ""): r for r in blended}

    appended = 0
    for cand in candidates:
        uid = str(cand.get("user_id") or "")
        if not uid:
            continue
        same_place = int(cand.get("same_place_bonus") or 0) > 0
        place_tag = (
            place_tags.get(str(cand.get("shared_place_ref") or "")) if same_place else None
        )
        concept_labels = [str(s) for s in (cand.get("shared_concept_labels") or [])]
        row = by_uid.get(uid)
        if row is None:
            # A new seat is earned by a PROVEN shared place alone — the vector
            # matcher is block-scoped on purpose, and reaching beyond the block
            # is justified only by "you two already go to the same spot".
            if not place_tag:
                continue
            row = {
                "peer_user_id": uid,
                "nickname": cand.get("nickname"),
                "avatar_url": cand.get("avatar_url"),
                # No cosine was computed for this pair — never invent one.
                "similarity_score": None,
                "matching_peer_label": place_tag,
                "matching_peer_concept": None,
                "has_exact_concept_match": False,
                "shared_labels": [],
            }
            blended.append(row)
            by_uid[uid] = row
            appended += 1
        row["shared_labels"] = _merged_labels(row.get("shared_labels"), place_tag, concept_labels)
        if int(cand.get("shared_concept_count") or 0) > 0:
            row["has_exact_concept_match"] = True
        row["onion_match"] = True
        row["onion_score"] = int(cand.get("score") or 0)
        row["same_place_bonus"] = int(cand.get("same_place_bonus") or 0)
        row["same_type_bonus"] = int(cand.get("same_type_bonus") or 0)
        row["shared_place_ref"] = cand.get("shared_place_ref")

    if appended:
        logger.info("onion_blend user=%s appended=%d", user_id, appended)

    order = {id(r): i for i, r in enumerate(blended)}
    blended.sort(
        key=lambda r: (-_proof_count(r), -int(r.get("onion_score") or 0), order[id(r)])
    )
    return blended[: max(limit, 1)]
