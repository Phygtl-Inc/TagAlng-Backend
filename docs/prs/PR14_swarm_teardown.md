# PR14 · Swarm teardown — and a defect that would have broken night 1

**Target repo:** `Phygtl-Inc/TagAlng-Backend`
**Migration:** `supabase/migrations/20260917170000_swarm_teardown.sql`
**Environment:** Supabase `kmetmatfxdkrialwrnzj` (**tagalng-prod**)
**Author:** harness build · 2026-07-30
**Status:** specification — verified against prod inside `begin; … rollback;`, **nothing applied**
**Stacks on:** PR #125 (`20260917160000_simulations_harness`)
**Program refs:** `LANA_AUTONOMY_GAPS.md` gap 4 · `LANA_ZERO_BUG_PROGRAM_FINAL.md` §5 · `SPEC_P0_SIGNUP.md` Appendix B · handover T3

> `LANA_AUTONOMY_GAPS.md` gap 4: *"`run_id` tagging is specified but nothing enforces it, and nothing
> sweeps. One teardown function keyed on `run_id`, run at the end of every night, non-negotiable
> before night 1."*

---

## 1. The defect · `cleanup_stale_anonymous_users` deletes nothing the first night a persona says "pin it"

This is the finding, and it is not a hypothetical.

`cleanup_stale_anonymous_users` (from `20260616120000_anonymous_guest_lana`) deletes straight from
`auth.users` and relies on `ON DELETE CASCADE` to sweep the rest. Most of the ~90 FK edges into
`public.users` do cascade. **Eight are `ON DELETE NO ACTION`, and each one blocks the delete:**

| FK column | on delete | reachable by the swarm? |
|---|---|---|
| `places.created_by` | NO ACTION | **yes — every persona** |
| `places.claimed_by` | NO ACTION | yes |
| `events.host_id` | NO ACTION | **yes — P5 Hosting** |
| `place_features.contributed_by` | NO ACTION | yes |
| `circle_affiliations.invited_by` | NO ACTION | yes — P8 invites |
| `event_reports.reporter_id` / `reviewer_id` | NO ACTION | unlikely |
| `thread_events.actor_id` | NO ACTION | yes |
| `users.invited_by` | NO ACTION | yes — P8 |

Two are directly on the swarm's critical path:

- **`places.created_by`** — circle grounding inserts a `places` row owned by the grounding user.
  **All nine personas ground a circle.** `personas.json` PER-01 turn 5 is literally
  `"Yes, that's the one on Tavistock Lakes. Pin it."` with
  `asserts.expect_db_write: circle_affiliations`.
- **`events.host_id`** — P5 Hosting creates an event owned by its host.

### Verified on prod, in a rolled-back transaction

```sql
insert into auth.users (…, is_anonymous = true)          -- what the harness creates
insert into public.places (google_place_id, name, created_by = that user)   -- what grounding does
delete from auth.users where id = that user               -- what the sweep does today
```

```
ERROR 23503: update or delete on table "users" violates foreign key
             constraint "places_created_by_fkey" on table "places"
DETAIL: Key (id)=(00000000-0000-4000-8000-00000000dead) is still referenced from table "places".
```

Identically for hosting:

```
ERROR 23503: … violates foreign key constraint "events_host_id_fkey" on table "events"
```

`cleanup_stale_anonymous_users` has **no exception handling** and returns `integer`. The raised
`23503` aborts the whole call, so **one un-sweepable user stops the sweep for every user** — not
partially, entirely, and silently, from the first night the swarm grounds a circle. Which is night
one of P4, and P4 is scheduled a full night to itself.

Per the handover's *"extend, don't duplicate"*, the fix is factored into one function that **both**
sweeps call. `cleanup_swarm_run` is not a parallel implementation of the same logic.

---

## 2. Why the anchor is identity, not `run_id`

The program's hard rail is *"tag every written row with `run_id`."* At the row level that is **not
achievable through the public API**, and it is worth being explicit about why rather than shipping a
sweep that pretends otherwise.

The swarm drives the worker over HTTP. The worker has no `run_id` concept, so nothing stamps
`run_id` onto the `user_identity_claims`, `local_signals`, `circle_affiliations` or `lana_sessions`
rows it writes on a persona's behalf. Adding it would mean a new field on 34 endpoints.

What the harness *does* know is the identity it minted. Every row the swarm causes is reachable
from that identity by foreign key. So:

`public.swarm_run_actors (run_id, user_id, persona_id, section_id, arm, is_manifest_account, sim_email)`

The harness writes one row per test identity at the moment it creates it. Teardown resolves its
target set by **join**, not by pattern match.

That distinction is the safety property. A sweep keyed on `nickname like 'simqa-%'` or on a
`created_at` window can widen — a real user picking a colliding nickname, a clock skew, a run that
overlaps real signups. **A join against a registry the harness itself wrote cannot reach a user that
was never registered.** With 31 real accounts in prod, that is the difference between a test
fixture and an incident.

---

## 3. What `cleanup_swarm_run` does

```
cleanup_swarm_run(p_run_id text, p_include_manifest boolean default false, p_dry_run boolean default false)
  returns jsonb
```

1. Resolve targets from `swarm_run_actors` for `p_run_id`, excluding manifest accounts.
2. **Guard 1** — raise if the `run_id` has no registered actors.
3. **Guard 2** — raise if any target is neither anonymous nor a `lana-sim+…` address.
4. `swarm_release_user_fk_blockers()` — clear the eight NO ACTION blockers.
5. `swarm_purge_user_residue()` — handle the thirteen SET NULL edges.
6. `delete from auth.users` — cascades `public.users` and its ~50 cascading children.
7. Return a per-table `jsonb` report.

### Signature deviation from the handover

The handover specifies `cleanup_swarm_run(p_run_id uuid)`. **This takes `text`.** `run_id` is
already `text` everywhere it exists — `simulations.run_id text not null` — and the account
convention is `lana-sim+{run_id}-{persona_id}@{domain}` with run_ids of the form `2026-07-31-a`
(`SPEC_P0_SIGNUP.md` Appendix A). A `uuid` parameter could not accept the run_ids the specs define.

### Guard 1 raises rather than returning zero

A mistyped `run_id` returning `{"targets": 0}` is indistinguishable from a clean sweep, and the
nightly log would read green while a night's worth of test users sat in prod. It raises:

```
ERROR P0001: cleanup_swarm_run: no actors registered for run_id 2026-07-31-typo.
  Refusing to run — a silent no-op on a mistyped run_id is indistinguishable from a clean sweep.
```

### Guard 2 catches a harness bug, not a real user

The registry join already makes an unregistered user unreachable. Guard 2 is the second line: it
catches the case where the **harness registered the wrong id**. A test identity is either still
anonymous or carries a sim address; anything else is refused with the offending id named.

Verified with a synthetic non-anonymous account (no real user was involved in the test):

```
ERROR P0001: cleanup_swarm_run: refusing to delete non-test identities under run_id
  danger-run: 00000000-0000-4000-8000-00000000beef (someone@example.com).
  These are neither anonymous nor lana-sim+ addresses. Fix the harness registration
  before re-running teardown.
```

### `places` is deliberately conservative

Six tables reference `places.place_ref` with `ON DELETE NO ACTION`, and **a real user may have
grounded to the same place a persona did** — there are only 5 `places` rows in prod (D-21), so
collision is likely, not theoretical. Deleting the place would then require deleting that real
user's `user_identity_claims` row.

So the place is deleted **only when nothing outside the target set references it**; otherwise the
row is kept and the ownership columns are nulled. Teardown never cascades into a real user's data to
tidy up after a test.

### What survives on purpose

- **Manifest accounts.** `SPEC_P0_SIGNUP.md` F07: *"verified accounts must survive teardown — they
  are the factory output."* Excluded unless `p_include_manifest => true`.
- **`simulations` rows.** `SPEC_P0_SIGNUP.md` Appendix B.3 keeps them as the record of the run.
  Reported as `simulations_kept`.

---

## 4. Verification — run against prod, rolled back

Fixture: two synthetic anonymous users; one grounds a place **and** hosts an event **and** holds an
identity claim; the other is registered as a P0 manifest account.

`cleanup_swarm_run('probe-run')` returned:

```json
{"targets": 1, "auth_users_deleted": 1, "manifest_preserved": 1, "simulations_kept": 0,
 "blockers_released": {"events": 1, "places_deleted": 1, "event_reports": 0, "place_features": 0, …},
 "residue_purged": {"messages": 0, "lana_audit_log": 0, "feature_requests": 0, …}}
```

`auth_users_deleted: 1` is the whole point — **that delete raises `23503` today.**

Post-state, read in a separate statement:

| probe | value | meaning |
|---|---|---|
| `target_user_gone` | `0` | swept |
| `manifest_survived` | `1` | **F07 holds** |
| `claims_cascaded_to_zero` | `0` | `user_identity_claims` cascaded |
| `probe_event_gone` | `0` | hosting blocker released |
| `probe_place_gone` | `0` | grounding blocker released |
| `registry_row_cascaded` | `1` | swept user's registry row gone; manifest's kept |
| `second_sweep_targets` | `0` | **idempotent** |

> ⚠️ Measurement note for anyone re-running this: putting the sweep and the assertions in one
> `SELECT … UNION ALL` gives wrong answers. All subqueries in a single statement share one snapshot,
> so the assertions read **pre-sweep** state. Call the sweep in its own statement. An earlier pass
> here reported `target_user_gone = 1` for exactly that reason.

Both guards verified to raise (§3).

---

## 5. Still required before night 1

Per the handover: *"Nothing runs against prod until teardown is proven on dev."*

This PR proves the mechanism on **prod, in a rolled-back transaction**, which is stronger than a dev
apply for correctness but is **not** the same as an end-to-end rehearsal. Before night 1:

- [ ] Apply on dev `rjlcyvwogmfmngemhbmn`, run a real swarm section, sweep, assert zero residue
- [ ] Confirm `cleanup_stale_anonymous_users` still returns sane counts on dev's 787-run backlog
- [ ] Schedule the sweep at the end of every night, and **fail the run loudly if it raises**

---

## 6. Blast-radius check this enables

`SPEC_P0_SIGNUP.md` F05 requires that, for the run window, no `nudges` / `intros` / `messages` /
`chat_threads` / `events` / RSVP row involves a party outside the manifest. `swarm_run_actors` is
what makes that query writable:

```sql
select count(*) from public.nudges n
where n.sent_at between :run_start and :run_end
  and (n.sender_id    not in (select user_id from public.swarm_run_actors where run_id = :run_id)
    or n.recipient_id not in (select user_id from public.swarm_run_actors where run_id = :run_id));
-- expected: 0
```

---

## 7. Rollback

```sql
begin;
  -- restore the original body (20260616120000_anonymous_guest_lana lines 362-386)
  create or replace function public.cleanup_stale_anonymous_users(p_older_than interval default interval '30 days')
  returns integer language plpgsql security definer set search_path = pg_catalog, public as $$
  declare v_deleted int;
  begin
    with doomed as (
      select u.id from auth.users u
      left join public.users p on p.id = u.id
      where u.is_anonymous is true
        and u.created_at < now() - p_older_than
        and (p.phone_verified_at is null)
    )
    delete from auth.users au using doomed d where au.id = d.id;
    get diagnostics v_deleted = row_count;
    return v_deleted;
  end; $$;

  drop function if exists public.cleanup_swarm_run(text, boolean, boolean);
  drop function if exists public.swarm_purge_user_residue(uuid[]);
  drop function if exists public.swarm_release_user_fk_blockers(uuid[]);
  drop table if exists public.swarm_run_actors;
commit;
```

⚠️ Reverting `cleanup_stale_anonymous_users` restores the `23503` defect. If any anonymous user has
grounded a circle by then, the nightly sweep silently deletes nothing again. **Prefer keeping §5's
fix even if the rest is rolled back** — it is independently correct and has no dependency on the
swarm.

---

## 8. Reviewer checklist

- [ ] #125 has landed (slot ordering)
- [ ] The eight NO ACTION edges in §1 still match `pg_constraint` at merge time — a new NO ACTION FK added since would need adding to `swarm_release_user_fk_blockers`
- [ ] `swarm_run_actors` RLS posture matches `simulations` (admin-only read, service_role writes)
- [ ] Someone is enrolled in `admin_allowlist` (still **0 rows**) or nobody can read either table
- [ ] Agreed that `places` retention-over-deletion (§3) is the right call for shared places
