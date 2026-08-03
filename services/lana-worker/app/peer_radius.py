"""Radius peer matching (20260921120000_peer_radius_match.sql) — worker side.

Why this module exists: `match_peers_by_claim_vectors` picks candidates with
`home_block_id = <caller's block>`, so a block boundary is a wall. Prod has
"Lake Nona — Area A" and "Lake Nona — Area B" 0.70 km apart; today those users
are mutually invisible while the app calls both areas "Lake Nona".

`match_peers_within_radius` swaps that equality for `st_dwithin` on the two
coarse points. This module is the thin wrapper, gated by LANA_PEER_RADIUS_MATCH
(default OFF) so the swap is a deliberate flip, not a side effect of deploying.

Fail-open in both directions: flag off, RPC error, or empty result all fall back
to the block-scoped list the caller already had. Radius must never cost a user
matches they were getting before.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from app.auth import service_client

logger = logging.getLogger(__name__)

# ~5 miles. Wide enough that adjacent blocks are in (prod's two H3 areas are
# 0.70 km apart) and the next town over is not (St. Cloud is 14 km from Lake
# Nona). The empty-result widen offer is the escape hatch, not a bigger default.
_DEFAULT_RADIUS_M = 8000.0


def radius_match_enabled() -> bool:
    return os.environ.get("LANA_PEER_RADIUS_MATCH", "off").strip().lower() in (
        "1",
        "on",
        "true",
        "yes",
    )


def radius_meters() -> float:
    raw = os.environ.get("LANA_PEER_RADIUS_METERS", "").strip()
    if not raw:
        return _DEFAULT_RADIUS_M
    try:
        return max(100.0, min(float(raw), 200000.0))
    except ValueError:
        logger.warning("peer_radius_bad_env value=%r — using default", raw)
        return _DEFAULT_RADIUS_M


def radius_rpc(base_name: str, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """(rpc_name, payload) — the `_near` twin plus a radius when the flag is on,
    the untouched original when it is off.

    Deliberately pure: it resolves a name, it does not call anything. Wrapping
    the transport here would hide it from callers that inject their own client
    (and from every test that patches call_rpc), so dispatch stays at the call
    site where the transport is visible.
    """
    if not radius_match_enabled():
        return base_name, args
    return f"{base_name}_near", {**args, "p_radius_meters": radius_meters()}


def fetch_peer_matches_within_radius(
    user_id: str | None,
    *,
    limit: int = 5,
    locale: str = "en",
) -> list[dict[str, Any]] | None:
    """Peers within the configured radius, or None to mean "use the old path".

    None (not []) signals "no answer from here" — flag off, no user, or the RPC
    errored. An empty list is a real answer: located, searched, nobody near.
    """
    if not user_id or not radius_match_enabled():
        return None
    try:
        res = service_client().rpc(
            "match_peers_within_radius",
            {
                "p_user_id": user_id,
                "p_radius_meters": radius_meters(),
                "p_limit": limit,
                "p_locale": locale,
            },
        ).execute()
        rows = res.data if isinstance(res.data, list) else []
    except Exception:
        logger.exception("peer_radius_match_failed user=%s", user_id)
        return None
    peers = [r for r in rows if isinstance(r, dict)]
    logger.info(
        "peer_radius_match user=%s radius_m=%.0f matches=%d",
        user_id, radius_meters(), len(peers),
    )
    return peers
