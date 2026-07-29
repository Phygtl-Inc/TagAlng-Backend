# Circles + ZIP-Unlock — sim/eval harness

Parameter-tuning + sensitivity harness for the onion matcher (§C) and ZIP-unlock state
machine (§D) from `LANA_CIRCLES_ZIP_MASTER_v1.md`, runnable today against a
spec-accurate stub, swappable to Asjid's real backend later via one file.

**Phase 1 IS built** — reconciled with Asjid 2026-07-28. It lives on branch `circles`
(migrations `20260906`/`20260907`/`20260908`, worker in `services/lana-worker`, live in
dev), which is why it isn't visible from this branch. The original "nothing exists yet"
claim was wrong: the earlier grep missed it only because the as-built names diverge from
the spec — the table is **`zip_unlock`** (not a `zips` alter), places live on
**`place_ref → places(id)`** (not inline `place_id/place_name/lat/lng` columns), and there
is no literal `ZIP_OPENED` constant.

**REV-2 (Asjid 2026-07-28): the onion matcher (§C) is now BUILT + wired + DEPLOYED.** The
rev-1 "genuinely-unbuilt, separate owner, keep the stub as the reference" note is LIFTED. §C
shipped on branch **`main`** (PR **#107/#114**), migrations **`20260913`** (`count_shared_
concepts_for_user`) + **`20260914`** (the scoring RPC `score_onion_candidates_for_user`), and
the prod worker is live (**Cloud Run rev 00010**). The scoring is:

```
score        = circle_bonus + shared_concept_count
circle_bonus = MAX over circle pairs of (same place_ref → +3) ELSE (same circle_type → +1)   # MAX, NOT sum
shared_concept_count = COUNT(DISTINCT shared concept_ids) over PUBLIC, non-dismissed claims
candidate set = status='confirmed' AND dismissed_at IS NULL AND place_ref IS NOT NULL   # grounded
order = score desc, then nickname asc ;  p_limit default 20 clamp [1,50] ;  p_min_score 1
NO proximity term (users store no coordinates) ;  blocked pairs excluded (lana_is_blocked)
```

`stub_impl.py` now **MIRRORS** this algorithm exactly — it is the **parity target** for the
live RPC, not a placeholder for absent code. Matcher measurements are now **parity checks**
(not just spec-sensitivity); offline re-weighting of the RPC's returned component columns
(place/type/concept) is the intended tuning loop. The harness still runs against the stub
only because `main` isn't reachable from here; it becomes a full regression tool the moment
`SIM_BACKEND=live` is wired (see below).

**Two live caveats.** (1) *Concept-arm dormancy:* the concept arm reads `claim_concept_links`,
populated only when `IDENTITY_CONCEPT_LINK_ENABLED` is on AND the `20260905` backfill has run
(**PR #96 pending**) — until then every real score is circle-arm only (flag-off, not a bug;
model with `ScoringConfig.concept_arm_enabled=False`). (2) *Prod-DB lag:* prod DB still lacks
`20260913/14`, so the serving blend **FAILS OPEN** in prod (find-peers list byte-identical to
pre-onion) until that push — **dev has everything**. This is the standing prod smoke item.

## Run it

```bash
cd services/lana-worker/simulations/circles_zip
python sweep.py            # full sweep + report → out/report.md, out/*.png
python sweep.py --quick    # smaller population, faster — for iterating on the harness itself
```

Needs `matplotlib` for the plots (falls back to text-only tables if it isn't
installed — nothing breaks either way).

## File layout

| File | What it is |
|---|---|
| `ports.py` | **The swap seam.** 4 Protocols (`MatcherPort`, `DisclosurePort`, `AreaStatePort`, `CircleActivationPort`) + every data model/config that crosses them, now shaped to the as-built schema. Read this first — every field is commented as confirmed or `# RESOLVED (Asjid 2026-07-28[ rev-2])`. `ScoringConfig` reframed as offline re-weighting; `RankedCandidate` carries the RPC's component columns (`same_place_bonus`/`same_type_bonus`/`shared_concept_count`/`shared_concept_labels`/`shared_place_ref`); `BlendConfig` documents the serving/blend seat rule. |
| `stub_impl.py` | Reference implementation of all 4 ports. Pure functions, no I/O. **Faithful parity mirror of the deployed §C onion algorithm** (MAX-not-sum circle bonus, confirmed+grounded-only candidates, concept arm gated for dormancy, order score desc/nickname asc, clamp [1,50]); its `transition_zip` is already a recount, matching the as-built `recount_zip_unlock`. |
| `live_impl.py` | Shells for the same 4 ports pointing at the **real backend**. `score_matches` now points at the SQL RPC `score_onion_candidates_for_user` (ungated, service-role) **or** the gated wrapper `app.onion.score_onion_candidates()` — no longer blocked (still raises `NotImplementedError`: no dev DB reachable here). `transition_zip`/`is_active` → real POST endpoints; `filter_by_tier` → structural passthrough. This is the *only* file to edit when wiring live. |
| `backend.py` | `SIM_BACKEND=stub\|live` switch. `sweep.py` never imports `stub_impl`/`live_impl` directly — only through this. |
| `population.py` | Synthetic mom generator. Every distribution parameter is named on `PopulationConfig` and is part of the sweep surface. Seeded. Now also generates durable **accepted intros** (see #4 below). |
| `sweep.py` | Measurements + the parameter sweep + report rendering. Entry point. |

## What gets measured (and which spec acceptance criterion it checks)

- **Onion scoring parity** (rev-2 §C) — a hand-seeded fixture asserting the stub reproduces the deployed algorithm: **MAX-not-sum** circle bonus (a peer sharing both a place and a type scores 3, never 4), only **confirmed+grounded** rows score (suggested/dismissed/ungrounded never do), order **score desc / nickname asc**, **p_limit clamp [1,50]**, **p_min_score** filter, and the **concept-arm dormancy toggle**. This is the stub↔built regression gate.
- **Match coverage + component breakdown** — % of moms with ≥1 match, bucketed by the match's strength. (The `R1-R5` labels are a cosmetic §C.1 artifact for continuity; the built matcher emits no ring.)
- **Day-zero floor** (§C.4) — a dedicated fixture proving a lone pioneer (1 grounded circle, 0 co-members) still gets ≥1 match — under the built algorithm a **type-only floor row** (shared circle_type, score +1 ≥ p_min_score), with the score-0 stranger excluded. (Adjacency is now irrelevant — the built matcher has no zip/proximity scoping.)
- **Circle activation rate** (§B.2/U3) — % of grounded places that cross the activation threshold. Mirrors the as-built derived-at-read `active = member_count ≥ 2`.
- **Founding-eligibility vs. raw invite volume** (§E.5/§I) — buckets moms by how many people *they* invited and checks founding-eligibility rate stays flat across buckets, using synthetic invite-spam agents (rarely verified, rarely active). Founding now = **not yet open** (closed or warming) + verified + ≥1 confirmed thing (circle **or accepted intro**).
- **Disclosure correctness** (§F.3) — every grounded circle checked at `stranger` tier; hard-fails if `place_ref`/`place_name` ever leaks. This is a **stub reference** check; in prod §F is enforced structurally (RLS + serializers), so the live disclosure port is a passthrough.
- **Unlock-timing distribution** — a temporal day-by-day recount of one zip under varied arrival rates, reporting days-to-open per rate.

## What's swept

`unlock_threshold` (U1, per-ZIP), `activation_threshold` (U3), scoring weights
(`exact_place_weight` and friends — offline re-weighting of the RPC's returned columns),
**concept-arm dormancy** (`concept_arm_enabled` off = current prod / on = post-backfill),
**concept-overlap density** (`affinity_vocab_size`), population density per ZIP,
circles-per-mom distribution (sparse vs. rich), invite conversion rate — each re-runs the
full measurement set.

## Knobs the harness parameterizes (rev-2 onion additions)

| Knob | Where | Value / default |
|---|---|---|
| `LANA_ONION_MATCHER` | `GateConfig.onion_matcher_enabled` | ON, **fails open** — gates the serving/blend path only; the scoring RPC itself is **unflagged**. |
| onion weights place · type · concept | `ScoringConfig.exact_place_weight` / `same_type_weight` / `affinity_weight` | **3 · 1 · +1** — SQL **constants** server-side (`circle_bonus = MAX(place, type)`, not sum); re-weightable **offline** via the returned component columns. |
| `p_limit` / `p_min_score` | `ScoringConfig.n_results` / `p_min_score` | 20 (clamp **[1,50]**) / **1**. |
| blend serve cap / labels per card | `BlendConfig.serve_cap` / `labels_per_card` | **5** rows to the turn / **≤3** concept labels (RPC returns ≤50). |
| concept-arm dormancy | `ScoringConfig.concept_arm_enabled` | default True (algorithm); False reproduces current-prod (needs `IDENTITY_CONCEPT_LINK_ENABLED` + `20260905` backfill, PR #96). |

## Invariants documented for the live wiring (parity targets, not yet exercised on stub)

- **Gate-in-wrapper** — the RPC `score_onion_candidates_for_user` **never** enforces the ZIP gate; `app/onion.py :: score_onion_candidates()` enforces the §D.2 peers gate **before** the RPC and **fails open** on gate errors. Any caller hitting the RPC directly bypasses the gate. Hard-gate mode → `gated:true`, RPC never called.
- **Blend seat rule** — an onion peer **not** already vector-matched earns a find-peers seat **only** when `same_place_bonus > 0` AND the caller has a confirmed circle at that place. **Concept-only or type-only strangers never earn a seat; same-place strangers do.** Onion-only rows keep `similarity_score: null` (no invented cosine). The disclosure tag is caller-relative and **never a place name**.
- **Fail-open** — with `LANA_ONION_MATCHER` off/erroring (or prod DB lacking `20260913/14`), the find-peers list is **byte-identical** to the pre-onion vector-only list.

## The 8 guesses — ALL RESOLVED (Asjid 2026-07-28, rev-2)

Flagged inline in `ports.py`/`stub_impl.py`/`population.py` as `# RESOLVED (Asjid ...)`.
**ZERO remain open** — rev-2 answered the last two (#1, #7), which had been blocked on the
onion matcher. Summary:

1. **Proximity/distance formula** — *RESOLVED (rev-2): there is NO proximity term at all.* Users store no coordinates; locality lives in the **serving layer**, not the matcher. The rev-1 placeholder is neutralized (contributes a hard 0.0). Nothing to tune.
2. **ZIP adjacency ("nearby blocks")** — *RESOLVED: no counterpart.* No adjacency concept exists anywhere; the gate keys on exact `users.home_zip`, and `zip_centroids` is just a seeded market registry. The synthetic ring is not invested in.
3. **`circle_key` stand-in** — *RESOLVED: keep, reframed.* Real `circle_key` is a per-user slug (`^[a-z][a-z0-9_]{1,63}$`) that dedupes one user's mentions — it is **not** the cross-user join key. Membership joins on `place_ref`; semantics live in the 768-dim embedding column. Asjid: the `place_id` stand-in (now `place_ref`) models the real join *better* than real circle_keys would — kept.
4. **"Or intro" branch for U2's active count** — *RESOLVED: MODEL IT (correctness fix).* Intros are **durable** rows (`intros`, `status='accepted'`, `initiator`/`candidate`), counted by both the recount and the founding stamp. The population now generates accepted intros and the count includes moms with ≥1 accepted intro (not only ≥1 confirmed circle), so it no longer undercounts vs prod.
5. **"Session ≤30 days"** — *RESOLVED: equivalent.* Real check is `EXISTS a lana_sessions row with created_at > now − 30d`. There is no `users.last_active_at` column; the harness models the most-recent session timestamp. Behaviorally equivalent.
6. **Founding's "warming or within first-N"** — *RESOLVED: adjust.* "Not closed" → **"not yet open"** (closed or warming) + verified + ≥1 confirmed thing. The stamp fires for everyone qualifying at the instant of the open transition, inside the recount txn. No first-N ordinal tracking exists or is needed.
7. **Match page size (`n_results`)** — *RESOLVED (rev-2).* The RPC's `p_limit` default is **20**, clamped to **[1,50]**; `p_min_score` default **1**. The serving/blend layer fetches `max(20, limit·4)` candidates, returns **≤5** rows to the turn, and merges **≤3** concept labels per card.
8. **Disclosure tier collapsing** — *RESOLVED: confirmed.* Below Direct nothing place-identifying leaves the worker; no per-tier data variation. §F is enforced by RLS + serializers, not per-request logic.

## Swapping to the real backend later

The real surface is **all POST, all authed**. Wire in `live_impl.py`:

1. `transition_zip` → **POST `/lana/area/progress`** (recounts on read; returns `{zip5, state, count, threshold, is_founding_eligible, founding_earned, founding_area}`), or call the `recount_zip_unlock(zip5)` RPC directly with a service key. To move a test population's count you **seed rows** (users, `lana_sessions`, `circle_affiliations`, `intros`) and recount — the function is pure over the DB.
2. `is_active` → **POST `/lana/circles/mine`** — read the derived `active` field (`member_count ≥ 2`) off each circle row.
3. `filter_by_tier` → **structural passthrough** (RLS + serializers enforce §F; tier-gating applies to *others'* views, not the user's own `/mine`).
4. `score_matches` → **BUILT (rev-2)** — the SQL RPC `score_onion_candidates_for_user(p_user_id, p_limit=20, p_min_score=1)` (migration `20260914`, service-role) for raw scoring, **or** the gated wrapper `app.onion.score_onion_candidates()` (enforces the §D.2 peers gate, fails open) for the product path. There is **no HTTP route** — onion matches are consumed inside the find-peers chat turn (`discovery_route._fetch_verified_peer_matches` → `app.onion_blend.blend_onion_matches`). Drive it from the harness by calling the RPC (raw) or wrapper (gated) directly with a service key.

Other real endpoints in the surface: `/lana/circles/add|update|remove` (profile CRUD;
remove is soft-delete), `/lana/circles/ground-options` and `/lana/circles/ground` (the only
promotion path to `status='confirmed'`), `/lana/invites/mint|redeem|self-confirm` (redeem
also triggers the ZIP recount), and `/lana/rapport/next-ask` (grounding asks carry
`kind:"place_grounding"`).

Then: `export SIM_BACKEND=live` and re-run `python sweep.py` — no other file changes.
