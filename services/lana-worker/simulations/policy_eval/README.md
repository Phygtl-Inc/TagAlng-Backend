# Conversational-Policy eval harness

Spec-accurate eval harness for the **conversational policy** (Lana's per-turn decisioning +
tone/lingo + safety), built to the three external design docs — `LANA_CONVERSATIONAL_POLICY_ENGINEERING_v1`,
`LANA_CONVERSATIONAL_POLICY_PROMPT_v1`, `LANA_LINGO_v1` — which describe a single
`decide_turn(session, user_text) -> NextAction` brain.

**That brain now EXISTS** (this README revised 2026-08-18). It shipped as:

| thing | where |
|---|---|
| `decide_turn(*, user_id, session_ctx, history, user_message, answering_question=None) -> NextAction \| None` | `app/policy/decide.py:499` |
| the real `NextAction` (`kind, utterance, chips, goal_id, defer_goal_id, pending_action, distress_turn, why, guardrail`) | `app/policy/decide.py:37` |
| `world_state(user_id) -> {user, area, circles, states}` | `app/policy/world.py:105` |
| `candidate_goals(...)` — the queue unifier | `app/policy/goals.py:391` |

`None` from `decide_turn` means *no decision* — the caller falls through to the legacy pipeline,
which is behaviourally the same as `kind='handoff'`.

The harness was written before any of that landed, so several of its `# GUESSED` markers had gone
**false**: they asserted "no such column exists" about columns that had shipped, and they encoded a
capability gate that a later migration deliberately removed. Those are corrected; `# GUESSED` now
survives only where the real system is still genuinely undecided. The swappable-backend structure
stays, because the stub is still the PART-5 reference and the two real adapters observe different
things.

## Run it

```bash
cd services/lana-worker/simulations/policy_eval
python run_eval.py --dry-run                 # no API key, no server — smoke-test the pipeline
python selftest.py                           # prove the mechanical checks fire on violations
python run_eval.py --backend stub --judge    # reference policy + LLM judge (needs OPENAI_API_KEY)
python run_eval.py --backend stub --judge --multi-judge   # 3-stance judge + disagreement/REVIEW flag
python run_eval.py --backend stub --judge --blind-judge   # omit judge_focus — blind-vs-guided = the calibration probe
python run_eval.py --backend inproc --judge  # the REAL decide_turn, in-process (needs DB + user id)
SIM_INPROC_INJECT_WORLD=1 python run_eval.py --backend inproc   # ...with the scenario's world forced in
SIM_ALLOW_WRITES=1 SIM_INPROC_SEED_WORLD=1 python run_eval.py --backend inproc   # ...with it SEEDED for real (LOCAL stack only)
python run_eval.py --backend live  --judge   # REAL Lana over HTTP (needs running worker + account)
python run_eval.py --id sf_crisis_distress --judge        # one scenario
python run_eval.py --bucket lingo_tone                     # one bucket
```

Writes `out/report.md`. Mechanical axes always run; judged axes need `--judge`. `--gate` exits 1
on any HARD_FAIL **or any UNSCORED axis** (CI mode) — an axis nobody could measure must not be
able to carry a green gate.

> **Do not start a live/inproc multi-turn run casually.** The six shared sim accounts are contended
> across machines; a contended run fails spuriously and can break someone else's.

## File layout

| File | What it is |
|---|---|
| `ports.py` | **The swap seam + contract.** `NextAction`/`Chip`/`Goal`/`WorldState`/`TurnContext` (external PART 5) + the `PolicyPort` protocol. Every field is spec-confirmed or `# GUESSED` with a reason. Read this first. |
| `lingo_guardrail.py` | The LANA_LINGO §14.2 banned-word scanner (§2/§7 lexicon) + an ES/PT gendered-token detector. Pure regex, mechanical, importable by the existing `simulation.py` pipeline too. |
| `world_state.py` | The 9 real `capability_id`s + the `required_state` gate + the `is_active` set, **replayed from the migrations** (the replay is in the module docstring). Also `capability_drift()`: an offline check of the mirror against `supabase/migrations/*.sql` — ids, `required_state` values, and deletions — which reports the statements it could not parse instead of assuming them clean. |
| `scenarios.py` | 29 scenarios in 3 buckets (`decision_quality`, `lingo_tone`, `safety`), each with mechanical expectations + judged-axis focus. |
| `stub_policy.py` | The reference policy — PART 1-7 constitution → `NextAction`. Runnable; the spec-faithful reference. Needs `OPENAI_API_KEY`. |
| `live_policy.py` | **Both real adapters.** `InProcPolicy` calls `app.policy.decide.decide_turn` directly (kind/why/defer/distress REAL); `LivePolicy` drives the HTTP endpoint (whole shipped pipeline, fewer observable fields). The world-injection honesty rules live here. |
| `backend.py` | `SIM_BACKEND=stub\|inproc\|live\|dry` switch (+ `DryPolicy` for the no-key smoke test). |
| `checks.py` | **Mechanical** axes (lingo, schema, chips, capability-grounding, world-fidelity, dead-end, expected-kind, defer, neutral-gender). Non-circular. |
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
| `inproc` | DB + service-role key + LLM key + a real `user_id` | the REAL policy decision: `kind`, `why`, `defer_goal_id`, `distress_turn`, chip labels, capability id (via `goal_id`'s `cap:` prefix) |
| `live` | running worker + provisioned account | the REAL user-visible turn end to end (guardrail output, ui_actions, legacy fall-through); decision-structure fields not returned |

**What is observable where**

|  | stub/dry | inproc | live (HTTP) |
|---|---|---|---|
| `kind` | real | **real** (`decide.py:38`) | heuristic → exempt |
| `why` | real | **real** (`decide.py:56`) | absent → exempt |
| `defer_goal_id` | real | **real** (`decide.py:42`) | absent → exempt |
| `distress_turn` | real | **real** (`decide.py:55`) | absent → assumed false |
| chip labels / count | real | real | real |
| typed chip `action` | real | **absent from `app/` entirely** (chips are `{label, send}`) | absent |
| tool as a `capability_id` | real | via `goal_id` `cap:` prefix | engine tool NAME → not gated |
| the scenario's pinned world | honoured | `SIM_INPROC_INJECT_WORLD=1` (fabricated) or `SIM_INPROC_SEED_WORLD=1` (written, local stacks only) | never |

**The world problem, and why it can't quietly pass.** `decide_turn` takes a `user_id` and reads the
world from the DB itself, so a scenario that pins "quiet area, unverified, no circle" otherwise runs
against whatever the sim account really is — a different question wearing the scenario's name. So:

* **default (`inproc`, and always for `live`)** — the world is *not* honoured, `world_fidelity`
  returns `UNSCORED`, and `UNSCORED` ranks above `SOFT_FAIL` and fails `--gate`. A scenario whose
  world could not be honoured can never come out of a run clean.
* **`SIM_INPROC_INJECT_WORLD=1`** — the pinned world is injected at the
  `app.policy.world.world_state` seam and every report line says **HARNESS-SUPPLIED WORLD**. The
  decision is real; the situation it was made in is fabricated.
* **`SIM_INPROC_SEED_WORLD=1`** *(local Supabase stack only — see
  [`../LOCAL_STACK.md`](../LOCAL_STACK.md))* — the pinned world is **written** for the real
  account before each decision (`local_world.apply_world`), so *every* read `decide_turn` makes
  agrees: goals, claims and capability rows see the same world as the area/verification
  snapshot, which injection cannot reach. The pin is then **read back** through the real
  `world_state()`; `world_fidelity` PASSes only if the read-back equals it, and is `UNSCORED`
  otherwise — a seed that silently did not take can never look like control. It takes
  precedence if `SIM_INPROC_INJECT_WORLD` is also set.
* `decide_turn` returning `None` (legacy fall-through) makes the whole turn a single `UNSCORED`
  axis — not a PASS (nothing was verified) and not a HARD_FAIL (the legacy path may have answered
  it perfectly, out of this backend's sight; use `--backend live` for those).

Writing a scenario's world into the DB used to be ruled out entirely, and the reason survives
intact: the sim accounts live in the **shared** dev project and are contended, so seeding one
from an eval destroys other people's runs. That objection is about *sharedness*, not about
seeding — so `SIM_INPROC_SEED_WORLD` exists, and `local_world.py` refuses to run unless
`SUPABASE_URL` is a local stack (`local_guard.require_local`, fails closed on anything it does
not positively recognise). It cannot be pointed at dev by accident; the deliberate escape hatch
is `SIM_ALLOW_NONLOCAL_WRITES=1`, which prints a banner.

## GUESSED / FLAGGED items — need Asjid/Yunchao sign-off

`grep -rn "GUESSED\|FLAGGED" .` lists them; the root handoff report summarizes them. The load-bearing
ones:

**Resolved — these used to be listed here and were wrong.** `users.role` and
`users.grammatical_gender` exist (`20260909120000`, with check constraints that are now the
harness's enums); `zip_unlock` exists (`20260906120000:201`); the `NextAction` contract exists
(`decide.py:37`); `candidate_goals` exists (`goals.py:391`); and the state-token vocabulary is
`verified / has_home_zip / zip_open / has_circle` (`world.py:124-134`) — the harness's invented
`phone_verified` token was never real.

**Still open:**

1. **`looking.tip.required_state`** — never SET by any migration, so it sits at the column default
   `'{}'`. Reading that default as a deliberate "always-on" decision is the remaining guess, and
   this row is *active*, so the guess is load-bearing. `looking.swap` has the same gap but is
   `is_active=false`, so it is moot.
2. **Typed chip actions** — shipped chips are `{label, send}`; there is no `action` enum anywhere
   in `app/`. The NOT_NOW/graceful-escape sub-check is therefore unobservable against any real
   backend and is reported as such rather than passed.
3. **8 of the 9 `required_state` values cannot be verified offline** — `capability_drift()`'s
   value arm replays only unambiguous SQL, and four statements are not:
   `20261005120000`'s UPDATE keys on `capability_name` rather than `capability_id`, and
   `20260908120000` (×2) / `20260917120000` carry `and (required_state is null or
   required_state = '{}')` guards that may or may not have applied. Those are reported as
   **UNPARSED** by `selftest.py`, not silently treated as clean. Only `discovery.communities`
   (inserted by `20261028120000`, after every unparsed statement) is verified end-to-end —
   which is the one that matters, since it is the only row with a non-empty gate. The other
   eight are `'{}'` in the mirror by hand-replay.
4. **`bridge_offer` with no `goal_id`** — a real observability gap in the product, not the harness:
   `audit_offer_goal` (`decide.py:339`) logs `decide_turn_offer_without_goal` because it happens,
   and when it does, nothing downstream (including this harness) can tell which capability was
   pitched.

## Keeping this honest as the product moves

The failure mode this harness has already hit once is a **stale assumption that reads as ground
truth**: it encoded a `{zip_open}` gate on discovery, migration `20261005120000` deliberately
removed that gate (because it made Lana say "there aren't any local communities to show yet" in a
ZIP that had some), and the harness then HARD_FAILed Lana for the corrected behaviour. A mechanical
gate that fires on correct behaviour is worse than no gate.

The second form of the same failure is a **pin that cannot see what it claims to pin**.
`selftest.py` used to assert `available_capabilities(cold_area()) == available_capabilities(
live_area())` and describe it as catching "a future gate migration" — but both fixtures are
`verified=True` and differ only in `zip_unlock_state`, so `20261028120000`'s `{verified}` gate
cancels out of both sides and the assertion could never have fired on it. An availability pin has
to vary **one token at a time**: the suite now asserts that an *unverified* world EXCLUDES
`discovery.communities`, that a verified one INCLUDES it in an open, closed *and* warming ZIP
(communities are never area-gated), and separately that nothing gates on `zip_open` or
`has_home_zip` today.

So, when `app/` or a migration changes: re-read `world_state.py`'s migration replay, re-run
`python selftest.py` (whose drift block compares the mirror's ids *and* its `required_state`
values against the SQL, and prints the statements it could not parse), and delete any
`# GUESSED` the change made false.
