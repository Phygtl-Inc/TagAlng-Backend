"""Domain standing — whose recommendation carries weight on THIS subject.

SPEC_RECOMMENDER_AUTHORITY.md §4/A2 (2026-09-10). The read side of
`attester_authority()` (migration 20261209120000).

The requirement, in Tommaso's words: "Show me a Turkish restaurant, but the recommendation
must come from someone from Turkey." A review site can count ratings; it cannot say that
one of them came from someone who grew up in Gaziantep. We already hold that evidence —
190 claims across 149 concepts — and until this module nothing read it as authority.

Two rules that shape every function here:

  * Authority is per CONCEPT, never global (§7). There is no expert badge, no reputation
    number, no leaderboard. Those turn a knowledge signal into a status game, which is
    exactly what Yelp Elite and Google Local Guides are.
  * The score never reaches the UI. Callers render `evidence_quote`. An authority number
    with no explanation is the black box we are replacing.

No new embedding path and no new LLM call on the request path (contract v2 Part 0): the
tip lane has already embedded the ask by the time it gets here, so `concepts_for_ask`
takes that vector when the caller has one and only embeds as a fallback.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from app.auth import service_client

logger = logging.getLogger(__name__)

# §A2: top 3, floor 0.55. Env-overridable the same way the tip floor is — Tim moves these
# in T4 calibration without waiting on a deploy.
_TOP_K = 3
_MIN_SIM = 0.55

# §A4: "at least one specific claim". A bare self-declared claim scores 0.10 and must
# never satisfy an explicit requirement — that is the anti-gaming floor, not a preference.
MIN_EXPLICIT_SCORE = 0.35


def _top_k() -> int:
    try:
        return max(1, int(os.environ.get("LANA_AUTHORITY_TOP_K", _TOP_K)))
    except ValueError:
        return _TOP_K


def _min_sim() -> float:
    try:
        return float(os.environ.get("LANA_AUTHORITY_MIN_SIM", _MIN_SIM))
    except ValueError:
        return _MIN_SIM


def concepts_for_ask(
    query_text: str,
    *,
    embedding: list[float] | None = None,
) -> list[str]:
    """Resolve an ask to the identity concepts it is about. Returns at most 3 ids.

    `p_bucket := null` on purpose: an ask lands in whichever bucket fits it, and the
    spec's own two examples land in different ones — "from someone from Turkey" is
    heritage, "who has run a marathon" is activity. Bucketing the search here would
    silently answer only half the requirement.

    Pass `embedding` when the caller already has the ask vector (the tip lane does).
    """
    text = (query_text or "").strip()
    if not text:
        return []

    if embedding is None:
        from app.layer1_handlers import _embed_attr_filter

        embedding = _embed_attr_filter(text)
    if not embedding:
        # Loud: a dead embedding model and an ask about nothing in particular look
        # identical from the caller's side, and one of them is an outage.
        logger.warning("authority_concepts_no_embedding query=%r", text[:60])
        return []

    try:
        res = (
            service_client()
            .rpc(
                "match_concepts_by_embedding",
                {
                    "p_bucket": None,
                    "p_embedding": embedding,
                    "p_limit": _top_k(),
                    "p_min_similarity": _min_sim(),
                },
            )
            .execute()
        )
    except Exception:  # noqa: BLE001
        # Authority is an ordering refinement. Losing it costs relevance, never the answer.
        logger.warning("authority_concepts_rpc_failed", exc_info=True)
        return []

    rows = res.data if isinstance(res.data, list) else []
    return [str(r["id"]) for r in rows if r.get("id")]


def authority_for(
    user_id: str,
    concept_ids: list[str],
    *,
    as_of: str | None = None,
    include_relations: bool = False,
) -> dict[str, dict[str, Any]]:
    """One attester's standing on each concept, keyed by concept_id.

    `as_of` is not decoration. Callers pass the moment the recommendation was made
    (`attestation.valid_from`, or the tip's `created_at`) so that a claim written
    afterwards cannot back-date authority onto it. Omitting it means "as of now", which
    is only correct when scoring a recommendation that is being made right now.
    """
    if not user_id or not concept_ids:
        return {}

    args: dict[str, Any] = {
        "p_user_id": user_id,
        "p_concept_ids": concept_ids,
        "p_include_relations": include_relations,
    }
    if as_of:
        args["p_as_of"] = as_of

    try:
        res = service_client().rpc("attester_authority", args).execute()
    except Exception:  # noqa: BLE001
        logger.warning("attester_authority_rpc_failed user=%s", user_id, exc_info=True)
        return {}

    rows = res.data if isinstance(res.data, list) else []
    return {
        str(r["concept_id"]): {
            # INTERNAL. Never put this on a wire model — §7, and A3 says so twice.
            "score": float(r.get("authority") or 0.0),
            "evidence": list(r.get("evidence_kinds") or []),
            "quote": r.get("evidence_quote"),
        }
        for r in rows
        if r.get("concept_id")
    }


def best_authority(
    user_id: str,
    concept_ids: list[str],
    *,
    as_of: str | None = None,
) -> dict[str, Any] | None:
    """The attester's strongest concept among these, or None if they have no standing.

    P1 orders on one number per candidate, and P3 renders one reason. Picking the max
    here rather than in the ranking keeps "which concept won" next to the quote that
    explains it — they must agree, or the reason describes a different concept than the
    one that moved the row.
    """
    scored = authority_for(user_id, concept_ids, as_of=as_of)
    if not scored:
        return None
    concept_id, row = max(scored.items(), key=lambda kv: kv[1]["score"])
    if row["score"] <= 0:
        return None
    return {"concept_id": concept_id, **row}
