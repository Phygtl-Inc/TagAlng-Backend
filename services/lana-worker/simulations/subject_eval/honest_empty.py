"""
honest_empty.py — T6: when a search comes up empty, does Lana say so honestly?

Offline, deterministic, no LLM, no DB. Scans a (reply, result-state) pair against the rules the
product ALREADY ships in `app/activity_browse.py`, rather than a new set invented here.

WHY REUSE RATHER THAN INVENT — Pouya's point, and he is right
------------------------------------------------------------
The authority spec's T6 says the explicit-requirement filter must announce an empty set rather
than silently widening. The browse path already solved that problem, and `_compose_empty_seek_offer`
states the governing rule in its own docstring:

    "ONE rule holds the whole thing together: the options this copy offers are exactly the
     pills rendered under it, and nothing else."

It also records what happened when that broke: the prompt hardcoded *"or widen the search"*,
which contradicted the pill whenever a far-area or community shape was armed — and with no LLM
configured, that mismatch was the ONLY output the user ever saw.

Writing a second set of rules for "I could not find what you asked for" would give the app two
voices for the same moment. So these checks assert the shipped invariant and extend it to the
authority filter.

THE RULES
  copy_pill_agreement   every option the copy offers is a rendered pill, and vice versa
  no_invented_supply    the reply names no activity that is not in the result set
  honest_empty          zero results => the reply says so, and does not imply a match
  widening_disclosed    results from the wider ring are announced as further out
  constraint_honesty    a filter that emptied the set is named, and the fallback set is not
                        presented as if it met the constraint

Every rule has a clean control. A false positive here blocks a merge on copy that was fine.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Phrases that OFFER an action. Matched against the pills actually rendered, because the
# shipped bug was copy promising something no pill could do.
_OFFERS = (
    # An OFFER to widen needs a volitional frame. Bare "further out" is how the product
    # DISCLOSES a widened result, and `rule_widening_disclosed` requires exactly that wording —
    # matching it here punished the honest phrasing (caught by t6_08's clean control).
    ("widen", re.compile(
        r"\bwiden(?:ing)? the search\b|"
        r"(?:want me to|shall i|should i|i can|i could|would you like me to)\s+"
        r"(?:widen|broaden|look (?:further|wider)|search (?:further|wider))", re.I)),
    ("look_in_area", re.compile(r"\blook in ([A-Z][\w' -]{2,30})", re.I)),
    ("look_beyond_community", re.compile(r"\blook beyond ([A-Z][\w' -]{2,30})", re.I)),
    # The product's own empty-state copy says "set one up", which this missed (t6_06).
    ("host", re.compile(r"\bset (?:something|one|it|that) up\b|\bset up\b|\bhost\b|"
                        r"\bstart (?:one|something)\b", re.I)),
    ("notify", re.compile(r"\b(keep an ear out|let you know|tell you when|listen for)\b", re.I)),
)

# Language that asserts a match exists. Illegal when the result set is empty.
_ASSERTS_MATCH = re.compile(
    r"\bhere(?:'s| is| are)\b|\bi found\b|\bthere(?:'s| is| are) (?:a|an|\d|some|several)\b|"
    r"\bthese (?:are|look)\b|\bfound (?:a|an|\d|some|several)\b", re.I)

# Language that admits nothing was found.
_ADMITS_EMPTY = re.compile(
    r"\bnothing\b|\bno (?:one|body|matches|results|activities|events|meets)\b|"
    # "No running groups nearby at the moment" — a bare `no <noun phrase>` carrying a place or
    # time qualifier. The qualifier is required so this cannot fire on "no problem" (t6_03).
    # `*_` are in the class because the shipped copy bolds the interest:
    # "No **pottery** activities near you right now". Without them this reads as a reply that
    # never admits the search was empty — a false alarm on correct product copy.
    r"\bno\s+[\w'*_ -]{2,40}?\s*(?:near(?:by)?\b|around\b|in your area\b|at\b|"
    r"at the moment\b|right now\b|yet\b|so far\b)|"
    r"\bcouldn'?t find\b|\bcould not find\b|\bdidn'?t find\b|\bnot (?:seeing|finding)\b|"
    r"\bnone\b|\bempty\b", re.I)

# Language that admits the results are from further away than asked.
_ADMITS_FAR = re.compile(
    r"\bfurther out\b|\bfarther\b|\ba bit further\b|\bnot close\b|\b\d+\s*(?:mi|miles|km)\b|"
    r"\bin ([A-Z][\w' -]{2,30})\b|\boutside\b|\bbeyond\b", re.I)


@dataclass
class ReplyState:
    """One empty-or-thin search turn, as the harness sees it."""
    reply: str
    interest: str = ""
    matched: list[str] = field(default_factory=list)      # names actually in the result set
    pills: list[str] = field(default_factory=list)        # action ids rendered under the copy
    widened: bool = False                                  # results came from the wider ring
    constraint: str = ""                                   # e.g. "from someone Turkish"
    constraint_emptied: bool = False                       # the filter removed everything


def _offers_in(reply: str) -> set[str]:
    return {name for name, rx in _OFFERS if rx.search(reply)}


def rule_copy_pill_agreement(s: ReplyState) -> str | None:
    """The shipped invariant: offered options and rendered pills are the same set."""
    offered, pills = _offers_in(s.reply), set(s.pills)
    only_copy = offered - pills
    only_pill = pills - offered
    bits = []
    if only_copy:
        bits.append(f"copy offers {sorted(only_copy)} with no pill to deliver it")
    if only_pill:
        bits.append(f"pills {sorted(only_pill)} are rendered but never mentioned")
    return "; ".join(bits) or None


def rule_no_invented_supply(s: ReplyState) -> str | None:
    """No activity named in the reply that is not in the result set.

    Conservative on purpose: only quoted or Capitalised multi-word names are treated as
    claims, because a loose noun match would fire on ordinary prose."""
    named = set(re.findall(r"[“\"']([^”\"']{3,40})[”\"']", s.reply))
    named |= set(re.findall(r"\b([A-Z][a-z]+(?: [A-Z][a-z]+){1,3})\b", s.reply))
    known = {m.lower() for m in s.matched}
    # Area and community names legitimately appear in the two non-plain shapes.
    allowed = known | {s.interest.lower()} | {w.lower() for w in re.findall(
        r"\blook (?:in|beyond) ([A-Z][\w' -]{2,30})", s.reply)}
    invented = [n for n in named if n.lower() not in allowed and len(n.split()) > 1]
    return f"reply names {invented} which are not in the result set" if invented else None


def rule_honest_empty(s: ReplyState) -> str | None:
    """Zero results must be stated, and never dressed as a match."""
    if s.matched:
        return None
    admits = _ADMITS_EMPTY.search(s.reply)
    # ORDER MATTERS. The far-area shape legitimately does both: "No pottery activities near you
    # right now — though there are some in Winter Park." It has admitted the local result is
    # empty, and the assertion is about somewhere else. Checking the assertion FIRST flagged
    # the product's own shipped copy as dishonest.
    if not admits:
        if _ASSERTS_MATCH.search(s.reply):
            return "result set is empty but the reply asserts a match"
        return "result set is empty and the reply never says so"
    return None


def rule_widening_disclosed(s: ReplyState) -> str | None:
    """`_widen_search` exists so an empty state becomes a wider one — but its own docstring
    says 'nothing near is announced as far'. The converse has to hold too: nothing FAR may be
    announced as near."""
    if not s.widened or not s.matched:
        return None
    if not _ADMITS_FAR.search(s.reply):
        return ("results came from the widened ring but the reply does not say they are "
                "further out — a silent widen")
    return None


def rule_constraint_honesty(s: ReplyState) -> str | None:
    """The authority spec's own T6: when the explicit filter empties the set, say so and do not
    present the unfiltered fallback as if it met the constraint."""
    if not s.constraint_emptied:
        return None
    head = s.constraint.strip().split()[-1].lower() if s.constraint.strip() else ""
    if not _ADMITS_EMPTY.search(s.reply):
        return f"the {s.constraint!r} filter emptied the set but the reply never says so"
    if s.matched and head and re.search(rf"\b{re.escape(head)}\b", s.reply):
        # Naming the constraint next to results is only safe if the reply also disclaims it.
        if not re.search(r"\binstead\b|\bothers\b|\bbut\b|\bhere'?s what\b|\bnot\b", s.reply, re.I):
            return (f"fallback results are shown while still naming {s.constraint!r}, with no "
                    f"wording separating the two — reads as if the constraint was met")
    return None


RULES = (
    ("copy_pill_agreement", rule_copy_pill_agreement),
    ("no_invented_supply", rule_no_invented_supply),
    ("honest_empty", rule_honest_empty),
    ("widening_disclosed", rule_widening_disclosed),
    ("constraint_honesty", rule_constraint_honesty),
)


def scan(s: ReplyState) -> tuple[list[str], list[str]]:
    fired, detail = [], []
    for name, fn in RULES:
        got = fn(s)
        if got:
            fired.append(name)
            detail.append(f"{name}: {got}")
    return fired, detail


def from_case(case: dict[str, Any]) -> ReplyState:
    return ReplyState(
        reply=str(case.get("reply") or ""),
        interest=str(case.get("interest") or ""),
        matched=list(case.get("matched") or []),
        pills=list(case.get("pills") or []),
        widened=bool(case.get("widened")),
        constraint=str(case.get("constraint") or ""),
        constraint_emptied=bool(case.get("constraint_emptied")),
    )
