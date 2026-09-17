"""
d7_groundedness.py — D7: does a subject-graph reply claim only what `subject_context()` actually
returned, and does it respect the payload's OWN suppression rules?

    cd services/lana-worker/simulations/subject_eval
    python d7_groundedness.py

Offline, deterministic, no LLM, no DB. A4 (`subject_context`) has not shipped, so this is built
against a STUB implementing the v2 spec AS CORRECTED — the five fields below are not the
as-shipped-in-the-original-doc versions, they are the versions already accepted into contract v2
after this eval found them wrong (EVAL_STATUS.md §2.3 findings #1-#3). Point this at the real
`subject_context()` the day A4 ships by swapping `StubSubjectContext` for the real call; the rule
functions do not change.

WHY THIS IS "GROUNDEDNESS" AND NOT A DUPLICATE OF T6
-----------------------------------------------------
T6 (honest_empty.py) checks that a REPLY does not claim a result the SEARCH did not return. D7
checks the same shape of thing one level up the stack: that a reply about a subject does not
claim a fact the AGGREGATION did not return — a count, a confidence, a community name, a
consensus — where "the aggregation" is `subject_context()`, not a places search. Different data
source, same failure family: a number in the reply that traces to nothing in the payload.

THE FIVE RULES, each mirroring a fix already accepted into v2
  floor_honesty        a subject is surfaced only when `effective_n >= 3` (the display floor).
                        Below it, the payload itself should be empty/absent — a reply built from
                        a below-floor payload is grounded in data the product has decided isn't
                        enough to show.
  no_invented_count     any attester count named in the reply is <= what the payload supports.
                        Distinguishes DECAYED support (effective_n, what "how many people" should
                        mean) from RAW support (n_attesters) — EVAL_STATUS.md 2.3#7 note that
                        n_attesters is computed then unused elsewhere; a reply that quotes it
                        instead of effective_n overstates freshness-weighted support.
  contested_honesty     `is_contested` (corrected: unanimous negatives count, not just a
                        high/low split) forbids a confident single-answer framing.
  community_reid_guard  the shared-community NAME appears only when `n_shared_community >= 2`
                        (below that, naming it re-identifies one specific neighbour — v2 §A4.2
                        finding #3). The COUNT may still appear.
  no_uncorroborated_specifics  a reply names no attribute (price, hours, a named detail) that
                        is not present in the payload's own `attributes` — same shape as T6's
                        `no_invented_supply`, applied to subject attestations instead of places.

Every rule below is fed a PLANTED violation and a matching clean control in main(). A check that
fires on everything is as useless as one that never fires — both block good merges or let bad
ones through, and this suite's convention (see CLAUDE.md) is that neither ships without both
cases proven.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_HERE = Path(__file__).resolve().parent


# ── the stub subject_context(), honoring v2 AS CORRECTED ───────────────────────────────────────

@dataclass
class SubjectContext:
    """What A4's `subject_context()` returns, per the spec as corrected in v2.

    `effective_n = floor(sum(max(w) per attester))` — a DECAYED count: an attester's weight
    decays with attestation age, so this is "how much fresh support exists", not a headcount.
    `n_attesters` is the RAW headcount, computed but (per the product code today) unused in
    subject-level sort — kept here only so a check can verify a reply doesn't quote it instead.
    """
    subject_id: str
    effective_n: int              # floor(sum decayed weight) — the DISPLAY FLOOR uses this
    n_attesters: int              # raw headcount — NOT the number a reply should quote
    n_high: int                   # raw, undecayed
    n_low: int                    # raw, undecayed
    shared_community_name: str | None
    n_shared_community: int       # members of that community among the attesters
    attributes: dict[str, Any] = field(default_factory=dict)   # e.g. {"price": None, "hours": None}

    @property
    def is_contested(self) -> bool:
        """Corrected per EVAL_STATUS.md §2.3#2: unanimous negatives must count as contested.
        The original `n_high >= 2 and n_low >= 2` needs four attesters and reads a 2-vs-1 split
        (three people, no n_high>=2) as consensus. Share-based catches both."""
        total = self.n_high + self.n_low
        if total == 0:
            return False
        return (self.n_low / total) >= 0.25

    @property
    def below_floor(self) -> bool:
        return self.effective_n < 3

    @property
    def community_suppressed(self) -> bool:
        """v2 §A4.2 finding #3: naming the community with only one shared member
        re-identifies that one neighbour. The NAME is suppressed; the count is not."""
        return self.n_shared_community < 2


@dataclass
class ReplyClaim:
    """One reply, as the harness sees it, checked against the SubjectContext it was built from."""
    reply: str
    ctx: SubjectContext


# ── rules ────────────────────────────────────────────────────────────────────────────────────

_COUNT_CLAIM = re.compile(
    r"\b(\d+)\s*(?:people|neighbou?rs|households|families|attesters?)\b", re.I)
_CONFIDENT_CONSENSUS = re.compile(
    r"\beveryone (?:says|agrees|loves|recommends)\b|\ball(?:\s\w+){0,2}\s(?:say|agree|love)s?\b|"
    r"\bthe (?:go-?to|clear favou?rite|consensus)\b|\bunanimously\b|\bwithout exception\b", re.I)
_ADMITS_MIXED = re.compile(
    r"\bmixed\b|\bsplit\b|\bsome (?:say|liked?|didn'?t)\b|\bnot everyone\b|\bfew (?:said|had)\b|"
    r"\bdivided\b|\bopinions? (?:vary|differ)\b|\bcouple of (?:complaints|concerns)\b", re.I)


def rule_no_invented_count(c: ReplyClaim) -> str | None:
    """Any number-of-people claim in the reply must not exceed the DECAYED support
    (`effective_n`), and must not be the raw `n_attesters` figure when that overstates it."""
    nums = [int(m.group(1)) for m in _COUNT_CLAIM.finditer(c.reply)]
    if not nums:
        return None
    bad = [n for n in nums if n > c.ctx.effective_n]
    if bad:
        return (f"reply claims {bad} attester(s) but effective_n (decayed support) is only "
                f"{c.ctx.effective_n}")
    if c.ctx.n_attesters > c.ctx.effective_n and any(n == c.ctx.n_attesters for n in nums):
        return (f"reply quotes the RAW headcount ({c.ctx.n_attesters}) instead of the decayed "
                f"effective_n ({c.ctx.effective_n}) — overstates freshness-weighted support")
    return None


def rule_contested_honesty(c: ReplyClaim) -> str | None:
    """is_contested forbids a confident single-answer framing, and REQUIRES the reply to admit
    the split rather than presenting only the majority view."""
    if not c.ctx.is_contested:
        return None
    if _CONFIDENT_CONSENSUS.search(c.reply):
        return "payload is contested (unanimous-negative-aware) but reply frames it as consensus"
    if not _ADMITS_MIXED.search(c.reply):
        return "payload is contested but the reply never signals the split"
    return None


def rule_community_reid_guard(c: ReplyClaim) -> str | None:
    """The community NAME may appear only once >=2 attesters share it."""
    name = c.ctx.shared_community_name
    if not name:
        return None
    if c.ctx.community_suppressed and re.search(rf"\b{re.escape(name)}\b", c.reply, re.I):
        return (f"community {name!r} is named but only {c.ctx.n_shared_community} attester(s) "
                f"share it — re-identifies that one neighbour")
    return None


def rule_no_uncorroborated_specifics(c: ReplyClaim) -> str | None:
    """A quoted price or hours must exist in the payload's own attributes. Same shape as T6's
    `no_invented_supply`: a specific claim with nothing behind it."""
    money = re.search(r"\$\s?\d+(?:\.\d{2})?", c.reply)
    if money and c.ctx.attributes.get("price") in (None, ""):
        return f"reply states a price ({money.group(0)}) not present in subject_context attributes"
    hours = re.search(r"\b(?:open|closes?|hours?)\b.{0,20}\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b", c.reply, re.I)
    if hours and c.ctx.attributes.get("hours") in (None, ""):
        return f"reply states hours ({hours.group(0)!r}) not present in subject_context attributes"
    return None


RULES = (
    ("no_invented_count", rule_no_invented_count),
    ("contested_honesty", rule_contested_honesty),
    ("community_reid_guard", rule_community_reid_guard),
    ("no_uncorroborated_specifics", rule_no_uncorroborated_specifics),
)


def scan(c: ReplyClaim) -> tuple[list[str], list[str]]:
    fired, detail = [], []
    for name, fn in RULES:
        got = fn(c)
        if got:
            fired.append(name)
            detail.append(f"{name}: {got}")
    return fired, detail


def rule_floor_gate(ctx: SubjectContext, surfaced: bool) -> str | None:
    """The floor is a property of the PAYLOAD, checked independently of reply wording: a subject
    with effective_n < 3 must not be surfaced at all. Separate signature from the reply rules
    above because this is a decision made before any reply text exists."""
    if ctx.below_floor and surfaced:
        return f"subject surfaced despite effective_n={ctx.effective_n} < floor (3)"
    if not ctx.below_floor and not surfaced:
        return f"subject withheld despite effective_n={ctx.effective_n} >= floor (3)"
    return None


# ── non-vacuity proof: every rule gets a planted violation AND a clean control ─────────────────

_failures: list[str] = []


def check(name: str, condition: bool) -> None:
    print(f"  [{'ok ' if condition else 'FAIL'}] {name}")
    if not condition:
        _failures.append(name)


def main() -> int:
    print("[D7] groundedness — reply claims vs. a spec-correct subject_context() stub\n")

    # ---- floor_gate: independent of reply text --------------------------------------------
    below = SubjectContext("s1", effective_n=2, n_attesters=2, n_high=2, n_low=0,
                            shared_community_name=None, n_shared_community=0)
    at = SubjectContext("s2", effective_n=3, n_attesters=3, n_high=3, n_low=0,
                         shared_community_name=None, n_shared_community=0)
    check("floor_gate: below-floor subject surfaced -> flags",
          rule_floor_gate(below, surfaced=True) is not None)
    check("...below-floor subject correctly WITHHELD -> silent",
          rule_floor_gate(below, surfaced=False) is None)
    check("...at-floor subject correctly surfaced -> silent",
          rule_floor_gate(at, surfaced=True) is None)
    check("...at-floor subject wrongly withheld -> flags",
          rule_floor_gate(at, surfaced=False) is not None)

    # ---- no_invented_count -------------------------------------------------------------------
    ctx = SubjectContext("s3", effective_n=3, n_attesters=6, n_high=3, n_low=0,
                          shared_community_name=None, n_shared_community=0)
    over = ReplyClaim("6 neighbors have mentioned this spot.", ctx)
    raw_quoted = ReplyClaim("6 people said good things.", ctx)          # n_attesters, not effective_n
    honest = ReplyClaim("A few neighbors have mentioned this spot.", ctx)
    exact = ReplyClaim("3 neighbors have mentioned this spot.", ctx)
    check("no_invented_count: claiming the raw headcount as a fact -> flags",
          rule_no_invented_count(over) is not None)
    check("...quoting n_attesters(6) instead of effective_n(3) -> flags",
          rule_no_invented_count(raw_quoted) is not None)
    check("...vague wording with no number -> silent",
          rule_no_invented_count(honest) is None)
    check("...a count within effective_n -> silent",
          rule_no_invented_count(exact) is None)

    # ---- contested_honesty --------------------------------------------------------------------
    contested = SubjectContext("s4", effective_n=3, n_attesters=3, n_high=0, n_low=3,
                                shared_community_name=None, n_shared_community=0)
    clean = SubjectContext("s5", effective_n=3, n_attesters=3, n_high=3, n_low=0,
                            shared_community_name=None, n_shared_community=0)
    check("contested_honesty: unanimous-negative payload IS flagged contested",
          contested.is_contested)
    check("...unanimous-positive payload is NOT contested",
          not clean.is_contested)
    over_confident = ReplyClaim("Everyone loves this place!", contested)
    silent_on_split = ReplyClaim("This place has been mentioned by a few neighbors.", contested)
    admits_split = ReplyClaim("Opinions are mixed on this one.", contested)
    fine_on_clean = ReplyClaim("Everyone loves this place!", clean)
    check("contested_honesty: consensus framing on a contested payload -> flags",
          rule_contested_honesty(over_confident) is not None)
    check("...contested payload with no admission of the split at all -> flags",
          rule_contested_honesty(silent_on_split) is not None)
    check("...contested payload that DOES admit the split -> silent",
          rule_contested_honesty(admits_split) is None)
    check("...the SAME consensus wording on a genuinely clean payload -> silent",
          rule_contested_honesty(fine_on_clean) is None)

    # ---- community_reid_guard ------------------------------------------------------------------
    one_shared = SubjectContext("s6", effective_n=3, n_attesters=3, n_high=3, n_low=0,
                                 shared_community_name="Winter Park Moms", n_shared_community=1)
    two_shared = SubjectContext("s7", effective_n=3, n_attesters=3, n_high=3, n_low=0,
                                 shared_community_name="Winter Park Moms", n_shared_community=2)
    named_at_one = ReplyClaim("A few folks from Winter Park Moms recommended this.", one_shared)
    count_only_at_one = ReplyClaim("A couple of neighbors from a shared group recommended this.", one_shared)
    named_at_two = ReplyClaim("A few folks from Winter Park Moms recommended this.", two_shared)
    check("community_reid_guard: naming the community with only 1 shared member -> flags",
          rule_community_reid_guard(named_at_one) is not None)
    check("...same payload, count-only wording (no name) -> silent",
          rule_community_reid_guard(count_only_at_one) is None)
    check("...naming the SAME community once 2+ share it -> silent",
          rule_community_reid_guard(named_at_two) is None)

    # ---- no_uncorroborated_specifics -----------------------------------------------------------
    bare_ctx = SubjectContext("s8", effective_n=3, n_attesters=3, n_high=3, n_low=0,
                               shared_community_name=None, n_shared_community=0, attributes={})
    priced_ctx = SubjectContext("s9", effective_n=3, n_attesters=3, n_high=3, n_low=0,
                                 shared_community_name=None, n_shared_community=0,
                                 attributes={"price": "$12/mo"})
    invented_price = ReplyClaim("Membership runs about $12 a month.", bare_ctx)
    grounded_price = ReplyClaim("Membership runs about $12 a month.", priced_ctx)
    no_specifics = ReplyClaim("A few neighbors have mentioned this place.", bare_ctx)
    check("no_uncorroborated_specifics: a price with nothing in attributes -> flags",
          rule_no_uncorroborated_specifics(invented_price) is not None)
    check("...the SAME price, backed by attributes -> silent",
          rule_no_uncorroborated_specifics(grounded_price) is None)
    check("...no specifics claimed at all -> silent",
          rule_no_uncorroborated_specifics(no_specifics) is None)

    print()
    if _failures:
        print(f"[D7] {len(_failures)} FAILED: {_failures}")
        return 1
    print("[D7] all groundedness rules fire correctly, with no false positives.")
    print("     STATUS: stub only — subject_context() here implements v2 AS CORRECTED, not A4's")
    print("     shipped code (A4 has not shipped). Point ctx construction at the real RPC when it")
    print("     does; the five rules above do not change.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
