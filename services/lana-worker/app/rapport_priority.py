"""How much is one rapport question worth? Derived from the MATCHER's own weights.

Before this module every semantic gap was opened with ``unlock_score=0.8`` — the
hardcoded default in ``open_semantic_gap``. So every question tied, the ranker's
``opened_at`` tie-break became the real ordering, and the "By the way…" tile was
FIFO dressed up as a priority queue: a brand-new user's most valuable question sat
behind whatever happened to be asked first.

The weights are not invented here. ``score_onion_candidates_for_user``
(20260914120000) ranks an introduction on exactly three things:

    same_place_bonus  +3   both confirmed at the SAME place (that gym, that church)
    same_type_bonus   +1   both at the same TYPE of place
    shared_concept    +1   each, per shared PUBLIC claim

…and drops any pair scoring 0 (``p_min_score`` default 1). Two hard gates sit in
front of it: ``users.home_block_id`` (no block → the radius function returns zero
rows immediately) and the §D.2 ZIP supply gate.

Read backwards, that tells us what to ask first:

  * A PLACE is worth +3 — one "which gym do you actually go to?" outranks three
    interest questions. Nothing in the old flat scoring knew that.
  * A shared interest only scores IF A NEIGHBOR NEARBY HOLDS IT TOO. An interest
    nobody within reach shares contributes 0, however true it is — so a question
    drawn from local claim supply beats one drawn from thin air.
  * Breadth beats depth. The fourth question about running adds a claim to a
    concept the user already matches on; a first question in an untouched bucket
    adds a new axis. Same +1 on paper, very different marginal value.
  * Some answers gate the whole product rather than scoring in it — the language
    the user reads. Those come first of all.

Deliberately NOT modelled: `disclosure`. Faith, religion, sobriety, recovery and
LGBTQ+ claims are forced to ``mutual`` by the extractor, and the matcher counts
only ``public`` claims — so those questions cannot score by construction. That is
the correct privacy call and it is left alone here; it only means a faith question
should not occupy a scarce early slot, which the bucket rule already handles.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Blocks the product outright rather than scoring inside it: until Lana knows what
# language to speak, every reply is a coin flip. Same tier as a missing block.
P_BLOCKING = 0.95
# A place affiliation. +3 in the matcher — the single largest signal available.
P_PLACE = 0.90
# Opens a bucket this user has no answered claim in yet: a NEW match axis.
P_NEW_BUCKET = 0.85
# An interest we know neighbors nearby already claim, so the answer has a
# guaranteed counterpart to match against.
P_LOCAL_SUPPLY = 0.70
# A new thread inside a bucket already covered — real, but a narrower gain.
P_SAME_BUCKET = 0.55
# Deepens a thread that already has an answered question. Adds texture, no new axis.
P_DEEPEN = 0.40

# Concepts that gate the product rather than feeding the matcher.
_BLOCKING_CONCEPTS = frozenset({"languages_spoken", "home_language"})

# Used when the covered-bucket read fails. Deliberately mid-table: a transient
# Supabase error must not promote a deepening question above a place question,
# nor bury a genuinely new bucket.
P_UNKNOWN = P_SAME_BUCKET


def covered_buckets(user_id: str) -> set[str]:
    """Buckets where this user has already ANSWERED a question.

    Answered, not merely asked: an open question in a bucket has not yet produced
    the claim that makes the bucket matchable, so the bucket is still worth asking
    into. Returns an empty set on any read error — the caller treats that as
    unknown rather than as "nothing covered".
    """
    from app.auth import service_client

    res = (
        service_client()
        .table("rapport_gaps")
        .select("parent_bucket")
        .eq("user_id", user_id)
        .eq("status", "answered")
        .limit(200)
        .execute()
    )
    return {
        str(r.get("parent_bucket") or "")
        for r in (res.data or [])
        if r.get("parent_bucket")
    }


def score_for(
    user_id: str,
    *,
    bucket: str | None = None,
    deepens_concept: str | None = None,
    affiliation_ref: str | None = None,
    from_local_supply: bool = False,
) -> float:
    """Priority for one question, highest first. Never raises.

    ``from_local_supply`` is set by the cold-start seeder: the topic came from
    claims real neighbors nearby already hold, so an answer is guaranteed a
    counterpart in ``score_onion_candidates_for_user`` rather than merely
    possibly having one.
    """
    concept = str(deepens_concept or "").strip().lower()
    if concept in _BLOCKING_CONCEPTS:
        return P_BLOCKING
    if affiliation_ref:
        return P_PLACE

    bucket = str(bucket or "").strip().lower()
    try:
        covered = covered_buckets(user_id)
    except Exception:  # noqa: BLE001 — priority must never fail a gap opening
        logger.warning("rapport_priority: covered-bucket read failed for %s", user_id)
        return P_LOCAL_SUPPLY if from_local_supply else P_UNKNOWN

    if bucket and bucket not in covered:
        # A brand-new user has nothing covered, so every early question lands here —
        # which is exactly right: their whole profile is new axes.
        return P_NEW_BUCKET
    if from_local_supply:
        return P_LOCAL_SUPPLY
    if concept:
        return P_DEEPEN
    return P_SAME_BUCKET


def describe(score: float) -> str:
    """Human label for a score — logging and the admin gap list only."""
    for value, name in (
        (P_BLOCKING, "blocking"),
        (P_PLACE, "place"),
        (P_NEW_BUCKET, "new_bucket"),
        (P_LOCAL_SUPPLY, "local_supply"),
        (P_SAME_BUCKET, "same_bucket"),
        (P_DEEPEN, "deepen"),
    ):
        if abs(score - value) < 0.001:
            return name
    return "custom"


def demo() -> None:
    """Self-check: the ordering the matcher's weights imply must actually hold."""
    # Patch this module's own globals, not a re-import: under `python -m` the
    # running module is __main__, so `import app.rapport_priority` would build a
    # SECOND copy and patch the one score_for never reads.
    g = globals()
    real_covered = g["covered_buckets"]
    calls: list[str] = []

    def fake_covered(uid: str) -> set[str]:
        calls.append(uid)
        return {"activity"}

    g["covered_buckets"] = fake_covered

    # A place question outranks any interest question — it is +3 vs +1.
    place = score_for("u1", bucket="activity", affiliation_ref="aff-1")
    new_bucket = score_for("u1", bucket="heritage")
    same_bucket = score_for("u1", bucket="activity")
    deepen = score_for("u1", bucket="activity", deepens_concept="running")
    local = score_for("u1", bucket="activity", from_local_supply=True)
    blocking = score_for("u1", bucket="heritage", deepens_concept="languages_spoken")

    assert blocking > place > new_bucket > local > same_bucket > deepen, (
        blocking, place, new_bucket, local, same_bucket, deepen
    )
    # An untouched bucket beats a covered one even with local corroboration:
    # a new match axis is worth more than another claim on an existing one.
    assert new_bucket > local
    # A blocking concept short-circuits before any DB read.
    before = len(calls)
    score_for("u2", deepens_concept="languages_spoken")
    assert len(calls) == before, "blocking concept must not hit the DB"

    # A read failure must still return something sane, never raise.
    def boom(_uid: str) -> set[str]:
        raise RuntimeError("supabase down")

    g["covered_buckets"] = boom
    assert score_for("u3", bucket="activity") == P_UNKNOWN
    assert score_for("u3", bucket="activity", from_local_supply=True) == P_LOCAL_SUPPLY

    g["covered_buckets"] = real_covered
    assert describe(P_PLACE) == "place"
    assert describe(0.123) == "custom"
    print("rapport_priority: ok")


if __name__ == "__main__":
    demo()
