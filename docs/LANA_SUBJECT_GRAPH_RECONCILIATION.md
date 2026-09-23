# `reco_subjects` vs contract v2's subject graph — reconciliation

2026-09-23. Written after discovering, mid-build, that contract v2 specifies a subject graph
(`subject` / `attestation` / `subject_context()`) which `reco_subjects` overlaps.

**Read this before merging `recommendation-new`.** The question it answers is not "is the
code right" — it is "is this one subject graph or two".

## TL;DR

Three resolvers now exist or are specified in this codebase, at three different layers.
They are **not duplicates**, but two of them are called "subject" and none of them agree on
a threshold. The concrete asks:

1. Decide whether `reco_subjects` **is** contract v2's `subject` table or a sibling. This
   is a naming-and-roadmap call, not a code call.
2. `reco_cards` should call the **already-shipped** `best_authority()` and does not.
3. `reco_subjects` **unblocks contract v2 decision #6** (per-class τ), which HANDOVER
   records as un-answerable from the current corpus.
4. This machine's `.env.local` points at **prod**, where the sim suite expects **local**.

---

## 1 · The three resolvers

| | layer | data | signal | thresholds | status |
|---|---|---|---|---|---|
| **A. concept resolver** | identity claims | `identity_concepts`, bucketed | embedding + LLM verifier (`match_concepts_by_embedding` → `resolve_cross_concept_match`) | `_concept_min_sim()` | **shipped** |
| **B. contract v2 D3/D11** | subject graph | `latent_signals` entity text | embedding cosine | τ_merge **0.86** / τ_new **0.62** | **spec + evals only**, table unshipped |
| **C. `reco_subjects`** | recommendation subjects | `reco_name` + Google place id | exact place id, else name string (`SequenceMatcher` + containment) + LLM adjudicator | auto **0.93** / adjudicate **0.80** / Google floor **0.82** | **built, unmerged** |

They genuinely resolve different things. A concept ("gym enthusiast"), a latent interest
("mom friends") and a recommended subject ("Dr. Sarah Chen") are not the same kind of
object, and C is the only one with an external identifier — a Google place id, which is a
fact rather than a similarity.

**But B and C are both called "subject", and both stamp a column called `subject_ref`.**
That is the collision, and it is a naming collision more than an architectural one.

## 2 · Where they agree

- Refusal is first class. B has a τ_new band; C has `ambiguous` + `subject_candidate_ref`.
- Precision over recall on merges. Both treat a false merge as the expensive error.
- An LLM adjudicates only the middle band, never the corpus.
- Authority is per-subject and never a global reputation score (§7).

## 3 · Where they conflict

**Thresholds are stated in different currencies.** B's 0.86 is embedding cosine; C's 0.93
is a name-string ratio. They are not comparable numbers and must never be tuned by
analogy — a reader seeing "0.86" and "0.93" in one codebase will assume the second is
stricter. It is not; it measures something else entirely.

**`attestation` vs `local_signals.subject_ref`.** Contract v2 models a contribution as its
own row in an `attestation` table. C reuses `local_signals`, stamping the subject onto the
recommendation that already exists. C's version is cheaper and avoids a second write path;
B's is more general (it can attest to a subject without a recommendation). If the two ever
merge, this is the real schema decision, not the table name.

**C4 elicitation vs C's confirm-ask.** Contract v2 §C4 specifies a particle-filter question
selector with a hard 2-question cap and an `EIG` objective. C's Stage 2 step 4 (deliberately
unbuilt) is the same job done conversationally. **Do not build C's version independently** —
C4 is specified, has 675 synthetic problems behind it in `elicit.py`, and already knows the
failure mode that matters (`confidently_wrong`: reaching the confidence bar on the wrong
candidate).

## 4 · What `reco_subjects` unblocks

HANDOVER §4 records per-class thresholds as blocked:

> **Per-class thresholds** (contract v2 decision #6 — do people-entities and
> business-entities need different τ?) are **not answerable from this corpus** —
> `latent_signals` only holds Layer-3 entity mentions (activities/interests), no
> person/business distinction exists in the data. Needs either the real `subject` table
> (unshipped) or a differently-scoped labelling pass.

`reco_subjects` carries exactly that distinction, three ways:

- `reco_type` separates a **person** (`professional`, `service`) from a **business**
  (`restaurant`, `location`) from an **object** (`product`) from an **artifact**
  (`recipe`, `diy`).
- `merge_mode` separates aggregate from collection.
- `google_place_id` marks the subjects with an external identity.

So the corpus D11 needs is the one this table produces. That is the strongest argument that
these should be **one** graph rather than two.

## 5 · The calibration warning, applied to C

D11's first real measurement of B:

> τ_merge=0.86 → **84% precision** on the hard variant class (CI 65-94%, n=25), below the
> 90% D3 gate target. Reaching 95% would need τ_merge≈0.96, which pushes most of the
> 0.86-0.96 band into ADJUDICATE — a real cost tradeoff, not a free win.

C's 0.93 / 0.80 / 0.82 are **reasoned, never measured.** The one measurement C has is
adversarial-by-construction (`TestBands`), not a precision estimate on a real pool. Given B
came out 6 points below target when finally measured, C should be assumed loose until
someone runs the equivalent.

D11's method transfers directly and should be reused rather than rewritten:
`d3_resolve.py` mines candidate pairs → `d11_label_assist.py` labels them and validates
against a blind hand-labelled sample (85% trust bar; B scored 93%) → `d11_calibration.py`
sweeps. The only new part for C is mining pairs from `reco_subjects` instead of
`latent_signals`.

## 6 · Two defects in what was built

**`best_authority()` is not wired.** `app/authority.py` + `attester_authority()` (migration
20261209120000) shipped 2026-09-17 and answer precisely the merged-card ranking question —
its docstring is the requirement verbatim: *"Show me a Turkish restaurant, but the
recommendation must come from someone from Turkey."* `reco_cards.py` ranks on
`match_strength` and leaves affinity as a "seam". It should call `best_authority()` per
contributor and aggregate with **max** (never mean: extra voices must not dilute a
subject's rank), rendering `evidence_quote` and never the score — §4/A2 is explicit that the
number never reaches the UI.

**No detail endpoint**, so `allow_compose=True` is only reachable from the background warm.
Acceptable for now, but Stage 4 needs the visibility decision anyway.

## 7 · Environment hazard, unrelated but urgent

HANDOVER §2 documents three env files:

| file | should point at |
|---|---|
| `.env.local` (repo root) | **local Supabase stack**, `http://127.0.0.1:54321` |
| `.env.local.dev-backup` | shared dev (`rjlcy…`) |
| `deploy/lana-worker.env` | whatever the worker runs against (currently dev) |

**On this machine `.env.local` resolves to `kmetmatfxdkrialwrnzj` — production.** Anything
run as the suite documents it (`reco_eval`, `policy_eval`, `circles_zip`, `rapport`,
`runner.py` all read `.env.local`) would execute against prod, and several of those
**seed accounts and write rows**. Fix before running any simulation.

## 8 · Recommended path

1. **Decide #1 with Tommaso/Tim**: is `reco_subjects` contract v2's `subject`, or a sibling
   scoped to recommendations? If it is the subject table, rename before it has dependents —
   it currently has none outside this branch, which will not stay true.
2. **Wire `best_authority()`** into `reco_cards`. Small, and it closes the standup's
   Turkish-restaurant question with code that already exists.
3. **Fix `.env.local`** to the local stack, per §7.
4. **Run against the local stack** (`LOCAL_STACK.md`) — the first real execution of any of
   this.
5. **Calibrate C** with D11's method, now that `reco_subjects` supplies the per-class corpus
   that B could not.
6. **Do not build the confirm-ask** independently of §C4.
