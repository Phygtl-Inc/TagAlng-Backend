"""
live_impl.py — the ONE file to edit when wiring the harness to Asjid's real Circles +
ZIP backend (branch `circles`, migrations 20260906/20260907/20260908, live in dev).

RECONCILED 2026-07-28 with Asjid's reply, then REV-2 for the onion matcher. All four ports
are now WIREABLE in principle (the matcher shipped); none is executed here because no dev
DB/service credential is reachable from this branch — the point is accurate pointers, not a
live connection:

  * score_matches → SQL RPC `score_onion_candidates_for_user` (migration 20260914,
    service-role) directly, OR the GATED wrapper `app.onion.score_onion_candidates()`.
    NO LONGER BLOCKED — §C is BUILT + deployed (branch `main`, PR #107/#114).
  * transition_zip → POST /lana/area/progress (recounts on read). The ZIP machine is a
    RECOUNT, not an event: `recount_zip_unlock(zip5)` is pure over the DB.
  * is_active     → POST /lana/circles/mine (derived `active = member_count ≥ 2`).
  * filter_by_tier → PASSTHROUGH. §F is enforced structurally (RLS + worker serializers),
    not by a callable tier filter — there is no endpoint. Implemented as a no-op passthrough.

To DRIVE state in a live test population you WRITE rows (users, lana_sessions,
circle_affiliations, intros) and recount / re-score — the DB functions are pure over the DB,
so the harness's job in live mode is SEEDING, not event injection. The methods below sketch
the real call in comments but still raise NotImplementedError.
"""

from __future__ import annotations

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


class MatcherLive:
    """§C — the onion matcher. NO LONGER BLOCKED (rev-2): §C is BUILT + deployed (branch
    `main`, PR #107/#114, migration 20260914). Two real entry points, one gated, one not:

      * UNGATED (raw scoring): SQL RPC
          score_onion_candidates_for_user(p_user_id, p_limit=20, p_min_score=1)
        service-role only. Returns per-peer rows: peer_user_id, nickname, avatar_url, score,
        same_place_bonus, same_type_bonus, shared_concept_count, shared_concept_labels[<=50],
        shared_place_ref. This is what MatcherStub mirrors (score = MAX(place+3,type+1) +
        shared_concept_count; confirmed+grounded only; order score desc, nickname asc).
      * GATED (product path): `app.onion.score_onion_candidates()` — enforces the §D.2 peers
        ZIP gate BEFORE calling the RPC and FAILS OPEN on gate errors.

    GATE-IN-WRAPPER INVARIANT: the RPC itself NEVER enforces the gate. `app/onion.py` is the
    gate. Any caller that hits the RPC directly BYPASSES the gate — so a harness that wants
    gated behavior must go through the wrapper, and a harness that wants raw scoring calls
    the RPC. In hard-gate mode the wrapper returns gated:true and NEVER calls the RPC.

    NO HTTP ROUTE: onion matches are not exposed by an endpoint. They are consumed inside the
    find-peers CHAT TURN — discovery_route._fetch_verified_peer_matches → app.onion_blend.
    blend_onion_matches(peers, user_id, limit=5) merges them into the vector-matched list. To
    drive it from the harness, call the RPC (raw) or the wrapper (gated) directly with a
    service key; there is no request to POST.

    DORMANCY: the concept arm reads `claim_concept_links`, live only after
    IDENTITY_CONCEPT_LINK_ENABLED + the 20260905 backfill (PR #96 pending). Until then live
    scores are circle-arm only — mirror with ScoringConfig.concept_arm_enabled=False."""

    def score_matches(
        self, mom: Mom, population: list[Mom], config: ScoringConfig
    ) -> list[RankedCandidate]:
        # Real wiring (sketch — not executed; no dev DB/service credential reachable here):
        #
        #   # (a) RAW scoring — bypasses the ZIP gate (service-role only):
        #   rows = db.rpc("score_onion_candidates_for_user",
        #                 {"p_user_id": mom.user_id, "p_limit": config.n_results,
        #                  "p_min_score": config.p_min_score}).execute().data
        #
        #   # (b) GATED product path — app/onion.py enforces §D.2 then calls the RPC,
        #   #     failing OPEN on gate errors (hard mode → {gated: true}, RPC not called):
        #   import app.onion
        #   rows = app.onion.score_onion_candidates(mom.user_id, limit=config.n_results)
        #
        #   return [RankedCandidate(
        #       user_id=r["peer_user_id"], score=r["score"],
        #       same_place=r["same_place_bonus"] > 0, same_type=r["same_type_bonus"] > 0,
        #       shared_affinities=tuple(r["shared_concept_labels"]),
        #       same_place_bonus=r["same_place_bonus"], same_type_bonus=r["same_type_bonus"],
        #       shared_concept_count=r["shared_concept_count"],
        #       shared_concept_labels=tuple(r["shared_concept_labels"]),
        #       shared_place_ref=r["shared_place_ref"], ring=None,  # RPC emits no ring
        #   ) for r in rows]
        raise NotImplementedError(
            "MatcherLive.score_matches: wireable to the RPC "
            "score_onion_candidates_for_user(p_user_id, p_limit, p_min_score) [ungated, "
            "service-role] or app.onion.score_onion_candidates() [gated: enforces the §D.2 "
            "peers gate, fails open]. There is NO HTTP route — onion matches are consumed in "
            "the find-peers turn (discovery_route._fetch_verified_peer_matches -> "
            "app.onion_blend.blend_onion_matches). Not executed here — no dev DB/service "
            "credential is reachable from this branch. MatcherStub is the parity reference."
        )


class DisclosureLive:
    """§F — disclosure. RESOLVED (Asjid 2026-07-28): §F is STRUCTURAL — every table has RLS
    enabled with zero client policies, and place names leave only through worker
    serializers. There is no callable tier filter, so this port is a PASSTHROUGH in live
    mode: the user's OWN circles (POST /lana/circles/mine) always include their own place;
    OTHERS' views are redacted by RLS + serializers, never by this call. The tier-collapse
    the stub models (guess #8) describes that structural reality; it is not a request-time
    gate. The stub (DisclosureStub) stays the §F reference."""

    def filter_by_tier(
        self, circle: CircleAffiliation, requester_tier: DisclosureTier
    ) -> DisclosedCircleInfo:
        # Trivially-safe passthrough: return the circle unfiltered. In prod nothing below
        # Direct is served by this path at all (RLS + serializers handle it upstream); the
        # harness's §F.3 stub leak-check does not apply against live and should stay on the
        # stub. `requester_tier` is intentionally unused — there is no per-tier data
        # variation to apply here.
        return DisclosedCircleInfo(
            circle_type=circle.circle_type,
            place_name=circle.place_name,
            place_ref=circle.place_ref,
            can_vouch=(requester_tier == "irl_peer"),
        )


class AreaStateLive:
    """§D — the ZIP unlock derivation. WIREABLE TODAY.
    RESOLVED (Asjid 2026-07-28): this is a RECOUNT, not an event-driven transition.
    `recount_zip_unlock(zip5)` re-derives verified_active_count from scratch and maps
    count→state (read-repaired from 3 call sites; no cron, no trigger). The callable driver
    is POST /lana/area/progress, which recounts on read and returns
    {zip5, state, count, threshold, is_founding_eligible, founding_earned, founding_area};
    alternatively call the recount_zip_unlock RPC directly with a service key. `event` is
    the harness's snapshot carrier — in live mode you SEED rows and recount rather than
    inject events."""

    def transition_zip(
        self, zip_state: ZipState, event: ZipEvent, config: AreaStateConfig
    ) -> ZipState:
        # Real wiring (sketch — not executed; no dev DB/credential reachable from here):
        #
        #   resp = http.post(f"{BASE}/lana/area/progress",
        #                    json={"zip5": zip_state.zip_code}, headers=AUTH).json()
        #   return ZipState(
        #       zip_code=resp["zip5"],
        #       unlock_state=resp["state"],
        #       verified_active_count=resp["count"],
        #       unlock_threshold=resp["threshold"],
        #       opened_at=zip_state.opened_at,  # server owns opened_at; persists once stamped
        #   )
        #
        # To move the count in a test population you WRITE users/lana_sessions/
        # circle_affiliations/intros rows for that zip5, then call the above (or the
        # recount_zip_unlock RPC) — the function is pure over the DB.
        raise NotImplementedError(
            "AreaStateLive.transition_zip: wireable to POST /lana/area/progress (recounts "
            "on read) or the recount_zip_unlock(zip5) RPC. Not executed here — no dev DB/"
            "service credential is reachable from this branch."
        )


class CircleActivationLive:
    """§B.2/U3 — circle activation. WIREABLE TODAY.
    RESOLVED (Asjid 2026-07-28): derived-at-read as `active = member_count ≥ 2`, computed
    per place inside POST /lana/circles/mine. No dedicated endpoint and no stored flag — the
    `active` boolean rides on each row of the own-circles read (capped at 40)."""

    def is_active(
        self, circle_members: list[CircleAffiliation], config: ActivationConfig
    ) -> bool:
        # Real wiring (sketch — not executed): the `active` flag is server-derived and
        # returned per circle by POST /lana/circles/mine, e.g.
        #   row["active"]  # == (row["member_count"] >= 2)
        # There is no member-list-in / bool-out endpoint; the harness reads `active`
        # straight off /mine rather than recomputing it client-side.
        raise NotImplementedError(
            "CircleActivationLive.is_active: activation is derived-at-read on POST "
            "/lana/circles/mine (active = member_count >= 2). Read the `active` field off "
            "that response; there is no standalone check endpoint. Not executed here."
        )
