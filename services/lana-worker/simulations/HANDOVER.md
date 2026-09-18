# Handover — Lana eval suite

**Written 2026-09-18 by Tim, last day on this.** This is meant to be sufficient on its own —
if you're a new engineer or a fresh Claude session with zero prior context, read this file
top to bottom and you should be able to run, extend, or debug anything in this directory
without asking anyone anything first. `CLAUDE.md` in this same directory is the terse
conventions cheat-sheet; this doc is the full walkthrough. Read both, but this one first.

---

## 0 · What this directory is, in 30 seconds

This is **eval/simulation only** for Lana (TagAlng's neighborhood-app AI). It measures whether
Lana's product code behaves correctly — it never contains product code itself.

**The one rule that matters most:** when a run finds a bug in Lana's actual behavior, **write it
up, don't fix it.** `app/` and `supabase/` are owned by Asjid (backend), Yunchao/Aki (AI/prompts),
Tommaso (product) — not by whoever is running evals. Every finding in this suite's history has
gone into a markdown report, never a patch to `app/`. If you're a Claude session and someone asks
you to "just fix the bug you found," push back and ask whether they really mean patch `app/`
directly, because that's outside this directory's job.

**Everything here is additive.** Nobody has ever deleted or rewritten another engineer's eval
code without reason — extend, don't replace, unless something is genuinely proven wrong (and
even then, say so in a commit message, don't silently overwrite).

---

## 1 · The six pipelines, what each measures, and exactly how to run it

All six live under `services/lana-worker/simulations/`. Every command below assumes you've `cd`'d
into the pipeline's own directory first (`circles_zip/`, `policy_eval/`, `rapport/`, `reco_eval/`,
`subject_eval/` all use bare intra-package imports — `from ports import ...` — so they only work
run from inside their own folder, or with that folder already on `sys.path`).

Install deps once: `pip install -r services/lana-worker/requirements.txt` (the worker's own deps)
and `pip install -r services/lana-worker/simulations/requirements.txt` (PyYAML, httpx, etc. the
eval suite needs that the worker doesn't). Do these as **two separate installs** — the worker
pins `openai>=1.55,<2`, simulations pins `openai>=2.50,<3`; one `pip install -r a -r b` resolves
one of them away.

### 1.1 · `./` (root — `runner.py`, `simulation.py`, `evaluation.py`)

Full conversations against the **real Lana API** (HTTP), decision + tone scored by an LLM judge.
The oldest and most expensive pipeline here.

```
cd services/lana-worker/simulations
python selftest.py            # mock-user OOC guard + local_guard + drift tripwires, offline
python runner.py --pr         # must-have buckets only (safety, PII, core function, routing)
python runner.py              # full nightly matrix
```

Needs a running worker (`LANA_BASE_URL`, defaults to `http://localhost:8000` — **not** 8081,
see gotcha in §3) plus `SUPABASE_URL`/`SUPABASE_ANON_KEY`/`SUPABASE_SERVICE_ROLE_KEY`/
`SIM_PASSWORD`/`OPENAI_API_KEY`, all from `.env.local`.

**Session-collision hazard**: the six sim accounts live in the SHARED dev Supabase project, and
`create_session(force_new=True)` calls `abandon_other_active_sessions` — two concurrent runs
against dev destroy each other. `sim-gate.yml`'s CI concurrency group exists solely to serialize
this. Never run this pipeline against dev in parallel with anyone else, ever.

### 1.2 · `rapport/` — claim extraction + PII redaction

Real `vertex_extract` claim extractor, no DB.

```
cd services/lana-worker/simulations/rapport
python run_eval.py --dry-run          # validates fixtures, zero API calls
python run_eval.py --judge            # real extractor + gap-quality LLM judge
```

Latest real numbers (2026-09-18): facet precision 58-63%, recall 44-48%, 0 PII leaks across two
runs. `--out` writes to a **gitignored** path by design — the report contains pre-redaction PII.

### 1.3 · `circles_zip/` — onion matcher + ZIP-unlock state machine

Targets a feature that doesn't fully exist as a single code path yet, so it uses a swappable
seam: `SIM_BACKEND=stub` (default, spec-faithful reference impl, mirrors the deployed algorithm)
or `SIM_BACKEND=live` (real RPC calls).

```
cd services/lana-worker/simulations/circles_zip
python sweep.py --quick                              # stub, small population, fast
python sweep.py                                       # stub, full sweep
SIM_BACKEND=live python sweep.py --no-gate            # real matcher RPC (read-only, safe)
SIM_BACKEND=live SIM_ALLOW_WRITES=1 python sweep.py   # + real ZIP-unlock state writes
```

**Known blocker (2026-09-17):** the `SIM_ALLOW_WRITES=1` live sweep 400s, because it tries to
score a **synthetic population** (fabricated UUIDs from `population.py`) that was never actually
seeded into the target Supabase project. `live_seed.py` exists to do that seeding first — it's
also a destructive, `local_guard`-gated write path, and hasn't been run this cycle. The RPC
itself works fine (verified directly against a real seeded user) — this is a missing setup step,
not a code bug.

`SIM_ALLOW_WRITES=1` may get blocked by an agent auto-mode permission classifier even when the
user has authorized it in conversation — that's a session-level guard, not this suite's; if
blocked, it usually succeeds on a plain retry (transient), otherwise ask the user to explicitly
approve the write in whatever interface is prompting.

### 1.4 · `policy_eval/` — conversational-policy decisions, lingo, safety

`SIM_BACKEND=stub|inproc|live|dry`:
- `dry` — deterministic canned actions, no key, no server. Smoke test.
- `stub` — reference policy implementation, needs `OPENAI_API_KEY`.
- `inproc` — **the real `app.policy.decide.decide_turn`, called in-process.** Needs
  `SUPABASE_SERVICE_ROLE_KEY` + an LLM key + either `SIM_INPROC_USER_ID` or `SIM_LIVE_EMAIL`.
  Reads the account's REAL state by default (`world_source="account"`); world injection
  (`SIM_INPROC_INJECT_WORLD=1`) or seeding (`SIM_INPROC_SEED_WORLD=1`, **local stacks only** —
  `local_guard` refuses it against dev, on purpose, because seeding a shared sim persona destroys
  other people's runs) are opt-in.
- `live` — full HTTP adapter onto `POST /lana/sessions/.../messages`, the whole shipped pipeline.

```
cd services/lana-worker/simulations/policy_eval
python run_eval.py --dry-run --gate
SIM_INPROC_USER_ID="51000001-0001-4000-8000-000000000001" python run_eval.py --backend inproc --judge
```

That UUID is `p1-sim@phygtl.dev` on the **local** seeded stack (see §2) — safe to reuse, doesn't
write conversation history (InProcPolicy calls the pure decision function, not the message-
persisting HTTP path).

**Real finding, reproduced twice (2026-09-17/18):** `decide_turn` returned `kind=handoff,
tool=None, reply=''` — a completely empty message — on two different `bridge_offer` scenarios,
both scored `right_action`/`ack_bridge_offer` at 0.0 by the judge. Caveat: both scenarios needed
an injected world premise that wasn't turned on, so it may be a world-mismatch artifact rather
than a real bug — but the identical empty-reply shape twice is worth Asjid checking directly:
does the handoff path ever legitimately produce an empty body?

**Also, on this machine specifically:** every `decide_turn` call during local dev logs
`decide_claims_rank_failed` because `vertex_embed()` has no local Google Application Default
Credentials. `decide_turn` degrades gracefully (claims ranking is skipped, the turn still
completes) — this is a local environment gap, not a product bug. Fix: `gcloud auth
application-default login`, or accept that claims-informed ranking is unexercised locally.

### 1.5 · `reco_eval/` — recommendation capture (~2,900 lines)

The questions Lana writes when someone shares a recommendation, and the answers the flow
accepts. `--backend inproc|live|static|dry`, `--validator shipped|reference|dry`.

```
cd services/lana-worker/simulations/reco_eval
python selftest.py
python run_eval.py --dry-run --gate                                    # canned, CI-safe
python run_eval.py --backend inproc --arm both --trials 3 --judge      # real generator + judge
python run_eval.py --arm answers --validator shipped --gate            # §11/§12.4 regression gate
```

**Nondeterministic by nature** — identical calls to the question generator return six questions,
three, or none. Every number is a rate over `--trials`, never a single-run verdict.

Latest real numbers (two runs, 2026-09-17/18): static-fallback rate 9-12% (down from a
previously reported 45% — worth a bigger-trial confirmation before calling it fixed); junk
answers accepted 8/8 (100%, the §11 gap, unchanged across every measurement this suite has ever
taken); accuracy 62% both times. Systemic, seen in nearly every trial: the `subject` step never
carries an example placeholder (`head_step()` injects it, sets no `placeholder`); `contact`
sometimes lands last in the question order.

CI: wired into `sim-gate.yml` (offline, ~2s) and `sim-nightly.yml` (9 real trials).

### 1.6 · `subject_eval/` — subject-graph + recommender-authority spec (~6,500 lines)

The least uniform pipeline, on purpose: contract v2's subject graph (`subject_context()`, a real
`subject`/`attestation` table) **has not shipped**, confirmed by direct code search as of
2026-09-17. Everything that CAN run against real data does; everything else runs against a stub
built from the accepted spec text, ready to swap the day the real thing lands.

| Script | What | Run |
|---|---|---|
| `d3_resolve.py` | Resolver accuracy (real dev `latent_signals` embeddings) | `python d3_resolve.py --env ../../../../.env.local.dev-backup` |
| `n1_thresholds.py` | τ_merge/τ_new calibration on real dev embeddings | same `--env` pattern |
| `n2_extract_baseline.py` | Extractor yield + stability, real dev messages | `python n2_extract_baseline.py --env ../../../../.env.local.dev-backup --messages 40 --trials 3` |
| `band_spread.py` | §4.4 — top-10 result spread, real dev `latent_signals` proxy | same `--env` pattern |
| `t7_coverage.py` | Authority-tip coverage ceiling | same `--env` pattern — **circular on dev**, needs prod |
| `t3_anti_gaming.py` | `attester_authority()` anti-gaming properties | `python t3_anti_gaming.py` (offline mirror) or `--live --env ...dev-backup` (+1 safe real RPC call) |
| `n4_floor_decay.py` | Floor+decay decision table for §6.2 | `python n4_floor_decay.py` — pure sim, no DB |
| `d7_groundedness.py`, `d9_a8_a9.py`, `t2_authority_topic.py` | Stubs for unshipped subject_context/C1-C3 retrieval | `python <file>.py` — all offline |
| `elicit.py` / `run_eval.py` | D5 elicitation, 675 synthetic problems | `python run_eval.py` — deterministic, no DB, no LLM |
| `honest_empty.py` / `run_t6.py` | T6 — honest empty-state copy | `python run_t6.py` — offline fixtures |
| `safety.py` / `run_safety.py` | F-SAFETY structural rules | `python run_safety.py` — offline fixtures |
| `d11_label_assist.py` | Model-assisted labelling of resolver variant pairs + blind human audit | see §4 below — this is mid-workflow, pick it up here |
| `d11_calibration.py` | Scores τ_merge/τ_new against the labelled ground truth | `python d11_calibration.py` (needs `out/d11_model_labels.json` from the labeller first) |

`python selftest.py` runs 175+ assertions across the fixture-based pieces; the real-data scripts
have no selftest (nothing to plant a violation into — they measure the world as it is).

---

## 2 · Environment: which file, which target, why

Three env files, three different jobs. Confusing them is exactly what cost a full day early in
this project (see §5.1) — read this section before touching any of them.

| File | Points at | Used by | Gitignored? |
|---|---|---|---|
| `.env.local` (repo root) | **Local Supabase stack**, `http://127.0.0.1:54321`, seeded with 10 users incl. 6 sim personas (`p1-sim@phygtl.dev` .. `p6-sim@phygtl.dev`, local UUIDs `51000001-...` through `51000006-...`) | `reco_eval`, `policy_eval`, `circles_zip`, `rapport`, root `runner.py` — anything that exercises real `decide_turn`/generator code and needs to be safe against concurrent runs | Yes |
| `.env.local.dev-backup` | The **real shared dev** Supabase project (`rjlcyvwogmfmngemhbmn.supabase.co`) | `subject_eval`'s data-reading scripts (D3, N1, N2, T7, band-spread, T3 `--live`) via an explicit `--env` flag | Yes |
| `deploy/lana-worker.env` | Whatever the **actual worker process** (`scripts/run-lana-worker-local.sh`) runs against | Only the worker itself, never the eval scripts directly | Yes |

Setting up `.env.local` for the local stack: see `LOCAL_STACK.md` in this directory. If it's not
seeded yet, that doc has the checklist. Do NOT skip straight to dev — dev is shared, and the
whole point of the local stack is that you can run anything against it without worrying about
colliding with a teammate's or another Claude session's run.

**Getting `.env.local.dev-backup`**: it's your own copy, not checked in. Ask Asjid for read-only
dev credentials if you don't have one, or copy `SUPABASE_URL`/`SUPABASE_SERVICE_ROLE_KEY` from
wherever the team keeps them and save as `.env.local.dev-backup` at repo root.

**The API key gotcha, permanently**: every entry point in this suite calls `load_dotenv(...,
override=True)` specifically so a personal `OPENAI_API_KEY` exported in your shell cannot
silently shadow the repo's working key. `provenance.py`'s `key_fingerprint()` /
`shadowed_key_warning()` are the belt-and-suspenders check — every report header now names which
last-4-characters key actually ran, and warns if it doesn't match `.env.local`. If you ever add a
new script that reads an API key, copy this pattern; don't assume it's someone else's problem.

---

## 3 · Gotchas that will cost you an hour if you don't know them

- **Worker port.** `scripts/run-lana-worker-local.sh` defaults to port **8081**. Everything else
  in this repo (including the shared `LANA_BASE_URL` default and backend's own tests) assumes
  **8000**. If you started the worker with that script and didn't set `LANA_BASE_URL=http://
  localhost:8081`, every live-HTTP eval run will fail on turn 1 against a dead port.
- **The worker's launch-script credential guard is broken for local runs** (confirmed by Asjid,
  fix in progress as of 2026-09-17): it checks `OPENAI_API_KEY OR GCP_VERTEX_PROJECT` while the
  provider is pinned to `openai` — so an empty `OPENAI_API_KEY` in `deploy/lana-worker.env`
  passes the guard silently, and the worker runs entirely on the canned fallback path with no
  error. **Prod is unaffected** (its key is set). If you're running the worker locally and
  answers look suspiciously generic, check `deploy/lana-worker.env`'s `OPENAI_API_KEY` is
  actually filled in.
- **No local Vertex/Google ADC by default.** Any code path calling `vertex_embed()` — authority's
  `concepts_for_ask` fallback, `decide_turn`'s claims ranking, anything embedding-based in
  `subject_eval` — will raise `DefaultCredentialsError` until you run `gcloud auth
  application-default login`. The suite degrades gracefully around this everywhere it's been
  found, but the affected feature is simply unmeasured until you fix it.
- **`extract_entities_from_message` swallows BOTH its OpenAI and Vertex failures and returns
  `[]`.** A dead key, a quota error, and "this message genuinely had nothing to extract" are
  indistinguishable at the call site. `n2_extract_baseline.py` has an all-empty-run guard (exits
  3, refuses to report "100% stability" on universally-failed calls) specifically because this
  bit us once. If you build a new script touching this function, copy that guard.
- **`local_signals` has zero embedded rows, of any intent, in BOTH the local stack and dev**
  (confirmed 2026-09-17). `attester_authority()`'s entire behavioural component (up to 0.40 of
  1.0) is currently unreachable for every real user, independent of anything else — this is a
  data-population gap someone on the backend side needs to know about, not an eval bug.
- **Destructive writes are gated twice, and both gates must pass.** `local_guard.require_local()`
  classifies the target and refuses non-local hosts by default (fails closed on anything it
  doesn't recognize); `SIM_ALLOW_WRITES=1` is a SEPARATE flag layered on top ("I meant to write"),
  not a replacement for the local check. `SIM_ALLOW_NONLOCAL_WRITES=1` is the explicit, banner-
  printing escape hatch for CI's serialized dev runs — never set it for an ad-hoc local session.
- **Git is the human's job here** (per `CLAUDE.md`) — don't auto-commit or auto-push unless
  explicitly asked. When asked, batch by logical unit (one pipeline or one clearly-scoped fix per
  commit), terse one-line messages matching the existing log style, no attribution trailers
  unless the user's standing instructions say otherwise.
- **`out/` directories are gitignored** in every pipeline — full per-trial reports live there but
  never get committed. If you want to hand someone a specific run's detail, copy the file or
  paste its content; don't expect `git log` to have it.

---

## 4 · Mid-flight work — pick up here

**D11 calibration (started 2026-09-17, in progress).** `d3_resolve.py` mines 204 "variant"
candidate pairs (same real-world thing, worded differently, vs. different things sharing a
word) into `out/d3_variant_candidates.json` — this is the hard class the resolver exists for,
as opposed to D3's headline 99.9% which only measures the trivial identical-vs-random classes.

Status as of 2026-09-18: `d11_label_assist.py` ran a model-assisted first pass on all 204 pairs
(`out/d11_model_labels.json`), a 30-pair blind stratified audit sample was hand-labelled and
scored at **93% agreement** with the model (`--score`) — comfortably above the 85% trust bar.
`d11_calibration.py` then scored the resolver's actual merge/new decisions against this ground
truth for the first time ever:

- τ_merge=0.86 → **84% precision** on pairs it actually merges (CI 65-94%, n=25 — small sample,
  wide interval, worth re-measuring as more pairs get labelled). Meaningfully below the 90% D3
  gate target. Reaching 95% precision on this pool would need τ_merge≈0.96 — NOT a recommendation
  to move it there, since that would push most of the current 0.86-0.96 band into ADJUDICATE
  (already 57% of this hard class), a real cost tradeoff, not a free win. Does NOT contradict
  Asjid's separate finding that lowering τ_merge is unsafe (the husband/wife pair at 0.8575) —
  both are true at once: 0.86 has real error on this pool, and going lower would make it worse.
- τ_new=0.62 → 95% precision, solid, with headroom down to ~0.42 if a lower τ_new is ever wanted
  for other reasons (e.g. shrinking the adjudicate band).

**Next step, not yet done:** this calibration result needs a decision from whoever owns τ_merge
(Tim, or whoever inherits that role) — raise it, leave it, or explicitly accept the ~16% merge
error rate as a known cost. Full detail in `out/d11_calibration.md` after running the script.

**Per-class thresholds** (contract v2 decision #6 — do people-entities and business-entities
need different τ?) are **not answerable from this corpus** — `latent_signals` only holds Layer-3
entity mentions (activities/interests), no person/business distinction exists in the data. Needs
either the real `subject` table (unshipped) or a differently-scoped labelling pass.

---

## 5 · History worth knowing before you repeat it

Full detail is in git log (`git log --oneline -- services/lana-worker/simulations/`) and in
`LANA_EVAL_STATUS_REPORT.md` if it still exists at repo root when you read this (it's an
uncommitted snapshot, not durable — check its date before trusting any number in it, and re-run
rather than assume it's current). The condensed lessons:

### 5.1 · The 429 mystery (2026-09-15/16)

A personal `OPENAI_API_KEY` exported in a shell, with no credits, silently shadowed the repo's
working key for the ONE script that didn't `load_dotenv(..., override=True)`
(`n2_extract_baseline.py`). Every call 429'd; the extractor swallowed the failure; the harness
reported "100% stability" across 90 calls that had all failed identically. Fixed with an
all-empty-run guard (exit 3) and, more durably, with `provenance.py`'s key-fingerprinting so
every report now states which credential actually ran. If a run ever looks suspiciously perfect
or suspiciously empty, check the provenance line first.

### 5.2 · Vacuous gates, twice

D5's original metric (mean questions to reach P>0.85) was mathematically incapable of failing —
a loop hard-capped at 2 questions can never average above 2.0. Fixed to "% of asks reaching
P>0.85 within the cap." Separately, a resolver reference implementation's naive tie-break
(`if u > best_u`) handed every all-missing-data matrix a "confidence 1.00" on an arbitrary pick.
**Lesson generalized**: any metric that can max out by construction, or that averages agreement
without first checking there's something to agree about, is not measuring what it claims to.

### 5.3 · False positives from fixtures that were too clean

T6's honest-empty checker passed its own fixtures and then hit 6 false positives in one day
against real shipped copy — markdown bold breaking a noun-class regex, a two-clause sentence
("no X near you — though there's some in Y") read backwards. **Lesson**: a mechanical checker
isn't trustworthy until it's been run against real product output, not just fixtures its author
imagined. This is why D3/N1/N2/band-spread/T7 are all built against real data from day one now.

### 5.4 · This pull unblocked real things, but not everything it looked like

`attester_authority()` shipped (ASJID-5) — genuinely real, T3 built against it same-day. But its
own migration comment claims authority "reorders inside the ±0.05 topic band," and **no code
anywhere enforces that** — the one live caller (`app/tip_ask_route.py`) sorts purely by
`(-authority_score, distance)`, no topic term at all. A shipped comment describing an invariant
is not the same as shipped code enforcing it — always verify, never take a docstring's word for
what the code does.

---

## 6 · Who owns what

| Area | Owner | Ask about |
|---|---|---|
| Backend / DB / migrations | Asjid | `attester_authority()`, subject-graph schema timeline, `LANA_LATENT_EXTRACT` prod state, the launch-script fix |
| Ranking / retrieval formulas | Pouya | C2/C3 topic-vs-profile weights, `search_events_semantic()` (shipped, unwired — he was about to build C3, point him at reusing it) |
| Product decisions | Tommaso | §6.1 (does authority ever outrank topic unbounded — one live surface already answered this by omission, worth escalating), the τ_merge precision/cost tradeoff (§4) |
| AI / prompts | Yunchao, Aki | Prompt-level fixes if an eval finding traces to prompt wording rather than logic |

---

## 7 · If you're a Claude session picking this up cold

1. Read this file and `CLAUDE.md` fully before running anything.
2. Check whether `.env.local` (local stack) is actually reachable — `curl http://127.0.0.1:54321/
   rest/v1/`. If not, follow `LOCAL_STACK.md` before trying any "official" run.
3. `python selftest.py` in each pipeline directory to confirm the suite itself is healthy before
   trusting any new finding you produce.
4. Never patch `app/` or `supabase/` from here, no matter how small the fix looks. Write it up.
5. When in doubt about whether something is a real product bug or an artifact of this
   environment (missing Vertex creds, sparse local seed data, a world-not-injected scenario),
   say so explicitly rather than picking one — this suite's credibility depends on never
   overclaiming a finding, and every report in its history that got this wrong had to be walked
   back later.
