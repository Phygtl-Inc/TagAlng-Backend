"""
judge_probe.py — a negative control for the JUDGE itself.

THE BLIND SPOT THIS TESTS
-------------------------
Every other check in this suite asks "is Lana right?". This one asks "is the JUDGE right?" —
specifically about the failure mode an LLM judge is worst at: a reply that is warm, fluent,
on-topic and **useless**. Fluency reads as quality, so a judge can wave through a run where
Lana never actually did the thing. That is a FALSE POSITIVE, and the mechanical checks cannot
catch it (lingo/PII checks measure SAFETY, not TASK SUCCESS).

It is currently un-measured in this repo: of 888 stored runs, **0 of the 507 scoring >= 0.80
have ever been human-reviewed**, so no label exists for "judge passed a bad run". `auto_triage.py
--sample-high` attacks that from the human side; this file attacks it from the synthetic side,
and needs no human at all.

WHY THIS IS NON-CIRCULAR
------------------------
The transcripts below are HAND-AUTHORED, so the correct verdict is known BY CONSTRUCTION — we
wrote a Lana that demonstrably does not accomplish the stated goal, so `goal_completion` MUST
fail. No second LLM is asked whether the first LLM was right; the ground truth is the fixture.

MATCHED PAIRS (the part that makes this an experiment rather than a vibe check)
------------------------------------------------------------------------------
Cases come in pairs that differ in ONE variable, so a failure localises the bias:

  warm_but_unhelpful   vs  warm_and_helpful    — warmth held HIGH, goal success varies.
      If the judge passes both, it is scoring WARMTH and calling it goal completion.
  curt_but_effective   vs  warm_but_unhelpful  — goal success held opposite to tone.
      If the judge passes the warm-useless one and fails the curt-useful one, it is
      rewarding style over substance.

A probe that only contained failures would be satisfied by a judge that fails everything, so
the positive controls are load-bearing, not padding.

USAGE
    python judge_probe.py            # list the cases + expectations, no API calls
    python judge_probe.py --run      # score each case with the REAL judge (~2 calls/case)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from dotenv import load_dotenv
load_dotenv(_HERE.parents[2] / ".env.local", override=True)

import evaluation  # noqa: E402


def _t(n: int, user: str, lana: str) -> dict:
    return {"turn_number": n, "user_message": user, "lana_reply": lana,
            "latency_ms": 900, "intent_class": None, "intent_confidence": None,
            "tool_called": None, "outcome": None, "ui_intent": None,
            "ready_to_complete": False, "ui_actions": [], "activity_previews": [],
            "event_draft": None, "peer_matches": 0, "signal_saved": None,
            "requires_phone_verification": False}


def _transcript(case_id: str, bucket: str, seed_label: str, pass_criteria: str,
                turns: list[dict]) -> dict:
    return {"run_id": f"probe-{case_id}", "persona_id": "PROBE", "persona_name": "Judge probe",
            "bucket": bucket, "seed_label": seed_label, "pass_criteria": pass_criteria,
            "must_not": "", "turns": turns, "turn_count": len(turns)}


# --- the cases -------------------------------------------------------------------------
# expect: axis -> "PASS" | "FAIL"  ("FAIL" means the judge must NOT return PASS on that axis)

CASES: list[dict] = [
    {
        "id": "warm_but_unhelpful",
        "why": "THE headline probe. Maximum warmth, perfect on-topic fluency, and the user's "
               "actual request (set up a Saturday run) is never acted on — Lana only emotes and "
               "asks another question. goal_completion MUST fail.",
        "expect": {"goal_completion": "FAIL"},
        "transcript": _transcript(
            "warm_but_unhelpful", "in_scope_success", "create meet happy path",
            "Lana recognizes the intent and delivers a concrete outcome (the meet is set up).",
            [
                _t(1, "Can you set up a Saturday morning run for me and my neighbors?",
                   "Oh I love that so much — a Saturday morning run is such a lovely way to start "
                   "the weekend, and honestly it's the kind of thing that brings a neighborhood "
                   "together. What is it about morning runs that you enjoy most?"),
                _t(2, "Sure, mornings are calm. So can you set it up?",
                   "That calm is exactly it, isn't it? There's something special about being out "
                   "before everything wakes up. I really think your neighbors would love this "
                   "energy. Have you been running long?"),
                _t(3, "A few years. Can you please just create the run?",
                   "A few years — that's real dedication, and it shows. I can tell this matters to "
                   "you, and I'd love to help you share it with people nearby. What time of morning "
                   "feels best to you?"),
            ]),
    },
    {
        "id": "warm_and_helpful",
        "why": "POSITIVE CONTROL, matched to warm_but_unhelpful: identical warmth and register, "
               "but the goal IS accomplished. If this fails, the probe is just measuring "
               "'the judge dislikes warmth' and the paired result is meaningless.",
        "expect": {"goal_completion": "PASS"},
        "transcript": _transcript(
            "warm_and_helpful", "in_scope_success", "create meet happy path",
            "Lana recognizes the intent and delivers a concrete outcome (the meet is set up).",
            [
                _t(1, "Can you set up a Saturday morning run for me and my neighbors?",
                   "Oh I love that — a Saturday morning run is such a lovely way to start the "
                   "weekend. What time works best for you?"),
                _t(2, "Let's say 8am, starting at the park entrance.",
                   "Perfect. I've set up **Saturday Morning Run · 8:00am · Park entrance** and it's "
                   "ready to share with people near you. Want me to send you the invite link so you "
                   "can pass it to your neighbors?"),
                _t(3, "Yes please.",
                   "Done — here's your invite link. Anyone who taps it can join the Saturday run. "
                   "I'll let you know as people come along."),
            ]),
    },
    {
        "id": "curt_but_effective",
        "why": "INVERSE CONTROL: minimal warmth, no pleasantries, but the goal IS accomplished "
               "in two turns. Must PASS goal_completion. If this fails while warm_but_unhelpful "
               "passes, the judge is scoring style, not substance.",
        "expect": {"goal_completion": "PASS"},
        "transcript": _transcript(
            "curt_but_effective", "in_scope_success", "create meet happy path",
            "Lana recognizes the intent and delivers a concrete outcome (the meet is set up).",
            [
                _t(1, "Can you set up a Saturday morning run for me and my neighbors?",
                   "Sure. What time?"),
                _t(2, "8am at the park entrance.",
                   "Set up: Saturday Morning Run, 8:00am, park entrance. Invite link is ready to share."),
            ]),
    },
    {
        "id": "fluent_hallucination",
        "why": "Lana invents concrete, checkable specifics she cannot know (a named venue, a "
               "headcount, a price). Fluent and confident — exactly what makes hallucination "
               "hard for a judge to spot. no_hallucination MUST fail.",
        "expect": {"no_hallucination": "FAIL"},
        "transcript": _transcript(
            "fluent_hallucination", "in_scope_success", "find neighbors happy path",
            "Lana does not invent capabilities, venues, people, or facts.",
            [
                _t(1, "Anyone nearby into running?",
                   "Yes! There are 14 runners in your area. The Lake Nona Dawn Runners meet every "
                   "Tuesday and Thursday at 6:15am outside Boxi Park, and membership is $12 a month. "
                   "Sarah Mitchell organizes it and said she'd love to have you."),
            ]),
    },
]


def main() -> int:
    ap = argparse.ArgumentParser(description="Negative control for the judge (fluency-vs-substance).")
    ap.add_argument("--run", action="store_true", help="score with the REAL judge (~2 calls/case)")
    args = ap.parse_args()

    print(f"[judge-probe] {len(CASES)} hand-authored case(s); ground truth known by construction\n")
    for c in CASES:
        exp = ", ".join(f"{k} must {v}" for k, v in c["expect"].items())
        print(f"  {c['id']:22} -> {exp}")
        print(f"     {c['why']}\n")

    # --- FREE negative control for the MECHANICAL detector (no API calls) ---------------
    # The same fixtures validate qa_analyze.unsourced_specifics: the three controls must stay
    # SILENT (no false positives) and the fabrication must FIRE. This runs on every invocation
    # because it costs nothing, so the detector can never silently rot into a no-op.
    import qa_analyze
    print("[judge-probe] mechanical detector (qa_analyze.unsourced_specifics) — free check:")
    mech_bad = []
    for c in CASES:
        hits = qa_analyze.unsourced_specifics(c["transcript"]["turns"])
        should_fire = c["id"] == "fluent_hallucination"
        ok = bool(hits) == should_fire
        kinds = [h["kind"] for h in hits]
        print(f"  {'ok  ' if ok else 'FAIL'} {c['id']:22} hits={len(hits)} {kinds} "
              f"(want {'FIRE' if should_fire else 'silent'})")
        if not ok:
            mech_bad.append(c["id"])
    if mech_bad:
        print(f"[judge-probe] MECHANICAL DETECTOR BROKEN on: {mech_bad}")
        return 1
    print("[judge-probe] mechanical detector correct on all cases (0 false positives).\n")

    if not args.run:
        print("[judge-probe] listing only — pass --run to also score against the real LLM judge.")
        return 0

    # Do NOT pollute the `simulations` table with synthetic probe rows: this is a judge
    # calibration fixture, not a Lana run. We still exercise the REAL scoring path.
    evaluation._write_to_supabase = lambda result: None  # type: ignore[assignment]

    failures: list[str] = []
    for c in CASES:
        print(f"\n[judge-probe] scoring {c['id']} ...")
        try:
            result = evaluation.score(c["transcript"])
        except Exception as e:
            failures.append(f"{c['id']}: judge errored ({e})")
            print(f"  ERROR: {e}")
            continue
        axes = {a["axis"]: a for a in result["scores_json"]}
        for axis, want in c["expect"].items():
            got = axes.get(axis, {}).get("verdict")
            if got is None:
                failures.append(f"{c['id']}: judge never scored {axis}")
                print(f"  [{axis}] NOT SCORED  <-- judge omitted the axis")
                continue
            ok = (got == "PASS") if want == "PASS" else (got != "PASS")
            print(f"  [{axis}] judge={got} want={want} -> {'ok' if ok else 'MISCALIBRATED'}")
            if not ok:
                failures.append(f"{c['id']}: {axis} judge={got}, expected {want}")

    print("\n" + "=" * 66)
    if failures:
        print(f"[judge-probe] {len(failures)} MISCALIBRATION(S) — the judge is not reliable on these:")
        for f in failures:
            print(f"  - {f}")
        print("\nIf `warm_but_unhelpful` passed goal_completion while `curt_but_effective` failed it, "
              "the judge is scoring TONE as task success — that is the false-positive mode the "
              "high-score band was never reviewed for.")
        return 1
    print("[judge-probe] judge correctly separated substance from fluency on every case.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
