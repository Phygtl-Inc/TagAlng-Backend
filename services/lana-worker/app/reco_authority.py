"""Recommender standing on a results page — "a barber recommended by someone from Spain".

SPEC_RECOMMENDER_AUTHORITY P1 (ranking) and P3 (the reason). app/authority.py is the read
side of attester_authority(); until this module the only reader was the directed ask
(tip_ask_route). This puts the same standing on the ordinary tip search.

What it answers is a requirement on WHO recommends, never on the thing: the barber is not
Spanish because a Spaniard recommended him (RecoCardRow says so). The requirement comes
from reco_aspects.split_query_full's `recommender_trait`; a bare "Spanish barber" has none
and this module does nothing — that ask is about the barber, and aspect search owns it.

Three rules, each from a decision already taken elsewhere:

  * Tier, not gate (authority-tier-not-gate, 2026-09-18). Recommendations whose author has
    strong standing (>= MIN_EXPLICIT_SCORE) come first, thin standing (a bare "I'm
    Spanish") next as the fallback, everyone else after. Never interleaved, so a thin
    claim can never outrank a strong one — and nothing the ordinary search found is
    dropped.
  * FIND, not only re-order — the same second pass as aspect_round.recall_and_rerank: the
    ordinary search for the kind asked about ("barber"), keeping a pass-2 row only when
    its author has standing. Visibility stays find_neighbor_tips' one definition.
  * The score never leaves the worker. Rows carry a tier; cards carry the recommenders'
    own quotes, and only for strong standing — a bare claim has no quote to show, and a
    reason line on one would read as endorsement.

OFF BY DEFAULT (LANA_RECO_AUTHORITY). Needs migration 20261230120000 for the batched read;
without it this falls back to one attester_authority() call per row.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Same pool as the aspect second pass: enough to find the one barber a Spaniard
# recommended, small enough that scoring it is one cheap batched call.
RECALL_POOL = 12

TIER_STRONG = "strong"
TIER_THIN = "thin"
# Sort rank; rows and cards without standing share the last rank and keep their order.
TIER_RANK = {TIER_STRONG: 0, TIER_THIN: 1}
NO_TIER_RANK = 2


def authority_enabled() -> bool:
    return os.environ.get("LANA_RECO_AUTHORITY", "0").strip().lower() not in {
        "", "0", "false", "off",
    }


def tier_of(score: float) -> str | None:
    from app.authority import MIN_EXPLICIT_SCORE

    if score >= MIN_EXPLICIT_SCORE:
        return TIER_STRONG
    if score > 0:
        return TIER_THIN
    return None


def _best(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The strongest concept among one attester's rows — score and quote from the SAME
    concept, so the reason describes the concept that moved the row."""
    scored = [r for r in rows if float(r.get("authority") or 0.0) > 0]
    if not scored:
        return None
    top = max(scored, key=lambda r: float(r.get("authority") or 0.0))
    return {
        "score": float(top.get("authority") or 0.0),
        "quote": top.get("evidence_quote"),
        # They SAID it (a public claim), as opposed to standing from behaviour alone.
        "stated": "stated" in (top.get("evidence_kinds") or []),
        "concept_id": str(top.get("concept_id") or "") or None,
    }


def score_rows(tips: list[dict[str, Any]], concept_ids: list[str]) -> list[dict[str, Any] | None]:
    """Each row's author's best standing on these concepts, as of when the row was posted.
    Parallel to `tips`. One batched RPC; per-row calls when the batch is not deployed."""
    if not tips or not concept_ids:
        return [None] * len(tips)
    user_ids = [str(t.get("peer_user_id") or "") or None for t in tips]
    as_of = [str(t.get("created_at") or "") or None for t in tips]
    try:
        from app.auth import service_client

        res = service_client().rpc(
            "attester_authority_many",
            {"p_user_ids": user_ids, "p_as_of": as_of, "p_concept_ids": concept_ids},
        ).execute()
        by_idx: dict[int, list[dict[str, Any]]] = {}
        for r in res.data if isinstance(res.data, list) else []:
            by_idx.setdefault(int(r.get("idx") or 0), []).append(r)
        return [_best(by_idx.get(i + 1, [])) for i in range(len(tips))]
    except Exception:  # noqa: BLE001
        logger.warning("reco_authority_batch_failed; per-row fallback", exc_info=True)

    from app.authority import authority_for

    out: list[dict[str, Any] | None] = []
    for uid, when in zip(user_ids, as_of):
        if not uid:
            out.append(None)
            continue
        # public_only: a DB without 20261230120000 rejects the argument, authority_for
        # returns {}, and the page is simply unranked — fail closed, never on private claims.
        scored = authority_for(uid, concept_ids, as_of=when, public_only=True)
        out.append(_best([
            {"authority": v["score"], "evidence_quote": v.get("quote"),
             "evidence_kinds": v.get("evidence") or [], "concept_id": cid}
            for cid, v in scored.items()
        ]))
    return out


def _concept_labels(concept_ids: list[str]) -> dict[str, str]:
    """{concept id: label} for the (at most 3) concepts an ask resolved to. {} on failure —
    the reply then simply has no "said" line to report."""
    try:
        from app.auth import service_client

        res = (
            service_client().table("identity_concepts").select("id,label")
            .in_("id", concept_ids).execute()
        )
        return {str(r["id"]): str(r["label"]) for r in res.data or [] if r.get("label")}
    except Exception:  # noqa: BLE001
        logger.warning("reco_authority_concept_labels_failed", exc_info=True)
        return {}


def recall_and_rank_by_standing(
    tips: list[dict[str, Any]],
    *,
    parsed: dict[str, Any] | None,
    fetch: Callable[[str, int], list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Rank (and widen) a tip page by its recommenders' standing on the asked-for trait.

    Stamps `_standing = {"tier", "quote", "trait"}` on rows whose author has any standing.
    Pass-1 rows are always kept; pass-2 rows only with standing. Never raises; returns
    `tips` unchanged when off, when the ask names no recommender trait, or when the trait
    resolves to no concept."""
    if not authority_enabled() or not isinstance(parsed, dict):
        return tips
    trait = str(parsed.get("recommender_trait") or "").strip()
    if not trait:
        return tips
    try:
        from app.authority import concepts_for_ask

        concept_ids = concepts_for_ask(trait)
    except Exception:  # noqa: BLE001
        logger.warning("reco_authority_concepts_failed trait=%r", trait, exc_info=True)
        return tips
    if not concept_ids:
        logger.info("reco_authority trait=%r resolved to no concept", trait)
        return tips

    pool = list(tips)
    kind = str(parsed.get("subject_kind") or "").strip()
    extra: list[dict[str, Any]] = []
    if kind:
        try:
            extra = list(fetch(kind, RECALL_POOL) or [])
        except Exception:  # noqa: BLE001
            logger.debug("reco_authority_recall_fetch_failed kind=%s", kind, exc_info=True)
    seen = {str(t.get("signal_id")) for t in pool}
    new = [t for t in extra if str(t.get("signal_id")) not in seen]
    rows = pool + new

    try:
        standings = score_rows(rows, concept_ids)
    except Exception:  # noqa: BLE001
        logger.warning("reco_authority_score_failed", exc_info=True)
        return tips
    labels = _concept_labels(concept_ids)
    for row, st in zip(rows, standings):
        tier = tier_of(st["score"]) if st else None
        if tier:
            row["_standing"] = {
                "tier": tier,
                # Only a specific claim has a quote; a bare one renders nothing on a card.
                "quote": (str(st.get("quote") or "").strip() or None) if tier == TIER_STRONG else None,
                # What they publicly said, as the concept names it ("From Madrid") — so the
                # reply can report a bare claim truthfully instead of calling it nothing.
                "said": labels.get(str(st.get("concept_id") or "")) if st.get("stated") else None,
                "trait": trait,
            }

    new_ids = {str(t.get("signal_id")) for t in new}
    kept = [t for t in rows if str(t.get("signal_id")) not in new_ids or t.get("_standing")]
    # Stable: inside a tier the search's own order stands.
    kept.sort(key=lambda t: TIER_RANK.get((t.get("_standing") or {}).get("tier"), NO_TIER_RANK))
    logger.info(
        "reco_authority trait=%r kind=%r concepts=%d pass1=%d pass2_new=%d kept_new=%d "
        "strong=%d thin=%d",
        trait, kind or None, len(concept_ids), len(tips), len(new),
        sum(1 for t in kept if str(t.get("signal_id")) in new_ids),
        sum(1 for t in kept if (t.get("_standing") or {}).get("tier") == TIER_STRONG),
        sum(1 for t in kept if (t.get("_standing") or {}).get("tier") == TIER_THIN),
    )
    return kept


def card_standing(rows: list[dict[str, Any]], contributors: list[dict[str, Any]]) -> tuple[
    str | None, dict[str, Any] | None
]:
    """(tier, wire summary) for one subject card from its rows' `_standing` stamps.

    The tier sorts the card. The summary goes on the wire only for STRONG standing backed
    by the person's own public words, and counts people, not rows: `n_people` of
    `of_people` recommenders have it. Also stamps `standing_quote` on each of them."""
    stamps = {str(r.get("signal_id")): r["_standing"] for r in rows if r.get("_standing")}
    if not stamps:
        return None, None
    tier = min((s["tier"] for s in stamps.values()), key=lambda t: TIER_RANK.get(t, NO_TIER_RANK))
    people: set[str] = set()
    everyone: set[str] = set()
    quotes: list[dict[str, str]] = []
    for c in contributors:
        pid = str(c.get("peer_user_id") or "")
        everyone.add(pid)
        st = stamps.get(str(c.get("signal_id")))
        # Counted only with their OWN words. Strong standing can come from behaviour alone
        # (two recs near the concept) — fine for ORDER, but "recommended by someone from
        # Spain" about a person who never said so is a claim about who they are.
        if not st or st["tier"] != TIER_STRONG or not st.get("quote"):
            continue
        people.add(pid)
        c["standing_quote"] = st["quote"]
        if len(quotes) < 3:
            quotes.append({"nickname": str(c.get("nickname") or ""), "quote": st["quote"]})
    if not people:
        return tier, None
    trait = next(iter(stamps.values()))["trait"]
    return tier, {
        "trait": trait,
        "n_people": len(people),
        "of_people": len(everyone),
        "quotes": quotes,
    }


def composer_fact(
    tips: list[dict[str, Any]], trait: str | None, *, checked: bool = True
) -> str | None:
    """The reply composer's rule for an ask that names who must recommend.

    Without it the composer reads "What they asked for: …recommended by someone from Spain"
    beside "Dom recommended Tony" and writes "a neighbour from Spain named Dom" — a claim
    about who a real person is, made from the ASK (prod QA 2026-09-29). And staying silent
    is no better: under an ask that named a requirement, a plain list reads as meeting it.

    So the reply MUST say where the list stands, from the data, in one of three cases:
      · someone meets it in their own words (strong + quote)  → name them, with the quote;
      · someone only mentioned it (a bare public claim)       → report exactly that;
      · nobody has said it                                    → lead with that, plainly.
    `checked=False` (LANA_RECO_AUTHORITY off): Lana never looked, so she says she can't
    tell yet — never "nobody is", which would be a claim she did not check."""
    trait = str(trait or "").strip()
    if not trait:
        return None
    # Pronouns are not in the data; a name does not tell you them.
    ask = (
        f"They asked for a recommendation from someone who is \"{trait}\". Refer to "
        "recommenders by name or \"they\" — never he/she."
    )
    if not checked:
        return (
            f"{ask} You cannot check who recommends yet. Say that plainly FIRST — that you "
            "can't tell which of these neighbours, if any, are that — then give the list. "
            f"NEVER describe or imply that anyone below is \"{trait}\"."
        )
    strong: dict[str, str] = {}
    said: dict[str, str] = {}
    for t in tips:
        st = t.get("_standing") or {}
        who = str(t.get("neighbor_label") or "").strip()
        if not who:
            continue
        if st.get("tier") == TIER_STRONG and st.get("quote"):
            strong.setdefault(who, str(st["quote"]))
        elif st.get("said") and who not in strong:
            said.setdefault(who, str(st["said"]))
    if strong or said:
        parts = [f'{who} said, in their own words: "{q}"' for who, q in list(strong.items())[:3]]
        parts += [
            f"{who} has mentioned this about themselves: {lbl} — a profile note, not their "
            "words: paraphrase it briefly (\"mentioned being from Madrid\"), never in quotes"
            for who, lbl in list(said.items())[:3]
        ]
        quoting = (
            " The words given in quotation marks above are theirs: repeat them EXACTLY, in "
            "quotation marks (keep their \"I\")."
            if strong else ""
        )
        return (
            f"{ask} What the recommenders below have actually said about that: "
            f"{'; '.join(parts)}. Say this FIRST — who said what.{quoting} NEVER write any "
            "quotation that is not given here. Never apply it to anyone else below; if "
            "others are listed, say plainly that they haven't said."
        )
    return (
        f"{ask} NONE of the recommenders below has said that about themselves. Say that "
        "plainly FIRST (e.g. that none of these come from someone who has said they are "
        "that), then that these are the closest neighbour recommendations anyway. NEVER "
        f"describe or imply that anyone below is \"{trait}\"."
    )


def chips_for_card(
    chips: list[str], recommender_chips: list[str] | None, card: dict[str, Any]
) -> list[str]:
    """The ask's chips as ONE card may show them under "Why Lana sees a fit".

    Every facet of the ask stays. The chip carrying its requirement on the recommender —
    labelled so by the ask draft's own model (field "recommended_by"), never matched from
    text — is shown only when the card has `recommender_standing`, i.e. a recommender said
    it in their own public words. Without it the chip claims a fit nobody checked."""
    drop = {str(c).strip().casefold() for c in recommender_chips or [] if str(c).strip()}
    if not drop or card.get("recommender_standing"):
        return list(chips)
    return [c for c in chips if str(c).strip().casefold() not in drop]
