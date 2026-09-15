"""
live_impl.py — the harness wired to Asjid + Pouya's real Circles + ZIP backend
(branch `main`; migrations 20260906/07/13/14/16; app/onion.py, onion_blend.py, zip_unlock.py).
[migration list corrected 2026-08-03: there is no circles migration 20260908 —
 20260908120000 is capability_required_state; zip_unlock + recount_zip_unlock are both in
 20260906120000, and 20260913120000 is count_shared_concepts_for_user.]

WIRED 2026-07-29. The four ports now make real calls against Supabase (service-role, from
.env.local). Reads are safe; the one WRITE (recount) is gated behind SIM_ALLOW_WRITES=1.

  * MatcherLive.score_matches → RPC score_onion_candidates_for_user (READ-only scoring SELECT).
    ⚠ BLOCKED IN PRACTICE by a backend bug: the RPC currently throws `min(uuid) does not exist`
    on every call (see ONION_RPC_BUG.md). The wiring below is correct and runs the moment that
    one-line SQL fix lands.
  * CircleActivationLive.is_active → direct count of confirmed, non-dismissed circle_affiliations
    at a place_ref (the server-derived `active = member_count ≥ 2`). READ-only.
  * AreaStateLive.transition_zip → recount_zip_unlock(zip5) RPC. This WRITES (re-derives the
    count, may stamp opened_at/founding), so it is gated behind SIM_ALLOW_WRITES=1.
  * DisclosureLive.filter_by_tier → PASSTHROUGH (§F is structural: RLS + serializers, no call).

Seeding a synthetic population to score against lives in live_seed.py (service-key, write-gated,
reversible). MatcherStub remains the parity reference the live output is diffed against.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
from dotenv import load_dotenv

from ports import (
    ActivationConfig,
    AreaStateConfig,
    CircleAffiliation,
    DisclosedCircleInfo,
    DisclosureTier,
    Mom,
    RankedCandidate,
    ScoringConfig,
    ZipEvent,
    ZipState,
)

# repo-root .env.local (circles_zip → simulations → lana-worker → services → root)
load_dotenv(Path(__file__).resolve().parents[4] / ".env.local", override=True)

_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
_ALLOW_WRITES = os.environ.get("SIM_ALLOW_WRITES", "").strip() == "1"
_TIMEOUT = 30


# ---------------------------------------------------------------------------
# Shared Supabase (service-role) helpers
# ---------------------------------------------------------------------------

def _require_creds() -> None:
    if not _URL or not _KEY:
        raise RuntimeError(
            "live mode needs SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY in .env.local "
            "(use SIM_BACKEND=stub to run without a live DB)."
        )


def _headers(extra: dict | None = None) -> dict:
    h = {"apikey": _KEY, "Authorization": f"Bearer {_KEY}", "Content-Type": "application/json"}
    if extra:
        h.update(extra)
    return h


def _rpc(name: str, payload: dict) -> list[dict]:
    """POST a Postgres function. Returns its rows (or [] / scalar wrapped)."""
    _require_creds()
    with httpx.Client(timeout=_TIMEOUT) as c:
        r = c.post(f"{_URL}/rest/v1/rpc/{name}", headers=_headers(), json=payload)
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else [data]


def _select(table: str, params: dict) -> list[dict]:
    _require_creds()
    with httpx.Client(timeout=_TIMEOUT) as c:
        r = c.get(f"{_URL}/rest/v1/{table}", headers=_headers(), params=params)
        r.raise_for_status()
        return r.json()


def _count(table: str, filters: dict) -> int:
    """Exact row count via PostgREST's content-range header (read-only, fetches no rows)."""
    _require_creds()
    with httpx.Client(timeout=_TIMEOUT) as c:
        r = c.get(f"{_URL}/rest/v1/{table}",
                  headers=_headers({"Prefer": "count=exact"}),
                  params={**filters, "select": "user_id", "limit": "0"})
        r.raise_for_status()
        cr = r.headers.get("content-range", "*/0")  # e.g. "*/25"
        try:
            return int(cr.split("/")[-1])
        except ValueError:
            return len(r.json())


class MatcherLive:
    """§C — the onion matcher. Wired to the real RPC (rev-2: §C is BUILT + deployed on `main`,
    PR #107/#114, migration 20260914). Two entry points; this uses the RAW/ungated RPC:

      * UNGATED (raw scoring): score_onion_candidates_for_user(p_user_id, p_limit=20,
        p_min_score=1), service-role. Returns per-peer: peer_user_id, nickname, avatar_url,
        score, same_place_bonus, same_type_bonus, shared_concept_count, shared_concept_labels,
        shared_place_ref. This is exactly what MatcherStub mirrors.
      * GATED (product path): app.onion.score_onion_candidates() enforces the §D.2 peers ZIP gate
        then calls the RPC, failing OPEN on gate errors. Swap `_rpc(...)` for that import if you
        want gated behavior (needs the app package importable).

    ⚠ KNOWN BACKEND BUG: the RPC currently raises `min(uuid) does not exist` on every call
    (ONION_RPC_BUG.md). This wiring is correct and runs unchanged once that SQL is fixed.

    DORMANCY: the concept arm reads `claim_concept_links`, live only after
    IDENTITY_CONCEPT_LINK_ENABLED + the 20260905 backfill (PR #96). Until then live scores are
    circle-arm only — a 0-concept score is flag-off, not a bug (mirror with
    ScoringConfig.concept_arm_enabled=False on the stub side).

    FLAGGED (ordering, when diffing this against MatcherStub): the RPC's secondary sort key is
    `u.nickname asc nulls last`, which the stub cannot reproduce (no nicknames here) and which
    is not a total order server-side either — nickname is frequently NULL and Postgres does not
    define the order within the trailing NULL block. Since the tiebreak runs BEFORE the LIMIT,
    a score tie straddling p_limit can legitimately return DIFFERENT rows on the two sides.
    Compare the returned SET at each score level, never the sequence. See README "Known
    divergences" #1."""

    def score_matches(
        self, mom: Mom, population: list[Mom], config: ScoringConfig
    ) -> list[RankedCandidate]:
        # `population` is expected to ALREADY be in the DB (seed it via live_seed first) — the RPC
        # scores mom.user_id against whatever rows exist, so the in-memory list is not re-sent.
        rows = _rpc("score_onion_candidates_for_user", {
            "p_user_id": mom.user_id,
            "p_limit": config.n_results,
            "p_min_score": config.p_min_score,
        })
        return [
            RankedCandidate(
                user_id=r["peer_user_id"],
                score=float(r["score"]),
                # The RPC splits the ALREADY-MAXed circle_bonus, so the two columns are mutually
                # exclusive: same_type_bonus>0 means "TYPE-ONLY match". When place won we CANNOT
                # know whether a type is also shared -> report None (not observable), never False.
                same_place=(r.get("same_place_bonus") or 0) > 0,
                same_type=(True if (r.get("same_type_bonus") or 0) > 0
                           else (None if (r.get("same_place_bonus") or 0) > 0 else False)),
                shared_affinities=tuple(r.get("shared_concept_labels") or ()),
                same_place_bonus=float(r.get("same_place_bonus") or 0),
                same_type_bonus=float(r.get("same_type_bonus") or 0),
                shared_concept_count=int(r.get("shared_concept_count") or 0),
                shared_concept_labels=tuple(r.get("shared_concept_labels") or ()),
                shared_place_ref=r.get("shared_place_ref"),
                ring=None,  # the built RPC emits no ring — reporting artifact only (stub derives it)
            )
            for r in rows
        ]


class DisclosureLive:
    """§F — disclosure. STRUCTURAL (RLS + worker serializers), no callable tier filter, so this
    is a PASSTHROUGH in live mode. The user's OWN /mine view always sees its own place; OTHERS'
    views are redacted upstream by RLS + serializers, never by this call. `requester_tier` is
    intentionally unused — there is no per-tier data variation to apply here. The stub keeps the
    tier-collapse behavior as the §F reference; the §F.3 leak-check applies to the stub, not live."""

    def filter_by_tier(
        self, circle: CircleAffiliation, requester_tier: DisclosureTier
    ) -> DisclosedCircleInfo:
        return DisclosedCircleInfo(
            circle_type=circle.circle_type,
            place_name=circle.place_name,
            place_ref=circle.place_ref,
            can_vouch=(requester_tier == "irl_peer"),
        )


class AreaStateLive:
    """§D — ZIP unlock derivation. A RECOUNT, not an event: recount_zip_unlock(zip5) re-derives
    verified_active_count from scratch and maps count→state (read-repaired from 3 call sites).

    ⚠ recount_zip_unlock WRITES: it updates the zip_unlock row and, on a closed/warming→open
    crossing, stamps opened_at + founding on qualifying members and fires a push. So this method
    is gated behind SIM_ALLOW_WRITES=1 — it refuses to run otherwise. Prefer a dedicated dev ZIP
    seeded via live_seed, never a real one, since the founding stamps persist."""

    def transition_zip(
        self, zip_state: ZipState, event: ZipEvent, config: AreaStateConfig
    ) -> ZipState:
        if not _ALLOW_WRITES:
            raise RuntimeError(
                "AreaStateLive.transition_zip calls recount_zip_unlock, which WRITES (updates "
                "zip_unlock; may stamp opened_at/founding). Set SIM_ALLOW_WRITES=1 to allow it, "
                "and only against a seeded dev ZIP — never a real one."
            )
        zip5 = zip_state.zip_code
        _rpc("recount_zip_unlock", {"p_zip5": zip5})  # write: recompute + persist state
        rows = _select("zip_unlock", {"zip5": f"eq.{zip5}", "select": "*", "limit": "1"})
        if not rows:
            return zip_state
        row = rows[0]
        return ZipState(
            zip_code=row.get("zip5", zip5),
            unlock_state=row["unlock_state"],
            verified_active_count=row["verified_active_count"],
            unlock_threshold=row.get("unlock_threshold", config.unlock_threshold),
            opened_at=row.get("opened_at"),
        )


class CircleActivationLive:
    """§B.2/U3 — circle activation, derived-at-read (`active = member_count ≥ 2`). In prod the
    `active` flag rides on each /lana/circles/mine row; here we compute the same predicate with a
    direct, READ-only count of confirmed, non-dismissed affiliations at the place."""

    def is_active(
        self, circle_members: list[CircleAffiliation], config: ActivationConfig
    ) -> bool:
        place_ref = next((c.place_ref for c in circle_members if getattr(c, "place_ref", None)), None)
        if not place_ref:
            return False  # ungrounded → never active
        n = _count("circle_affiliations", {
            "place_ref": f"eq.{place_ref}",
            "status": "eq.confirmed",
            "dismissed_at": "is.null",
        })
        return n >= config.activation_threshold
