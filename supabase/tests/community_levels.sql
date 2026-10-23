-- 20270213120000_community_levels · behaviour checks (local stack or validation container)
--   psql postgresql://postgres:postgres@127.0.0.1:54322/postgres -f supabase/tests/community_levels.sql
-- Runs the settings RPCs as `authenticated` with request.jwt.claim.sub switched per actor,
-- and the worker-side reads as the superuser (they are service-role only). Everything rolls
-- back. Prints one PASS/FAIL row per check and a final failed|total row; expect 0 failed.

begin;
\set ON_ERROR_STOP 1
set client_min_messages = warning;

-- fixtures (superuser) --------------------------------------------------------
create or replace function pg_temp.mkuser(p_id uuid, p_nick text) returns void
language plpgsql as $$
begin
  insert into auth.users (id, email) values (p_id, p_nick || '@t.test') on conflict (id) do nothing;
  insert into public.users (id, nickname) values (p_id, p_nick)
    on conflict (id) do update set nickname = excluded.nickname;
end $$;

select pg_temp.mkuser('00000000-0000-0000-0000-0000000000a1', 'Julia');    -- owner
select pg_temp.mkuser('00000000-0000-0000-0000-0000000000e1', 'Eve');      -- E-board
select pg_temp.mkuser('00000000-0000-0000-0000-0000000000b1', 'Leo');      -- Lead
select pg_temp.mkuser('00000000-0000-0000-0000-0000000000c1', 'Amy');      -- Ambassador
select pg_temp.mkuser('00000000-0000-0000-0000-0000000000d1', 'Max');      -- plain member
select pg_temp.mkuser('00000000-0000-0000-0000-0000000000f1', 'Xan');      -- outsider

insert into public.places (id, google_place_id, name, created_by, governance_state)
values ('20000000-0000-0000-0000-000000000001', 'test:rcc', 'Responsible Computing Club',
        '00000000-0000-0000-0000-0000000000a1', 'community_started');
insert into public.places (id, google_place_id, name, created_by, governance_state)
values ('20000000-0000-0000-0000-000000000002', 'test:other', 'Other Club',
        '00000000-0000-0000-0000-0000000000f1', 'community_started');

insert into public.circle_affiliations (user_id, circle_type, circle_key, place_ref, status, source)
select u::uuid, 'school', 'rcc', '20000000-0000-0000-0000-000000000001', 'confirmed', 'profile_add'
from unnest(array['00000000-0000-0000-0000-0000000000a1','00000000-0000-0000-0000-0000000000e1',
                  '00000000-0000-0000-0000-0000000000b1','00000000-0000-0000-0000-0000000000c1',
                  '00000000-0000-0000-0000-0000000000d1']) u;

-- harness ----------------------------------------------------------------------
create table pg_temp.results (n serial, label text, ok boolean, detail text);
grant all on pg_temp.results to authenticated;
grant usage on sequence pg_temp.results_n_seq to authenticated;

create or replace function pg_temp.act(p_user text) returns void language sql as $$
  select set_config('request.jwt.claim.sub', p_user, false),
         set_config('request.jwt.claims', '{"is_anonymous":false}', false);
$$;
grant execute on function pg_temp.act(text) to authenticated;

-- run p_sql; expect p_err (null = success) and, when given, the exact text result
create or replace function pg_temp.t(p_label text, p_sql text, p_err text default null, p_want text default null)
returns text language plpgsql as $$
declare v text; v_msg text;
begin
  begin
    execute p_sql into v;
    insert into pg_temp.results (label, ok, detail)
    values (p_label, p_err is null and (p_want is null or v is not distinct from p_want), coalesce(v, '∅'));
    return v;
  exception when others then
    v_msg := sqlerrm;
    insert into pg_temp.results (label, ok, detail)
    values (p_label, p_err is not null and v_msg = p_err, 'raised: ' || v_msg);
    return null;
  end;
end $$;
grant execute on function pg_temp.t(text, text, text, text) to authenticated;

-- The titles a person may be answered from, sorted, as one string.
create or replace function pg_temp.reads(p_user uuid) returns text language sql as $$
  select coalesce(string_agg(d.title, ',' order by d.title), '')
    from public.community_documents d
   where d.id in (select public.community_docs_readable('20000000-0000-0000-0000-000000000001', p_user));
$$;

\set P '''20000000-0000-0000-0000-000000000001'''
\set P2 '''20000000-0000-0000-0000-000000000002'''
\set J '''00000000-0000-0000-0000-0000000000a1'''
\set E '''00000000-0000-0000-0000-0000000000e1'''
\set L '''00000000-0000-0000-0000-0000000000b1'''
\set A '''00000000-0000-0000-0000-0000000000c1'''
\set M '''00000000-0000-0000-0000-0000000000d1'''
\set X '''00000000-0000-0000-0000-0000000000f1'''

-- ── who may manage ──────────────────────────────────────────────────────────
set role authenticated;
select pg_temp.act(:X);
select pg_temp.t('outsider cannot read the ladder', format('select community_levels(%L)::text', :P), 'not_member');
select pg_temp.act(:M);
select pg_temp.t('member cannot add a level', format('select community_level_add(%L, %L)::text', :P, 'Lead'), 'not_manager');
select pg_temp.t('member reads the ladder as member',
  format($q$select community_levels(%L)->>'you'$q$, :P), null, 'member');
select pg_temp.t('member gets no counts',
  format($q$select (community_levels(%L)->'member_count')::text$q$, :P), null, 'null');

-- ── the owner builds RCC's ladder ───────────────────────────────────────────
select pg_temp.act(:J);
select pg_temp.t('owner adds Lead', format('select community_level_add(%L, %L) is not null', :P, 'Lead'), null, 'true');
select pg_temp.t('owner adds Ambassador (lands above Member)', format('select community_level_add(%L, %L) is not null', :P, 'Ambassador'), null, 'true');
select pg_temp.t('owner adds E-board', format('select community_level_add(%L, %L) is not null', :P, 'E-board'), null, 'true');
select pg_temp.t('new levels stack lowest-first as added last',
  format($q$select string_agg(l->>'name', '<' order by (l->>'rank')::int) from jsonb_array_elements(community_levels(%L)->'levels') l$q$, :P),
  null, 'E-board<Ambassador<Lead');
select pg_temp.t('reserved name refused', format('select community_level_add(%L, %L)::text', :P, 'Members'), 'reserved_name');
select pg_temp.t('duplicate name refused', format('select community_level_add(%L, %L)::text', :P, 'lead '), 'name_taken');
reset role;
create temp view community_levels_ids as select id, name from public.community_levels
 where place_id = '20000000-0000-0000-0000-000000000001';
grant select on community_levels_ids to authenticated;
set role authenticated;
select pg_temp.act(:J);
select pg_temp.t('reorder with a missing level refused',
  format($q$select community_levels_reorder(%L, array[(select id from community_levels_ids where name='Lead')])::text$q$, :P),
  'levels_mismatch');
select pg_temp.t('reorder to Ambassador < Lead < E-board',
  format($q$select community_levels_reorder(%L, array[
      (select id from community_levels_ids where name='Ambassador'),
      (select id from community_levels_ids where name='Lead'),
      (select id from community_levels_ids where name='E-board')])::text$q$, :P));
select pg_temp.t('ladder now reads Ambassador<Lead<E-board',
  format($q$select string_agg(l->>'name', '<' order by (l->>'rank')::int) from jsonb_array_elements(community_levels(%L)->'levels') l$q$, :P),
  null, 'Ambassador<Lead<E-board');
select pg_temp.t('rename keeps the rank',
  format($q$select community_level_rename((select id from community_levels_ids where name='E-board'), 'Exec Board')::text$q$));
select pg_temp.t('renamed back',
  format($q$select community_level_rename((select id from community_levels_ids where name='Exec Board'), 'E-board')::text$q$));

-- members get their levels
select pg_temp.t('Eve → E-board', format($q$select community_member_set_level(%L, %L, (select id from community_levels_ids where name='E-board'))::text$q$, :P, :E));
select pg_temp.t('Leo → Lead', format($q$select community_member_set_level(%L, %L, (select id from community_levels_ids where name='Lead'))::text$q$, :P, :L));
select pg_temp.t('Amy → Ambassador', format($q$select community_member_set_level(%L, %L, (select id from community_levels_ids where name='Ambassador'))::text$q$, :P, :A));
select pg_temp.t('outsider cannot be given a level', format($q$select community_member_set_level(%L, %L, (select id from community_levels_ids where name='Lead'))::text$q$, :P, :X), 'not_a_member');
select pg_temp.t('the owner cannot be given a level', format($q$select community_member_set_level(%L, %L, (select id from community_levels_ids where name='Lead'))::text$q$, :P, :J), 'not_a_member');
select pg_temp.t('another community''s level refused', format('select community_member_set_level(%L, %L, gen_random_uuid())::text', :P, :M), 'level_not_found');
select pg_temp.t('member list shows levels, highest first, owner excluded',
  format($q$select string_agg(nickname, ',') from community_level_members(%L)$q$, :P), null, 'Eve,Leo,Amy,Max');
select pg_temp.act(:X);
select pg_temp.t('outsider cannot manage another community''s members', format($q$select count(*)::text from community_level_members(%L)$q$, :P), 'not_manager');
reset role;

-- ── documents at every level (written as the worker would) ──────────────────
insert into public.community_documents (place_id, title, source_mime, storage_path, visibility, min_level_id, review_status)
values
  (:P, 'pub',  'text/markdown', 'x', 'public',  null, 'approved'),
  (:P, 'mem',  'text/markdown', 'x', 'members', null, 'approved'),
  (:P, 'amb',  'text/markdown', 'x', 'members', (select id from community_levels_ids where name='Ambassador'), 'approved'),
  (:P, 'lead', 'text/markdown', 'x', 'members', (select id from community_levels_ids where name='Lead'), 'approved'),
  (:P, 'eb',   'text/markdown', 'x', 'members', (select id from community_levels_ids where name='E-board'), 'approved'),
  (:P, 'own',  'text/markdown', 'x', 'owners',  null, 'approved'),
  (:P, 'pend', 'text/markdown', 'x', 'public',  null, 'pending');

select pg_temp.t('outsider reads public only',  format('select pg_temp.reads(%L)', :X), null, 'pub');
select pg_temp.t('member reads public + members', format('select pg_temp.reads(%L)', :M), null, 'mem,pub');
select pg_temp.t('ambassador adds amb',         format('select pg_temp.reads(%L)', :A), null, 'amb,mem,pub');
select pg_temp.t('lead adds lead',              format('select pg_temp.reads(%L)', :L), null, 'amb,lead,mem,pub');
select pg_temp.t('e-board adds eb',             format('select pg_temp.reads(%L)', :E), null, 'amb,eb,lead,mem,pub');
select pg_temp.t('owner reads all approved, never pending', format('select pg_temp.reads(%L)', :J), null, 'amb,eb,lead,mem,own,pub');
select pg_temp.t('signed-out reads public only', 'select pg_temp.reads(null)', null, 'pub');
select pg_temp.t('a missing level reads as owners-only', 'select community_doc_rank(''members'', gen_random_uuid())::text', null, '1000');
select pg_temp.t('a level on a public doc is refused',
  format($q$insert into public.community_documents (place_id, title, source_mime, storage_path, visibility, min_level_id)
            values (%L, 'bad', 'text/markdown', 'x', 'public', (select id from community_levels_ids where name='Lead')) returning 'inserted'$q$, :P),
  'new row for relation "community_documents" violates check constraint "community_documents_level_needs_members"');
select pg_temp.t('deleting a level that guards documents directly fails',
  $q$delete from public.community_levels where id = (select id from community_levels_ids where name='Lead') returning 'deleted'$q$,
  'update or delete on table "community_levels" violates foreign key constraint "community_documents_min_level_id_fkey" on table "community_documents"');

-- ── approve / log policy ────────────────────────────────────────────────────
select pg_temp.t('no policy: lead cannot approve', format($q$select community_docs_access(%L, %L)->>'can_approve'$q$, :P, :L), null, 'false');
select pg_temp.t('no policy: owner can approve',  format($q$select community_docs_access(%L, %L)->>'can_approve'$q$, :P, :J), null, 'true');
select pg_temp.t('member can upload', format($q$select community_docs_access(%L, %L)->>'can_upload'$q$, :P, :M), null, 'true');
select pg_temp.t('outsider cannot upload', format($q$select community_docs_access(%L, %L)->>'can_upload'$q$, :P, :X), null, 'false');
select pg_temp.t('ambassador access names the level', format($q$select community_docs_access(%L, %L)->>'level_name'$q$, :P, :A), null, 'Ambassador');
set role authenticated;
select pg_temp.act(:M);
select pg_temp.t('member cannot set the policy', format('select community_doc_policy_set(%L, null, null)::text', :P), 'not_manager');
select pg_temp.act(:J);
select pg_temp.t('owner: Lead approves, E-board reads the log',
  format($q$select community_doc_policy_set(%L, (select id from community_levels_ids where name='Lead'), (select id from community_levels_ids where name='E-board'))::text$q$, :P));
select pg_temp.t('another community''s level refused in policy', format('select community_doc_policy_set(%L, gen_random_uuid(), null)::text', :P), 'level_not_found');
reset role;
select pg_temp.t('lead can approve',       format($q$select community_docs_access(%L, %L)->>'can_approve'$q$, :P, :L), null, 'true');
select pg_temp.t('e-board can approve',    format($q$select community_docs_access(%L, %L)->>'can_approve'$q$, :P, :E), null, 'true');
select pg_temp.t('ambassador cannot',      format($q$select community_docs_access(%L, %L)->>'can_approve'$q$, :P, :A), null, 'false');
select pg_temp.t('lead cannot read the log', format($q$select community_docs_access(%L, %L)->>'can_read_log'$q$, :P, :L), null, 'false');
select pg_temp.t('e-board reads the log',  format($q$select community_docs_access(%L, %L)->>'can_read_log'$q$, :P, :E), null, 'true');

-- ── deleting never widens access ────────────────────────────────────────────
set role authenticated;
select pg_temp.act(:J);
select pg_temp.t('delete Lead',
  format($q$select community_level_delete((select id from community_levels_ids where name='Lead'))::text$q$));
reset role;
select pg_temp.t('Leo dropped to Ambassador', format($q$select community_docs_access(%L, %L)->>'level_name'$q$, :P, :L), null, 'Ambassador');
select pg_temp.t('lead doc moved up to E-board: Leo loses it', format('select pg_temp.reads(%L)', :L), null, 'amb,mem,pub');
select pg_temp.t('e-board still reads it', format('select pg_temp.reads(%L)', :E), null, 'amb,eb,lead,mem,pub');
select pg_temp.t('approve right moved up to E-board', format($q$select community_docs_access(%L, %L)->>'can_approve'$q$, :P, :L), null, 'false');
select pg_temp.t('ranks closed to 1..2',
  $q$select string_agg(name || '=' || rank, ',' order by rank) from public.community_levels where place_id = '20000000-0000-0000-0000-000000000001'$q$,
  null, 'Ambassador=1,E-board=2');
set role authenticated;
select pg_temp.act(:J);
select pg_temp.t('delete the top level (E-board)',
  format($q$select community_level_delete((select id from community_levels_ids where name='E-board'))::text$q$));
reset role;
select pg_temp.t('its docs become owners-only', format('select pg_temp.reads(%L)', :E), null, 'amb,mem,pub');
select pg_temp.t('owner still reads them', format('select pg_temp.reads(%L)', :J), null, 'amb,eb,lead,mem,own,pub');
select pg_temp.t('log right falls back to owners', format($q$select community_docs_access(%L, %L)->>'can_read_log'$q$, :P, :E), null, 'false');
set role authenticated;
select pg_temp.act(:J);
select pg_temp.t('delete the bottom level (Ambassador)',
  format($q$select community_level_delete((select id from community_levels_ids where name='Ambassador'))::text$q$));
reset role;
select pg_temp.t('its members are plain members again', format($q$select community_member_rank(%L, %L)::text$q$, :P, :A), null, '0');
select pg_temp.t('its docs went to owners-only (no level above)', format('select pg_temp.reads(%L)', :A), null, 'mem,pub');

-- ── leaving ─────────────────────────────────────────────────────────────────
update public.circle_affiliations set dismissed_at = now()
 where place_ref = :P and user_id = :M;
select pg_temp.t('a member who left reads public only', format('select pg_temp.reads(%L)', :M), null, 'pub');

select n, case when ok then 'PASS' else 'FAIL' end, label, detail from pg_temp.results order by n;
select count(*) filter (where not ok) as failed, count(*) as total from pg_temp.results;

rollback;
