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
