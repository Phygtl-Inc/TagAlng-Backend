-- ============================================================================
-- 20260917170000_swarm_teardown.sql
--
-- Repo path: supabase/migrations/20260917170000_swarm_teardown.sql
-- Target:    Supabase kmetmatfxdkrialwrnzj (tagalng-prod)
-- Follows:   20260917160000_simulations_harness (PR #125)
--
-- WHY
--   The overnight swarm writes to PROD, which holds 31 real users. run_id
--   tagging is specified but nothing sweeps, and nothing enforces it
--   (LANA_AUTONOMY_GAPS.md gap 4). This adds the sweep.
--
--   It also fixes a defect in the existing sweep that would have surfaced on
--   night 1 as a hard failure. See §0.
--
-- ============================================================================
-- §0 · THE DEFECT THIS MIGRATION FIXES
--
--   `cleanup_stale_anonymous_users` (20260616120000_anonymous_guest_lana) deletes
--   straight from `auth.users` and relies on ON DELETE CASCADE to sweep the rest.
--   Most of the ~50 user-referencing FKs do cascade. **Eight do not.** They are
--   ON DELETE NO ACTION, and each one BLOCKS the delete:
--
--     places.created_by            places.claimed_by
--     events.host_id               place_features.contributed_by
--     circle_affiliations.invited_by
--     event_reports.reporter_id    event_reports.reviewer_id
--     thread_events.actor_id       users.invited_by
--
--   Two of those are on the swarm's critical path:
--
--     * places.created_by  — circle grounding inserts a `places` row owned by
--       the grounding user. EVERY one of the nine personas grounds a circle
--       (personas.json: PER-01 turn 5 "Pin it." -> expect_db_write
--       circle_affiliations). So every persona in every run creates a places row.
--     * events.host_id     — P5 Hosting creates an event owned by the host.
--
--   Verified on prod inside begin/rollback, using a synthetic anonymous user:
--
--     insert auth.users(is_anonymous=true) -> public.users row auto-created
--     insert public.places(created_by = that user)
--     delete from auth.users where id = that user
--       => ERROR 23503: update or delete on table "users" violates foreign key
--          constraint "places_created_by_fkey" on table "places"
--          DETAIL: Key (id)=(...) is still referenced from table "places".
--
--     ... and identically for events:
--       => ERROR 23503: ... violates foreign key constraint "events_host_id_fkey"
--
--   `cleanup_stale_anonymous_users` has no exception handling and returns
--   integer, so the raised 23503 aborts the whole call. One un-sweepable user
--   takes down the entire nightly cleanup, for every user, silently, from the
--   first night the swarm grounds a circle.
--
--   Per the handover ("extend, don't duplicate"), the blocker-clearing logic is
--   factored into ONE function that BOTH sweeps call. `cleanup_swarm_run` is not
--   a parallel implementation.
--
-- SAFETY
--   * `cleanup_swarm_run` deletes ONLY user ids registered in
--     `swarm_run_actors` for the given run_id. It cannot reach a real user by
--     construction — there is no heuristic (no `nickname like 'simqa-%'`, no
--     date window) that could widen its blast radius.
--   * A second, independent guard rejects any registered id that is neither
--     anonymous nor carrying a `lana-sim+...` address. A mis-registration is an
--     exception, not a deletion.
--   * Manifest accounts (the P0 account factory output) are preserved by
--     default. SPEC_P0_SIGNUP.md F07 requires they SURVIVE teardown.
--   * `simulations` rows are never deleted — they are the record of the run
--     (SPEC_P0_SIGNUP.md Appendix B.3).
--   * `p_dry_run => true` returns the identical report without deleting.
--   * Idempotent: a second call for the same run_id returns zero counts.
--
-- ROLLBACK: see PR14_swarm_teardown.md §7.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- 1. The actor registry — the only anchor teardown can trust
-- ----------------------------------------------------------------------------
--
-- The swarm drives the worker API, not the database. The worker knows nothing
-- about run_id, so it cannot stamp run_id onto user_identity_claims,
-- local_signals, circle_affiliations or any other row it writes on the
-- persona's behalf. Tagging "every written row with run_id" is therefore not
-- achievable at the row level through the public API.
--
-- What IS knowable is the identity the harness created. Every row the swarm
-- causes is reachable from that identity by FK. So the registry maps
-- run_id -> user_id, the harness writes it at the moment it mints each
-- anonymous user, and teardown resolves the row set by join instead of by
-- pattern match. Exact, and incapable of touching a non-test user.

create table if not exists public.swarm_run_actors (
  run_id              text        not null,
  user_id             uuid        not null references auth.users(id) on delete cascade,
  persona_id          text        not null,
  section_id          text,
  arm                 text        not null default 'E-VOICE',
  -- P0 produces verified accounts that P5-P8 consume. These must survive
  -- teardown (SPEC_P0_SIGNUP.md F07) and are excluded unless explicitly asked for.
  is_manifest_account boolean     not null default false,
  sim_email           text,
  created_at          timestamptz not null default now(),
  primary key (run_id, user_id)
);

create index if not exists swarm_run_actors_user_idx on public.swarm_run_actors (user_id);
create index if not exists swarm_run_actors_manifest_idx
  on public.swarm_run_actors (run_id) where is_manifest_account;

comment on table public.swarm_run_actors is
  'run_id -> user_id map written by the tools/swarm harness as it mints each anonymous test '
  'identity. The anchor for cleanup_swarm_run(); the worker API cannot stamp run_id onto the '
  'rows it writes, so identity is the only exact handle. Holds no PII beyond the sim address.';

-- Same posture as `simulations` under 20260917150000: admin-only read, no
-- client writes, service_role bypasses RLS and does the writing.
alter table public.swarm_run_actors enable row level security;

drop policy if exists "swarm_run_actors_admin_read" on public.swarm_run_actors;
create policy "swarm_run_actors_admin_read"
  on public.swarm_run_actors
  for select
  to authenticated
  using (public.is_tagalng_admin());

revoke insert, update, delete, truncate on public.swarm_run_actors from anon, authenticated;
revoke select on public.swarm_run_actors from anon;


-- ----------------------------------------------------------------------------
-- 2. The shared blocker-release step — ONE implementation, two callers
-- ----------------------------------------------------------------------------
--
-- Clears every ON DELETE NO ACTION reference to the given users, so that a
-- subsequent `delete from auth.users` can cascade instead of raising 23503.
-- Nullable blockers are nulled; NOT NULL blockers force the owning row to go.

create or replace function public.swarm_release_user_fk_blockers(p_user_ids uuid[])
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v jsonb := '{}'::jsonb;
  n int;
begin
  if p_user_ids is null or cardinality(p_user_ids) = 0 then
    return v;
  end if;

  -- events.host_id is NOT NULL, so the event itself must go. This cascades
  -- chat_threads, event_bring_items, event_cohost_invites, event_dismissals,
  -- event_reports, event_requests, irl_attendance_processed and thread_events.
  delete from public.events where host_id = any(p_user_ids);
  get diagnostics n = row_count; v := v || jsonb_build_object('events', n);

  update public.events set cohost_id = null where cohost_id = any(p_user_ids);
  get diagnostics n = row_count; v := v || jsonb_build_object('events_cohost_nulled', n);

  -- event_reports.reporter_id is NOT NULL; reviewer_id is nullable.
  delete from public.event_reports where reporter_id = any(p_user_ids);
  get diagnostics n = row_count; v := v || jsonb_build_object('event_reports', n);

  update public.event_reports set reviewer_id = null where reviewer_id = any(p_user_ids);
  get diagnostics n = row_count; v := v || jsonb_build_object('event_reports_reviewer_nulled', n);

  -- Sim-authored supply data: drop it rather than leave it unattributed.
  delete from public.place_features where contributed_by = any(p_user_ids);
  get diagnostics n = row_count; v := v || jsonb_build_object('place_features', n);

  update public.thread_events set actor_id = null where actor_id = any(p_user_ids);
  get diagnostics n = row_count; v := v || jsonb_build_object('thread_events_actor_nulled', n);

  update public.circle_affiliations set invited_by = null where invited_by = any(p_user_ids);
  get diagnostics n = row_count; v := v || jsonb_build_object('circle_affiliations_inviter_nulled', n);

  update public.users set invited_by = null where invited_by = any(p_user_ids);
  get diagnostics n = row_count; v := v || jsonb_build_object('users_invited_by_nulled', n);

  update public.users set referred_by = null where referred_by = any(p_user_ids);
  get diagnostics n = row_count; v := v || jsonb_build_object('users_referred_by_nulled', n);

  -- `places` is the delicate one. Six tables reference places.place_ref with
  -- ON DELETE NO ACTION (circle_affiliations, circle_invites, events,
  -- rapport_gaps, user_identity_claims) — and a REAL user may have grounded to
  -- the same place a persona did. Deleting the place would then require
  -- deleting that real user's claim.
  --
  -- So: delete the place only when nothing outside this user set references it;
  -- otherwise keep the row and null the ownership. Never cascade into a real
  -- user's data to tidy up after a test.
  delete from public.places p
  where (p.created_by = any(p_user_ids) or p.claimed_by = any(p_user_ids))
    and not exists (select 1 from public.circle_affiliations   x where x.place_ref = p.id and not (x.user_id = any(p_user_ids)))
    and not exists (select 1 from public.circle_invites        x where x.place_ref = p.id and not (x.owner_user_id = any(p_user_ids)))
    and not exists (select 1 from public.events                x where x.place_ref = p.id)
    and not exists (select 1 from public.rapport_gaps          x where x.place_ref = p.id and not (x.user_id = any(p_user_ids)))
    and not exists (select 1 from public.user_identity_claims  x where x.place_ref = p.id and not (x.user_id = any(p_user_ids)));
  get diagnostics n = row_count; v := v || jsonb_build_object('places_deleted', n);

  update public.places set created_by = null where created_by = any(p_user_ids);
  get diagnostics n = row_count; v := v || jsonb_build_object('places_creator_nulled', n);

  update public.places set claimed_by = null, claimed_at = null where claimed_by = any(p_user_ids);
  get diagnostics n = row_count; v := v || jsonb_build_object('places_claim_released', n);

  return v;
end;
$$;

revoke all on function public.swarm_release_user_fk_blockers(uuid[]) from public;
grant execute on function public.swarm_release_user_fk_blockers(uuid[]) to service_role;

comment on function public.swarm_release_user_fk_blockers(uuid[]) is
  'Clears the eight ON DELETE NO ACTION references to public.users so a subsequent delete from '
  'auth.users can cascade instead of raising 23503. Shared by cleanup_swarm_run and '
  'cleanup_stale_anonymous_users. Returns a per-table count report.';


-- ----------------------------------------------------------------------------
-- 3. Rows that would survive as orphans rather than block the delete
-- ----------------------------------------------------------------------------
--
-- Thirteen FK columns are ON DELETE SET NULL. Those do not block anything, but
-- they leave the row behind with a null actor. Where the row is OWNED by the
-- departing user it is test residue and must go; where the row belongs to
-- someone else, only the pointer is cleared.

create or replace function public.swarm_purge_user_residue(p_user_ids uuid[])
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v jsonb := '{}'::jsonb;
  n int;
begin
  if p_user_ids is null or cardinality(p_user_ids) = 0 then
    return v;
  end if;

  -- Owned by the departing user -> delete outright.
  delete from public.lana_audit_log where user_id = any(p_user_ids);
  get diagnostics n = row_count; v := v || jsonb_build_object('lana_audit_log', n);

  delete from public.feature_requests where user_id = any(p_user_ids);
  get diagnostics n = row_count; v := v || jsonb_build_object('feature_requests', n);

  delete from public.moderation_flags where user_id = any(p_user_ids);
  get diagnostics n = row_count; v := v || jsonb_build_object('moderation_flags', n);

  delete from public.moderation_actions where actor_user_id = any(p_user_ids);
  get diagnostics n = row_count; v := v || jsonb_build_object('moderation_actions', n);

  delete from public.moderation_reports where reporter = any(p_user_ids);
  get diagnostics n = row_count; v := v || jsonb_build_object('moderation_reports', n);

  delete from public.messages where sender_id = any(p_user_ids);
  get diagnostics n = row_count; v := v || jsonb_build_object('messages', n);

  -- Belongs to someone else -> clear the pointer only.
  update public.chat_threads set created_by = null where created_by = any(p_user_ids);
  get diagnostics n = row_count; v := v || jsonb_build_object('chat_threads_creator_nulled', n);

  update public.block_log_entries set peer_user_id = null where peer_user_id = any(p_user_ids);
  get diagnostics n = row_count; v := v || jsonb_build_object('block_log_peer_nulled', n);

  update public.recommendation_impressions set candidate_user_id = null where candidate_user_id = any(p_user_ids);
  get diagnostics n = row_count; v := v || jsonb_build_object('rec_impressions_candidate_nulled', n);

  update public.marketplace_items set reserved_for = null where reserved_for = any(p_user_ids);
  get diagnostics n = row_count; v := v || jsonb_build_object('marketplace_reservation_released', n);

  update public.inquiries set closed_by = null where closed_by = any(p_user_ids);
  get diagnostics n = row_count; v := v || jsonb_build_object('inquiries_closer_nulled', n);

  update public.event_bring_items set claimed_by = null where claimed_by = any(p_user_ids);
  get diagnostics n = row_count; v := v || jsonb_build_object('bring_items_released', n);

  update public.unmask_requests set declined_by = null where declined_by = any(p_user_ids);
  get diagnostics n = row_count; v := v || jsonb_build_object('unmask_decliner_nulled', n);

  update public.lana_message_holds set denied_by = null where denied_by = any(p_user_ids);
  get diagnostics n = row_count; v := v || jsonb_build_object('holds_denier_nulled', n);

  update public.lana_message_holds set released_by = null where released_by = any(p_user_ids);
  get diagnostics n = row_count; v := v || jsonb_build_object('holds_releaser_nulled', n);

  return v;
end;
$$;

revoke all on function public.swarm_purge_user_residue(uuid[]) from public;
grant execute on function public.swarm_purge_user_residue(uuid[]) to service_role;

comment on function public.swarm_purge_user_residue(uuid[]) is
  'Handles the thirteen ON DELETE SET NULL references: deletes rows owned by the departing users, '
  'clears the pointer on rows owned by others. Prevents null-actor orphans after teardown.';


-- ----------------------------------------------------------------------------
-- 4. cleanup_swarm_run — the nightly sweep
-- ----------------------------------------------------------------------------
--
-- SIGNATURE NOTE: the handover specifies cleanup_swarm_run(p_run_id uuid).
-- run_id is `text` everywhere it already exists — `simulations.run_id text not
-- null`, and the account convention `lana-sim+{run_id}-{persona_id}@{domain}`
-- with run_ids of the form `2026-07-31-a` (SPEC_P0_SIGNUP.md Appendix A). A
-- uuid parameter could not accept the run_ids the specs actually define, so
-- this takes text.

create or replace function public.cleanup_swarm_run(
  p_run_id           text,
  p_include_manifest boolean default false,
  p_dry_run          boolean default false
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_targets   uuid[];
  v_manifest  uuid[];
  v_unsafe    text;
  v_report    jsonb := '{}'::jsonb;
  v_blockers  jsonb;
  v_residue   jsonb;
  n           int;
begin
  if p_run_id is null or btrim(p_run_id) = '' then
    raise exception 'cleanup_swarm_run: p_run_id is required';
  end if;

  select coalesce(array_agg(user_id), '{}')
    into v_targets
  from public.swarm_run_actors
  where run_id = p_run_id
    and (p_include_manifest or not is_manifest_account);

  select coalesce(array_agg(user_id), '{}')
    into v_manifest
  from public.swarm_run_actors
  where run_id = p_run_id and is_manifest_account;

  -- A typo'd run_id must not look like a clean sweep. Raise, do not no-op.
  if cardinality(v_targets) = 0 then
    if not exists (select 1 from public.swarm_run_actors where run_id = p_run_id) then
      raise exception 'cleanup_swarm_run: no actors registered for run_id %. Refusing to run — a '
                      'silent no-op on a mistyped run_id is indistinguishable from a clean sweep.',
                      p_run_id;
    end if;
    return jsonb_build_object(
      'run_id', p_run_id, 'dry_run', p_dry_run, 'targets', 0,
      'manifest_preserved', cardinality(v_manifest),
      'note', 'every registered actor is a manifest account; nothing to sweep'
    );
  end if;

  -- Independent second guard. The registry join already makes a real user
  -- unreachable; this catches a HARNESS BUG that registered the wrong id.
  -- A test identity is either still anonymous, or carries a sim address.
  select string_agg(u.id::text || ' (' || coalesce(u.email, 'no-email') || ')', ', ')
    into v_unsafe
  from auth.users u
  where u.id = any(v_targets)
    and coalesce(u.is_anonymous, false) = false
    and coalesce(u.email, '') not like 'lana-sim+%';

  if v_unsafe is not null then
    raise exception 'cleanup_swarm_run: refusing to delete non-test identities registered under '
                    'run_id %: %. These are neither anonymous nor lana-sim+ addresses. Fix the '
                    'harness registration before re-running teardown.', p_run_id, v_unsafe;
  end if;

  if p_dry_run then
    return jsonb_build_object(
      'run_id', p_run_id, 'dry_run', true,
      'targets', cardinality(v_targets),
      'manifest_preserved', cardinality(v_manifest),
      'would_delete_auth_users', v_targets
    );
  end if;

  v_blockers := public.swarm_release_user_fk_blockers(v_targets);
  v_residue  := public.swarm_purge_user_residue(v_targets);

  -- Cascades public.users and the ~50 ON DELETE CASCADE children:
  -- user_identity_claims, lana_sessions (-> lana_messages, latent_signals),
  -- circle_affiliations, local_signals, rapport_gaps, suggestion_queue,
  -- chat_thread_members, nudges, intros, push_subscriptions, and the rest.
  delete from auth.users where id = any(v_targets);
  get diagnostics n = row_count;

  -- The registry rows for the swept users go with them (FK cascade). Manifest
  -- rows stay, so the run remains auditable and re-runnable.
  v_report := jsonb_build_object(
    'run_id',              p_run_id,
    'dry_run',             false,
    'targets',             cardinality(v_targets),
    'auth_users_deleted',  n,
    'manifest_preserved',  cardinality(v_manifest),
    'blockers_released',   v_blockers,
    'residue_purged',      v_residue,
    'simulations_kept',    (select count(*) from public.simulations where run_id = p_run_id),
    'swept_at',            now()
  );

  return v_report;
end;
$$;

revoke all on function public.cleanup_swarm_run(text, boolean, boolean) from public;
grant execute on function public.cleanup_swarm_run(text, boolean, boolean) to service_role;

comment on function public.cleanup_swarm_run(text, boolean, boolean) is
  'Sweeps every row a swarm run caused, resolved via swarm_run_actors (never by pattern match, so '
  'a real user is unreachable by construction). Preserves manifest accounts (SPEC_P0_SIGNUP F07) '
  'and simulations rows (the record). p_dry_run reports without deleting. Returns a per-table report.';


-- ----------------------------------------------------------------------------
-- 5. Fix cleanup_stale_anonymous_users so one un-sweepable user cannot abort it
-- ----------------------------------------------------------------------------
--
-- Same signature, same semantics, same grants. Two changes:
--   (a) release the NO ACTION blockers first, so grounding a circle or hosting
--       an event no longer makes an anonymous user permanently undeletable;
--   (b) purge the SET NULL residue, so the sweep does not leave null-actor rows.
--
-- Without (a) this function raises 23503 and deletes NOTHING the first night a
-- persona says "pin it" — verified on prod, see §0.

create or replace function public.cleanup_stale_anonymous_users(p_older_than interval default interval '30 days')
returns integer
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_deleted int;
  v_doomed  uuid[];
begin
  select coalesce(array_agg(u.id), '{}')
    into v_doomed
  from auth.users u
  left join public.users p on p.id = u.id
  where u.is_anonymous is true
    and u.created_at < now() - p_older_than
    and p.phone_verified_at is null;

  if cardinality(v_doomed) = 0 then
    return 0;
  end if;

  perform public.swarm_release_user_fk_blockers(v_doomed);
  perform public.swarm_purge_user_residue(v_doomed);

  delete from auth.users au where au.id = any(v_doomed);
  get diagnostics v_deleted = row_count;
  return v_deleted;
end;
$$;

revoke all on function public.cleanup_stale_anonymous_users(interval) from public;
grant execute on function public.cleanup_stale_anonymous_users(interval) to service_role;

comment on function public.cleanup_stale_anonymous_users(interval) is
  'Deletes stale anonymous auth users with no verified phone. Releases ON DELETE NO ACTION FK '
  'blockers first (places.created_by, events.host_id, ...) — without that step a single anonymous '
  'user who grounded a circle raises 23503 and the whole sweep deletes nothing.';


-- ============================================================================
-- POST-CONDITIONS (assert after apply)
--
--   -- 1. the blocker that motivated this migration is gone
--   begin;
--     insert into auth.users (id, instance_id, aud, role, is_anonymous, created_at, updated_at)
--     values ('00000000-0000-4000-8000-00000000dead','00000000-0000-0000-0000-000000000000',
--             'authenticated','authenticated',true, now(), now());
--     insert into public.places (google_place_id, name, created_by)
--       values ('probe','Probe YMCA','00000000-0000-4000-8000-00000000dead');
--     insert into public.events (host_id, title, starts_at, block_id, cluster_id)
--       values ('00000000-0000-4000-8000-00000000dead','Probe', now()+interval '2 days',
--               'zip-34771','lake-nona');
--     insert into public.swarm_run_actors (run_id, user_id, persona_id)
--       values ('probe','00000000-0000-4000-8000-00000000dead','PER-01');
--     select public.cleanup_swarm_run('probe');
--     -- Expected: auth_users_deleted = 1, blockers_released.events = 1,
--     --           blockers_released.places_deleted = 1
--     select count(*) from auth.users where id='00000000-0000-4000-8000-00000000dead';
--     -- Expected: 0
--   rollback;
--
--   -- 2. an unregistered run_id raises rather than reporting a clean sweep
--   select public.cleanup_swarm_run('no-such-run');
--   -- Expected: ERROR ... no actors registered for run_id no-such-run
--
--   -- 3. manifest accounts survive (SPEC_P0_SIGNUP.md F07)
--   --    register one manifest + one throwaway under a run, sweep, assert the
--   --    manifest id still exists and the throwaway does not.
--
--   -- 4. RLS posture matches `simulations`
--   select relrowsecurity from pg_class where oid='public.swarm_run_actors'::regclass;  -- t
-- ============================================================================
