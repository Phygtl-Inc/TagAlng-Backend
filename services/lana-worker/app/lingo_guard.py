"""Output-side lingo guardrail (LANA_LINGO §14) — the enforcement half of the
constitution.

The constitution in ``prompts/lana_lingo_constitution.md`` teaches the model the
rules; this module makes them non-negotiable on anything the policy path sends
to a user. Cheap by design: a regex scan runs on every enforced utterance
(~0ms), and the LLM rewrite fires only on a violation — so clean turns pay
nothing. If the rewrite itself fails (LLM down, still-dirty output), a naive
word-map substitution guarantees the banned lexicon never ships.

The real verdict is returned so callers can persist it to
``lana_audit_log.guardrail_result`` — replacing the constant ``{"rail": "ok"}``
stamp with something evals can count.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# User-facing banned lexicon (constitution hard rules 1-4 + 6). Deliberately
# scoped to phrasings a user would read — backstage vocabulary ("block" as a
# data-model noun inside prompts/tools) is out of scope because enforce() only
# ever sees text bound for a user's screen.
_BANNED_RE = re.compile(
    r"\b("
    r"moms?|mommy|mommies|mamas?|mums?|mam[aá]|mam[ãa]e"  # rule 1 (en/es/pt)
    r"|blocks?|cuadra|quadra"                              # rule 2
    r"|circles?|c[ií]rculos?|groups?|grupos?"              # rule 3
    r"|leaderboards?|streaks?|level\s*up"                  # rule 6
    r")\b",
    re.IGNORECASE,
)
# Rule 4 — never call a PERSON a match. Verb uses ("that matches your schedule")
# stay legal; only the person-noun forms are banned.
_MATCH_PERSON_RE = re.compile(
    r"\b(?:a|your|new|perfect|great|good)\s+match\b|\bmatch(?:ed)?\s+you\s+with\b",
    re.IGNORECASE,
)

# Rule 6 also bans "points" and "rank", which _BANNED_RE could never carry: both
# are ordinary English outside the score frame ("that points to the same spot",
# "the highest-ranked taco place"). Scoped to the frame instead, the way rule 4
# scopes "match". The live failure this was written for: asked "am I winning?
# how many points do I have?", Lana answered "No points here — you're not being
# scored" — denying the frame while speaking it (evals, 2026-08-25).
_GAMIFICATION_RE = re.compile(
    r"\b(?:no|any|my|your|their|how\s+many|earn(?:ed|ing)?|\d+)\s+points?\b"
    r"|\bpoints?\s+(?:system|total|balance)\b"
    r"|\b(?:my|your|their|the)\s+rank\b"
    r"|\brank(?:ed|ing)?\s+(?:you\s+)?(?:up|higher|against)\b",
    re.IGNORECASE,
)

# Features that DO NOT EXIST yet. The policy is told never to OFFER swapping, and
# it obeys — but the word alone reads as the feature. A tester saw "if you ever
# want to swap favorite spots" (she meant trading recommendations) and reasonably
# read it as the unbuilt item-swap. Intent doesn't matter here; the word does.
# DELETE THIS RULE the day swap ships — it is a shipping-state guard, not a
# lexicon principle, and it will start suppressing legitimate copy.
_UNSHIPPED_FEATURE_RE = re.compile(
    r"\b(swap(?:s|ped|ping)?|hand[-\s]?me[-\s]?downs?)\b",
    re.IGNORECASE,
)

# Last-resort substitutions when the LLM rewrite is unavailable or still dirty.
# Crude but always lexicon-clean — a slightly stiff sentence beats a banned word.
_NAIVE_SWAPS: list[tuple[re.Pattern[str], str]] = [
    # "swap favorite spots" -> "share favorite spots" reads naturally; the
    # item-trading senses are forbidden upstream, so nothing legitimate is lost.
    (re.compile(r"\bswap(?:s|ped|ping)?\b", re.I), "share"),
    (re.compile(r"\bhand[-\s]?me[-\s]?downs?\b", re.I), "shared items"),
    (re.compile(r"\bmoms\b", re.I), "people"),
    (re.compile(r"\b(?:mom|mommy|mama|mum|mamá|mamãe)\b", re.I), "parent"),
    (re.compile(r"\bblocks?\b", re.I), "area"),
    (re.compile(r"\b(?:cuadra|quadra)\b", re.I), "zona"),
    (re.compile(r"\bcircles?\b", re.I), "community"),
    (re.compile(r"\bc[ií]rculos?\b", re.I), "comunidad"),
    # Rule 3 bans "group" as well as "circle", but only "circle" was in the pattern, so
    # "I can add you to the group" shipped straight past the guard (QA 2026-08-21).
    (re.compile(r"\bgroups?\b", re.I), "community"),
    (re.compile(r"\bgrupos?\b", re.I), "comunidad"),
    (re.compile(r"\bleaderboards?\b", re.I), "neighborhood"),
    (re.compile(r"\bstreaks?\b", re.I), "progress"),
    (re.compile(r"\blevel\s*up\b", re.I), "grow"),
    # Last-resort only, and deliberately blunt: the real repair for a score frame
    # is a reframe ("you're not being scored here"), which the LLM pass above
    # does. These only guarantee the word never ships when that pass is down.
    (re.compile(r"\b(?:no|any|my|your|their|how\s+many|earn(?:ed|ing)?|\d+)\s+points?\b", re.I),
     "nothing to tally"),
    (re.compile(r"\bpoints?\s+(?:system|total|balance)\b", re.I), "tally"),
    (re.compile(r"\b(?:my|your|their|the)\s+rank\b", re.I), "your place"),
    (re.compile(r"\brank(?:ed|ing)?\s+(?:you\s+)?(?:up|higher|against)\b", re.I),
     "comparing you to"),
    # Neutral on purpose: "a match" may be a person OR an item pairing, and the
    # naive path can't tell — "a fit" is safe for both. The LLM rewrite (which
    # runs first) picks the right phrasing from context.
    (re.compile(r"\b(a|your|new|perfect|great|good)\s+match\b", re.I), r"\1 fit"),
    (re.compile(r"\bmatch(?:ed)?\s+you\s+with\b", re.I), "introduce you to"),
]


@dataclass
class GuardResult:
    """What enforce() did — persist this to lana_audit_log.guardrail_result."""

    text: str
    chip_labels: list[str] = field(default_factory=list)
    ok: bool = True
    hits: list[str] = field(default_factory=list)
    rewritten: bool = False
    naive_fallback: bool = False

    def audit_dict(self) -> dict[str, Any]:
        if self.ok:
            return {"rail": "clean"}
        return {
            "rail": "violation",
            "hits": self.hits[:10],
            "rewritten": self.rewritten,
            "naive_fallback": self.naive_fallback,
        }


def find_violations(text: str) -> list[str]:
    """Banned words present in one user-facing string (deduped, lowercase)."""
    if not text:
        return []
    hits = [m.group(0).lower() for m in _BANNED_RE.finditer(text)]
    hits += [m.group(0).lower() for m in _MATCH_PERSON_RE.finditer(text)]
    hits += [m.group(0).lower() for m in _GAMIFICATION_RE.finditer(text)]
    hits += [m.group(0).lower() for m in _UNSHIPPED_FEATURE_RE.finditer(text)]
    seen: set[str] = set()
    out: list[str] = []
    for h in hits:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


def naive_clean(text: str) -> str:
    """Deterministic substitution pass — guaranteed lexicon-clean output."""
    out = text
    for pattern, repl in _NAIVE_SWAPS:
        out = pattern.sub(repl, out)
    return out


def _rewrite_clean(text: str, chip_labels: list[str], hits: list[str]) -> tuple[str, list[str]] | None:
    """One LLM call that rewrites the reply (and any dirty chips) without the
    banned words, keeping meaning/warmth/length. None on any failure."""
    try:
        from app.orchestrator.llm import llm_configured, llm_json, synthesizer_model

        if not llm_configured():
            return None
        payload = {"reply": text, "chips": chip_labels, "banned_words_found": hits}
        import json

        data = llm_json(
            model=synthesizer_model(),
            system=(
                "You fix one chat reply from Lana, a warm neighborhood concierge. "
                "It accidentally used banned words. Rewrite the reply (and any chip "
                "labels that contain them) with the SAME meaning, warmth, language, "
                "and length — only the banned words change. Banned: mom/moms/mama/"
                "mommy/mum (say the person's name, 'you', 'parents', 'people near "
                "you'); block/cuadra/quadra (say 'your area', 'near you'); circle "
                "(name the concrete community — 'your gym', 'your people'); calling "
                "a person a 'match' (say 'someone to meet', 'an intro'); leaderboard/"
                "streak/level up/points/rank — never adopt a score frame even to "
                "deny it, say nobody is being scored here. Return JSON "
                '{"reply": "...", "chips": ["..."]} with exactly one chip per input '
                "chip, same order."
            ),
            user_payload=json.dumps(payload, ensure_ascii=False),
            max_tokens=400,
            temperature=0.2,
        )
        if not isinstance(data, dict):
            return None
        reply = str(data.get("reply") or "").strip()
        chips_raw = data.get("chips")
        chips = (
            [str(c or "").strip() for c in chips_raw]
            if isinstance(chips_raw, list) and len(chips_raw) == len(chip_labels)
            else chip_labels
        )
        if not reply:
            return None
        return reply, chips
    except Exception:  # noqa: BLE001 — a broken rewrite must not break the turn
        logger.exception("lingo_rewrite_failed")
        return None


def enforce(text: str, chip_labels: list[str] | None = None) -> GuardResult:
    """Guarantee one reply + its chip labels are lexicon-clean.

    Clean input returns unchanged at regex cost. A violation triggers one LLM
    rewrite; if that fails or is still dirty, the naive word-map runs. The
    returned GuardResult.audit_dict() is the real guardrail verdict for
    lana_audit_log.
    """
    chips = [str(c or "") for c in (chip_labels or [])]
    hits = find_violations(text)
    for label in chips:
        hits += [h for h in find_violations(label) if h not in hits]
    if not hits:
        return GuardResult(text=text, chip_labels=chips, ok=True)

    rewritten = _rewrite_clean(text, chips, hits)
    if rewritten is not None:
        new_text, new_chips = rewritten
        if not find_violations(new_text) and not any(
            find_violations(c) for c in new_chips
        ):
            return GuardResult(
                text=new_text, chip_labels=new_chips, ok=False, hits=hits, rewritten=True
            )

    return GuardResult(
        text=naive_clean(text),
        chip_labels=[naive_clean(c) for c in chips],
        ok=False,
        hits=hits,
        rewritten=False,
        naive_fallback=True,
    )
