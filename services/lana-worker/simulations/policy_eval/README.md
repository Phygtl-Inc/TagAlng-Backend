# Conversational-Policy eval harness

Spec-accurate eval harness for the **conversational policy** (Lana's per-turn decisioning +
tone/lingo + safety), built to the three external design docs — `LANA_CONVERSATIONAL_POLICY_ENGINEERING_v1`,
`LANA_CONVERSATIONAL_POLICY_PROMPT_v1`, `LANA_LINGO_v1` — which describe a single
`decide_turn(session, user_text) -> NextAction` brain.

**That brain does not exist in the codebase.** The live runtime is `run_lana_unified_pipeline`
(a 5-tuple behind `POST /lana/sessions/{id}/messages`), and the in-repo architecture docs
(`QUESTION_ASKING_AND_PROACTIVITY_v1 §2`) deliberately chose the *opposite* design (purpose-built
flows, no unified brain, no `candidate_goals` queue-unification). So — exactly like the sibling
`circles_zip/` harness — this is a **spec stand-in with a swappable backend**, and a handoff report
(`../../../../POLICY_EVAL_HARNESS_HANDOFF.md`, repo root) tells Asjid/Yunchao what to build/confirm.

## Run it

```bash
cd services/lana-worker/simulations/policy_eval
python run_eval.py --dry-run                 # no API key, no server — smoke-test the pipeline
python selftest.py                           # prove the mechanical checks fire on violations
python run_eval.py --backend stub --judge    # reference policy + LLM judge (needs OPENAI_API_KEY)
python run_eval.py --backend stub --judge --multi-judge   # 3-stance judge + disagreement/REVIEW flag
python run_eval.py --backend stub --judge --blind-judge   # omit judge_focus — blind-vs-guided = the calibration probe
python run_eval.py --backend live  --judge   # score REAL Lana (needs running worker + account)
python run_eval.py --id sf_crisis_distress --judge        # one scenario
python run_eval.py --bucket lingo_tone                     # one bucket
```

Writes `out/report.md`. Mechanical axes always run; judged axes need `--judge`. `--gate` exits 1
on any HARD_FAIL (CI mode).

## File layout

| File | What it is |
|---|---|
| `ports.py` | **The swap seam + contract.** `NextAction`/`Chip`/`Goal`/`WorldState`/`TurnContext` (external PART 5) + the `PolicyPort` protocol. Every field is spec-confirmed or `# GUESSED` with a reason. Read this first. |
| `lingo_guardrail.py` | The LANA_LINGO §14.2 banned-word scanner (§2/§7 lexicon) + an ES/PT gendered-token detector. Pure regex, mechanical, importable by the existing `simulation.py` pipeline too. |
| `world_state.py` | The 8 real `capability_id`s + the `required_state` gate capability-grounding checks against. **The gate is `# GUESSED`** — the DB column is empty. |
| `scenarios.py` | ~23 scenarios in 3 buckets (`decision_quality`, `lingo_tone`, `safety`), each with mechanical expectations + judged-axis focus. |
| `stub_policy.py` | The reference policy — PART 1-7 constitution → `NextAction`. Runnable; the spec-faithful reference. Needs `OPENAI_API_KEY`. |
| `live_policy.py` | Adapter onto the **real** runtime. Maps the response → `NextAction`; fields the runtime doesn't emit (`why`/`kind`/typed chip actions) are `# FLAGGED`. |
| `backend.py` | `SIM_BACKEND=stub\|live\|dry` switch (+ `DryPolicy` for the no-key smoke test). |
| `checks.py` | **Mechanical** axes (lingo, schema, chips, capability-grounding, dead-end, expected-kind, defer, neutral-gender). Non-circular. |
| `judge.py` | **LLM-judged** axes (right-action, ack→bridge→offer, timing, gender-agreement, role-address, anti-sycophancy, AI-honesty, safety, never-dead-end, tier-privacy) + **multi-judge calibration** (dropped axis → `UNSCORED` fail-closed; no-plurality split → `REVIEW`). |
| `run_eval.py` | Entry point: scenarios × backend → checks + judge → `out/report.md`. |
| `selftest.py` | Adversarial self-check: feeds each mechanical axis a planted violation and asserts it fires. |

## Mechanical vs. judged (the core design choice)

Following the principle validated on the rapport + QA harnesses: **ground truth known by
construction is checked mechanically; ground truth needing a "correct?" judgment is LLM-judged**
(with a multi-judge calibration guard, since LLM judges are miscalibrated).

- **Mechanical** (`checks.py`, no LLM, non-circular): banned lexicon in utterance + every chip
  label; a planted place-name that must not leak at stranger tier; a tool that isn't registered
  or isn't available for the world-state; chip count/escape; an overtly-gendered ES/PT token when
  gender is unknown; empty utterance; a `capture_defer` with no `defer_goal_id`.
- **Judged** (`judge.py`, LLM, multi-judge): did she pick the right action; is the acknowledge→
  bridge→offer there; timing/defer appropriateness; correct ES/PT agreement when gender is known;
  role-appropriate address; anti-sycophancy; AI-honesty; crisis handling.

The mechanical axes are the trustworthy signal against the **stub** (which shares the constitution
with the judge, so judging it is partly circular). Run against **live** for a real quality read.

Two circularity caveats surfaced by review and kept honest in-code/report:
- **6 decision scenarios are verbatim in the stub's few-shot exemplars** (`exemplar_of_stub=True`) —
  a stub verdict on them measures parroting; the report flags them and they're fully valid vs `live`.
- The stub now **derives** capability availability from the registry + `required_state` (it is no
  longer handed the pre-filtered list), so the capability-grounding check isn't tautological against it.
- The mechanical checks are non-circular, but a green *lingo* result against the stub still only shows
  the model obeyed an explicit lexical rule (instruction-following), not taste — that's why `live` matters.

## What each backend can measure

| backend | needs | measures |
|---|---|---|
| `dry` | nothing | pipeline plumbing only (canned actions) — for CI of the harness itself |
| `stub` | `OPENAI_API_KEY` | the reference policy end-to-end; mechanical = real signal, judged = partly circular |
| `live` | running worker + provisioned account | REAL Lana's tone/lingo/safety; decision-structure axes flagged (see below) |

**Live-mode limits (honest):** the current runtime emits no `why`, no `kind`, and untyped chip
labels, and this harness can't seed the account's real world-state — so `why`/`kind`/typed-chip
and capability-grounding results in live mode are indicative only until `NextAction` (or at least
`kind`/`tool`/`why` in the routing payload) is instrumented and a seedable test account exists.
Tone/lingo/safety axes (utterance + chip labels) are fully valid against live today.

## GUESSED / FLAGGED items — need Asjid/Yunchao sign-off

`grep -rn "GUESSED\|FLAGGED" .` lists them; the root handoff report summarizes them. The load-bearing
ones:

1. **`required_state` capability gate** (`world_state.py`) — the whole capability-grounding axis
   leans on it, but `capability_index.required_state` is **empty on every DB row**; the gate here
   encodes the engineering-§C.3 *intent*, not DB truth. Confirm the token vocabulary (`zip_open`?).
2. **`users.role`** — no such column exists (only `founder_role`: cohort). Role-address is
   harness-supplied.
3. **`users.grammatical_gender`** — no such column exists (gender is deliberately not extracted).
   Gender-agreement has no known-gender source; unknown → neutral is the only safe default today.
4. **`NextAction` contract itself** — the runtime emits nothing shaped like it (`why`/`kind`/typed
   chips absent). The whole decision-structure eval presumes this shape is added.
5. **`candidate_goals` unified Goal shape** — the four queues exist as tables but nothing unifies
   them, and `QUESTION_ASKING §3.3` explicitly declines the unification for v1.

None of these are guesses about what the product should *do* — they're shapes the specs left
open or DB state that doesn't exist yet. Don't treat them as load-bearing against `live` until confirmed.

## Swapping to a real `decide_turn` later

If/when a real policy engine exists behind an endpoint or import: implement `decide_turn` in a new
`PolicyPort` in `live_policy.py` (or point the adapter at the new endpoint), `export SIM_BACKEND=live`,
re-run `python run_eval.py --judge`. No other file changes.
