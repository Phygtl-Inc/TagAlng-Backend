# `reco_eval/` — recommendation capture quality

Evaluates the recommendation flow in the two places it fails independently: the questions Lana
writes for a subject, and the answers the flow accepts into a card.

Built against Asjid Malik's *How Lana decides what to ask about a recommendation* (2026-09-07).
Section references below (`§6`, `§11`, `§12.1`) are to that document. §11 — *"Everything above
is about asking good questions. **Nothing checks the answers.**"* — is the gap Arm B measures,
and the R&D standup of the same day turned it into an action item: *"add an evaluation step to
the report system; ensure the system includes validation for data quality."*

```
cd services/lana-worker/simulations/reco_eval

python selftest.py                                   # offline, no key. Run this first.
python run_eval.py --dry-run                         # offline smoke test of the whole pipeline

python run_eval.py --arm answers                     # ARM B. No key, no server, seconds.
python run_eval.py --arm answers --validator reference   # ...and what a §12.3 validator gets

python run_eval.py --arm questions --backend inproc --trials 5          # ARM A, real generator
python run_eval.py --arm questions --backend inproc --trials 5 --judge  # ...+ the judged axes
python run_eval.py --arm questions --backend static                     # the negative control
python run_eval.py --id q06_barnes_noble --backend inproc               # one fixture
```

Writes `out/report.md`. `out/` is gitignored — do not commit run artifacts.

---

## What it attaches to

| | real code under test | needs |
|---|---|---|
| **Arm A** | `app/tip_share.py:110` `_extract_tip_fields` → `app/reco_question_sets.py:292` `validate_steps` | `OPENAI_API_KEY`. **No DB, no server.** |
| **Arm B** | `app/main.py:2509` `/tip-setup`, `app/reco_question_sets.py:181` `missing_required` | nothing (`shipped`) · a key (`reference`) |

Arm A calls the same two functions the product calls, in the same order, with the same inputs.
Arm B's `shipped` validator calls `missing_required` for real and **mirrors** the two rules that
live inline in a FastAPI endpoint body and cannot be imported — each with its citation, and with
a drift assertion in `selftest.py`, the same way `policy_eval/lingo_guardrail.py` handles the
same problem.

---

## The two arms

### Arm A — what Lana ASKS

Mechanical (14 axes, ground truth by construction):

| axis | § | catches |
|---|---|---|
| `generation_ran` | §1–2 | the set is the type's **static fallback**, not written for the subject |
| `banned_generic` | §6 | "What stood out?", "Anything else?", "Why is it good?" |
| `privacy_floor` | §6 | home address, full name, DOB, income — **wider than `_BLOCKED_ASK`** |
| `google_answerable` | §12.1 | hours / phone / website / address / price band on a listed subject |
| `floor_present` | §5 | the must-haves survived generation, **and sit early not at the end** |
| `tail_integrity` | §5 | the consent question byte-identical; no empty agree row |
| `field_keys` | §9 | storage keys stay snake_case ASCII when the copy is translated |
| `stated_facts` | §6 | a fact the opening line gave, re-asked and not pre-filled |
| `tappable` | §6 | the set offers nothing filterable at all |
| `no_duplicate` | §6 | two questions with the same answer |
| `placeholders` | §6 | an example answer under each free-text question |
| `set_size` | §7 | a card, not an interrogation |
| `language` | §9 | a Spanish speaker handed an English set |
| `reco_type` | — | a type the DB's check constraint would reject |

Judged (`--judge`), because their ground truth genuinely needs judgment:
`filterable` (§6's "three questions a neighbour would filter on"), `lazy_vs_good` (§6's table),
`subject_tailored` (§2's per-subject premise).

### Arm B — what the flow ACCEPTS

21 hand-labelled answers, drawn verbatim from §11's table and the standup's own examples.
Each carries an auditable `reason`; the file is rejected at load time if it has only one label,
because then "accept everything" would score perfectly.

Three validators behind one port:

- **`shipped`** — every check the product applies today and nothing else. The measurement.
- **`reference`** — §12.3's AI validator, built to spec. **Not a proposed patch:** it exists to
  prove the fixtures are scoreable at all, and to give the "Add AI Validator" action item a
  target it can be measured against on day one.
- **`dry`** — no key, no imports, for `--dry-run`.

Metrics: **junk acceptance rate** (the §11 gap), **false reject rate** (must stay 0 — §12.4
rules out telling a neighbour their recommendation was not good enough), accuracy, unscorable.

---

## Three things that will surprise you

**1. Arm A is a rate, never a verdict.** The generator is severely nondeterministic. Identical
calls — same opener, same `prev`, temperature 0.2, model pinned, `LANA_LLM_FALLBACK=0` — return
a set of six questions, or three, or **none at all**. An empty proposal sends `validate_steps`
down `if not middle:` and the neighbour silently receives the type's static questionnaire: §1's
one-form-for-everything, under a flow that records nothing. `--trials` defaults to 3 on `inproc`
and `live` for this reason. A single trial measures sampling noise.

**2. `contact` is a floor field but is NOT required.** §11 treats it as a must-have whose
blank-check is too weak. In the shipped code `required` is assigned to the first **two** floor
fields only (`reco_question_sets.py:367-368`), and `service`/`professional` have contact third.
So it is offered once, and a neighbour who says "that's it, post it" is never asked for it. A
card ships unreachable by two routes, not one.

**3. `({}, None)` from the extractor is an ERROR, not a zero.** `_extract_tip_fields` swallows
every exception (`tip_share.py:191-195`), so a dead key, a 401, a timeout and "the model
returned nothing" are indistinguishable at the call site. `live_impl._ExtractFailureWatcher`
captures `app.tip_share`'s log record to tell them apart. Without it, a broken key would score
as `generation_ran: HARD_FAIL` on every fixture — a false finding against Lana caused entirely
by our own environment.

---

## Backends

| `--backend` | what it is | when |
|---|---|---|
| `inproc` | the REAL generation path, in-process. **Default.** | always |
| `static` | the type's fallback set, deliberately | the negative control — see below |
| `live` | the whole shipped flow over HTTP | rarely; see the caveat |
| `dry` | a clean canned set through the real `validate_steps` | `--dry-run` |

**`static` is the non-vacuity proof.** The static sets literally contain "What stood out for
you?" and, for `location`, "When is it open — and the best time to go?" — §6's first banned row
and §12.1's opening-hours ban. A run against `static` **must** light up `banned_generic` and
`google_answerable`. If it ever comes back clean, those checks have gone vacuous and every green
run above them is worthless. `selftest.py` asserts exactly this.

**`live` is strictly lossy for Arm A** and is not the default. `steps_raw` — the model's actual
proposal, which is what distinguishes "Lana wrote a good set" from "Lana forgot the phone number
and the guards put it back" — never leaves the worker process; the wire carries only the
post-`validate_steps`, post-fallback result. The running worker also has fallback enabled, so a
flaky turn is silently served by Vertex Gemini and the model under test cannot be pinned. It
stays because it is the only proof that what this harness measures is what a neighbour receives.

---

## Conventions

Follows the house rules in `../CLAUDE.md`: mechanical vs. judged, fail closed (`UNSCORED` ranks
above `SOFT_FAIL` and fails `--gate`), an independent judge model, bare intra-package imports,
`# GUESSED` / `# FLAGGED`, and **this suite evaluates — it never patches `app/` or `supabase/`**.

**Verdict policy is deliberately not uniform.** `HARD_FAIL` is only for rules the product states
as absolute and already enforces. `google_answerable` is `SOFT_FAIL` because §12 is titled *"What
we could do about it"* and marks that option *"Already prototyped, not shipped"* — scoring an
unshipped proposal as a hard violation would leave the gate permanently red against behaviour
nobody has agreed to, and a permanently red gate is an ignored gate.

**Every mechanical axis has a planted-violation case in `selftest.py`, and a clean-input case.**
A check that fires on everything is as useless as one that fires on nothing, and more expensive:
it poisons a gate, people mute it, and the real finding underneath goes with it. This suite has
shipped a false `HARD_FAIL` against Lana before. So `banned_generic` bans *"what did you like
about it"* and permits *"what did you like about the crust"*; `google_answerable` never fires on
a type's own floor field or on a `kind=place` step.

## CI

Only the offline half fits the PR gate: `selftest.py` and `run_eval.py --dry-run` run in
seconds, need no secrets and touch no database. Arm A against `inproc` and the judged axes are
nightly. **`PyYAML` is not in `services/lana-worker/requirements.txt`** — the only file the
workflows install — so a step running this harness needs `pip install "PyYAML>=6.0.2,<7"`, and
must not install the worker's and the suite's requirements in the same step (the `openai` pins
disagree: `>=1.55,<2` vs `>=2.50,<3`).
