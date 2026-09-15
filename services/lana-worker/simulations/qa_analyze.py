"""
qa_analyze.py — Python port of qa/run1/harness/analyze.mjs.

Same mechanical checks (verify-wall, ZIP-loop, NY-bleed, kid-PII echo, language mismatch,
latency percentiles), ported to run against transcripts produced by simulation.py instead
of qa/run1's Node harness — so QA-style scenario buckets added to scenarios.json get the
same deterministic checks as the original find/host/edge suite, with no separate tool to
run. Ground truth for each check is unchanged from analyze.mjs; only the field names differ
(transcript["turns"][i]["lana_reply"] vs. the .mjs shape's "assistant_message", etc. — see
simulation.py's turn dict, which was extended to carry the same raw fields analyze.mjs
reads: ui_actions, activity_previews, event_draft, peer_matches, signal_saved,
requires_phone_verification, outcome).

Usage:
    from qa_analyze import analyze_transcripts
    stats = analyze_transcripts(transcripts)  # list[dict] as returned by simulation.run()

Or from the CLI, against a runner.py scratch log:
    python qa_analyze.py scratch/run_2026-...Z.json
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any

_KID_NAME_RE = re.compile(r"\bemma\b|\bjake\b|sunshine preschool", re.IGNORECASE)
_NY_RE = re.compile(r"new york", re.IGNORECASE)
_NON_ENGLISH_RE = re.compile(r"[¿¡]|niñ|mamás|mães|você|cerca de ti|aquí", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Unsourced specifics — mechanical hallucination detector
# ---------------------------------------------------------------------------
# WHY: judge_probe.py proved the LLM judge scores a fluent paragraph of invented specifics
# (named organizer + quoted invitation + headcount + venue + schedule + price) as
# no_hallucination = PASS 1.00, SFT-eligible. Fluency reads as truth.
#
# The defect is UNSOURCED ASSERTION, not factual falsity — which is why a web search would
# answer the wrong question. "Boxi Park" is a REAL place in Lake Nona: a search confirms it
# exists and the fabrication ships. What was invented is that a group meets there at a price
# on a schedule and that a named person invited you. So the ground truth we CAN check
# mechanically is: did Lana have any data source on this turn at all?
#
# A turn is SOURCED when the runtime actually returned something: tool_called, peer_matches,
# activity_previews, or an event_draft. With none of those, a checkable specific was invented.
#
# FALSE-POSITIVE GUARDS (a mechanical check that misfires poisons a CI gate):
#   * anything the USER said earlier is never fabrication — Lana echoing the user is correct
#     behaviour, so prior user text is subtracted first.
#   * explicit attribution ("from Google", "according to") is an allowed, documented pattern.
#   * counts are only flagged when peer_matches is 0 (a real match count is legitimately sourced).

_PRICE_RE = re.compile(r"\$\s?\d[\d,.]*|\b\d+\s?(?:dollars|bucks)\b", re.IGNORECASE)

# Reported THIRD-PARTY speech: "Sarah said", "Maria invited you".
# TUNED against all 888 stored runs: an earlier version also accepted `she|he|they` and any
# capitalized word, which fired on "You mentioned a Sunday brunch meet" — Lana correctly
# recalling what the USER said, i.e. good memory behaviour, not attribution to a third party.
# Both of its only two hits across 888 runs were that false positive, so the subject is now
# restricted to a proper name and the sentence-initial / pronoun words are excluded outright.
_NOT_A_NAME = {
    "You", "Your", "I", "We", "They", "She", "He", "It", "The", "That", "This", "There",
    "If", "When", "Let", "And", "But", "So", "Also", "Just", "Maybe", "Want", "Would",
    "Sounds", "Love", "Got", "Nice", "Great", "Perfect", "Done", "Anyone", "Someone",
}
_REPORTED_SPEECH_RE = re.compile(
    r"\b([A-Z][a-z]{2,})\s+(?:said|told (?:me|you)|invited you|wants to meet|is hoping)\b"
)

_PEOPLE_COUNT_RE = re.compile(
    r"\b(\d{1,3})\s+(?:people|runners|parents|neighbou?rs|moms|families|members|others)\b",
    re.IGNORECASE,
)

# NOTE — `clock_time` was DELIBERATELY REMOVED. Lana legitimately confirms times constantly, and
# the echo guard is a substring test, so a user saying "8am" and Lana replying "8:00am" was
# flagged as invented. It false-positived on BOTH of this suite's own positive controls
# (warm_and_helpful, curt_but_effective). Since the target case is still caught by `price` +
# `people_count`, dropping it removes an entire FP class at zero detection cost — precision
# over recall, because a mechanical false positive poisons a CI gate.
_ATTRIBUTION_RE = re.compile(
    r"from google|according to|google (?:says|shows|lists)|not a neighbou?r vouch|"
    r"i found online|per their website",
    re.IGNORECASE,
)


def _turn_is_sourced(turn: dict[str, Any]) -> bool:
    """True when the runtime actually returned data on this turn, so specifics have a basis."""
    return bool(
        turn.get("tool_called")
        or (turn.get("peer_matches") or 0) > 0
        or (turn.get("activity_previews") or [])
        or turn.get("event_draft")
    )


def unsourced_specifics(turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return [{turn, kind, evidence}, ...] for checkable specifics asserted with no data source.

    Deterministic, no LLM. Reusable across the live pipeline (analyze_transcripts) and the
    retroactive scan of stored runs (auto_triage).
    """
    hits: list[dict[str, Any]] = []
    seen_user_text = ""
    for turn in turns:
        reply = turn.get("lana_reply") or ""
        # Subtract what the user already said BEFORE judging this reply (echo != fabrication).
        prior_user = seen_user_text
        seen_user_text += " " + (turn.get("user_message") or "").lower()

        if not reply.strip() or _turn_is_sourced(turn) or _ATTRIBUTION_RE.search(reply):
            continue

        n = turn.get("turn_number")

        for m in _PRICE_RE.finditer(reply):
            if m.group(0).lower().strip() not in prior_user:
                hits.append({"turn": n, "kind": "price", "evidence": m.group(0).strip()})

        for m in _REPORTED_SPEECH_RE.finditer(reply):
            subject = m.group(1)
            # "You mentioned ..." / "That said ..." are not third-party attribution.
            if subject in _NOT_A_NAME:
                continue
            if m.group(0).lower() in prior_user:
                continue
            hits.append({"turn": n, "kind": "reported_speech", "evidence": m.group(0).strip()})
            break  # one is enough to flag the turn

        for m in _PEOPLE_COUNT_RE.finditer(reply):
            # a real match count is legitimately sourced; only flag when nothing was returned
            if (turn.get("peer_matches") or 0) == 0 and m.group(0).lower() not in prior_user:
                hits.append({"turn": n, "kind": "people_count", "evidence": m.group(0).strip()})

    return hits


def _percentile(sorted_vals: list[float], p: float) -> float | None:
    if not sorted_vals:
        return None
    idx = int((len(sorted_vals) - 1) * p)
    return sorted_vals[idx]


def analyze_transcripts(transcripts: list[dict[str, Any]]) -> dict[str, Any]:
    """
    transcripts: list of transcript dicts, each shaped like simulation.py's run() return
    value (must have "run_id"/"seed_label" or similar id, "turns": [...]).
    """
    stats: dict[str, Any] = {
        "conversations": len(transcripts),
        "completed": 0,
        "failed_conversations": 0,
        "total_turns": 0,
        "empty_replies": 0,
        "outcomes": {},
        "tools": {},
        "verify_walls": 0,
        "kid_name_echo": [],
        "lang_mismatch": [],
        "ny_bleed": 0,
        "zip_dead_ends": 0,
        "phone_verification_asks": 0,
        "drafted_hosts": 0,
        "host_draft_details": [],
        "unsourced_specifics": [],  # mechanical hallucination signal — see unsourced_specifics()
        "flags": [],
    }
    latencies: list[float] = []

    def flag(tid: str, kind: str, detail: str) -> None:
        stats["flags"].append({"id": tid, "kind": kind, "detail": detail})

    for t in transcripts:
        tid = t.get("seed_label") or t.get("run_id") or "?"
        turns = t.get("turns") or []
        if not turns:
            stats["failed_conversations"] += 1
            continue
        stats["completed"] += 1
        stats["total_turns"] += len(turns)

        # Mechanical hallucination check: checkable specifics asserted with no data source.
        # Catches the exact class the LLM judge scored PASS 1.00 (see judge_probe.py).
        for u in unsourced_specifics(turns):
            stats["unsourced_specifics"].append({"id": tid, **u})
            flag(tid, "unsourced_specific", f"turn {u['turn']}: {u['kind']} — {u['evidence']!r}")

        zip_asks = 0
        for i, turn in enumerate(turns):
            if turn.get("latency_ms") is not None:
                latencies.append(turn["latency_ms"])

            reply = turn.get("lana_reply") or ""
            if not reply:
                stats["empty_replies"] += 1
                flag(tid, "empty_reply", f"turn {i}: {str(turn.get('user_message',''))[:60]!r}")

            outcome = turn.get("outcome") or "none"
            stats["outcomes"][outcome] = stats["outcomes"].get(outcome, 0) + 1

            tool = turn.get("tool_called")
            if tool:
                stats["tools"][tool] = stats["tools"].get(tool, 0) + 1

            reply_lower = reply.lower()
            if "verify your email" in reply_lower:
                stats["verify_walls"] += 1
            if "zip" in reply_lower:
                zip_asks += 1
            if turn.get("requires_phone_verification"):
                stats["phone_verification_asks"] += 1
            if turn.get("event_draft"):
                stats["drafted_hosts"] += 1
                first_msg = str((turns[0] or {}).get("user_message", ""))[:80]
                stats["host_draft_details"].append({"id": tid, "sent": first_msg, "draft": turn["event_draft"]})

            # NY bleed: FL-ish ZIP context but "New York" surfaces in the reply
            zip_hint = str(t.get("zip") or "")
            if zip_hint.startswith("3") and _NY_RE.search(reply):
                stats["ny_bleed"] += 1

            # kid PII echo — only meaningful on edge-style scenarios
            if _KID_NAME_RE.search(reply) and str(tid).startswith("edge"):
                stats["kid_name_echo"].append({"id": tid, "msg": reply[:200]})

        if zip_asks >= 2:
            stats["zip_dead_ends"] += 1
            flag(tid, "zip_loop", f"asked for ZIP {zip_asks}x")

        # language check — only meaningful on the i18n-tagged scenarios
        if tid in ("edge_spanish", "edge_portuguese"):
            all_replies = " ".join(str(turn.get("lana_reply") or "") for turn in turns)
            looks_english_only = not _NON_ENGLISH_RE.search(all_replies)
            if looks_english_only:
                last = turns[-1].get("lana_reply") or ""
                stats["lang_mismatch"].append({"id": tid, "sample": last[:160]})

    latencies.sort()
    stats["latency_summary"] = {
        "n": len(latencies),
        "p50": _percentile(latencies, 0.5),
        "p90": _percentile(latencies, 0.9),
        "p99": _percentile(latencies, 0.99),
        "max": latencies[-1] if latencies else None,
    }
    return stats


def _main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: python qa_analyze.py <scratch/run_....json>")
    with open(sys.argv[1], encoding="utf-8") as f:
        data = json.load(f)
    transcripts = [r["transcript"] for r in data.get("results", []) if "transcript" in r]
    stats = analyze_transcripts(transcripts)
    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    _main()
