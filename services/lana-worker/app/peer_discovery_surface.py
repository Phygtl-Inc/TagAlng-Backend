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
    raw_kids = row.get("shared_child_labels")
    kids = _clean_tags(
        [str(s) for s in raw_kids] if isinstance(raw_kids, list) else [], max_tags=3
    )
    if shared or kids:
        # Two subjects, two clauses. A claim held about a child says nothing
        # about the adult reading it, so it never rides on "You both".
        clauses = []
        if shared:
            clauses.append("You both: " + " · ".join(shared))
        if kids:
            clauses.append("Your kids both: " + " · ".join(kids))
        return " · ".join(clauses), shared + kids
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
    raw_kids = out.get("shared_child_labels")
    shared_count = len(
        _clean_tags(
            [
                str(s)
                for key in (raw_shared, raw_kids)
                if isinstance(key, list)
                for s in key
            ],
            max_tags=10,
        )
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


def peer_tiers(user_id: str, peer_ids: list[str]) -> dict[str, str]:
    """{peer_user_id: relationship tier} for the caller. {} on any failure.

    Fail-open by design: every caller degrades to today's behaviour when the lookup
    breaks, rather than hiding neighbors or blocking an intro on an infra blip.
    """
    ids = [i for i in dict.fromkeys(str(p) for p in peer_ids) if i]
    if not user_id or not ids:
        return {}
    try:
        from app.auth import service_client

        res = service_client().rpc(
            "get_relationship_tiers_for_user",
            {"p_user_id": user_id, "p_other_user_ids": ids},
        ).execute()
        return {
            str(r["other_user_id"]): str(r.get("tier") or "stranger")
            for r in (res.data or [])
            if r.get("other_user_id")
        }
    except Exception:  # noqa: BLE001 - never let a tier lookup break a search or a card
        import logging

        logging.getLogger(__name__).exception("relationship_tier_lookup_failed")
        return {}


def drop_connected_peers(
    rows: list[dict[str, Any]], *, user_id: str | None, keep_connected: bool = False
) -> list[dict[str, Any]]:
    """Peers the caller already connected with are not candidates — filter at the source.

    `keep_connected=True` for a FACTUAL search ("who plays laser tag?"): the answer to a
    question about the neighborhood is not an intro pitch, so dropping the one neighbor
    who matches made Lana say "nobody has popped up for laser tag yet" about someone the
    user already knows who does exactly that (prod, 2026-08-19). Those rows are kept and
    stamped `connection` instead, which takes the Nudge button off and lets her say "you
    two already know each other" rather than nothing at all.


    No peer source consults user_relationships: they match on claims and proximity and
    filter blocked users only. So an accepted nudge never stopped Lana re-offering the
    same neighbor as a fresh intro, and the send then died on the 7-day pair cooldown
    ("I couldn't send that nudge right now — try again in a moment", forever).

    Dropping here rather than on the card is what keeps her prose honest: with the row
    gone, an exhausted search takes the real "nobody new yet" branch instead of pitching
    an intro to someone the user already knows. 'nudge' rows stay — one is genuinely out
    awaiting a reply, which the card labels intro_sent instead of offering Nudge again.

    Those surviving 'nudge' rows are STAMPED here, not only on the card. The tier lookup
    is already paid for, and stamp_connection_state runs after the reply is composed — so
    the card showed Daniel with a "✓ Sent" badge under prose reading "Want an intro?"
    (2026-08-18). The reply writer needs the same fact the button has, at the one place
    every peer source passes through.
    """
    if not user_id or not rows:
        return rows
    tiers = peer_tiers(
        user_id,
        [str(r.get("peer_user_id") or "") for r in rows if isinstance(r, dict)],
    )
    if not tiers:
        return rows
    kept: list[dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict):
            kept.append(r)
            continue
        tier = tiers.get(str(r.get("peer_user_id") or ""))
        if tier in _CONNECTED_TIERS:
            if not keep_connected:
                continue
            r.setdefault("connection", tier)
        elif tier == "nudge" and not r.get("connection"):
            r["connection"] = "intro_sent"
        kept.append(r)
    return kept


def stamp_connection_state(rows: list[dict[str, Any]], *, user_id: str) -> None:
    """Mark rows the caller already has a relationship with, in place.

    Backstop to drop_connected_peers for rows that reached a card without passing a
    peer source (stale session ctx, orchestrator tool results): the Nudge button comes
    off, because a nudge here can only fail the pair cooldown.
    """
    ids = [
        str(r["peer_user_id"])
        for r in rows
        if isinstance(r, dict) and r.get("peer_user_id") and not r.get("connection")
    ]
    if not user_id or not ids:
        return
    tiers = peer_tiers(user_id, ids)
    if not tiers:
        return
    for row in rows:
        if not isinstance(row, dict) or row.get("connection"):
            continue
        tier = tiers.get(str(row.get("peer_user_id") or ""))
        if tier in _CONNECTED_TIERS:
            row["connection"] = "connected"
        elif tier == "nudge":
            row["connection"] = "intro_sent"


def drop_stale_intro_offer(ctx: dict[str, Any], rows: list[Any]) -> None:
    """A single-peer intro offer cannot outlive the turn that showed that one peer.

    `pending_intro_offer` is deliberately cross-turn state — the accept ("yes") arrives a
    turn after the offer. But derive_ui_intent returns offer_neighbor_intro whenever it is
    armed, which renders the SINGLE-card intro surface. So a later turn that ships a real
    list drew one card under prose counting three, and the card belonged to the old offer
    (QA 2026-08-18: "There are 3 people near you…" over one Sofia card with her nudge
    chips, session ctx holding three rows).

    The rule: the offer survives only while the turn shows exactly its own candidate.
    Anything else — a list, a different peer, a recommendation strip — means the surface
    moved on and the offer goes with it. Enforced here rather than in any one lane because
    the turn that exposed this came through the orchestrator's find_peers tool, which
    never touches the offer at all; every path that ships peer rows passes through this
    function on its way to derive_ui_intent.

    A turn with NO peer rows clears nothing (the caller returns before this) — that is the
    accept window, and the offer has to still be there to be accepted.
    """
    offer = ctx.get("pending_intro_offer")
    if not isinstance(offer, dict):
        return
    candidate = str(offer.get("candidate_user_id") or "").strip()
    ids = [
        str(r.get("peer_user_id") or "").strip()
        for r in rows
        if isinstance(r, dict) and r.get("peer_user_id")
    ]
    if candidate and ids == [candidate]:
        return
    import logging

    from app.intro_list import clear_intro_offer_ctx

    clear_intro_offer_ctx(ctx)
    logging.getLogger(__name__).info(
        "stale_intro_offer_dropped candidate=%s rows=%d", candidate or None, len(ids)
    )


def stamp_peer_discovery_ctx(
    ctx: dict[str, Any], *, phone_verified: bool, user_id: str = ""
) -> None:
    """Enrich peer_matches + discovery_surface on ctx (additive, safe for legacy FE)."""
    raw = ctx.get("peer_matches")
    if not isinstance(raw, list) or not raw:
        return
    # Ahead of every early return below: a tip-rec strip or an unverified list moves the
    # surface just as much as a peer list does.
    drop_stale_intro_offer(ctx, raw)
    if any(
        isinstance(r, dict) and (r.get("tip_rec") or r.get("community_roster")) for r in raw
    ):
        # Recommendation-cascade turn: the rows and their counts strip were built by
        # tip_rec_cascade, which ranks by the rec, not by claim affinity. Re-ranking them
        # here (shared_count / stars, none of which these rows have) would scramble the
        # order the user was just shown.
        # A community roster is the same contract: built by community_members, ordered
        # members-then-curious, and carrying the members' own threads as chips — the
        # unscored branch below would wipe those chips and invent nothing in their place.
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
