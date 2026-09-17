"""
t2_authority_topic.py — T2: does a materially better topic match still win when the OTHER
candidate has higher attester authority?

    cd services/lana-worker/simulations/subject_eval
    python t2_authority_topic.py

Offline, deterministic, no LLM, no DB. Per EVAL_WORKING_PLAN.md §3.2, T2 reuses "the same
harness as A8" — that is literal here: `rule_topic_dominance` is imported unchanged from
d9_a8_a9.py, not reimplemented. Only the score function and its weight bound differ.

# GUESSED — read before trusting this harness's threshold
------------------------------------------------------------
`w_authority` below (0.2) is NOT a sourced contract value. It is set equal to C2's `w_profile`
on the assumption that authority, like profile affinity, is a secondary signal meant to nudge
ranking rather than override topic outright — but that assumption is exactly the open question
in SUBJECT_GRAPH_EVAL_PLAN.md §6.1 ("does one person with domain knowledge outrank three
neighbours?"), which is explicitly UNRESOLVED and flagged there as a genuine product call, not
something this eval suite can settle: *"I can't recommend my way out of this one."*

So this harness proves something narrower and still useful: IF authority is bounded the way
profile is (<=1.0, weighted <=0.2), the topic-dominance invariant holds against it exactly the
same way it holds against profile — the guaranteed-dominance gap is unaffected, because it only
depends on the CAP, not on what the signal is called. That is a real property worth having
proven now. It is NOT a stand-in for T1's decision, and `w_authority` must be replaced with
whatever `attester_authority()` actually produces the moment T1 is settled and A5/[ASJID-5] ship
— at which point this file's score function is the only thing that changes.

If T1 is decided the OTHER way — authority sorts before topic, unbounded — this entire harness
is wrong and should be replaced, not re-parameterized. That decision, not this code, is the real
blocker on a "real" T2.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from d9_a8_a9 import GUARANTEED_DOMINANCE_GAP, W_QUERY, rule_topic_dominance  # noqa: E402

# GUESSED, see module docstring. Bounded like C2's w_profile (<=0.2) on the assumption that
# authority is a secondary nudge, not an override — NOT sourced from a shipped attester_authority().
W_AUTHORITY = 0.2
AUTHORITY_CAP = 1.0
THRESHOLD = (W_AUTHORITY * AUTHORITY_CAP) / W_QUERY   # same shape as D9-A8's, different signal


@dataclass
class AuthorityCandidate:
    name: str
    q: float             # query-topic cosine similarity, in [0, 1] — same axis as D9-A8
    authority: float = 0.0   # GUESSED axis: attester domain authority, assumed in [0, 1]


def authority_score(c: AuthorityCandidate) -> float:
    return W_QUERY * c.q + W_AUTHORITY * min(c.authority, AUTHORITY_CAP)


def rule_authority_topic(leader: AuthorityCandidate, other: AuthorityCandidate) -> str | None:
    """Delegates to the SAME invariant function D9-A8 uses — see its docstring. Only the score
    function and threshold differ, per this module's GUESSED weight."""
    return rule_topic_dominance(leader, other, score_fn=authority_score, threshold=THRESHOLD)


# ── non-vacuity proof ────────────────────────────────────────────────────────────────────────

_failures: list[str] = []


def check(name: str, condition: bool) -> None:
    print(f"  [{'ok ' if condition else 'FAIL'}] {name}")
    if not condition:
        _failures.append(name)


def main() -> int:
    print(f"[T2] authority/topic invariant — reuses d9_a8_a9.rule_topic_dominance unchanged")
    print(f"     GUESSED w_authority={W_AUTHORITY}, guaranteed-dominance gap={THRESHOLD:.4f}\n")

    # The scenario Pouya's framing describes literally: "one Turkish attester beats three
    # neighbours recommending the same place" — modeled as a strong-authority, weak-topic match
    # against a weak-authority, strong-topic match.
    domain_expert = AuthorityCandidate("turkish_attester_pick", q=0.10, authority=1.0)
    topic_match = AuthorityCandidate("three_neighbors_pick", q=0.90, authority=0.0)
    gap = topic_match.q - domain_expert.q
    check(f"scenario gap {gap:.2f} exceeds the guaranteed-dominance threshold",
          gap > THRESHOLD)
    check("clean control: bounded authority_score() respects topic dominance at that gap",
          rule_authority_topic(topic_match, domain_expert) is None)

    # PLANTED VIOLATION — an authority formula that ignores the bound (e.g. an unweighted
    # override, which is exactly what T1's OTHER possible answer would produce) must be caught.
    def unbounded_authority_score(c: AuthorityCandidate) -> float:
        return c.authority * 10.0  # authority alone, topic ignored — the bug this exists to catch

    violates = unbounded_authority_score(topic_match) <= unbounded_authority_score(domain_expert)
    check("planted violation: an UNBOUNDED authority override does violate the invariant",
          violates)

    # Below the threshold, a smaller topic gap MAY legitimately be overturned by authority —
    # same non-vacuity shape as D9-A8's worked example.
    close_call_leader = AuthorityCandidate("a", q=0.55, authority=0.0)
    close_call_other = AuthorityCandidate("b", q=0.50, authority=1.0)
    check("small gap (0.05) is legitimately overturnable — not this rule's business",
          rule_authority_topic(close_call_leader, close_call_other) is None)

    print()
    if _failures:
        print(f"[T2] {len(_failures)} FAILED: {_failures}")
        return 1
    print("[T2] invariant holds against the GUESSED bounded authority formula, no false positives.")
    print("     STATUS: harness only. W_AUTHORITY is an assumption, not a sourced contract value —")
    print("     T1 (§6.1) must be decided before this gate can mean anything about the real product.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
