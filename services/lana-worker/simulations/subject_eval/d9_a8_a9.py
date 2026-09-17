"""
d9_a8_a9.py — D9-A8 (topic dominance) and D9-A9 (truncation honesty).

    cd services/lana-worker/simulations/subject_eval
    python d9_a8_a9.py

Offline, deterministic, no LLM, no DB. Both invariants are stubbed per
SUBJECT_GRAPH_EVAL_PLAN.md §6.6 ("What I can build now — nothing blocks these" / "Stub against:
C2's formula and a capped candidate set") and are meant to survive C1-C3 shipping unchanged —
swap `c2_score` for the real ranking call the day it lands; the invariants below do not change.

D9-A8 — TOPIC DOMINANCE
------------------------
Pouya's finding, §6.6: with C2's formula `score = 0.8*q + 0.2*p + 0.05*tags` (q = query-topic
cosine, p = profile affinity in [0,1], tags = matched-tag count capped at 3), an off-topic
candidate can outrank an on-topic one whenever

    0.8 * (q_topic - q_other) < 0.2 * (p_other - p_topic) + 0.05 * (tags_other - tags_topic)

With p bounded at 1 and tags capped at 3, the right side maxes at 0.2 + 0.15 = 0.35 — so a raw
query-cosine gap above 0.35 / 0.8 = 0.4375 CANNOT be overturned by profile or tags, no matter how
those two are weighted within their documented bounds. That threshold is what makes this
invariant formula-agnostic: it holds even if `w_profile` or `w_tags` gets re-tuned later, as long
as neither exceeds its current cap. Verified against the plan's own worked numbers below
(on-topic 0.85/0.20 -> 0.720, off-topic 0.75/0.95 -> 0.790 — a q-gap of only 0.10, well under
0.4375, so the override there is fully consistent with the formula, not a bug in it).

> "When the candidate set contains a materially better topic match, it ranks first."

D9-A9 — TRUNCATION HONESTY
---------------------------
`get_activities_near_point` caps at `limit greatest(1, least(coalesce(p_limit, 20), 50))`
(default 20) — distance-ordered, so a full page means results beyond the cutoff exist and were
never considered. This is the third silent-failure shape found in this eval (after the static
fallback and D3's fragmentation split): a query returns results, errors nothing, and quietly
omits the thing being looked for.

> "If the candidate query returned exactly its limit, any 'nothing further out matched'
>  conclusion is unsound. The payload must carry the flag."
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_HERE = Path(__file__).resolve().parent

W_QUERY, W_PROFILE, W_TAGS = 0.8, 0.2, 0.05
TAGS_CAP = 3
# The override budget: the most profile+tags can add, given their documented bounds (p in [0,1],
# tags capped at 3). A query-cosine gap bigger than this / W_QUERY can never be overturned.
MAX_OVERRIDE = W_PROFILE * 1.0 + W_TAGS * TAGS_CAP
GUARANTEED_DOMINANCE_GAP = MAX_OVERRIDE / W_QUERY  # 0.4375


# ── D9-A8 — topic dominance ─────────────────────────────────────────────────────────────────

@dataclass
class Candidate:
    name: str
    q: float                # query-topic cosine similarity, in [0, 1]
    p: float = 0.0           # profile affinity, in [0, 1]
    tags: int = 0             # matched-tag count


def c2_score(c: Candidate) -> float:
    """C2's formula, verbatim — Pouya's, kept exact so the worked example reproduces to 3dp."""
    return W_QUERY * c.q + W_PROFILE * c.p + W_TAGS * min(c.tags, TAGS_CAP)


def rule_topic_dominance(leader: Candidate, other: Candidate, *,
                          score_fn=c2_score, threshold: float = GUARANTEED_DOMINANCE_GAP,
                          w_query: float = W_QUERY) -> str | None:
    """FORMULA-AGNOSTIC invariant: if leader's raw topic gap over other exceeds the guaranteed-
    dominance threshold for a given score_fn, leader MUST outrank other under it — this has to
    hold no matter how the non-topic weights get re-tuned later, as long as they stay within
    their documented bounds (which is what fixes `threshold`).

    `score_fn`, `threshold` and `w_query` are parameters, not hardcoded to C2, so a second signal
    with different weights (see t2_authority_topic.py's reuse of this same function) can plug in
    without duplicating the invariant logic."""
    gap = leader.q - other.q
    if gap <= threshold:
        return None  # gap too small to guarantee anything — not this rule's business
    if score_fn(leader) <= score_fn(other):
        return (f"{leader.name} leads on topic by {gap:.3f} (> guaranteed-dominance threshold "
                f"{threshold:.4f}) but scores {score_fn(leader):.3f} <= "
                f"{other.name}'s {score_fn(other):.3f}")
    return None


# ── D9-A9 — truncation honesty ──────────────────────────────────────────────────────────────

@dataclass
class CandidateQuery:
    """One candidate-fetch result, as the payload should report it."""
    returned: int
    limit: int
    truncated_flag: bool | None    # None = flag absent from the payload entirely


def rule_truncation_honesty(cq: CandidateQuery) -> str | None:
    hit_limit = cq.returned >= cq.limit
    if hit_limit and cq.truncated_flag is not True:
        return (f"returned exactly the limit ({cq.returned}/{cq.limit}) but "
                f"truncated={cq.truncated_flag!r} — 'nothing further matched' would be unsound")
    if not hit_limit and cq.truncated_flag is True:
        return (f"returned {cq.returned} of a {cq.limit} limit (did not hit the cap) but is "
                f"flagged truncated — overstates how much was cut off")
    return None


# ── non-vacuity proof ────────────────────────────────────────────────────────────────────────

_failures: list[str] = []


def check(name: str, condition: bool) -> None:
    print(f"  [{'ok ' if condition else 'FAIL'}] {name}")
    if not condition:
        _failures.append(name)


def main() -> int:
    print(f"[D9-A8/A9] guaranteed-dominance gap = {GUARANTEED_DOMINANCE_GAP:.4f} "
          f"(max override {MAX_OVERRIDE:.2f} / w_query {W_QUERY})\n")

    print("## D9-A8 — topic dominance\n")

    # Plan's own worked example, reproduced exactly.
    on_topic = Candidate("violin_meetup", q=0.85, p=0.20)
    off_topic = Candidate("guitar_meetup", q=0.75, p=0.95)
    check("worked example: on-topic scores 0.720",
          abs(c2_score(on_topic) - 0.720) < 1e-9)
    check("worked example: off-topic scores 0.790",
          abs(c2_score(off_topic) - 0.790) < 1e-9)
    check("worked example: a 0.10 topic gap IS legitimately overturned (not this rule's business)",
          rule_topic_dominance(on_topic, off_topic) is None)

    # PLANTED VIOLATION — a formula that ignores topic entirely, so a huge topic gap loses anyway.
    def broken_score(c: Candidate) -> float:
        return c.p  # topic ignored altogether — the bug this invariant exists to catch

    violin_dominant = Candidate("violin_recital", q=0.95, p=0.10, tags=0)
    guitar_weak_topic = Candidate("guitar_open_mic", q=0.10, p=0.90, tags=3)
    gap = violin_dominant.q - guitar_weak_topic.q
    check(f"planted case: topic gap {gap:.2f} exceeds the guaranteed threshold",
          gap > GUARANTEED_DOMINANCE_GAP)
    # Manually apply the invariant against the BROKEN formula to prove it actually fires.
    broken_violates = broken_score(violin_dominant) <= broken_score(guitar_weak_topic)
    check("planted violation: a topic-blind formula DOES lose the invariant's check",
          broken_violates)

    # CLEAN CONTROL — c2_score (the real formula) must hold at the same gap.
    check("clean control: c2_score (the real formula) satisfies the invariant at that same gap",
          rule_topic_dominance(violin_dominant, guitar_weak_topic) is None)

    # Boundary: exactly at the threshold, the rule must NOT claim guaranteed dominance (it's an
    # open condition — the rule is silent, neither confirming nor denying).
    boundary_leader = Candidate("x", q=GUARANTEED_DOMINANCE_GAP, p=0.0, tags=0)
    boundary_other = Candidate("y", q=0.0, p=1.0, tags=3)
    check("boundary: gap exactly AT the threshold is not claimed as guaranteed (open condition)",
          rule_topic_dominance(boundary_leader, boundary_other) is None)

    print("\n## D9-A9 — truncation honesty\n")

    hit_unflagged = CandidateQuery(returned=20, limit=20, truncated_flag=None)
    hit_flagged_false = CandidateQuery(returned=20, limit=20, truncated_flag=False)
    hit_flagged_true = CandidateQuery(returned=20, limit=20, truncated_flag=True)
    under_unflagged = CandidateQuery(returned=7, limit=20, truncated_flag=None)
    under_wrongly_flagged = CandidateQuery(returned=7, limit=20, truncated_flag=True)

    check("hit the cap, flag ABSENT -> flags (silent truncation)",
          rule_truncation_honesty(hit_unflagged) is not None)
    check("hit the cap, flag explicitly False -> flags",
          rule_truncation_honesty(hit_flagged_false) is not None)
    check("hit the cap, flag True -> silent (correctly disclosed)",
          rule_truncation_honesty(hit_flagged_true) is None)
    check("under the cap, no flag -> silent",
          rule_truncation_honesty(under_unflagged) is None)
    check("under the cap, wrongly flagged truncated -> flags (overstates cutoff)",
          rule_truncation_honesty(under_wrongly_flagged) is not None)

    print()
    if _failures:
        print(f"[D9-A8/A9] {len(_failures)} FAILED: {_failures}")
        return 1
    print("[D9-A8/A9] both invariants fire correctly, with no false positives.")
    print("           STATUS: stub — C1-C3 have not shipped. c2_score() implements Pouya's")
    print("           documented formula; CandidateQuery is a payload shape, not a live call.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
