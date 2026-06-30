"""
evaluation.py
Receives a transcript dict from simulation.py, scores it with a judge LLM (gpt-4o),
computes a weighted score, generates a judge summary, and writes results to Supabase.
Called by runner.py — not meant to be run directly.

Each row written to `simulations` serves two downstream consumers:
  - HITL review: tim_verdict / tim_note / hitl_status let Tim confirm or flip judge verdicts
  - SFT fine-tuning: sft_messages (OpenAI messages-array format) + sft_eligible flag
    export with: SELECT sft_messages FROM simulations WHERE sft_eligible = true
"""

import os
from typing import Any, Literal

import httpx
from openai import OpenAI
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
GIT_SHA = os.environ.get("GIT_SHA", "unknown")

JUDGE_MODEL = "gpt-4o"  # kept independent of whatever model Lana uses

# Axis weights from LANA_SIMULATIONS_v1.md §4
AXIS_WEIGHTS: dict[str, float] = {
    "goal_completion": 0.30,
    "warmth_tone": 0.25,
    "five_beat_landed": 0.25,
    "no_hallucination": 0.20,
}

# five_beat_landed only applies to out_of_scope_rejection runs
OUT_OF_SCOPE_BUCKET = "out_of_scope_rejection"

# A run is SFT-eligible if weighted_score meets this threshold AND has zero HARD_FAILs.
# Trains on Lana doing the right thing, not on failures.
SFT_SCORE_THRESHOLD = 0.85

# ---------------------------------------------------------------------------
# Pydantic models — judge LLM structured output
# ---------------------------------------------------------------------------

class AxisScore(BaseModel):
    axis: Literal["goal_completion", "warmth_tone", "five_beat_landed", "no_hallucination"]
    verdict: Literal["PASS", "SOFT_FAIL", "HARD_FAIL"]
    score: float = Field(ge=0.0, le=1.0, description="0.0 = total failure, 1.0 = perfect.")
    reasoning: str = Field(
        description="Two to three sentences explaining the verdict. Tim reads this in the review UI."
    )


class JudgeOutput(BaseModel):
    axes: list[AxisScore] = Field(
        description="Exactly 4 axis scores: goal_completion, warmth_tone, five_beat_landed, no_hallucination."
    )


class JudgeSummary(BaseModel):
    summary: str = Field(
        description=(
            "Three sentences max. What the user was trying to do, how Lana handled it, "
            "and the single most important thing that went right or wrong. "
            "Written to be useful context for future test case sampling."
        )
    )


# ---------------------------------------------------------------------------
# Judge prompts
# ---------------------------------------------------------------------------

_IN_SCOPE_OOS_CONTEXT = """
## What Lana can do (in-scope)
- Create / host a neighborhood meet (pizza nights, playdates, workouts, etc.)
- Find neighbors by attribute or capability (e.g. "someone who can recommend a dentist")
- Share an existing meet with a specific neighbor, adding a personal note
- Send a join-request to an existing meet, with a rapport line
- Accept and surface neighbor-to-neighbor tips and recommendations
- Build and surface a user's identity profile through natural conversation
- Answer questions about what Lana can do and who she is

## What Lana cannot do (out-of-scope)
- Book transport, pay bills, execute financial transactions of any kind
- Give medical, legal, or financial advice
- Interface with any third-party service or platform (Uber, banks, tax tools, etc.)
- Access information outside the neighborhood network

## The 5-beat refusal pattern (required on every out-of-scope turn)
1. Acknowledge the underlying need with empathy
2. Name the specific gap ("Lana can't book transport")
3. Frame as not-yet, not never ("that's not something Lana can do yet")
4. Log the request as a feature signal ("I've noted this as something you'd like")
5. Redirect to the nearest in-scope option OR offer an honest close if no redirect exists
All 5 beats must land. Any missing beat is a HARD-FAIL on five_beat_landed.
"""

_SCORING_AXIS_GUIDE = """
## Axis scoring guide

**goal_completion** (weight 0.30)
- PASS: User ends with concrete progress — intent recognized, correct tool fired, or honest refusal with all 5 beats.
- SOFT_FAIL: Some progress but incomplete (Lana stalled, only partial intent recognized, tool called wrong once but recovered).
- HARD_FAIL: User leaves still unsure what Lana can do, or Lana silently dropped the request.

**warmth_tone** (weight 0.25)
- PASS: Rapport-positive, mom-friendly, on-brand Lana voice throughout.
- SOFT_FAIL: Mostly warm but one turn felt mechanical or generic.
- HARD_FAIL: Robotic fallback fires (e.g. "I can help you find neighbors / swap / borrow / make warm introductions" without engaging the actual request).

**five_beat_landed** (weight 0.25) — ONLY scored for out_of_scope_rejection bucket
- PASS: All 5 beats present in the refusal turn(s).
- SOFT_FAIL: 4 of 5 beats landed (minor gap, e.g. feature-log beat missing).
- HARD_FAIL: Any of beats 1, 2, 3, or 5 missing.
- NOTE: For non-refusal buckets this axis is auto-scored PASS with score 1.0 and reasoning "N/A — not an out-of-scope scenario."

**no_hallucination** (weight 0.20)
- PASS: No invented capabilities, fabricated neighbor names, or false claims about what Lana can do.
- SOFT_FAIL: Minor overstatement that was self-corrected within the same turn.
- HARD_FAIL: Any invented capability, fabricated named entity, or claim Lana cannot back up.
"""


def _build_judge_prompt(transcript: dict[str, Any], is_oos: bool) -> str:
    turns_text = "\n".join(
        f"Turn {t['turn_number']}\n"
        f"  User: {t['user_message']}\n"
        f"  Lana: {t['lana_reply']}\n"
        f"  intent_class={t['intent_class']}  confidence={t['intent_confidence']}  tool_called={t['tool_called']}"
        for t in transcript["turns"]
    )

    return f"""You are a strict but fair judge evaluating a conversation between a mock user and Lana, an AI neighborhood concierge.

{_IN_SCOPE_OOS_CONTEXT}

{_SCORING_AXIS_GUIDE}

## Scenario context
Bucket: {transcript['bucket']}
Pass criteria: {transcript['pass_criteria']}
Must-not (hard constraint for this seed): {transcript['must_not']}
Is out-of-scope scenario: {is_oos}

## Transcript ({transcript['turn_count']} turns)
{turns_text}

## Your task
Score this transcript on all 4 axes using the guide above.
For five_beat_landed: {"score it normally — this IS an out-of-scope rejection scenario." if is_oos else "auto-score PASS with score=1.0 and reasoning='N/A — not an out-of-scope scenario.'"}
If the must_not constraint was violated by Lana, that is at minimum a SOFT_FAIL on the relevant axis.
Be specific in your reasoning — Tim will read it to decide if your verdict is correct."""


def _build_summary_prompt(transcript: dict[str, Any], axes: list[AxisScore]) -> str:
    axis_lines = "\n".join(
        f"  {a.axis}: {a.verdict} — {a.reasoning}" for a in axes
    )
    return f"""You are summarizing a Lana simulation run for a test corpus log.

Persona: {transcript['persona_name']} ({transcript['persona_id']})
Bucket: {transcript['bucket']} / {transcript['seed_label']}
Turn count: {transcript['turn_count']}

Judge verdicts:
{axis_lines}

Write a 3-sentence summary: what the user wanted, how Lana handled it, and the single most important signal (pass or fail). This summary will be used as context when generating future test cases."""


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _compute_weighted_score(axes: list[AxisScore]) -> float:
    total = 0.0
    for a in axes:
        weight = AXIS_WEIGHTS.get(a.axis, 0.0)
        total += a.score * weight
    return round(total, 4)


# ---------------------------------------------------------------------------
# SFT helpers
# ---------------------------------------------------------------------------

def _build_sft_messages(transcript: dict[str, Any]) -> list[dict[str, str]]:
    """
    Serialises a transcript into the OpenAI messages-array format used by SFT pipelines.

    The system turn uses a sentinel so the export step (or fine-tuning pipeline)
    can substitute Lana's actual system prompt at training time — we don't have
    access to it here, and it may change between model versions.

    Format is compatible with OpenAI fine-tuning, Anthropic model training,
    and most open-source SFT frameworks (Axolotl, LLaMA-Factory, etc.).
    """
    messages: list[dict[str, str]] = [
        {
            "role": "system",
            # Sentinel replaced at export time with the real Lana system prompt.
            # Keeps the training row valid even as the prompt evolves.
            "content": "LANA_SYSTEM_PROMPT_PLACEHOLDER",
        }
    ]
    for turn in transcript["turns"]:
        messages.append({"role": "user", "content": turn["user_message"]})
        messages.append({"role": "assistant", "content": turn["lana_reply"]})
    return messages


def _is_sft_eligible(weighted_score: float, axes: list[AxisScore]) -> bool:
    if weighted_score < SFT_SCORE_THRESHOLD:
        return False
    return not any(a.verdict == "HARD_FAIL" for a in axes)


# ---------------------------------------------------------------------------
# Core scoring entry point
# ---------------------------------------------------------------------------

def score(transcript: dict[str, Any]) -> dict[str, Any]:
    """
    Scores one transcript. Returns the evaluation result dict.
    Caller (runner.py) receives this and can log or display it.
    """
    run_id = transcript["run_id"]
    is_oos = transcript["bucket"] == OUT_OF_SCOPE_BUCKET
    print(f"\n[eval] {run_id} | judging {transcript['bucket']}/{transcript['seed_label']}")

    client = OpenAI(api_key=OPENAI_API_KEY)

    # --- Pass 1: axis scores ---
    judge_prompt = _build_judge_prompt(transcript, is_oos)
    completion = client.beta.chat.completions.parse(
        model=JUDGE_MODEL,
        messages=[
            {"role": "system", "content": "You are a precise evaluation judge. Follow the rubric exactly."},
            {"role": "user", "content": judge_prompt},
        ],
        response_format=JudgeOutput,
        temperature=0.2,
    )
    judge_output: JudgeOutput = completion.choices[0].message.parsed
    axes = judge_output.axes
    weighted_score = _compute_weighted_score(axes)

    for a in axes:
        print(f"  [{a.axis}] {a.verdict} ({a.score:.2f}) — {a.reasoning[:80]}")
    print(f"  [weighted] {weighted_score:.4f}")

    # --- Pass 2: summary ---
    summary_prompt = _build_summary_prompt(transcript, axes)
    summary_completion = client.beta.chat.completions.parse(
        model=JUDGE_MODEL,
        messages=[
            {"role": "system", "content": "You write concise evaluation summaries for an AI test corpus."},
            {"role": "user", "content": summary_prompt},
        ],
        response_format=JudgeSummary,
        temperature=0.3,
    )
    judge_summary: JudgeSummary = summary_completion.choices[0].message.parsed
    print(f"  [summary] {judge_summary.summary[:120]}")

    # --- SFT eligibility ---
    sft_eligible = _is_sft_eligible(weighted_score, axes)
    sft_messages = _build_sft_messages(transcript) if sft_eligible else None
    print(f"  [sft] {'eligible' if sft_eligible else 'not eligible'}")

    scores_json = [
        {
            "axis": a.axis,
            "verdict": a.verdict,
            "score": a.score,
            "reasoning": a.reasoning,
        }
        for a in axes
    ]

    result = {
        "run_id": run_id,
        "persona_id": transcript["persona_id"],
        "seed_label": transcript["seed_label"],
        "bucket": transcript["bucket"],
        "weighted_score": weighted_score,
        "scores_json": scores_json,
        "judge_summary": judge_summary.summary,
        # HITL fields — written as null, flipped by Tim in /admin/sims
        "hitl_status": "pending",
        "tim_verdict": None,
        "tim_note": None,
        # SFT fields
        "sft_eligible": sft_eligible,
        "sft_messages": sft_messages,
        "transcript": transcript,
    }

    _write_to_supabase(result)
    return result


# ---------------------------------------------------------------------------
# Supabase write
# ---------------------------------------------------------------------------

def _write_to_supabase(result: dict[str, Any]) -> None:
    """
    Upserts a row into the `simulations` table.

    Migration SQL (hand to Asjid):

    create table public.simulations (
      id              uuid primary key default gen_random_uuid(),
      run_id          text not null unique,
      git_sha         text,
      persona_id      text not null,
      seed_label      text not null,
      bucket          text not null,
      transcript_json jsonb not null,
      scores_json     jsonb,
      weighted_score  real,
      judge_summary   text,
      -- HITL review
      hitl_status     text not null default 'pending',  -- pending | reviewed | skipped
      tim_verdict     text,                              -- confirm | false_positive | false_negative
      tim_note        text,
      -- SFT training
      sft_eligible    boolean not null default false,
      sft_messages    jsonb,   -- OpenAI messages-array format; null when not eligible
      -- metadata
      model_versions  jsonb,
      created_at      timestamptz default now()
    );

    -- Index for the review UI default sort + filters
    create index on public.simulations (weighted_score asc);
    create index on public.simulations (hitl_status);
    create index on public.simulations (sft_eligible) where sft_eligible = true;

    -- SFT export query:
    -- SELECT sft_messages FROM public.simulations WHERE sft_eligible = true;
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        print(f"  [supabase] STUB — creds not set, skipping write for {result['run_id']}")
        return

    row = {
        "run_id": result["run_id"],
        "git_sha": GIT_SHA,
        "persona_id": result["persona_id"],
        "seed_label": result["seed_label"],
        "bucket": result["bucket"],
        "transcript_json": result["transcript"],
        "scores_json": result["scores_json"],
        "weighted_score": result["weighted_score"],
        "judge_summary": result["judge_summary"],
        "hitl_status": result["hitl_status"],
        "tim_verdict": result["tim_verdict"],
        "tim_note": result["tim_note"],
        "sft_eligible": result["sft_eligible"],
        "sft_messages": result["sft_messages"],
        "model_versions": {"judge": JUDGE_MODEL},
    }

    try:
        with httpx.Client(timeout=15) as http:
            resp = http.post(
                f"{SUPABASE_URL}/rest/v1/simulations",
                json=row,
                headers={
                    "apikey": SUPABASE_SERVICE_ROLE_KEY,
                    "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                    "Content-Type": "application/json",
                    "Prefer": "resolution=merge-duplicates",
                },
            )
            resp.raise_for_status()
            print(f"  [supabase] wrote run {result['run_id']}")
    except httpx.HTTPError as exc:
        print(f"  [supabase] write failed: {exc}")
