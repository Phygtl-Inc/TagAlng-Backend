"""C-FIND-MOM-RESULTS: ranked peer cards + weak-match prompt metadata for FE."""

from __future__ import annotations

import os
import re
from typing import Any

from app.ui_actions import peer_card_nudge_action, weak_match_prompt_actions

_STRONG_MIN = 0.80
_PARTIAL_MIN = 0.65

# ── decision context (QA 2026-07-08: cards showed one generic label + a percent) ──
#
# Kid-stage BANDS only — derived from the wording of the peer's own public claim
# label, never from a stored age (child ages/names are never persisted; see
# app/pii.py and the vertex_extract "never capture a child's age" rules).
_STAGE_BAND_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("expecting", re.compile(r"\b(expecting|pregnan\w*|due\s+(?:in|this|next)|mo[mu]-to-be)\b", re.I)),
    ("baby", re.compile(r"\b(newborns?|infants?|bab(?:y|ies))\b", re.I)),
    ("toddler", re.compile(r"\btoddlers?\b", re.I)),
    ("prek", re.compile(r"\b(pre-?k\b|preschool\w*)\b", re.I)),
    ("school", re.compile(r"\b(school-?age\w*|kindergart\w*|elementary|grade-?school\w*)\b", re.I)),
)

# Backstop: a reason fragment that looks like a raw age never reaches a card.
# (Claims are already age-scrubbed at write time — this keeps the surface safe
# even if a legacy/unscrubbed label slips through.)
_AGE_FRAGMENT_RE = re.compile(
    r"\b(?:aged?\s*\d{1,2}|\d{1,2}\s*(?:yo|y/o|yrs?\s*old|years?\s*old|mos?\s*old|months?\s*old))\b",
    re.I,
)

# Words kept lowercase inside Title Case reasons ("Mom of Toddlers").
_TITLE_SMALL_WORDS = frozenset(
    {"a", "an", "and", "at", "by", "for", "in", "of", "on", "or", "the", "to", "with"}
)


def stage_band_from_text(text: str | None) -> str | None:
    """Band only (expecting|baby|toddler|prek|school) from claim-label wording."""
    hay = str(text or "")
    if not hay:
        return None
    for band, pattern in _STAGE_BAND_PATTERNS:
        if pattern.search(hay):
            return band
    return None


def titleize_reason(text: str) -> str:
    """Consistent Title Case for card reasons ("Enjoys playgrounds" → "Enjoys Playgrounds")."""
    words = str(text or "").split()
    out: list[str] = []
    for i, word in enumerate(words):
        low = word.lower()
        if i > 0 and low in _TITLE_SMALL_WORDS:
            out.append(low)
        elif word.isupper() and len(word) > 1:
            out.append(word)  # keep acronyms (PTA, ESL)
        else:
            out.append(word[:1].upper() + word[1:])
    return " ".join(out)


def shared_reasons_from_label(label: str, *, max_reasons: int = 3) -> list[str]:
    """Up to 3 human-readable shared reasons, title-cased, age-fragment-free."""
    out: list[str] = []
    seen: set[str] = set()
    for tag in trait_tags_from_label(label, max_tags=max_reasons * 2):
        if _AGE_FRAGMENT_RE.search(tag):
            continue
        reason = titleize_reason(tag)
        key = reason.lower()
        if not reason or key in seen:
            continue
        seen.add(key)
        out.append(reason)
        if len(out) >= max_reasons:
            break
    return out


def match_tier(shared_count: int, band: str) -> str:
    """"great" needs >= 2 shared dimensions (a one-trait match can't support it)."""
    return "great" if shared_count >= 2 and band != "weak" else "good"


# ── moms-first ranking (QA: a man ranked #1 in a moms-first product) ─────────


def moms_first_ranking_enabled() -> bool:
    """LANA_MOMS_FIRST_RANKING — default ON; "0"/"false"/"off"/"no" disables."""
    flag = os.environ.get("LANA_MOMS_FIRST_RANKING", "1").strip().lower()
    return flag not in {"0", "false", "off", "no"}


_FAMILY_ASK_RE = re.compile(
    r"\b(dads?|fathers?|famil(?:y|ies)|couples?|partners?|husbands?|parents?|whole\s+famil\w*)\b",
    re.I,
)


def ask_includes_family_context(text: str | None) -> bool:
    """True when the ask explicitly widens beyond moms (dads/family/parents/couples)."""
    return bool(_FAMILY_ASK_RE.search(str(text or "")))


_FEMALE_MARKERS = frozenset({"female", "woman", "f", "mom", "mother", "mum", "mama"})


def moms_first_rank_multiplier(row: dict[str, Any]) -> float:
    """Damp (not exclude) non-female candidates in mom-seeking-moms ranking.

    TODO(moms-first data): gender is NOT stored today — the extractor is forbidden
    from capturing sex/gender (see app/vertex_extract.py) and public.users has no
    gender column, so match_peers_by_claim_vectors rows never carry one. Until an
    explicit self-declared mom/dad profile field is surfaced through the peer RPCs,
    rows have no "gender" key and this multiplier is a production no-op (1.0).
    The mechanism below activates automatically once the RPC exposes the field.
    """
    gender = str(row.get("gender") or "").strip().lower()
    if not gender or gender in _FEMALE_MARKERS:
        return 1.0
    return 0.5


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
    label = str(out.get("matching_peer_label") or "")
    shared = shared_reasons_from_label(label)
    out["match_stars"] = stars
    out["match_band"] = band
    out["match_badge"] = match_badge(band, stars)
    # Casing cleanup at the source: display reasons are consistently Title Case.
    out["trait_tags"] = shared_reasons_from_label(label, max_reasons=5)
    # Decision context a mom actually weighs on a card:
    out["shared"] = shared
    out["stage_band"] = stage_band_from_text(label)
    # Peer matching is home-block-scoped by construction (match_peers_by_claim_vectors
    # filters on users.home_block_id) and no finer geo exists per user, so the honest
    # distance is block-level. blocks_away is not derivable today.
    out["distance_label"] = "On your block"
    out["tier"] = match_tier(len(shared), band)
    # similarity_score stays in the payload for telemetry only — never render it.
    out["display_score"] = False
    return out


def enrich_peer_match_rows(
    rows: list[dict[str, Any]],
    *,
    phone_verified: bool,
    ask_text: str | None = None,
) -> list[dict[str, Any]]:
    enriched = [enrich_peer_match_row(r) for r in rows if isinstance(r, dict)]
    # Moms-first ranking (default ON): in a mom-seeking-moms discovery, non-female
    # candidates are damped in the RANKING key only (never excluded); an ask that
    # explicitly includes dads/family context opts out. See moms_first_rank_multiplier
    # for the production data caveat (gender not stored yet → no-op).
    moms_first = moms_first_ranking_enabled() and not ask_includes_family_context(ask_text)

    def _rank_key(r: dict[str, Any]) -> tuple[int, float]:
        mult = moms_first_rank_multiplier(r) if moms_first else 1.0
        rank_score = _score_value(r) * mult
        has_exact = bool(r.get("has_exact_concept_match"))
        return (-score_to_stars(rank_score, has_exact=has_exact), -rank_score)

    enriched.sort(key=_rank_key)
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


def stamp_peer_discovery_ctx(
    ctx: dict[str, Any],
    *,
    phone_verified: bool,
    ask_text: str | None = None,
) -> None:
    """Enrich peer_matches + discovery_surface on ctx (additive, safe for legacy FE)."""
    raw = ctx.get("peer_matches")
    if not isinstance(raw, list) or not raw:
        return
    enriched = enrich_peer_match_rows(raw, phone_verified=phone_verified, ask_text=ask_text)
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
