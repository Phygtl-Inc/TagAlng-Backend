# PR15 · `rapport_events` — the two-axis maturity log

**Target repo:** `Phygtl-Inc/TagAlng-Backend`
**Migration:** `supabase/migrations/20260917180000_rapport_events.sql`
**Environment:** Supabase `kmetmatfxdkrialwrnzj` (**tagalng-prod**)
**Status:** specification — verified against prod inside `begin; … rollback;`, **nothing applied, no migration run**
**Spec:** `LANA_MATURITY_MODEL_v2.md` §7 (architecture) · §4 (Axis A) · §5 (Axis B) · §8 (analytics) · §9 (open decisions)
**Handover ref:** `HANDOVER_CLAUDE_CODE.md` T5

---

## 1. What this is for

`LANA_MATURITY_MODEL_v2.md` §7, verbatim: *"Event-sourced, both axes as projections. Do not store stage as a column — it drifts and can't be replayed."*

The model has **two orthogonal axes**, and the whole point of v2 over v1 is that they are not one axis:

| | what it measures | direction |
|---|---|---|
| **Axis A** product maturity | what the user can do and has got | **ratchet** — forward only. *"you don't un-meet someone"* (§4) |
| **Axis B** relationship depth | what Lana may ask and how she may speak | **bidirectional** — earned per turn, and it decays (§5) |

The cross-product names states the current schema cannot see at all — most importantly the **blocked confidant** (low A, high B): *"maximum disclosure, zero payoff… High churn, and it's our fault, not hers."* Prod sits almost entirely in that bottom row: **766 messages to Lana, 1 between neighbours.**

This migration is the log both projections read. Nothing else.

## 2. The pattern already exists — this mirrors it

§7: *"You already have the pattern. `relationship_tier_events` does exactly this for peer↔peer tiers. Mirror it for user↔Lana."* Read on prod before writing anything:

```
relationship_tier_events(id uuid pk default gen_random_uuid(), user_low, user_high,
                         viewer_user_id, from_tier, to_tier, trigger_event text,
                         proof_id uuid, created_at timestamptz not null default now())
  FKs:      all three user columns ON DELETE CASCADE
  RLS:      enabled, 2 policies
            tier_events_select_involved  SELECT  (auth.uid() in the pair or the viewer)
            tier_events_no_client_write  ALL     using(false) with check(false)
  Indexes:  (user_low, user_high, created_at desc)
```

Everything below follows that shape. The deviations are named in §4.

## 3. Shape

The base shape from §7, adapted to what prod actually has (`lana_sessions.id` and `lana_messages.id` are both `uuid`, confirmed):

| column | type | note |
|---|---|---|
| `id` | `uuid pk default gen_random_uuid()` | |
| `user_id` | `uuid not null → users(id)` | **ON DELETE CASCADE** — see §4 |
| `session_id` | `uuid → lana_sessions(id)` | **ON DELETE SET NULL** |
| `turn_id` | `uuid → lana_messages(id)` | **ON DELETE SET NULL** |
| `axis` | `text not null` | `product` \| `relationship` |
| `event_type` | `text not null` | constrained **per axis** |
| `sensitivity` | `smallint` | R0–R3, Axis B only |
| `delta` | `real` | signed; Axis A must be `> 0` or null |
| `evidence` | `jsonb not null default '{}'` | claim id, gap id, quote ref |
| `created_at` | `timestamptz not null default now()` | |

### The four CHECKs are where the orthogonality lives

A `text` column named `axis` next to a `text` column named `event_type` is two free-text fields, not two axes. Four constraints turn the model into schema:

1. `axis_check` — `axis in ('product','relationship')`.
2. `event_type_check` — the vocabulary is **partitioned by axis**. Without it `('product','ABANDONED')` is insertable, `get_product_stage()` ignores it, and a regression signal is lost while the funnel reads clean.
3. `product_ratchet_check` — `axis <> 'product' or delta is null or delta > 0`. §4: *"Regression: none. Product maturity is a ratchet."* A negative Axis-A delta is a writer bug, not a value.
4. `sensitivity_check` — `0..3`, and **null on the product axis**. Sensitivity is an Axis-B concept (§6: `sensitivity(question) <= relationship_depth`); it is meaningless on a product transition.

### `rapport_events_product_once_idx` — the ratchet at write time

```sql
create unique index … on public.rapport_events (user_id, event_type) where axis = 'product';
```

A product transition is a **one-time stage crossing**. A replayed `FIRST_VALUE` is a duplicate, not a second activation, and the partial unique index makes the worker's emit idempotent for free (`on conflict do nothing`).

**Deliberate consequence, stated so a reviewer can object:** the log therefore cannot count *"hosted 4 events"*. That is correct. Repeat contribution belongs in §7's **value ledger**, not in the transition log — conflating outcomes with transitions is precisely what made the v1 model *"a linear checklist, not a maturity model"* (§1 error 2). If you want the count, say so and it becomes a second table, not a relaxed index here.

## 4. The two things this PR is most careful about

### 4.1 RLS **with policies**, from the first commit

`circle_affiliations` has RLS enabled and **zero policies** — deny-all by accident. PR #119 §3 audited thirteen tables in that state and flagged four (`circle_affiliations`, `zip_unlock`, `lana_feedback`, `feature_requests`) as needing a recorded intent decision.

This table ships with both policies in the same file:

```sql
rapport_events_select_own      for select to authenticated using (user_id = auth.uid())
rapport_events_no_client_write for all    to authenticated using (false) with check (false)
```

Permissive policies OR together, so `SELECT` resolves to `(false OR user_id = auth.uid())` and every write path resolves to `(false)`. Identical to `rapport_gaps` and `latent_signals` in prod today. Plus the PR #119 REVOKEs as defence in depth.

### 4.2 No ninth `ON DELETE NO ACTION` edge

PR #126: nine FK edges into `public.users` are `ON DELETE NO ACTION`, and two of them (`places.created_by`, `events.host_id`) abort the whole anonymous-user sweep with `23503` — *"One un-sweepable user stops the sweep for every user."*

Every FK here names its behaviour, and the reasoning differs per column:

| column | behaviour | why |
|---|---|---|
| `user_id` | **CASCADE** | The sweep deletes from `auth.users` and trusts the cascade. NO ACTION here would raise `23503` and abort the sweep for *every* user. Matches `rapport_gaps.user_id`, `circle_affiliations.user_id`, `relationship_tier_events.*`. |
| `session_id` | **SET NULL** | A rapport event is a fact about the **user**, not the session. Purging a session must not erase maturity history and must not block. Matches `rapport_gaps.opened_from_message_id`. |
| `turn_id` | **SET NULL** | Same. `lana_messages` cascades from `lana_sessions`, so a session purge nulls this without touching the event. |

**This is verified, not asserted** — see the `SWEEP` probe in §5.

## 5. Verified on prod, rolled back

Every statement ran on `kmetmatfxdkrialwrnzj` inside `begin; … rollback;`. **Nothing was applied.** The complete migration body was run, then **re-run a second time in the same transaction** to prove idempotency; the second pass raised no error.

Fixture: one synthetic anonymous `auth.users` account (the PR #126 pattern), its trigger-created `public.users` row, and events on both axes.

```
PASS 1 applied without error
PASS 2 (re-run) applied without error — idempotent
structure · rls=true · policies=2 · checks=4 · indexes=5
fk on delete · session_id=n  turn_id=n  user_id=c        ← no 'a', no ninth NO ACTION edge

fold · after IDENTIFIED            -> P1
fold · after FIRST_VALUE           -> P2
fold · after CONTRIBUTED           -> P5
fold · after out-of-order GROUNDED -> P5   (ratchet holds: no downgrade)
axis B · 3 rows written (ABANDONED twice — repeatable, unlike product)
         signed sum = -2 · product stage unchanged -> P5

NEG product+ABANDONED         rejected 23514
NEG product delta=-1          rejected 23514
NEG sensitivity on product    rejected 23514
NEG sensitivity=9             rejected 23514
NEG duplicate FIRST_VALUE     rejected 23505

SWEEP · delete from auth.users OK (no 23503) · rapport rows left = 0
```

Three of those lines are the ones that matter:

- **`after out-of-order GROUNDED -> P5`** — the ratchet is a property of the *fold*, not a discipline the writer has to keep. Events may arrive late, replayed, or backdated; the stage cannot move down.
- **`ABANDONED twice … product stage unchanged`** — the axes are genuinely independent. Axis B took `-2` of signed weight in the same breath that Axis A stayed at P5. That is the *utility user* / *blocked confidant* distinction §3 says we currently cannot see.
- **`SWEEP … OK`** — the delete that raises `23503` on `places.created_by` today does **not** raise here.

After rollback, prod is unchanged:

```
to_regclass('public.rapport_events') = null
get_product_stage                    = 0 rows in pg_proc
probe auth.users row                 = 0
auth.users = 35 · public.users = 35   (untouched)
```

> ⚠️ Reproduction note, inherited from PR #126: keep the DDL, the DML and the assertions as **separate statements**. An earlier pass here collected probe output into a `text[]` inside one `do` block and produced a spurious `22P02` on the sweep branch that had nothing to do with the schema. The transcript above uses a temp table and is clean.

## 6. What this unblocks — precisely, and not more

Two specs currently emit `error` on this table's absence:

| Assertion | Today | After this lands |
|---|---|---|
| `SPEC_X1_MEMORY.md` **D05** — *"Relationship depth (R0–R3) is derivable for the persona"* | `error`, `note="rapport_events_absent — depth not derivable"` | **Still `error`**, but the note changes to `rapport_events_empty — no writer`. |
| `SPEC_X3_HONESTY.md` **R08** — *"no `M-NOTICE` at any turn where derived depth < R2"* | `error`, same note | **Still `error`**, same reason. |
| **RAPEVT pre-flight gate** — `select to_regclass('public.rapport_events')` | `null` → block **D** / block **R** general form degrade to `error` | **Non-null.** The gate stops firing. |

**Say the quiet part plainly: this migration does not turn D05 or R08 green, and a run that reports them as `pass` after applying it is wrong.** The table existing is necessary and not sufficient — depth is derived by folding *written events*, and nothing writes yet. Both specs are explicit that the failure mode to avoid is synthesis: *"Never synthesise a depth level to make an assertion pass."*

What this **does** deliver:

- the RAPEVT gate probe passes, so X1 block D and X3 block R stop degrading wholesale and only the depth-derived assertions stay `error`;
- `get_product_stage()` gives Axis A a real, replayable derivation — the funnel §8 asks for;
- `FIRST_VALUE` and `ABANDONED`, the two events §8 says to *"instrument first"*, have somewhere to land and an index to be counted by.

X3 **R04** (the conservative floor — *"zero `M-NOTICE` in turns 1–3 of a first-ever session"*) is unaffected either way: it deliberately needs no depth engine, which is why the spec calls it *"the assertion that catches the real defect"*.

## 7. Rollback

```sql
begin;
  drop function if exists public.get_product_stage(uuid);
  drop table if exists public.rapport_events;   -- indexes and policies go with it
commit;
```

Safe unconditionally *while nothing writes*. Once the worker emits events, `drop table` is data loss — at that point drop only the function and leave the log.

## 8. Reviewer decisions

1. **`get_relationship_depth()` is deliberately absent.** The Axis-B fold needs a decay rate and that is §9 open decision 3 (*"how fast does depth fade"*), unratified. I would rather ship no derived read than one carrying an invented constant that later reads as a decision. **Do you want a placeholder with a documented θ, or does Axis B stay a worker-side fold until §9.3 is answered?**
2. **`rapport_events_product_once_idx` makes product transitions single-fire.** Correct under §4, but it forecloses counting repeat contribution in this table. Confirm the value ledger is the right home for that (§7 names it as separate work).
3. **No backfill.** There is no honest source for a historical transition event. 204 sessions and 931 messages exist; inferring `FIRST_VALUE` from them retroactively would be fabrication. Stage starts at P0 for all 35 users on the day this applies. Flagging it because the first funnel chart will look like a cliff and someone will ask.
4. **Migration slot `20260917180000`** sits after the four claimed by open PRs (`…150000` #119, `…160000` #125, `…170000` #126). Re-check at merge time.
5. **`sensitivity` is `smallint`, not an enum.** §5's R0–R3 is a small ordered scale and an enum would need a migration to extend. Say if you want `rapport_depth` as a real enum type for parity with `user_relationships`' tier enum.

**Do not merge or apply without Asjid's review. No migration has been applied.**
