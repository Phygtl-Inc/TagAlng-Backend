"""C-FIND-MOM-RESULTS: ranked peer cards + weak-match prompt metadata for FE."""

from __future__ import annotations

from typing import Any

from app.ui_actions import peer_card_nudge_action, weak_match_prompt_actions

_STRONG_MIN = 0.80
_PARTIAL_MIN = 0.65


def _score_value(row: dict[str, Any]) -> float:
    raw = row.get("similarity_score")
    try:
        if raw is None:
            return 0.0
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def score_to_stars(score: float) -> int:
    if score >= 0.90:
        return 5
    if score >= 0.80:
        return 4
    if score >= 0.65:
        return 3
    if score >= 0.50:
        return 2
    return 1


def match_band(score: float) -> str:
    if score >= _STRONG_MIN:
        return "strong"
    if score >= _PARTIAL_MIN:
        return "partial"
    return "weak"


def match_badge(band: str, stars: int, *, shared_count: int = 0) -> str:
    """Badge states how much is PROVEN shared, not how high one cosine got:
    3+ shared claims = PERFECT FIT, 2 = STRONG, 1 = FIT; fuzzy-only falls back
    to the score band (never above FIT — unproven overlap can't be perfect)."""
    if shared_count >= 3:
        return "PERFECT FIT"
    if shared_count == 2:
        return "STRONG"
    if shared_count == 1:
        return "FIT"
    if band == "strong":
        return "FIT"
    if band == "partial":
        return "PARTIAL"
    return "WEAK"


# Seeded boilerplate claim labels that say nothing about a person — never chips.
# (Pre-existing filter, unchanged; real dedup/ranking stays with the matcher.)
_GENERIC_TAGS = {
    "block resident",
    "lives on my block",
    "neighborhood connection",
}


def _clean_tags(parts: list[str], *, max_tags: int = 5) -> list[str]:
    out: list[str] = []
    for part in parts:
        part = part.strip()
        if len(part) < 2 or part.lower() in _GENERIC_TAGS:
            continue
        if part not in out:
            out.append(part)
        if len(out) >= max_tags:
            break
    return out


def trait_tags_from_label(label: str, *, max_tags: int = 5) -> list[str]:
    return _clean_tags(str(label or "").split("·"), max_tags=max_tags)


def compose_match_reason(row: dict[str, Any]) -> tuple[str | None, list[str]]:
    """Truthful (display_label, trait_tags) for a scored match.

    Every exact-concept shared claim is listed ("You both: A · B"); a fuzzy-only
    match shows both sides so the user can judge the overlap themselves.
    Returns (None, []) when the matcher gave us no caller-side claim to stand on.
    """
    raw_shared = row.get("shared_labels")
    shared = _clean_tags(
        [str(s) for s in raw_shared] if isinstance(raw_shared, list) else [], max_tags=3
    )
    if shared:
        return "You both: " + " · ".join(shared), shared
    my_label = str(row.get("matching_my_label") or "").strip()
    peer_label = str(row.get("matching_peer_label") or "").strip()
    if not my_label or not peer_label:
        return None, []
    if bool(row.get("has_exact_concept_match")) or my_label.lower() == peer_label.lower():
        return f"You both: {peer_label}", _clean_tags([peer_label])
    return f"You: {my_label} · Them: {peer_label}", _clean_tags([my_label, peer_label])


def enrich_peer_match_row(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    if out.get("tip_rec"):
        # A recommendation-cascade row (app/tip_rec_cascade.py) arrives fully formed: its
        # tags are the TIP's, and it deliberately carries no stars, band or badge because
        # nothing here compared two people. Enriching it would either wipe those tags (the
        # unscored branch below) or invent an affinity we never computed.
        return out
    scored = out.get("similarity_score") is not None
    if not scored and not out.get("onion_match"):
        # Unscored row (block-preview fallback): a neighbor, not a match — never
        # dress it up with stars/badges or claim an affinity we didn't compute.
        out["match_stars"] = None
        out["match_band"] = None
        out["match_badge"] = None
        out["trait_tags"] = []
        return out
    raw_shared = out.get("shared_labels")
    shared_count = len(
        _clean_tags([str(s) for s in raw_shared], max_tags=10)
        if isinstance(raw_shared, list)
        else []
    )
    if not shared_count and bool(out.get("has_exact_concept_match")):
        shared_count = 1  # legacy rows without shared_labels: the best pair is exact
    if scored:
        score = _score_value(out)
        stars = score_to_stars(score)
        band = match_band(score)
    else:
        # Onion-proven row (same place / exact shared concepts, no cosine was
        # computed): the badge rides the proven-shared ladder alone; stars and
        # band stay None — we never invent a similarity we didn't compute.
        stars = None
        band = None
    out["match_stars"] = stars
    out["match_band"] = band
    out["match_badge"] = match_badge(band or "weak", stars or 0, shared_count=shared_count)
    out["shared_count"] = shared_count
    display_label, tags = compose_match_reason(out)
    if display_label:
        out["matching_peer_label"] = display_label
        out["trait_tags"] = tags
    else:
        out["trait_tags"] = trait_tags_from_label(str(out.get("matching_peer_label") or ""))
    return out


def enrich_peer_match_rows(
    rows: list[dict[str, Any]],
    *,
    phone_verified: bool,
) -> list[dict[str, Any]]:
    enriched = [enrich_peer_match_row(r) for r in rows if isinstance(r, dict)]
    # Proven overlap outranks one high cosine: shared-claim count, then stars/score.
    enriched.sort(
        key=lambda r: (
            -int(r.get("shared_count") or 0),
            -int(r.get("match_stars") or 0),
            -_score_value(r),
        )
    )
    if not phone_verified:
        for row in enriched:
            row.pop("actions", None)
    return enriched


def attach_peer_card_actions(
    rows: list[dict[str, Any]],
    *,
    phone_verified: bool,
) -> list[dict[str, Any]]:
    if not phone_verified:
        return rows
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        if item.get("preview"):
            item.pop("actions", None)
            out.append(item)
            continue
        peer_id = str(item.get("peer_user_id") or "").strip()
        nick = str(item.get("nickname") or "").strip()
        if item.get("connection"):
            # Intro already sent, or the two already connected — a nudge here can only
            # fail (7-day pair cooldown / duplicate_intro_recent).
            item.pop("actions", None)
            out.append(item)
            continue
        if peer_id and nick:
            item["actions"] = [
                peer_card_nudge_action(nickname=nick, peer_user_id=peer_id),
            ]
        else:
            item.pop("actions", None)
        out.append(item)
    return out


def build_discovery_surface(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not rows:
        return None
    # Unscored rows (no match_band) are block-preview neighbors, not matches —
    # they never get a rank, a star count, or a weak-match nudge.
    scored = [r for r in rows if r.get("match_band")]
    unscored = len(rows) - len(scored)
    strong = sum(1 for r in scored if r.get("match_band") == "strong")
    partial = sum(1 for r in scored if r.get("match_band") == "partial")
    weak = sum(1 for r in scored if r.get("match_band") == "weak")
    parts: list[str] = []
    if strong:
        parts.append(f"{strong} strong fit{'s' if strong != 1 else ''}")
    if partial:
        parts.append(f"{partial} partial")
    if unscored:
        parts.append(f"{unscored} neighbor{'s' if unscored != 1 else ''} near you")
    if not parts:
        parts.append(f"{len(rows)} neighbor{'s' if len(rows) != 1 else ''}")
    status_label = " · ".join(parts)
    weak_peer: dict[str, Any] | None = None
    weak_rows = [r for r in scored if r.get("match_band") == "weak" and r.get("peer_user_id")]
    if weak_rows and (strong or partial):
        weakest = min(weak_rows, key=_score_value)
        weak_peer = {
            "peer_user_id": weakest.get("peer_user_id"),
            "nickname": weakest.get("nickname"),
            "match_stars": weakest.get("match_stars"),
            "match_badge": weakest.get("match_badge"),
        }
    return {
        "strong_count": strong,
        "partial_count": partial,
        "weak_count": weak,
        "status_label": status_label,
        "weak_peer": weak_peer,
        "ranked_summary": " · ".join(
            f"{str(r.get('nickname') or 'Neighbor')[:12].upper()} {r.get('match_stars')}/5"
            for r in scored[:5]
        )
        or None,
    }


# Tiers that mean the two have actually connected — a Nudge button here is a dead end:
# the 7-day per-pair cooldown rejects it, and the user is being offered a stranger's
# affordance for someone they already know. 'nudge' means one is out, awaiting a reply.
_CONNECTED_TIERS = frozenset({"acquaintance", "direct", "irl_peer"})


def stamp_connection_state(rows: list[dict[str, Any]], *, user_id: str) -> None:
    """Mark rows the caller already has a relationship with, in place.

    The tier is recorded (user_relationships, promoted on nudge/accept) but no peer-search
    path reads it — they filter blocked users only. So Lana kept re-offering an intro to
    someone the user had already nudged, accepted and messaged, and the send then failed.
    Best-effort: a lookup failure just leaves the cards as they were.
    """
    ids = [
        str(r["peer_user_id"])
        for r in rows
        if isinstance(r, dict) and r.get("peer_user_id") and not r.get("connection")
    ]
    if not user_id or not ids:
        return
    try:
        from app.auth import service_client

        res = service_client().rpc(
            "get_relationship_tiers_for_user",
            {"p_user_id": user_id, "p_other_user_ids": list(dict.fromkeys(ids))},
        ).execute()
        tiers = {
            str(r["other_user_id"]): str(r.get("tier") or "stranger")
            for r in (res.data or [])
            if r.get("other_user_id")
        }
    except Exception:  # noqa: BLE001 - cards must render even if the tier lookup fails
        import logging

        logging.getLogger(__name__).exception("relationship_tier_lookup_failed")
        return
    for row in rows:
        if not isinstance(row, dict) or row.get("connection"):
            continue
        tier = tiers.get(str(row.get("peer_user_id") or ""))
        if tier in _CONNECTED_TIERS:
            row["connection"] = "connected"
        elif tier == "nudge":
            row["connection"] = "intro_sent"


def stamp_peer_discovery_ctx(
    ctx: dict[str, Any], *, phone_verified: bool, user_id: str = ""
) -> None:
    """Enrich peer_matches + discovery_surface on ctx (additive, safe for legacy FE)."""
    raw = ctx.get("peer_matches")
    if not isinstance(raw, list) or not raw:
        return
    if any(isinstance(r, dict) and r.get("tip_rec") for r in raw):
        # Recommendation-cascade turn: the rows and their counts strip were built by
        # tip_rec_cascade, which ranks by the rec, not by claim affinity. Re-ranking them
        # here (shared_count / stars, none of which these rows have) would scramble the
        # order the user was just shown.
        if not phone_verified:
            for row in raw:
                if isinstance(row, dict):
                    row.pop("actions", None)
        return
    enriched = enrich_peer_match_rows(raw, phone_verified=phone_verified)
    # Before actions are attached — attach_peer_card_actions skips connected rows.
    stamp_connection_state(enriched, user_id=user_id)
    enriched = attach_peer_card_actions(enriched, phone_verified=phone_verified)
    ctx["peer_matches"] = enriched
    surface = build_discovery_surface(enriched)
    if surface:
        ctx["discovery_surface"] = surface


def weak_match_ui_actions(ctx: dict[str, Any]) -> list[dict[str, Any]]:
    surface = ctx.get("discovery_surface")
    if not isinstance(surface, dict):
        return []
    weak = surface.get("weak_peer")
    if not isinstance(weak, dict) or not weak.get("peer_user_id"):
        return []
    nick = str(weak.get("nickname") or "them").strip() or "them"
    return weak_match_prompt_actions(
        nickname=nick,
        peer_user_id=str(weak.get("peer_user_id") or "") or None,
    )
