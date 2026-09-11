"""
lingo_guardrail.py — Lana in-app language guardrail (mechanical, pre-send).

Ported from LANA_LINGO_v1 §14.2 (the drop-in `lingo_guardrail.py` the doc hands to Asjid),
extended with the full §2 hard-rule + §7 lexicon ban list. This is a MECHANICAL check: the
ground truth ("this word must never appear in app copy") is known by construction, so no LLM
is involved and there is no circularity risk. checks.py calls scan() on every utterance AND
every chip label.

Applies to APP surfaces only. Marketing/website copy is exempt (LANA_LINGO §10) — this harness
only ever evaluates in-app turns, so everything here is in-scope.

Importable standalone: the existing simulation.py transcript pipeline can `import
lingo_guardrail` and run scan() over `lana_reply` + `ui_actions` labels to get the same
never-says-"mom" gate on REAL Lana output.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Hard bans — LINGO §2 (hard rules) + §7 (lexicon). Word-boundary, case-insensitive.
# Each entry: (compiled regex, short reason citing the rule).
# ---------------------------------------------------------------------------

# Each entry: (regex, reason, is_points). is_points=True marks the ONLY ban the "points you
# toward…" allow-list may suppress — the allow window is scoped to it so it can never silently
# swallow an unrelated hard ban (block/zip/circle) that happens to sit near a 'points' phrase.
_BANNED_SPECS: list[tuple[str, str, bool]] = [
    (r"\bmom(s|my|mies)?\b",          "no 'mom' in-app (§2.1 / §10)", False),
    (r"\bmama(s)?\b",                 "no 'mama' (§2.1)", False),
    (r"\bmum(s|my)?\b",               "no 'mum' (§2.1)", False),
    (r"\bmommy\b",                    "no 'mommy' (§2.1)", False),
    (r"\bcircles?\b",                 "say the concrete community, not 'circle' (§2.2 / §7.3)", False),
    (r"\bblocks?\b",                  "say 'area'/'near you', not 'block' (§2.3 / §7.2)", False),
    (r"\bzip\b",                      "say 'area'/'neighborhood', not 'ZIP' (§7.2)", False),
    (r"\bzones?\b",                   "say 'area', not 'zone' (§7.2)", False),
    (r"\bvicinity\b",                 "say 'near you', not 'vicinity' (§7.2)", False),
    (r"\bgeofence\b",                 "say 'your area', not 'geofence' (§7.2)", False),
    (r"\bleaderboards?\b",            "no gamification (§2.8 / §7.6)", False),
    (r"\bstreaks?\b",                 "no streaks (§7.6)", False),
    (r"\bpoints?\b",                  "no points (§7.6)", True),  # allow-list scoped to this only
    (r"\blevel up\b",                 "no 'level up' (§7.6)", False),
    (r"\bsubmit\b",                   "say 'Send'/'Next', not 'Submit' (§7.7)", False),
    (r"\bclick here\b",               "no 'click here' (§7.7)", False),
    (r"\bloading\b\s*[.…]*",          "warm loading copy, never 'Loading…' (§7.8)", False),
    (r"\bno results?\b",              "never dead-end with 'no results' (§7.8)", False),
    (r"\b0 results?\b",               "never dead-end with '0 results' (§7.8)", False),
    (r"\berrors?\b",                  "warm failure copy, not 'Error(s)' (§7.8)", False),
    (r"\bfail(ed|s|ing|ure)?\b",      "warm failure copy, not 'Fail/Failed/Failure' (§7.8)", False),
]

_BANNED = [(re.compile(p, re.I), why, is_pts) for p, why, is_pts in _BANNED_SPECS]

# "match" is banned ONLY when it refers to a person (§2.4 / §7.4: say "someone to meet"/"an
# intro"). "matches"/"match quality" appear legitimately in eval prose, so this is deliberately
# NOT in the hard list — checks.py treats a person-"match" as a judged nuance, not a regex hard-fail.

# Warm senses of "points" that are NOT gamification. Narrow on purpose: "points toward a reward"
# IS gamification, so only the "points you…" / "at this point" senses are exempted.
_DEFAULT_ALLOW: tuple[str, ...] = ("points you", "at this point")


def scan(text: str, *, allow: tuple[str, ...] = _DEFAULT_ALLOW) -> list[tuple[str, str]]:
    """Return [(matched_substring, reason), ...] for every banned in-app term in `text`.

    The `allow` window is applied ONLY to the 'points' ban (is_points), so a warm
    "points you toward your people" passes while unrelated hard bans are never suppressed.
    """
    text = text or ""
    low = text.lower()
    hits: list[tuple[str, str]] = []
    for rx, why, is_points in _BANNED:
        for m in rx.finditer(text):
            span = m.group(0)
            if is_points:
                start = m.start()
                window = low[max(0, start - 6): start + len(span) + 10]
                if any(a in window for a in allow):
                    continue
            hits.append((span, why))
    return hits


def is_clean(text: str, *, allow: tuple[str, ...] = _DEFAULT_ALLOW) -> bool:
    return not scan(text, allow=allow)


# ---------------------------------------------------------------------------
# ES/PT gendered-token detector — LINGO §4.2 mechanical assist.
# For the "grammatical_gender == unknown -> must stay neutral, NEVER default feminine" case,
# we can mechanically FLAG obviously-gendered greetings/adjectives. (Correct AGREEMENT when
# gender IS known is a judgment call left to judge.py.)
# ---------------------------------------------------------------------------

# (regex, tag) — deliberately ONLY unambiguous ADDRESSEE greetings, i.e. forms that gender the
# person being welcomed. Earlier drafts also flagged 'obrigad[oa]' (thanks — referent ambiguous),
# 'encantad[oa]'/'list[oa]' (refer to Lana herself, or 'lista' = the noun "list"), and 'tod[oa]s'
# ('todos' = generic "everyone"). Those cause false-positive HARD_FAILs on legitimate copy, so they
# are OUT of the mechanical gate — the judge's gender_agreement axis covers those subtler cases.
_GENDERED_SPECS: list[tuple[str, str]] = [
    (r"\bbienvenid[oa]s?\b",   "es-greeting"),   # bienvenido/bienvenida — welcomes the addressee
    (r"\bbem[- ]vind[oa]s?\b", "pt-greeting"),   # bem-vindo/bem-vinda — welcomes the addressee
]
_GENDERED = [(re.compile(p, re.I), tag) for p, tag in _GENDERED_SPECS]


def gendered_tokens(text: str) -> list[tuple[str, str]]:
    """Return [(token, tag), ...] of overtly gendered ES/PT ADDRESSEE greetings in `text`.

    Used by checks.py ONLY when world.grammatical_gender == 'unknown' and locale in (es, pt): a hit
    means Lana welcomed the user with a gendered greeting where a neutral rephrase was required.
    Kept narrow ON PURPOSE — a mechanical HARD_FAIL must have ~zero false positives, so only
    unambiguous addressee-gendered greetings qualify; subtler agreement is judge.py's job.
    """
    text = text or ""
    return [(m.group(0), tag) for rx, tag in _GENDERED for m in [rx.search(text)] if m]
