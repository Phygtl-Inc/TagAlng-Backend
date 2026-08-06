# PR12 · RLS hardening — `simulations`, `zip_centroids`, and the zero-policy audit

**Repo:** `Phygtl-Inc/TagAlng-Backend`
**Migration:** `supabase/migrations/20260919120000_rls_hardening.sql`
**Environments:** prod `kmetmatfxdkrialwrnzj` · dev `rjlcyvwogmfmngemhbmn`
**Origin:** data-layer audit 2026-07-30 · **Verified against prod:** 2026-07-31
**Status:** specification + migration written. **Nothing applied. No PR opened.**

---

## 0. Corrections to the original audit note

Four claims in the incoming spec did not survive verification. They are corrected here
because two of them would have caused an operational failure.

| # | Original claim | Verified reality |
|---|---|---|
| 1 | Migration `20260917150000`, "slots cleanly after `20260916120000_circle_place_mandatory`, the latest version present" | **Wrong and would fail to push.** Prod is at **`20260918120000`** (`scripts/db-push.sh prod --list`, 2026-07-31). `20260917150000` sorts *before* two applied migrations, so `supabase db push` rejects it as an out-of-order insert unless forced with `--include-all`. **Renumbered to `20260919120000`.** |
| 2 | "⚠️ Required pre-apply step: enrol an admin, `admin_allowlist` has 0 rows" | **Not required.** `lana-admin-portal/src/lib/ensure-admin.ts` upserts any signed-in `@phygtl.com` account into `admin_allowlist` on login, using the service-role key. The table self-populates on first portal login. The manual `insert` is optional. |
| 3 | Open question: "Is Lana-AdminPortal `service_role` or `authenticated`?" | **Answered: `service_role`.** `src/lib/admin-data.ts` reads PostgREST with `SUPABASE_SERVICE_ROLE_KEY`. So `simulations_admin_read` is belt-and-braces and the allowlist is not on the critical path. |
| 4 | `app/event_location.py`, `app/places.py` | Paths are `services/lana-worker/app/…`. Both confirmed to use `service_client()`. |

One risk the original spec **missed entirely** — see §2, "future write path".

---

## 1. The finding, and why it is worse than "RLS disabled"

| table | rows (prod) | RLS | policies | contents |
|---|---|---|---|---|
| `public.simulations` | 0 | **off** | 0 | verbatim user utterances (`transcript_json`, `sft_messages`, `judge_summary`) |
| `public.zip_centroids` | 13 | **off** | 0 | ZIP5 → approximate centroid reference data |

The severity comes from the grants, not the RLS flag alone. Supabase grants all privileges
on `public` tables to `anon` and `authenticated` by default and relies on RLS as the gate.
Verified on both tables, before this migration:

```
anon:          SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
authenticated: SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
```

With RLS off those grants are live. **Anyone holding the publishable (anon) key can today
read, insert, update, delete and truncate both tables.** The anon key ships in the client
bundle by design, so this is unauthenticated access.

### Exposure confirmed live on prod

Read-only probes with the **prod anon key**, 2026-07-31. No writes were attempted against
prod — the write test in §5 is deliberately post-apply only, since a successful `INSERT`
would mutate production data.

```
GET /rest/v1/simulations?select=id&limit=1     -> HTTP 200  []
GET /rest/v1/zip_centroids?select=zip5&limit=2 -> HTTP 200  [{"zip5":"32827"},{"zip5":"32832"}]
```

Stated plainly: `simulations` holds **0 rows**, so no user utterance is exposed *right now*.
The exposure is structural and becomes real the moment the sim harness runs against prod.
`zip_centroids` reads are harmless; the **write** side is the risk, since
`get_blocks_near_zip` and the event geocoders resolve locations through it.

`simulations` was created by `20260730120000_simulations` and never had RLS enabled.

---

## 2. What the migration does

**`public.simulations`**
- `enable row level security`
- `simulations_admin_read` — `for select to authenticated using (public.is_tagalng_admin())`
- no write policy (the nightly sim cron writes as `service_role`, which bypasses RLS)
- `revoke insert, update, delete, truncate … from anon, authenticated`
- `revoke select … from anon`

**`public.zip_centroids`**
- `enable row level security`
- `zip_centroids_read` — `for select to authenticated using (true)`
- `revoke insert, update, delete, truncate … from anon, authenticated`

The `REVOKE`s are defence in depth: if RLS is ever toggled off again, the tables still are
not client-writable.

### ⚠️ Future write path — the risk the original spec missed

`20260730120000_simulations.sql` describes the table as written by the nightly cron
**"and the admin UI"** — the HITL review fields (`hitl_status`, `tim_verdict`, `tim_note`,
`sft_eligible`) that Tim sets in `/admin/sims`.

**That UI does not exist yet.** `lana-admin-portal` has exactly four routes
(`/`, `/login`, `/auth/password`, `/auth/signout`) and zero references to `simulations`.
So nothing breaks today. But when `/admin/sims` is built:

- ✅ Write through a **server route on the service-role key** (the `src/lib/admin-data.ts`
  pattern) — works unchanged, RLS bypassed.
- ❌ Write through the **cookie-bound browser client** (`src/lib/supabase/client.ts`, anon
  key, acts as `authenticated`) — **blocked** by the revoke, with no policy to permit it.

If a browser-side write is genuinely needed, add a narrow policy rather than re-granting
broadly:

```sql
create policy "simulations_admin_review" on public.simulations
  for update to authenticated
  using (public.is_tagalng_admin()) with check (public.is_tagalng_admin());
grant update on public.simulations to authenticated;
```

This is recorded in a comment in the migration itself so it is found at the moment it
matters, not re-discovered as a mystery 403.

### Why `is_tagalng_admin()`

Verified to exist (defined in `20260605120000_admin_lana_inbox`, both migrations applied):

```sql
CREATE OR REPLACE FUNCTION public.is_tagalng_admin() RETURNS boolean
 LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO 'public'
AS $function$
  select exists (
    select 1 from public.admin_allowlist a where a.user_id = auth.uid()
  );
$function$
```

`admin_allowlist` is `(user_id uuid, label text, created_at timestamptz)` and has 0 rows in
prod. Per §0(2) that is self-healing on first `@phygtl.com` portal login, and per §0(3) the
portal reads as `service_role` anyway. No existing policy in the database references
`is_tagalng_admin()` in a `USING` clause — this migration establishes that pattern.

### Why `zip_centroids` gets a permissive read

Its table comment already classifies it: *"Not user PII; seed/expand per market."* No client
reads it today — `tagalng-pwa` has zero call sites (the sole hit is a generated type in
`src/utils/supabase/database.types.ts`). Every in-database reader is `SECURITY DEFINER` and
therefore bypasses RLS — verified:

- `public.get_blocks_near_zip` — `security definer`, `revoke execute … from anon`
- `public.auto_create_block_for_zip` — `security definer`
- the geocoder blocks in `20260829120000_event_has_time` and `20260911120000_host_rsvp`

Worker call sites, both on `service_client()`:

- `services/lana-worker/app/places.py:37` — `_zip_centroid(service_client(), …)`
- `services/lana-worker/app/event_location.py:127` — `sb = service_client()`

The permissive `authenticated` read is future-proofing for a client-side block picker; the
write revoke is the part that matters. `anon` keeps its `SELECT` grant but is covered by no
policy, so RLS denies it — adding `to anon` to the policy is the one-line change if a
pre-auth block picker ever needs centroids.

---

## 3. The 13 tables with RLS *enabled and zero policies*

A **different class of finding**, and in the main not bugs. RLS enabled with zero policies is
**deny-all** for `anon` and `authenticated`, while `service_role` bypasses RLS entirely. For a
system where every client data path goes through the lana-worker on `service_client()`,
deny-all is the *correct* posture — the opposite of the `simulations` problem.

| # | table | rows | verdict | recommendation |
|---|---|---|---|---|
| 1 | `places` | 5 | ✅ intentional | Leave. Comment is explicit: *"clients get places data only through worker serializers, never PostgREST."* |
| 2 | `claim_concept_links` | 3 | ✅ intentional | Leave. `20260905120000`: *"Deliberately NO policies: default deny; worker writes via service_role."* |
| 3 | `place_features` | 0 | ✅ intentional | Leave. Sibling of `places`, same tier-gating rationale. |
| 4 | `circle_affiliations` | 8 | ⚠️ confirm intent | Reads flow through the 6 `/lana/circles/*` worker endpoints, so deny-all works. Confirm no planned client-side circles UI, then add an explanatory `comment on table`. |
| 5 | `zip_unlock` | 3 | ⚠️ confirm intent | Served by `POST /lana/area/progress` (service_role). Fine as-is. Needs a read policy only if a client renders ZIP progress directly. |
| 6 | `circle_invites` | 0 | ⚠️ will need a policy | The `/i/<token>` redemption flow is unbuilt. When it ships: worker-mediated (keep deny-all) or direct token lookup (needs a narrow policy). |
| 7 | `circle_invite_redemptions` | 0 | ⚠️ will need a policy | Same; the growth-edge / rate-limit substrate. |
| 8 | `lana_feedback` | 0 | ⚠️ confirm intent | Written via `POST /lana/feedback` (service_role). If the client should re-read its own thumbs state, needs `using (user_id = auth.uid())`. |
| 9 | `feature_requests` | 0 | ⚠️ confirm intent | No table comment, no obvious worker endpoint. Determine the writer first. Low risk while empty. |
| 10 | `moderation_flags` | 0 | 🔴 no writer exists | Not an RLS problem. The table exists with no endpoint and no rows; the gap is the missing safety pipeline. Track separately. |
| 11 | `pending_event_drafts` | 0 | ✅ intentional | Worker scratch state. Deny-all correct. |
| 12 | `pending_meet_seeks` | 0 | ✅ intentional | Worker scratch state. Deny-all correct. |
| 13 | `pending_signal_asks` | 0 | ✅ intentional | Worker scratch state. Deny-all correct. |

**Summary:** 7 clearly correct as-is · 4 need an explicit intent decision recorded
(`circle_affiliations`, `zip_unlock`, `lana_feedback`, `feature_requests`) · 2 will need
policies when the invite/PLG flow ships · 1 (`moderation_flags`) is a product gap wearing an
RLS costume.

**Recommendation: change none of them in this PR.** Deny-all is safe. What is missing is
*documentation of intent*, so each audit stops re-litigating it. Cheapest durable fix — add a
`comment on table` to the four ambiguous ones, exactly as `places` and `claim_concept_links`
already do. Follow-up, not here; this migration stays narrowly scoped to the two genuinely
exposed tables.

---

## 4. Verification performed

**Verified read-only against prod (2026-07-31), nothing applied:**

1. Prod migration head is `20260918120000` — `scripts/db-push.sh prod --list`. This is what
   forced the renumber in §0(1).
2. Anon-key reachability of both tables — the HTTP 200s in §1.
3. Every `zip_centroids` reader is `service_role` or `SECURITY DEFINER` — §2.
4. Admin portal reads via `SUPABASE_SERVICE_ROLE_KEY` — §0(3).
5. `tagalng-pwa` has no call site on either table — §2.
6. No `/admin/sims` route exists, so the missing write policy breaks nothing today — §2.

**Carried over from the original audit** (executed against prod inside `begin; … rollback;`
before this document; the SQL body is unchanged apart from the header comments, so these
results still hold). The body was run, then re-run inside the same transaction to prove
idempotency, then inspected:

| table | `relrowsecurity` | policies | remaining `anon` / `authenticated` grants |
|---|---|---|---|
| `simulations` | `true` | 1 | `anon: REFERENCES, TRIGGER` · `authenticated: REFERENCES, SELECT, TRIGGER` |
| `zip_centroids` | `true` | 1 | `anon: REFERENCES, SELECT, TRIGGER` · `authenticated: REFERENCES, SELECT, TRIGGER` |

All `INSERT` / `UPDATE` / `DELETE` / `TRUNCATE` grants removed for both client roles. The
second pass raised no error. Post-rollback prod was confirmed untouched (`rls = false`,
`policies = 0` on both) — consistent with the anon probes in §1 still returning 200 today.

---

## 5. Test plan

**Pre-apply**

1. Nothing required. Optionally pre-enrol an admin so step 6 is testable immediately:
   `insert into public.admin_allowlist (user_id, label) values ('<uuid>', 'tommaso') on conflict do nothing;`
   Otherwise the first `@phygtl.com` portal login enrols itself (§0(2)).
2. Apply to **dev first**: `./scripts/db-push.sh dev`. Run steps 3–10 there before prod.

**Post-apply — structural**

3. Run the post-conditions block at the foot of the `.sql`. Expect `rls = t`,
   `policies = 1` on both, and no DML grants for `anon` / `authenticated`.

**Post-apply — behavioural** (the part that proves the fix)

4. **anon is locked out of `simulations`.**
   ```
   curl -s -o /dev/null -w '%{http_code}\n' "$SUPABASE_URL/rest/v1/simulations?select=id" -H "apikey: $ANON_KEY"
   ```
   Expect a permission error — **never** rows. Before this migration this returns `200`.
5. **anon cannot write `zip_centroids`.**
   ```
   curl -s -X POST "$SUPABASE_URL/rest/v1/zip_centroids" \
     -H "apikey: $ANON_KEY" -H "Content-Type: application/json" \
     -d '{"zip5":"99999","lat":0,"lng":0}'
   ```
   Expect `401`/`403` (permission denied for table). Before: `201 Created`.
   Run this on **dev only** pre-apply if you want the before/after contrast — on prod it
   would insert a junk row. If it does somehow succeed, delete `zip5 = '99999'`.
6. **A non-admin authenticated user sees no simulations.** Signed-in JWT, same request as
   (4) → 0 rows. **An enrolled admin does** → rows, once `simulations` is non-empty.

**Post-apply — regression** (proving nothing broke)

7. **Worker still reads centroids.** Exercise a service_role path — `POST /lana/places/search`,
   or any event-creation flow that geocodes — and confirm a normal response.
8. **`get_blocks_near_zip` still resolves.**
   `select * from public.get_blocks_near_zip('32827');` → same rows as before. It is
   `SECURITY DEFINER` so RLS does not apply, but assert it anyway.
9. **Event creation still geocodes.** Create a test event and confirm `lat`/`lng` populate.
10. **Sim harness can still write.** Next sim run inserts into `simulations` via service_role;
    assert row count increases.
11. **Admin portal still loads.** It reads on the service-role key, so the dashboard should be
    byte-identical. Confirms §0(3) empirically.

Steps 7–11 catch a mistake; 4–6 prove the point.

---

## 6. Rollback

Fully reversible. Restores the exact prior state (RLS off, default grants). No data is touched
in either direction, so rollback carries no data-loss risk.

```sql
begin;

drop policy if exists "simulations_admin_read" on public.simulations;
alter table public.simulations disable row level security;
grant select, insert, update, delete, truncate on public.simulations to anon, authenticated;

drop policy if exists "zip_centroids_read" on public.zip_centroids;
alter table public.zip_centroids disable row level security;
grant select, insert, update, delete, truncate on public.zip_centroids to anon, authenticated;

commit;
```

**Partial rollback** — if only the grant revokes prove disruptive (e.g. an unknown client path
needs to write), prefer re-granting the specific privilege while **keeping RLS enabled**,
rather than disabling RLS:

```sql
grant insert on public.zip_centroids to authenticated;   -- then add a matching WITH CHECK policy
```

Disabling RLS is the one change that should not be rolled back casually — it is the whole
point of the migration.

---

## 7. Residual risk / open questions

1. ~~Is Lana-AdminPortal `service_role` or `authenticated`?~~ **Resolved: `service_role`**
   (§0(3)). The admin read policy is belt-and-braces.
2. ~~`admin_allowlist` is empty and someone must action it.~~ **Resolved: self-populating**
   (§0(2)).
3. **`/admin/sims` write path** (§2). The only genuine forward-looking hazard in this PR.
   Documented in the migration body.
4. **`simulations` retention.** Out of scope. The table is designed to accumulate verbatim
   user utterances indefinitely and there is no deletion path anywhere in the system. RLS
   closes the access hole; it does not address retention or the right to erasure. **Separate
   ticket.**
5. **The other 13 tables** — §3. No change proposed; four need a recorded intent decision.

**Nothing has been applied. No PR has been created.** Apply order: dev → verify → prod, via
`scripts/db-push.sh`.
