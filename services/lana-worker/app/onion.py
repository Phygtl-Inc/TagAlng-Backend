"""Onion matcher (Circles master §C) — worker side.

Thin wrapper over the score_onion_candidates_for_user RPC
(20260914120000_score_onion_candidates.sql). Two jobs, in this order:

  1. Run the §D.2 consumption gate for the 'peers' surface. A blocked area never
     reaches the RPC — introductions in a 3-person ZIP are junk-quality and
     privacy-risky, so peers is the one surface that truly locks (hard mode only).
  2. Otherwise call the RPC and reshape its rows for the caller.

All scoring arithmetic lives in SQL and is passed through untouched — the weights
(same-place +3, same-type +1, +1 per shared concept) are returned as separate
columns precisely so they can be re-tuned without redeploying this module.

Disclosure (§F): the RPC returns a bare place uuid, never a place name or address
— resolving it to anything human-readable is the caller's decision (open question
O7). This module does not join or read `places`.
"""

from __future__ import annotations

import logging
from typing import Any

from app.auth import service_client

logger = logging.getLogger(__name__)


def _peers_gate(user_id: str | None) -> dict[str, Any] | None:
    """The ZIP unlock gate is enforced HERE and only here — the RPC does not
    enforce it (see note [1] in the migration), so any other caller of
    score_onion_candidates_for_user silently bypasses it.

    Returns the gate frame when introductions are blocked, None to proceed.
    Fails OPEN on any error: a gating bug must never lock a user out of
    discovery (mirrors discovery_route._zip_gate_peers_turn).
    """
    try:
        from app.zip_unlock import discovery_zip_gate

        frame = discovery_zip_gate(user_id, surface="peers")
    except Exception:  # noqa: BLE001 — a gating error must fail OPEN
        logger.exception("onion_peers_zip_gate_failed user=%s", user_id)
        return None
    if not frame or not frame.get("blocked"):
        return None
    return frame


def _shape(row: dict[str, Any]) -> dict[str, Any]:
    """One RPC row -> one candidate dict. Pass-through: no re-ranking, no
    re-weighting, no derived score of any kind."""
    labels = row.get("shared_concept_labels")
    return {
        "user_id": str(row.get("peer_user_id") or ""),
        "nickname": row.get("nickname"),
        "avatar_url": row.get("avatar_url"),
        "score": int(row.get("score") or 0),
        "same_place_bonus": int(row.get("same_place_bonus") or 0),
        "same_type_bonus": int(row.get("same_type_bonus") or 0),
        "shared_concept_count": int(row.get("shared_concept_count") or 0),
        "shared_concept_labels": list(labels) if isinstance(labels, list) else [],
        "shared_place_ref": row.get("shared_place_ref"),
    }


def score_onion_candidates(
    user_id: str,
    *,
    limit: int = 20,
    min_score: int = 1,
) -> dict[str, Any]:
    """Ranked introduction candidates for user_id (§C).

    Returns {gated, candidates, gate, gate_facts}:
      * gated=True  -> the area is not open and this is hard mode; `candidates`
                       is empty and the RPC was never called. `gate_facts` are
                       the composer-ready seed-forward facts (host something /
                       bring your people in) — the copy itself belongs to the
                       reply layer, not here.
      * gated=False -> `candidates` is whatever SQL ranked, in SQL's order.

    An RPC failure returns an empty candidate list rather than raising, matching
    how every other service-role RPC wrapper in the worker degrades.
    """
    frame = _peers_gate(user_id)
    if frame is not None:
        from app.zip_unlock import gate_framing_facts

        return {
            "gated": True,
            "candidates": [],
            "gate": frame,
            "gate_facts": gate_framing_facts(frame),
        }

    try:
        res = service_client().rpc(
            "score_onion_candidates_for_user",
            {
                "p_user_id": user_id,
                "p_limit": limit,
                "p_min_score": min_score,
            },
        ).execute()
        rows = res.data if isinstance(res.data, list) else []
        ok = True
    except Exception:
        logger.exception("score_onion_candidates_failed user=%s", user_id)
        rows = []
        ok = False

    candidates = [_shape(r) for r in rows if isinstance(r, dict)]
    # ok=False + candidates=0 is a broken RPC; ok=True + 0 is a real empty
    # result. They used to log identically, which is how a function that threw
    # on every call passed for "the area is just quiet" for weeks.
    logger.info("onion_scored user=%s candidates=%d ok=%s", user_id, len(candidates), ok)
    return {"gated": False, "candidates": candidates, "gate": None, "gate_facts": []}
