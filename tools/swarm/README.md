# `tools/swarm` — the zero-bug program's section runner

Walks a program section (P0–P8, X1–X3) with the nine `personas.json` fixtures,
asserts on `TurnRouting` and the database, and writes one `simulations` row per
`(run_id, section_id, persona_id, arm)`.

Answers `LANA_AUTONOMY_GAPS.md` gap 3: *"Scheduled tasks give cadence. They don't
give 9 personas × 12 sections × ~50 assertions in parallel overnight."*

---

## Quick start

```bash
pip install -r tools/swarm/requirements.txt

export SUPABASE_URL='https://kmetmatfxdkrialwrnzj.supabase.co'   # tagalng-prod
export SUPABASE_SERVICE_ROLE_KEY='...'
export SIM_EMAIL_DOMAIN='...'        # only needed by sections that sign up (P0)

# preflight only — read-only, writes nothing, safe to run any time
python tools/swarm/run_swarm.py --section P1 --run-id probe \
  --fixtures '../[R&D] TagAlng/tests' --dry-run

# night 1
python tools/swarm/run_swarm.py --section P1 --run-id 2026-07-31-a \
  --fixtures '../[R&D] TagAlng/tests' --out artifacts/2026-07-31-a.json
```

Exit codes, because a scheduler reads them: `0` clean · `1` assertions failed ·
`2` preflight aborted (nothing written) · `3` a hard rail was violated.

**Fixtures live in the design repo**, not here — `personas.json`,
`KNOWN_DELTA_REGISTRY.md` and the `SPEC_*.md` files. Point `--fixtures` (or
`SWARM_FIXTURES_DIR`) at `[R&D] TagAlng/tests`.

---

## The hard rails are code, not comments

`LANA_ZERO_BUG_PROGRAM_FINAL.md` §5 writes the rails as instructions. An
unattended overnight run against a database with **31 real users** needs them
unreachable, so:

| Rail | How it is enforced |
|---|---|
| Never `/complete` without `publish:false` | `complete_session()` has **no `publish` parameter**. It sends `{"publish": false}` literally. `CompleteSessionRequest.publish` defaults to `true` in the worker (`app/models.py:525`), so a forgotten kwarg would publish a real event. It also raises if the response comes back `published:true`. |
| Never call `/hooks/*` | Rejected by `worker.FORBIDDEN_PREFIXES` before a socket opens. Real push + email to real people, not rate limited. |
| Only endpoints a section needs | `worker.ALLOWED` allowlist. Anything else raises `RailViolation`. |
| Never look like a bot | `RateLimiter` enforces 3 sessions/min and 40/hour — the caps in `SPEC_P0_SIGNUP.md` rail 6 and PR #124 §4.2.2. |
| Stay off the Gemini-only path | `create_session()` refuses `purpose != "lana"` — D-05: `profile_intake` runs on Gemini regardless of the provider flag and would confound every language and extraction verdict. |
| Tag everything for teardown | The identity is written to `swarm_run_actors` **before the first message**, so a crash mid-walk is still sweepable. |

A `RailViolation` aborts the entire run, not just the walk — if we may already
have touched something we must not, the right move is to stop.

---

## Preflight: abort vs degrade

The distinction is the point.

**ABORT** — running would expose data or produce a meaningless result:

- `G4` — RLS on `simulations` and ≥1 row in `admin_allowlist` (PR #119). The
  harness writes verbatim user utterances; `admin_allowlist` still has **0 rows**,
  and #119's read policy is fail-closed, so nobody could read the results.
- `G4/T2` — `simulations` has the harness columns (PR #125). No columns, nowhere
  to put a verdict.
- `T3` — `swarm_run_actors` exists (PR #126). Without it `cleanup_swarm_run()`
  cannot resolve what the run created.
- `SCHEMA` — the `users` column set was actually readable. **A failed read must
  never be recorded as an absent column**: that would degrade every
  role/gender assertion to `blocked` and report a green night that tested nothing.
- `REGISTRY` — no TEMPORARY delta whose PR has merged. §4.6: a stale entry
  converts a real regression into `blocked-by-known-delta`.

**DEGRADE** — a known gap makes some assertions unobservable. Those become
`blocked-by-known-delta`, which is excluded from the score. This is not a reason
to skip the night; it is a reason not to file the same bug nine times.

Preflight detects the **live D-12 502** without credentials, straight off
`/health`:

```
[ degr ] G1  llm_provider=openai with lana_model=gemini-2.5-flash: the D-12 shape.
             _extract_model() hands a Gemini model to an OpenAI-routed llm_json(),
             so /complete returns 502.
```

When G1 is active the runner **does not call `/complete` at all** and records the
completion assertions as `blocked` with `delta_id=D-12`.

---

## Scoring

```
score = passed / (passed + failed)      # blocked and error are EXCLUDED
```

`score` is **`None`, not `0.0`**, when the denominator is zero. A section where
everything was blocked has no score; `0.0` would call it a total failure. On
today's prod build all of P1 block C is blocked by D-12, so that is the current
state, not a hypothetical.

Four verdicts (`personas.json#verdict_vocabulary`): `pass` · `fail` ·
`blocked-by-known-delta` · `error`.

`error` is deliberately distinct from `fail`: **"we did not measure"** is not
**"the product is wrong."**

### Nothing passes silently

All 58 assert keys in `personas.json` have a handler. Of 398 total occurrences:

| | count | |
|---|---|---|
| scored | 316 | real pass/fail/blocked verdicts |
| record-only | 38 | metadata (`note`, `expected_delta*`, `assertion_id`) |
| honest `error` | 44 | needs a semantic judgement (`expect_no_false_promise`, `expect_honest_supply_answer`, `expect_wrap`) or a browser |
| **unhandled** | **0** | — |

An unknown key returns `error` naming itself. Never `pass`. Silently passing an
unimplemented check is how a section reports green while testing nothing —
`SPEC_P1_LANGUAGE.md` §SCORE flags a run with **zero** blocked assertions as
"itself suspect" for exactly this reason, and `summarize()` emits that warning.

The 44 `error` keys are the honest boundary of what the worker API can prove.
Closing them needs either a rubric that removes the judgement, or the browser
driver P1's V-block calls for. **Do not convert them to `pass` to raise the
score.**

---

## The language classifier

`SPEC_P1_LANGUAGE.md` §CLASSIFIER and §GENDER, implemented exactly. `lingua`
over EN/ES/PT only — restricting the set is what makes PT separable from ES at
sentence length; a full-set detector spreads mass across Galician and Catalan and
the 0.90 threshold stops firing. **No LLM-as-judge anywhere**: the point of P1 is
determinism.

Verified behaviour:

```
B2 repro ("I found your account. Love it. What kind of thing are you up for?")
  banned literals: all 3 hit          EN sentences: 1
clean ES  -> ratio 1.0, 0 EN          clean PT -> 0 ES leak, ratio 1.0
"Busco un seguro médico"              -> masculine: []       (insurance sense excluded)
"Estoy seguro de eso"                 -> masculine: [seguro]  (agreement sense counted)
"Bem-vinda! Você está pronta?"        -> feminine: [bem-vinda, pronta]
"Sí. Ok. Claro. Ya."                  -> void (100% short) -> verdict `error`, not `pass`
```

Note the B2 case scores **1** EN sentence, not 3: *"What kind of thing are you up
for?"* falls below the 0.90 confidence threshold. That is why the spec pairs the
classifier with a banned-literal list — the literals catch precisely the short
colloquial strings the detector is least sure about. Both mechanisms are needed.

`§GENDER` exclusions are implemented: ES `solo` (adverb) is never scored, ES
`seguro` is dropped when it means insurance (Cata's actual use case), PT `pronto`
is dropped as a sentence-initial interjection.

---

## Reporting

Deltas are reported **by frequency**, per `KNOWN_DELTA_REGISTRY.md` §4.5 — that
count *is* the argument for what to fix next:

```
deltas: D-04 x 27, D-13 x 9, D-12 x 9, D-10 x 4
```

`supply-blocked` is a load-bearing term (§4.4). The runner writes it as the
`bucket` on every row and never emits `disengaged`, `low-intent`, `churned` or
`abandoned` — mislabelling a supply problem sends it to the wrong team.

---

## Teardown

Always, keyed on `run_id`:

```sql
select public.cleanup_swarm_run('2026-07-31-a');
select public.cleanup_swarm_run('2026-07-31-a', false, true);  -- dry run
```

The nightly workflow runs it with `if: always()` and **fails the job if it
raises** — a failed run leaves the same identities behind as a successful one,
and a silent teardown failure is worse than a red section because nobody looks at
a green run.

---

## Scheduling

`tools/swarm/deploy/swarm-nightly.yml` — **must be moved to `.github/workflows/`** by
someone with the GitHub `workflow` OAuth scope; see `deploy/README.md`. Weekdays 02:00 UTC, `workflow_dispatch`
for manual runs.

**Why an Action and not a Cloud Run job:** this repo deploys Cloud Run by
hand-run shell script and has no CI, no Terraform, and no `.github/workflows` at
all. A Cloud Run job would need a Dockerfile, an Artifact Registry push, a service
account, an IAM binding and a Cloud Scheduler trigger — all hand-provisioned —
before the first assertion. The runner is pure Python + httpx and two secrets.
Revisit if it ever needs VPC access or a >6h budget.

Secrets: `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `WORKER_BASE_URL`,
`SIM_EMAIL_DOMAIN`, `FIXTURES_REPO_TOKEN` (to check out the design repo).

Night 1 is **P1 alone across all nine personas** (§6) — language noise
contaminates every downstream transcript. `section` is a single input, not a
matrix, so running two sections on night 1 has to be a deliberate act.

---

## Known limits

- **P0 needs a mail inbox.** `SPEC_P0_SIGNUP.md` §PRE-FLIGHT calls a
  programmatic catch-all read *"the single hardest dependency in P0"* and makes
  it an ABORT. `identity.py` implements the link/verify calls; **the inbox poll
  is not implemented** because no inbox has been provisioned. P0 cannot run until
  it is.
- **The V block (voice) is unreachable here.** The worker API carries no voice,
  TTS or audio field (K1-5). Needs a browser driver.
- **The 44 judgement-dependent asserts** listed above.
- **Edge cases are not implemented.** The runner walks each persona's declared
  100% path. The five per-section edge cases in each spec are written as prose
  and need per-section code; `--section` currently selects fixtures and gates,
  not an edge suite.
