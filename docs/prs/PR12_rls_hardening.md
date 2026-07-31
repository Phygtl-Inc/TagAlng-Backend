# PR12 · RLS hardening — `simulations`, `zip_centroids`, and the zero-policy audit

**Target repo:** `Phygtl-Inc/TagAlng-Backend`
**Migration:** `supabase/migrations/20260917150000_rls_hardening.sql`
**Environment:** Supabase `kmetmatfxdkrialwrnzj` (**tagalng-prod**)
**Author:** data-layer audit · 2026-07-30
**Status:** specification — verified against prod inside `begin; … rollback;`, nothing applied, no PR opened

Migration slots cleanly after `20260916120000_circle_place_mandatory`, the latest version
present in `supabase_migrations.schema_migrations`.

---

## 1. The finding, and why it is worse than "RLS disabled"

Two `public` tables have RLS **disabled**:

| table | rows (prod) | RLS | policies | contents |
|---|---|---|---|---|
| `public.simulations` | 0 | **off** | 0 | verbatim user utterances (`transcript_json`, `sft_messages`, `judge_summary`) |
| `public.zip_centroids` | 13 | **off** | 0 | ZIP5 → approximate centroid reference data |

The severity comes from the grants, not the RLS flag alone. Supabase's default is to grant all
privileges on `public` tables to `anon` and `authenticated`, and to rely on RLS as the gate.
Verified privileges on **both** tables, before this migration:

```
anon:          SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
authenticated: SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
```

With RLS off, those grants are live. **Anyone holding the publishable (anon) API key can today
read, insert, update, delete and truncate both tables.** The anon key ships in the client bundle
by design, so this is unauthenticated access.

Mitigating fact, stated plainly: `simulations` currently holds **0 rows in prod**, so no user
utterance is exposed *right now*. The exposure is structural and will become real the moment the
sim harness runs against prod. `zip_centroids` has 13 rows of non-PII reference data — the read
side is harmless; the **write** side (truncate/poison the ZIP→centroid map) is not, since
`get_blocks_near_zip` and the event geocoders resolve locations through it.

`simulations` was created by migration `20260730120000_simulations` and never had RLS enabled.

---

## 2. What the migration does

**`public.simulations`**
- `enable row level security`
- `simulations_admin_read` — `for select to authenticated using (public.is_tagalng_admin())`
- no write policies (service_role bypasses RLS; the sim harness writes as service_role)
- `revoke insert, update, delete, truncate … from anon, authenticated`
- `revoke select … from anon`

**`public.zip_centroids`**
- `enable row level security`
- `zip_centroids_read` — `for select to authenticated using (true)`
- `revoke insert, update, delete, truncate … from anon, authenticated`

The `REVOKE`s are defence in depth: they ensure that if RLS is ever toggled off again the tables
still are not writable by client roles.

### Why `is_tagalng_admin()`

Verified to exist and to be the right primitive:

```sql
CREATE OR REPLACE FUNCTION public.is_tagalng_admin()
 RETURNS boolean
 LANGUAGE sql STABLE SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
  select exists (
    select 1
    from public.admin_allowlist a
    where a.user_id = auth.uid()
  );
$function$
```

`admin_allowlist` is `(user_id uuid, label text, created_at timestamptz)`.

⚠️ **`admin_allowlist` currently has 0 rows in prod.** The policy therefore denies everyone
until an admin is enrolled. That is the correct fail-closed default, but it means "admin can read
simulations" is not testable until someone is added:

```sql
insert into public.admin_allowlist (user_id, label)
values ('<auth.uid of the admin>', 'tommaso')
on conflict do nothing;
```

Note also that no existing policy in the database references `is_tagalng_admin()` in a `USING`
clause — this migration establishes that pattern. Worth a second pair of eyes on whether the
Lana-AdminPortal is expected to read `simulations` as `authenticated` (in which case this policy
is exactly right) or as `service_role` (in which case the policy is belt-and-braces and the
allowlist enrolment is optional).

### Why `zip_centroids` gets a permissive read

Its own table comment already classifies it: *"Not user PII; seed/expand per market."* No client
reads it today — a search of `Phygtl-Inc/tagalng-pwa` for `zip_centroids` returns 0 hits, and every
in-database reader is a `SECURITY DEFINER` function (`get_blocks_near_zip`,
`auto_create_block_for_zip`, and the geocoder blocks inside the event/RSVP migrations), all of
which bypass RLS. The permissive `authenticated` read is future-proofing for a client-side block
picker; the write revoke is the part that matters. `anon` keeps its `SELECT` grant but is covered
by no policy, so RLS denies it — adding `to anon` to the policy is the one-line change if a
pre-auth block picker ever needs centroids.

---

## 3. The 13 tables with RLS *enabled and zero policies*

These are a **different class of finding** and, in the main, are not bugs. RLS enabled with zero
policies is **deny-all** for `anon` and `authenticated`, while `service_role` bypasses RLS
entirely. For a system where every client data path goes through the lana-worker (which uses
`service_client()` / service_role), deny-all is the *correct* posture — it is the opposite of the
`simulations` problem.

Per `_CODE_TRUTH_2026-07-30.md`, the frontend exposes only `/chat`, `/meet/[id]` and
`/signin-required`, with all data access via the worker API. So none of these tables is
client-reachable today.

| # | table | rows | verdict | recommendation |
|---|---|---|---|---|
| 1 | `places` | 5 | ✅ **intentional** | Leave. Table comment is explicit: *"clients get places data only through worker serializers, never PostgREST."* Deny-all is the design. |
| 2 | `claim_concept_links` | 3 | ✅ **intentional** | Leave. Comment: *"worker-only."* Migration `20260905120000` says *"Deliberately NO policies: default deny; worker writes via service_role."* |
| 3 | `place_features` | 0 | ✅ **intentional** | Leave. Sibling of `places`, same tier-gating rationale. |
| 4 | `circle_affiliations` | 8 | ⚠️ **confirm intent** | Called out in CODE TRUTH. Reads flow through the 6 `/lana/circles/*` worker endpoints, so deny-all works. Confirm no planned client-side circles UI, then add an explanatory `comment on table` so this stops being re-flagged each audit. |
| 5 | `zip_unlock` | 3 | ⚠️ **confirm intent** | Area-progress state is served by `POST /lana/area/progress` (service_role). Fine as-is. If a client ever renders ZIP progress directly, it needs a read policy. |
| 6 | `circle_invites` | 0 | ⚠️ **will need a policy** | The `/i/<token>` redemption flow is unbuilt (0 rows, no route). When it ships, decide: worker-mediated (keep deny-all) or direct token lookup (needs a narrow policy). Flag for the PLG work, not now. |
| 7 | `circle_invite_redemptions` | 0 | ⚠️ **will need a policy** | Same as above; the growth-edge/rate-limit substrate. |
| 8 | `lana_feedback` | 0 | ⚠️ **confirm intent** | Written via `POST /lana/feedback` (service_role) — deny-all fine. But if the client should re-read a user's own thumbs state, that needs `using (user_id = auth.uid())`. Currently the UI would have to get it from the worker. |
| 9 | `feature_requests` | 0 | ⚠️ **confirm intent** | No table comment, no obvious worker endpoint. Determine the writer before deciding. Low risk while empty. |
| 10 | `moderation_flags` | 0 | 🔴 **no writer exists** | Not an RLS problem — CODE TRUTH confirms *"No safety mechanism at ANY tier."* The table exists with no endpoint and no rows. Deny-all is fine; the gap is the missing safety pipeline. Track separately. |
| 11 | `pending_event_drafts` | 0 | ✅ **intentional** | Worker scratch state. Deny-all correct. |
| 12 | `pending_meet_seeks` | 0 | ✅ **intentional** | Worker scratch state. Deny-all correct. |
| 13 | `pending_signal_asks` | 0 | ✅ **intentional** | Worker scratch state. Deny-all correct. |

**Summary:** 7 are clearly correct as-is · 4 need an explicit intent decision recorded
(`circle_affiliations`, `zip_unlock`, `lana_feedback`, `feature_requests`) · 2 will need policies
when the invite/PLG flow ships · 1 (`moderation_flags`) is a product gap wearing an RLS costume.

**Recommendation: change none of them in this PR.** Deny-all is safe. What is missing is
*documentation of intent*, so each audit stops re-litigating it. Cheapest durable fix — add a
`comment on table` to the four ambiguous ones stating "no client policies: worker-mediated via
service_role", exactly as `places` and `claim_concept_links` already do. That belongs in a
follow-up, not here; this migration stays narrowly scoped to the two genuinely exposed tables.

---

## 4. Verification performed

Executed against **PROD** (`kmetmatfxdkrialwrnzj`) wrapped in `begin; … rollback;`. The full
migration body was run, then **re-run a second time inside the same transaction** to prove
idempotency, then inspected:

| table | `relrowsecurity` | policies | remaining `anon` / `authenticated` grants |
|---|---|---|---|
| `simulations` | `true` | 1 | `anon: REFERENCES, TRIGGER` · `authenticated: REFERENCES, SELECT, TRIGGER` |
| `zip_centroids` | `true` | 1 | `anon: REFERENCES, SELECT, TRIGGER` · `authenticated: REFERENCES, SELECT, TRIGGER` |

All `INSERT` / `UPDATE` / `DELETE` / `TRUNCATE` grants removed for both client roles. The second
pass raised no error (`enable row level security` is a no-op when already on;
`drop policy if exists` + `create policy` is safely repeatable).

Post-rollback confirmation — prod is untouched:

```
simulations   | rls = false | policies = 0
zip_centroids | rls = false | policies = 0
```

---

## 5. Test plan

**Pre-apply**
1. Enrol at least one admin, or accept that step 3 will correctly return 0 rows:
   `insert into public.admin_allowlist (user_id, label) values ('<uuid>', 'tommaso');`

**Post-apply — structural**

2. Post-conditions block at the foot of the `.sql` file. Expect `rls = t`, `policies = 1` on both,
   and no DML grants for `anon` / `authenticated`.

**Post-apply — behavioural** (the part that actually proves the fix)

3. **anon is locked out of `simulations`.** With the publishable key:
   ```
   curl -s "$SUPABASE_URL/rest/v1/simulations?select=id" -H "apikey: $ANON_KEY"
   ```
   Expect an empty array or a permission error — **never** rows. Before this migration
   this call returns data and the matching `DELETE` succeeds.
4. **anon cannot write `zip_centroids`.**
   ```
   curl -s -X POST "$SUPABASE_URL/rest/v1/zip_centroids" \
     -H "apikey: $ANON_KEY" -H "Content-Type: application/json" \
     -d '{"zip5":"99999","lat":0,"lng":0}'
   ```
   Expect `401`/`403` (permission denied for table). Before: `201 Created`.
5. **A non-admin authenticated user sees no simulations.** Signed-in JWT, same request as (3) →
   0 rows.
6. **An enrolled admin does see them.** Same request with an `admin_allowlist` member's JWT →
   rows (once `simulations` is non-empty).

**Post-apply — regression** (proving nothing broke)

7. **Worker still reads centroids.** Exercise a path that hits `zip_centroids` via service_role —
   `POST /lana/places/search`, or any event-creation flow that geocodes — and confirm a normal
   response. `app/event_location.py` reads it through `service_client()`, and
   `app/places.py` documents its dependence on *"the zip_centroids table + dev block fallback."*
8. **`get_blocks_near_zip` still resolves.** `select * from public.get_blocks_near_zip('<a seeded zip5>');`
   → same rows as before. It is `SECURITY DEFINER`, so RLS does not apply, but assert it anyway.
9. **Event creation still geocodes.** The geocoder blocks inside `20260829120000_event_has_time`
   and `20260911120000_host_rsvp` select from `zip_centroids`; create a test event and confirm
   lat/lng populate.
10. **Sim harness can still write.** Next sim run inserts into `simulations` via service_role.
    Assert row count increases.

Steps 7–10 are the ones that catch a mistake; 3–6 are the ones that prove the point.

---

## 6. Rollback

Fully reversible. Restores the exact prior state (RLS off, default grants).

```sql
begin;

-- simulations
drop policy if exists "simulations_admin_read" on public.simulations;
alter table public.simulations disable row level security;
grant select, insert, update, delete, truncate on public.simulations to anon, authenticated;

-- zip_centroids
drop policy if exists "zip_centroids_read" on public.zip_centroids;
alter table public.zip_centroids disable row level security;
grant select, insert, update, delete, truncate on public.zip_centroids to anon, authenticated;

commit;
```

No data is touched by either direction, so rollback carries no data-loss risk.

**Partial rollback** — if only the grant revokes prove disruptive (e.g. an unknown client path
needs to write), prefer re-granting the specific privilege while **keeping RLS enabled**, rather
than disabling RLS:

```sql
grant insert on public.zip_centroids to authenticated;   -- then add a matching WITH CHECK policy
```

Disabling RLS is the one change that should not be rolled back casually — it is the whole point
of the migration.

---

## 7. Residual risk / open questions for the team

1. **`admin_allowlist` is empty.** `simulations` becomes readable by nobody except service_role
   until an admin is enrolled. Intended, but someone must action it.
2. **Is Lana-AdminPortal a `service_role` or an `authenticated` consumer?** If service_role, the
   admin policy is unused (harmless); if authenticated, allowlist enrolment is mandatory before
   the portal can show sims.
3. **`simulations` retention.** Out of scope here, but the table is designed to accumulate
   verbatim user utterances indefinitely and there is no deletion path anywhere in the system
   (CODE TRUTH: *"no DELETE anywhere (no data-deletion path)"*). RLS closes the access hole; it
   does not address retention or the right to erasure. Worth a separate ticket.
4. **The other 13 tables** — see §3. No change proposed; four need a recorded intent decision.

**No GitHub PR has been created; nothing has been pushed or applied.**
