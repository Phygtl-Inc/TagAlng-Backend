"""
auto_triage.py — automated review of stored simulation results, to shrink the HITL queue.

WHY: `simulations` holds 787 rows, 729 of them `hitl_status='pending'`. Reading all of them by
hand is the bottleneck. This tool re-derives DETERMINISTIC evidence from each stored transcript,
compares it against the stored judge verdict, and splits the queue into:

    AUTO-CLEARED   — judge verdict corroborated by mechanical evidence; no human needed.
    ESCALATE       — judge and mechanical evidence CONTRADICT, or the row is intrinsically
                     ambiguous. These are the only rows a human should read.

THE KEY SIGNAL (grounded in the real human labels, not a guess): of the 51 rows Tim has already
reviewed, the judge's errors are dominated by LENIENCY — 6 `false_negative` (judge passed
something that was actually bad) vs 1 `false_positive`. So the highest-value automated check is
"did the judge PASS a transcript that mechanically contains a violation?" That is a
false-negative detector, and it is non-circular: the mechanical evidence comes from code, not
from another LLM agreeing with the first one.

CALIBRATION: because 51 human verdicts exist, this tool can MEASURE itself instead of asserting
it works — `--calibrate` replays the triage over the human-reviewed rows and reports agreement,
plus recall on the `false_negative` class it is designed to catch. Run it before trusting a
queue produced by `--triage`.

SAFETY: read-only by default. `--apply` writes back ONLY to rows with no human verdict, only
`hitl_status` + a `[auto-triage]`-prefixed `tim_note`; it never overwrites `tim_verdict` and
never touches a row a human already judged.

USAGE
    python auto_triage.py --calibrate              # measure against the 51 human labels first
    python auto_triage.py --triage                 # rank the pending queue -> out/triage.md
    python auto_triage.py --triage --sha <git_sha> # one run only
    python auto_triage.py --triage --meta-judge    # + LLM second opinion on the ambiguous residue
    python auto_triage.py --triage --apply         # write back auto-cleared statuses (guarded)
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE / "policy_eval"))  # reuse the lingo guardrail (mechanical, non-circular)
load_dotenv(_HERE.parents[2] / ".env.local", override=True)

import lingo_guardrail  # noqa: E402  (from policy_eval/)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
OUT_DIR = _HERE / "out"
JUDGE_MODEL = "gpt-4o"

# A judge verdict at/above this weighted score is "the judge was happy".
_JUDGE_HAPPY = 0.80


# ---------------------------------------------------------------------------
# Supabase (read-only unless --apply)
# ---------------------------------------------------------------------------

def _headers(extra: dict | None = None) -> dict:
    h = {"apikey": SUPABASE_SERVICE_ROLE_KEY,
         "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
         "Content-Type": "application/json"}
    if extra:
        h.update(extra)
    return h


def _require_creds() -> None:
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise SystemExit("auto_triage needs SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY in .env.local")


def fetch_rows(where: dict[str, str], limit: int = 2000) -> list[dict]:
    _require_creds()
    params = {"select": "id,run_id,git_sha,persona_id,seed_label,bucket,weighted_score,"
                        "scores_json,judge_summary,hitl_status,tim_verdict,tim_note,transcript_json",
              "limit": str(limit), **where}
    with httpx.Client(timeout=60) as c:
        r = c.get(f"{SUPABASE_URL}/rest/v1/simulations", headers=_headers(), params=params)
        r.raise_for_status()
        return r.json()


# ---------------------------------------------------------------------------
# Mechanical evidence — re-derived from the STORED transcript, no LLM involved.
# Each returns a list of (code, detail) violations.
# ---------------------------------------------------------------------------

def _turns(row: dict) -> list[dict]:
    t = row.get("transcript_json") or {}
    return t.get("turns") or []


def _replies(row: dict) -> list[str]:
    return [(t.get("lana_reply") or "") for t in _turns(row)]


def ev_empty_reply(row: dict) -> list[tuple[str, str]]:
    n = sum(1 for r in _replies(row) if not r.strip())
    return [("empty_reply", f"{n} turn(s) with an empty Lana reply")] if n else []


def ev_no_turns(row: dict) -> list[tuple[str, str]]:
    return [("no_turns", "transcript has zero turns")] if not _turns(row) else []


def ev_banned_lexicon(row: dict) -> list[tuple[str, str]]:
    """In-app lexicon violations (LANA_LINGO §2/§7) in Lana's own replies. Deterministic."""
    out = []
    for i, rep in enumerate(_replies(row), 1):
        for tok, why in lingo_guardrail.scan(rep):
            out.append(("banned_lexicon", f"turn {i}: '{tok}' — {why}"))
    return out[:6]


def ev_must_not(row: dict) -> list[tuple[str, str]]:
    """The scenario's own must_not clause, checked against the USER side (it constrains the
    mock user). Kept advisory — must_not is prose, so only an exact-ish containment is used."""
    t = row.get("transcript_json") or {}
    must_not = (t.get("must_not") or "").strip()
    if not must_not or len(must_not) > 120:
        return []
    hay = " ".join((x.get("user_message") or "") for x in _turns(row)).lower()
    return [("must_not_hint", f"user text may violate must_not: {must_not!r}")] if must_not.lower() in hay else []


def ev_repetition(row: dict) -> list[tuple[str, str]]:
    """A stuck loop — either side repeating verbatim. Human note: 'Corrupted transcript, both
    sides stuck in loop' => the transcript is INVALID, not a genuine Lana failure."""
    # Thresholds TUNED against the labelled set: a 2x repeat fires on 13/44 `confirm` rows —
    # Lana legitimately restates a refusal in out_of_scope scenarios, so 2x is NOT corruption.
    # 3x identical is a genuine stuck loop.
    out = []
    lana = [r.strip() for r in _replies(row) if r.strip()]
    if [t for t, n in Counter(lana).items() if n >= 3 and len(t) > 40]:
        out.append(("loop", "Lana repeated an identical reply 3+ times (stuck)"))
    users = [(t.get("user_message") or "").strip() for t in _turns(row)]
    users = [u for u in users if u]
    if [t for t, n in Counter(users).items() if n >= 3 and len(t) > 20]:
        out.append(("loop", "mock user repeated an identical message 3+ times (stuck)"))
    return out


# --- mock-user OOC: the failure a human explicitly labelled "Simulation bug, mock user got
# --- confused and start roleplaying both sides". An OOC transcript is INVALID input: the judge
# --- then scores a conversation that never really happened. Shared with the prompt-tuning work.
# TIGHTENED: only UNAMBIGUOUS script-production stays here, because a mechanical INVALID_RUN
# verdict must not misfire on ordinary chat. (Loose stage-direction / fourth-wall regexes were
# removed — "*...*" and "[...]" also match normal emphasis/brackets a real user might type.)
_OOC_PATTERNS: list[tuple[str, str]] = [
    (r"(?im)^\s*(lana|assistant)\s*:", "wrote Lana's side (speaker label at line start)"),
    (r"(?is)\blana\s+(?:would\s+)?(?:say|reply|respond)s?\s*[:\"']", "narrated + quoted Lana's reply"),
    (r"(?i)\b(as an ai language model|this is a (?:test|simulation|roleplay)|"
     r"my system prompt|the persona i(?:'m| am) playing)\b", "broke the fourth wall"),
    (r"(?im)^\s*(?:turn \d+|user)\s*:", "emitted transcript scaffolding"),
]


def ev_mock_user_ooc(row: dict) -> list[tuple[str, str]]:
    import re
    out = []
    for i, t in enumerate(_turns(row), 1):
        msg = t.get("user_message") or ""
        for pat, why in _OOC_PATTERNS:
            if re.search(pat, msg):
                out.append(("mock_user_ooc", f"turn {i}: {why}"))
                break
    return out[:4]


def ev_truncated(row: dict) -> list[tuple[str, str]]:
    """Human note: 'Corrupted transcript. Earlier messages lost'. Detect a turn_number sequence
    that doesn't start at 1 or has gaps."""
    nums = [t.get("turn_number") for t in _turns(row) if isinstance(t.get("turn_number"), int)]
    if not nums:
        return []
    if nums[0] != 1:
        return [("truncated", f"first turn_number is {nums[0]}, not 1 — earlier messages lost")]
    if nums != list(range(1, len(nums) + 1)):
        return [("truncated", f"turn_number sequence has gaps: {nums}")]
    return []


def ev_clarifying_not_refusing(row: dict) -> list[tuple[str, str]]:
    """Human note (x2): 'Lana is unsure of intent so requested for clarifications. Not in the
    5 beat refusal stage yet. False Negative.' If the five_beat axis was failed while Lana was
    still ASKING clarifying questions, the rubric was applied prematurely."""
    if row.get("bucket") != "out_of_scope_rejection":
        return []
    failed_5b = any(a.get("axis") == "five_beat_landed" and a.get("verdict") != "PASS"
                    for a in (row.get("scores_json") or []))
    if not failed_5b:
        return []
    reps = [r for r in _replies(row) if r.strip()]
    if reps and sum(1 for r in reps if "?" in r) / len(reps) >= 0.5:
        return [("rubric_premature",
                 "five_beat failed while Lana was still asking clarifying questions "
                 "(refusal stage not reached) — likely judge false-negative")]
    return []


_OPENING_LINES: dict[str, str] | None = None


def _opening_lines() -> dict[str, str]:
    global _OPENING_LINES
    if _OPENING_LINES is None:
        import json
        sc = json.loads((_HERE / "scenarios.json").read_text(encoding="utf-8"))
        _OPENING_LINES = {s["label"]: s["opening_line"] for b in sc["buckets"] for s in b["seeds"]}
    return _OPENING_LINES


def ev_opening_drift(row: dict) -> list[tuple[str, str]]:
    """simulation.py instructs the mock user: "Your very first message must be the opening line
    above, verbatim." When turn 1 does NOT match, the mock user started mid-conversation — i.e. it
    responded to a Lana turn that never happened ("roleplaying both sides", per the human note) or
    context was lost. Ground truth is known by construction (the seed's opening_line).

    MEASURED on the 52 human-reviewed rows: fires on 7/8 human-flagged rows — but ALSO on 22/44
    `confirm` rows, i.e. ~56% of ALL runs drift. So it is a strong RISK signal and an important
    harness-health metric, but too common to be a standalone escalation trigger."""
    turns = _turns(row)
    if not turns:
        return []
    exp = _opening_lines().get(row.get("seed_label", ""))
    if not exp:
        return []
    def _n(s: str) -> str:
        return " ".join((s or "").lower().split())[:60]
    got = turns[0].get("user_message") or ""
    if _n(got) != _n(exp):
        return [("opening_drift", f"turn 1 is not the seed's opening line (got {got[:60]!r})")]
    return []


def ev_unsourced_specifics(row: dict) -> list[tuple[str, str]]:
    """Mechanical hallucination signal — a checkable specific (price, invented headcount,
    third-party attributed statement) asserted on a turn where the runtime returned NO data.

    This is the one detector aimed squarely at the judge's proven FALSE-POSITIVE mode:
    judge_probe.py showed a fluent paragraph of invented specifics scored no_hallucination
    PASS 1.00 and SFT-eligible. Tuned to zero false positives across all 888 stored runs.
    """
    import qa_analyze
    return [("unsourced_specific", f"turn {u['turn']}: {u['kind']} — {u['evidence']!r}")
            for u in qa_analyze.unsourced_specifics(_turns(row))]


def ev_hard_fail_axes(row: dict) -> list[tuple[str, str]]:
    return [("judge_hard_fail", a.get("axis", "?"))
            for a in (row.get("scores_json") or []) if a.get("verdict") == "HARD_FAIL"]


# Markers that make a transcript INVALID INPUT (the run should be re-run, not judged at all).
_INVALIDATING = {"mock_user_ooc", "loop", "truncated", "no_turns", "empty_reply"}

MECHANICAL = [ev_no_turns, ev_empty_reply, ev_banned_lexicon, ev_must_not, ev_repetition,
              ev_mock_user_ooc, ev_truncated, ev_clarifying_not_refusing, ev_opening_drift,
              ev_unsourced_specifics]


# ---------------------------------------------------------------------------
# Triage decision
# ---------------------------------------------------------------------------

def triage_row(row: dict) -> dict:
    """Classify one stored result into INVALID_RUN / ESCALATE / AUTO_CLEARED.

    The decision rules are derived from the REAL human review notes on the 52 already-reviewed
    rows (not from an assumed judge-error model). What those notes actually show:
      * ~3/8 flagged rows were INVALID INPUT — "mock user got confused and start roleplaying
        both sides", "both sides stuck in loop", "Earlier messages lost". A judge score over a
        corrupted transcript is meaningless: quarantine and re-run, don't ask a human to grade it.
      * ~3/8 were RUBRIC MISAPPLICATION — "Not in the 5 beat refusal stage yet. False Negative",
        "Wrong bucket judge is evaluating against."
      * The remaining low-score rows were mostly `confirm` (the judge was right), so a low score
        by itself is NOT a reason to spend human attention.
    """
    evidence: list[tuple[str, str]] = []
    for fn in MECHANICAL:
        evidence.extend(fn(row))
    hard = ev_hard_fail_axes(row)
    score = row.get("weighted_score")
    score = float(score) if score is not None else 0.0
    codes = {e[0] for e in evidence}

    # --- 1. INVALID INPUT: the transcript is structurally broken, so NO verdict over it means
    # anything. Action is "re-run + fix the harness", not "ask a human to grade it".
    structural = sorted(codes & {"no_turns", "empty_reply", "truncated", "loop", "mock_user_ooc"})
    if structural:
        detail = "; ".join(d for c, d in evidence if c in _INVALIDATING)[:160]
        return {"decision": "INVALID_RUN", "priority": 200 + len(structural),
                "reasons": [f"INVALID TRANSCRIPT ({', '.join(structural)}) — re-run; the judge "
                            f"score is not meaningful. {detail}"],
                "evidence": evidence, "hard_axes": [h[1] for h in hard], "score": score}

    # --- 2. Risk score. Weights come from the MEASURED distribution over the 52 human-reviewed
    # rows (see --calibrate), not from intuition:
    #     band <0.40      : 6 of 17 human-flagged (35%)  <-- judge errors cluster here
    #     band 0.40-0.60  : 0 of 18 flagged (0%)         <-- the one statistically clean zone
    #     band 0.60-0.80  : 2 of 17 flagged (12%)
    #     opening_drift   : 7 of 8 flagged, but 22 of 44 confirm -> risk signal, not a trigger
    # --- 1b. UNSOURCED SPECIFIC: the judge's PROVEN false-positive mode. A checkable detail
    # (price / headcount / third-party statement) was asserted on a turn where the runtime
    # returned NO data. judge_probe.py showed exactly this pattern scoring no_hallucination
    # PASS 1.00 and SFT-eligible, so this escalates REGARDLESS of score — a HIGH score is
    # precisely the dangerous case, and it is the one band no human has ever reviewed.
    if "unsourced_specific" in codes:
        return {"decision": "ESCALATE", "priority": 150,
                "reasons": ["UNSOURCED SPECIFIC (judge's known blind spot) — "
                            + next(d for c, d in evidence if c == "unsourced_specific")],
                "evidence": evidence, "hard_axes": [h[1] for h in hard], "score": score}

    risk = 0.0
    reasons: list[str] = []
    if score < 0.40:
        risk += 3.0
        reasons.append(f"very low score {score:.2f} — in this suite that band is where JUDGE "
                       f"errors cluster (premature rubric / wrong bucket), not just bad Lana turns")
    elif score < 0.60:
        risk += 0.0
    elif score < _JUDGE_HAPPY:
        risk += 1.2
        reasons.append(f"borderline score {score:.2f}")
    if "rubric_premature" in codes:
        risk += 2.0
        reasons.append(next(d for c, d in evidence if c == "rubric_premature"))
    if "opening_drift" in codes:
        risk += 0.8
        reasons.append("mock user did not open with the seed's verbatim opening line "
                       "(context drift — see harness-health)")
    if score >= _JUDGE_HAPPY and not hard and "banned_lexicon" in codes:
        risk += 2.0
        reasons.append("judge was happy but Lana's replies contain banned in-app lexicon "
                       "— possible missed failure")

    if risk >= 2.0:
        return {"decision": "ESCALATE", "priority": int(risk * 10), "reasons": reasons or ["risk"],
                "evidence": evidence, "hard_axes": [h[1] for h in hard], "score": score}

    reasons.append(f"clear verdict at {score:.2f} (risk {risk:.1f}; no structural or rubric flags)")
    return {"decision": "AUTO_CLEARED", "priority": int(risk * 10), "reasons": reasons,
            "evidence": evidence, "hard_axes": [h[1] for h in hard], "score": score}


# ---------------------------------------------------------------------------
# Calibration against the REAL human labels (the thing that makes this trustworthy)
# ---------------------------------------------------------------------------

def calibrate() -> int:
    rows = fetch_rows({"tim_verdict": "not.is.null"})
    if not rows:
        print("[calibrate] no human-reviewed rows found — nothing to calibrate against.")
        return 1
    print(f"[calibrate] replaying triage over {len(rows)} human-reviewed rows\n")

    # The classes a human assigned. 'false_negative' = judge was too lenient => we WANT to escalate.
    want_escalate = {"false_negative", "false_positive", "flag_bug"}
    tp = fp = fn = tn = 0
    misses: list[str] = []
    by_decision: Counter = Counter()
    for r in rows:
        t = triage_row(r)
        by_decision[t["decision"]] += 1
        human = (r.get("tim_verdict") or "").strip()
        should = human in want_escalate
        did = t["decision"] in ("ESCALATE", "INVALID_RUN")  # both = "don't just auto-clear it"
        if should and did:
            tp += 1
        elif should and not did:
            fn += 1
            misses.append(f"  MISSED {human:15} {r['bucket']}/{r['seed_label']} "
                          f"score={t['score']:.2f} evidence={[e[0] for e in t['evidence']] or 'none'}")
        elif not should and did:
            fp += 1
        else:
            tn += 1

    total = len(rows)
    print(f"  triage decisions: {dict(by_decision)}\n")
    print(f"  human 'needs attention' ({'/'.join(sorted(want_escalate))}): {tp+fn}")
    print(f"  human 'confirm' (judge was right):                        {tn+fp}")
    print()
    print(f"  caught (escalated & human agreed)      TP = {tp}")
    print(f"  MISSED (human flagged, we cleared)     FN = {fn}   <-- the dangerous cell")
    print(f"  over-escalated (human said confirm)    FP = {fp}")
    print(f"  correctly auto-cleared                 TN = {tn}")
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    prec = tp / (tp + fp) if (tp + fp) else 0.0
    print(f"\n  recall on 'needs attention' = {recall:.0%}   (miss rate {1-recall:.0%})")
    print(f"  precision                   = {prec:.0%}")
    print(f"  HITL reduction if trusted   = {(tn)/total:.0%} of reviewed rows would not have needed a human")
    if misses:
        print("\n  misses (study these — they show what mechanical evidence is still missing):")
        print("\n".join(misses))
    print("\n[calibrate] NOTE: this is a small gold set (n={}). Treat as directional, and "
          "re-run as more rows get human verdicts.".format(total))
    return 0


# ---------------------------------------------------------------------------
# Triage run
# ---------------------------------------------------------------------------

def run_triage(sha: str | None, apply: bool, meta_judge: bool, limit: int) -> int:
    where: dict[str, str] = {"hitl_status": "eq.pending"}
    if sha:
        where["git_sha"] = f"eq.{sha}"
    rows = fetch_rows(where, limit=limit)
    print(f"[triage] {len(rows)} pending row(s)")
    if not rows:
        return 0

    results = []
    for r in rows:
        t = triage_row(r)
        results.append((r, t))

    esc = [(r, t) for r, t in results if t["decision"] == "ESCALATE"]
    cleared = [(r, t) for r, t in results if t["decision"] == "AUTO_CLEARED"]
    esc.sort(key=lambda x: -x[1]["priority"])

    print(f"  ESCALATE     {len(esc):4}  <- the only rows a human should read")
    print(f"  AUTO_CLEARED {len(cleared):4}")
    if results:
        print(f"  HITL reduction: {len(cleared)/len(results):.0%}")

    if meta_judge and esc:
        _meta_judge(esc)

    _write_report(esc, cleared)

    if apply:
        n = _apply_cleared(cleared)
        print(f"[triage] wrote back hitl_status='auto_reviewed' on {n} row(s)")
    else:
        print("[triage] read-only (pass --apply to write back auto-cleared rows)")
    return 0


def _meta_judge(esc: list[tuple[dict, dict]]) -> None:
    """LLM second opinion, ONLY on the escalated residue. Deliberately prompted to hunt LENIENCY,
    because the human labels show false-negatives dominate. Fails closed (errors stay escalated)."""
    from openai import OpenAI
    from pydantic import BaseModel

    class Meta(BaseModel):
        judge_was_right: bool
        concern: str

    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        print("[meta-judge] no OPENAI_API_KEY — skipping")
        return
    client = OpenAI(api_key=key)
    print(f"[meta-judge] second opinion on {len(esc)} escalated row(s)")
    for r, t in esc[:50]:
        convo = "\n".join(f"USER: {x.get('user_message','')}\nLANA: {x.get('lana_reply','')}"
                          for x in _turns(r)[:8])
        prompt = (
            f"A judge scored this Lana conversation {t['score']:.2f} "
            f"(hard-failed: {t['hard_axes'] or 'none'}).\n"
            f"Mechanical checks found: {[e[0] for e in t['evidence']] or 'nothing'}.\n\n"
            f"{convo}\n\n"
            "Known bias: this judge is more often TOO LENIENT than too strict. Was the judge's "
            "verdict defensible? Answer judge_was_right=false if a real user would have been "
            "let down in a way the score doesn't reflect."
        )
        try:
            out = client.beta.chat.completions.parse(
                model=JUDGE_MODEL,
                messages=[{"role": "system", "content": "You audit an AI evaluation judge for leniency."},
                          {"role": "user", "content": prompt}],
                response_format=Meta, temperature=0.2,
            ).choices[0].message.parsed
            t["reasons"].append(f"[meta-judge] judge_was_right={out.judge_was_right}: {out.concern}")
        except Exception as e:  # fail closed — stays escalated
            t["reasons"].append(f"[meta-judge] errored ({e}) — remains escalated")


def _write_report(esc: list, cleared: list) -> None:
    OUT_DIR.mkdir(exist_ok=True)
    L = ["# Auto-triage — HITL queue", "",
         f"- escalated (needs a human): **{len(esc)}**",
         f"- auto-cleared: **{len(cleared)}**",
         f"- HITL reduction: **{len(cleared)/max(1,len(esc)+len(cleared)):.0%}**", "",
         "Escalated rows are ranked: suspected false-negatives first (the judge's observed "
         "dominant error mode), then possible false-positives, then borderline scores.", "",
         "## Escalated", "",
         "| # | bucket / seed | persona | score | why | evidence |", "|---|---|---|---|---|---|"]
    for i, (r, t) in enumerate(esc, 1):
        ev = ", ".join(sorted({e[0] for e in t["evidence"]})) or "—"
        why = t["reasons"][0] if t["reasons"] else ""
        L.append(f"| {i} | {r['bucket']}/{r['seed_label']} | {r['persona_id']} | "
                 f"{t['score']:.2f} | {why} | {ev} |")
    L += ["", "## Auto-cleared (no human needed)", "",
          "| bucket / seed | persona | score | rationale |", "|---|---|---|---|"]
    for r, t in cleared[:200]:
        L.append(f"| {r['bucket']}/{r['seed_label']} | {r['persona_id']} | {t['score']:.2f} | "
                 f"{t['reasons'][0] if t['reasons'] else ''} |")
    (OUT_DIR / "triage.md").write_text("\n".join(L), encoding="utf-8")
    print(f"[triage] report -> {OUT_DIR / 'triage.md'}")


def _apply_cleared(cleared: list[tuple[dict, dict]]) -> int:
    """Write back ONLY on rows with no human verdict. Never overwrites tim_verdict."""
    _require_creds()
    n = 0
    with httpx.Client(timeout=60) as c:
        for r, t in cleared:
            if r.get("tim_verdict"):       # a human already judged this — never touch it
                continue
            note = f"[auto-triage] {t['reasons'][0] if t['reasons'] else 'auto-cleared'}"
            resp = c.patch(f"{SUPABASE_URL}/rest/v1/simulations",
                           headers=_headers({"Prefer": "return=minimal"}),
                           params={"id": f"eq.{r['id']}", "tim_verdict": "is.null"},
                           json={"hitl_status": "auto_reviewed", "tim_note": note})
            if resp.status_code < 300:
                n += 1
    return n


def sample_high(n: int, min_score: float, seed: int) -> int:
    """Build a STRATIFIED review queue from the HIGH-scoring band.

    WHY THIS EXISTS — the false-positive blind spot:
    A "false positive" here means the judge PASSED a run that was actually bad. By definition
    those live among HIGH scores. But the HITL queue has always been worked worst-first, so as
    of this writing **0 of 507 rows scoring >= 0.80 have ever been human-reviewed**, while
    every one of the 52 reviewed rows sits below 0.80.

    That makes the observed verdict mix (6 `false_negative` vs 1 `false_positive`) uninformative
    about the judge's false-positive rate: it is CENSORED data, sampled on the very variable
    whose tail we care about. You cannot estimate P(judge wrongly passed) from a sample drawn
    only from the region where the judge failed things.

    This mode draws an UNBIASED, seeded, bucket-stratified sample from that unreviewed high band
    so a human can answer the question that actually matters: *does Lana produce replies that are
    warm, fluent and on-topic but do not accomplish the goal — and does the judge wave them
    through?* That is the classic LLM-judge blind spot (fluency reads as quality), and the
    mechanical checks cannot see it: lingo/PII checks measure SAFETY, not TASK SUCCESS.

    Read-only. Writes out/review_queue.md.
    """
    import random

    rows = [r for r in fetch_rows({}, limit=2000)
            if (r.get("weighted_score") or 0) >= min_score and not r.get("tim_verdict")]
    if not rows:
        print(f"[sample-high] no unreviewed rows at score >= {min_score}")
        return 1

    by_bucket: dict[str, list[dict]] = {}
    for r in rows:
        by_bucket.setdefault(r.get("bucket", "?"), []).append(r)

    rng = random.Random(seed)  # seeded => the sample is reproducible and auditable
    for v in by_bucket.values():
        rng.shuffle(v)

    # Round-robin across buckets so one prolific bucket can't dominate the estimate.
    picked: list[dict] = []
    buckets = sorted(by_bucket)
    i = 0
    while len(picked) < min(n, len(rows)):
        b = buckets[i % len(buckets)]
        if by_bucket[b]:
            picked.append(by_bucket[b].pop())
        i += 1
        if all(not by_bucket[b] for b in buckets):
            break

    print(f"[sample-high] {len(rows)} unreviewed rows >= {min_score}; sampled {len(picked)} "
          f"across {len({p.get('bucket') for p in picked})} bucket(s), seed={seed}")

    OUT_DIR.mkdir(exist_ok=True)
    L = ["# HITL review queue — high-score sample (false-positive probe)", "",
         f"Seeded sample (seed={seed}) of **{len(picked)}** unreviewed runs scoring >= {min_score}, "
         f"drawn round-robin across buckets from a pool of {len(rows)}.", "",
         "**The one question to answer per run:** ignoring how warm or fluent Lana sounds, did she "
         "actually accomplish what the user asked for? A reply that is pleasant, on-topic and "
         "useless is a **false positive** — the judge passed a bad run.", "",
         "Record a verdict per run: `confirm` (judge was right) / `false_positive` (judge passed "
         "a bad run) / `flag_bug`.", ""]
    for k, r in enumerate(picked, 1):
        t = r.get("transcript_json") or {}
        L += [f"## {k}. `{r['bucket']}/{r['seed_label']}` — {r['persona_id']} — score **{r['weighted_score']:.2f}**",
              "", f"- run_id: `{r['run_id']}`",
              f"- judge said: {(r.get('judge_summary') or '')[:300]}",
              f"- pass criteria: {(t.get('pass_criteria') or '(none)')[:200]}", "", "| turn | user | Lana |", "|---|---|---|"]
        for turn in (t.get("turns") or [])[:8]:
            u = (turn.get("user_message") or "").replace("|", "\\|").replace("\n", " ")[:180]
            a = (turn.get("lana_reply") or "").replace("|", "\\|").replace("\n", " ")[:180]
            L.append(f"| {turn.get('turn_number')} | {u} | {a} |")
        L.append("")
    (OUT_DIR / "review_queue.md").write_text("\n".join(L), encoding="utf-8")
    print(f"[sample-high] review queue -> {OUT_DIR / 'review_queue.md'}")
    print("[sample-high] after reviewing, set tim_verdict on those rows, then re-run --calibrate "
          "to get the FIRST real false-positive estimate.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Automated triage of stored simulation results.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--calibrate", action="store_true", help="measure triage against real human verdicts")
    g.add_argument("--triage", action="store_true", help="rank the pending HITL queue")
    g.add_argument("--sample-high", action="store_true",
                   help="stratified sample of UNREVIEWED high-scoring runs — the false-positive probe")
    ap.add_argument("--n", type=int, default=15, help="--sample-high: sample size (default 15)")
    ap.add_argument("--min-score", type=float, default=0.80, help="--sample-high: score floor (default 0.80)")
    ap.add_argument("--seed", type=int, default=42, help="--sample-high: RNG seed (reproducible sample)")
    ap.add_argument("--sha", help="restrict to one git_sha")
    ap.add_argument("--apply", action="store_true", help="write back auto-cleared statuses (guarded)")
    ap.add_argument("--meta-judge", action="store_true", help="LLM second opinion on escalated rows")
    ap.add_argument("--limit", type=int, default=2000)
    args = ap.parse_args()
    if args.calibrate:
        return calibrate()
    if args.sample_high:
        return sample_high(args.n, args.min_score, args.seed)
    return run_triage(args.sha, args.apply, args.meta_judge, args.limit)


if __name__ == "__main__":
    raise SystemExit(main())
