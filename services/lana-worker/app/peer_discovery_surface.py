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


def score_to_stars(score: float, *, has_exact: bool = False) -> int:
    if has_exact and score >= 0.75:
        return 5
    if score >= 0.90:
        return 5
    if score >= 0.80:
        return 4
    if score >= 0.65:
        return 3
    if score >= 0.50:
        return 2
    return 1


def match_band(score: float, *, has_exact: bool = False) -> str:
    if score >= _STRONG_MIN or (has_exact and score >= 0.75):
        return "strong"
    if score >= _PARTIAL_MIN:
        return "partial"
    return "weak"


def match_badge(band: str, stars: int) -> str:
    if band == "strong" and stars >= 5:
        return "PERFECT FIT"
    if band == "strong":
        return "STRONG"
    if band == "partial":
        return "PARTIAL"
    return "WEAK"


def trait_tags_from_label(label: str, *, max_tags: int = 5) -> list[str]:
    parts = [p.strip() for p in str(label or "").split("·") if p.strip()]
    out: list[str] = []
    for part in parts:
        if len(part) < 2:
            continue
        if part.lower() in {"block resident", "lives on my block", "neighborhood connection"}:
            continue
        out.append(part)
        if len(out) >= max_tags:
            break
    return out


def enrich_peer_match_row(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    score = _score_value(out)
    has_exact = bool(out.get("has_exact_concept_match"))
    stars = score_to_stars(score, has_exact=has_exact)
    band = match_band(score, has_exact=has_exact)
    out["match_stars"] = stars
    out["match_band"] = band
    out["match_badge"] = match_badge(band, stars)
    out["trait_tags"] = trait_tags_from_label(str(out.get("matching_peer_label") or ""))
    return out


def enrich_peer_match_rows(
    rows: list[dict[str, Any]],
    *,
    phone_verified: bool,
) -> list[dict[str, Any]]:
    enriched = [enrich_peer_match_row(r) for r in rows if isinstance(r, dict)]
    enriched.sort(
        key=lambda r: (
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
    strong = sum(1 for r in rows if r.get("match_band") == "strong")
    partial = sum(1 for r in rows if r.get("match_band") == "partial")
    weak = sum(1 for r in rows if r.get("match_band") == "weak")
    parts: list[str] = []
    if strong:
        parts.append(f"{strong} strong fit{'s' if strong != 1 else ''}")
    if partial:
        parts.append(f"{partial} partial")
    if not parts:
        parts.append(f"{len(rows)} neighbor{'s' if len(rows) != 1 else ''}")
    status_label = " · ".join(parts)
    weak_peer: dict[str, Any] | None = None
    weak_rows = [r for r in rows if r.get("match_band") == "weak" and r.get("peer_user_id")]
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
            f"{str(r.get('nickname') or 'Neighbor')[:12].upper()} {r.get('match_stars') or '?'}/5"
            for r in rows[:5]
        ),
    }


def stamp_peer_discovery_ctx(ctx: dict[str, Any], *, phone_verified: bool) -> None:
    """Enrich peer_matches + discovery_surface on ctx (additive, safe for legacy FE)."""
    raw = ctx.get("peer_matches")
    if not isinstance(raw, list) or not raw:
        return
    enriched = enrich_peer_match_rows(raw, phone_verified=phone_verified)
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
