-- 20270209120000_community_coowners · behaviour checks (local validation container only)
--   psql -h 127.0.0.1 -p 55432 -U supabase_admin -d postgres -f supabase/tests/community_coowners.sql
-- Runs as `authenticated` with request.jwt.claim.sub switched per actor, so grants are
-- exercised. Needs public._require_verified_neighbor_comms (20260616120000 lines 1-62;
-- that migration stops early in the bare container because auth.jwt() is missing).
-- Prints one PASS/FAIL row per check and a final failed|total row; expect 0|48.

begin;
\set ON_ERROR_STOP 1
set client_min_messages = warning;

-- fixtures (superuser) --------------------------------------------------------
create or replace function pg_temp.mkuser(p_id uuid, p_nick text, p_verified boolean) returns void
language plpgsql as $$
begin
  insert into auth.users (id, email) values (p_id, p_nick || '@t.test') on conflict (id) do nothing;
  insert into public.users (id, nickname) values (p_id, p_nick)
    on conflict (id) do update set nickname = excluded.nickname;
  update public.users set phone_verified_at = case when p_verified then now() end where id = p_id;
end $$;

select pg_temp.mkuser('00000000-0000-0000-0000-00000000000a', 'Owner', true);
select pg_temp.mkuser('00000000-0000-0000-0000-00000000000c', 'Cora', true);
select pg_temp.mkuser('00000000-0000-0000-0000-00000000000d', 'Dev', true);
select pg_temp.mkuser('00000000-0000-0000-0000-00000000000e', 'Outsider', true);
select pg_temp.mkuser('00000000-0000-0000-0000-00000000000f', 'Unverified', false);
select pg_temp.mkuser('00000000-0000-0000-0000-0000000000b0', 'Op', true);

-- P1: started community, owner = created_by (no place_managers row)
insert into public.places (id, google_place_id, name, created_by, governance_state)
values ('10000000-0000-0000-0000-000000000001', 'test:p1', 'Stroller Club',
        '00000000-0000-0000-0000-00000000000a', 'community_started');
-- P2: verified community, owner = place_managers operator (Op), created_by someone else
insert into public.places (id, google_place_id, name, created_by, governance_state)
values ('10000000-0000-0000-0000-000000000002', 'test:p2', 'Run Club',
        '00000000-0000-0000-0000-00000000000e', 'claim_pending');
insert into public.place_managers (place_id, user_id, role, verification_method)
values ('10000000-0000-0000-0000-000000000002', '00000000-0000-0000-0000-0000000000b0', 'operator', 'test');

insert into public.circle_affiliations (user_id, circle_type, circle_key, place_ref, status, source)
select u::uuid, 'hobby', 'stroller_club', '10000000-0000-0000-0000-000000000001', 'confirmed', 'profile_add'
from unnest(array['00000000-0000-0000-0000-00000000000a','00000000-0000-0000-0000-00000000000c',
                  '00000000-0000-0000-0000-00000000000d','00000000-0000-0000-0000-00000000000f']) u;
insert into public.circle_affiliations (user_id, circle_type, circle_key, place_ref, status, source)
select u::uuid, 'fitness', 'run_club', '10000000-0000-0000-0000-000000000002', 'confirmed', 'profile_add'
from unnest(array['00000000-0000-0000-0000-0000000000b0','00000000-0000-0000-0000-00000000000c']) u;

-- harness ----------------------------------------------------------------------
create table pg_temp.results (n serial, label text, ok boolean, detail text);
grant all on pg_temp.results to authenticated;
grant usage on sequence pg_temp.results_n_seq to authenticated;

create or replace function pg_temp.act(p_user text) returns void language sql as $$
  select set_config('request.jwt.claim.sub', p_user, false),
         set_config('request.jwt.claims', '{"is_anonymous":false}', false);
$$;
grant execute on function pg_temp.act(text) to authenticated;

-- run p_sql as the current actor; expect p_err (null = success)
create or replace function pg_temp.t(p_label text, p_sql text, p_err text default null)
returns text language plpgsql as $$
declare v text; v_msg text;
begin
  begin
    execute p_sql into v;
    insert into pg_temp.results (label, ok, detail)
    values (p_label, p_err is null, coalesce(v, '∅'));
    return v;
  exception when others then
    v_msg := sqlerrm;
    insert into pg_temp.results (label, ok, detail)
    values (p_label, p_err is not null and v_msg = p_err, 'raised: ' || v_msg);
    return null;
  end;
end $$;
grant execute on function pg_temp.t(text, text, text) to authenticated;

\set O '''00000000-0000-0000-0000-00000000000a'''
\set C '''00000000-0000-0000-0000-00000000000c'''
\set D '''00000000-0000-0000-0000-00000000000d'''
\set X '''00000000-0000-0000-0000-00000000000e'''
\set U '''00000000-0000-0000-0000-00000000000f'''
\set OP '''00000000-0000-0000-0000-0000000000b0'''
\set P1 '''10000000-0000-0000-0000-000000000001'''
\set P2 '''10000000-0000-0000-0000-000000000002'''

set role authenticated;

-- ── invite gates ──────────────────────────────────────────────────────────────
select pg_temp.act(:X);
select pg_temp.t('outsider cannot invite', format('select propose_coowner(%L, %L)::text', :P1, :C), 'not_owner');
select pg_temp.t('outsider cannot read roster', format('select community_coowners(%L)::text', :P1), 'not_member');
select pg_temp.t('outsider cannot list candidates', format('select count(*)::text from coowner_candidates(%L)', :P1), 'not_owner');

select pg_temp.act(:U);
select pg_temp.t('unverified cannot invite', format('select propose_coowner(%L, %L)::text', :P1, :C), 'phone_not_verified');

select pg_temp.act(:O);
select pg_temp.t('owner: non-member refused', format('select propose_coowner(%L, %L)::text', :P1, :X), 'not_a_member');
select pg_temp.t('owner: self refused', format('select propose_coowner(%L, %L)::text', :P1, :O), 'invalid_candidate');
select pg_temp.t('candidates = members minus owner (3)', format('select count(*)::text from coowner_candidates(%L)', :P1));
select pg_temp.t('candidate search', format('select string_agg(nickname, '','') from coowner_candidates(%L, %L)', :P1, 'co'));
select pg_temp.t('owner invites Cora', format('select propose_coowner(%L, %L) is not null', :P1, :C));
select pg_temp.t('second invite refused', format('select propose_coowner(%L, %L)::text', :P1, :C), 'coowner_invite_pending');
select pg_temp.t('invite lands in chat', format(
  $q$select string_agg(m.content, ' | ') from messages m join community_coowner_invites i on i.thread_id = m.thread_id
     where i.place_id = %L and m.kind = 'lana' and m.content like 'Co-owner invite%%'$q$, :P1));
select pg_temp.t('owner roster shows pending', format($q$select community_coowners(%L)->'pending'->0->>'name'$q$, :P1));
select pg_temp.t('candidate flagged invited', format('select already_invited::text from coowner_candidates(%L) where user_id = %L', :P1, :C));
select pg_temp.t('client cannot write invites directly', format(
  $q$insert into community_coowner_invites (place_id, owner_id, candidate_id) values (%L, %L, %L) returning 'x'$q$, :P1, :O, :D),
  'permission denied for table community_coowner_invites');

-- ── before accepting, Cora cannot edit ──────────────────────────────────────────
select pg_temp.act(:C);
select pg_temp.t('pre-accept: settings refused', format('select community_settings(%L)::text', :P1), 'not_operator');
select pg_temp.t('received invite visible', $q$select place_name || '/' || owner_name || '/' || status from get_my_coowner_invites('received') limit 1$q$);
select pg_temp.act(:D);
select pg_temp.t('other user cannot accept', format(
  $q$select accept_coowner_invite((select invite_id from get_my_coowner_invites('all') limit 1))::text$q$), 'coowner_invite_not_found');

-- ── accept → edit rights ────────────────────────────────────────────────────────
select pg_temp.act(:C);
select pg_temp.t('Cora accepts', $q$select accept_coowner_invite((select invite_id from get_my_coowner_invites('received') where status='proposed' limit 1))::text$q$);
select pg_temp.t('co-owner: settings readable', format('select (community_settings(%L) is not null)::text', :P1));
select pg_temp.t('co-owner: can rename', format($q$select update_community_settings(%L, 'Stroller Club ✨', null, null)::text$q$, :P1));
select pg_temp.t('co-owner roster: you=coowner, no pending', format(
  $q$select (community_coowners(%L)->>'you') || '/' || jsonb_array_length(community_coowners(%L)->'pending')$q$, :P1, :P1));
select pg_temp.t('co-owner cannot invite', format('select propose_coowner(%L, %L)::text', :P1, :D), 'not_owner');
select pg_temp.t('co-owner cannot list candidates', format('select count(*)::text from coowner_candidates(%L)', :P1), 'not_owner');
select pg_temp.t('co-owner cannot remove owner', format('select remove_coowner(%L, %L)', :P1, :O), 'not_owner');

-- ── member view ─────────────────────────────────────────────────────────────────
select pg_temp.act(:D);
select pg_temp.t('member roster: owners+coowners', format(
  $q$select (community_coowners(%L)->>'you') || ':' || (community_coowners(%L)->'owners'->0->>'name') || ':' || (community_coowners(%L)->'coowners'->0->>'name')$q$, :P1, :P1, :P1));
select pg_temp.t('member cannot edit', format('select community_settings(%L)::text', :P1), 'not_operator');

-- ── owner stays the owner ───────────────────────────────────────────────────────
select pg_temp.act(:O);
select pg_temp.t('owner cannot "remove" self', format('select remove_coowner(%L, %L)', :P1, :O), 'not_coowner');
select pg_temp.t('candidates now exclude Cora', format('select count(*)::text from coowner_candidates(%L)', :P1));

-- ── step down, re-invite, remove ────────────────────────────────────────────────
select pg_temp.act(:C);
select pg_temp.t('Cora steps down', format('select remove_coowner(%L, %L)', :P1, :C));
select pg_temp.t('after step-down: settings refused', format('select community_settings(%L)::text', :P1), 'not_operator');
select pg_temp.act(:O);
select pg_temp.t('re-invite Cora', format('select propose_coowner(%L, %L) is not null', :P1, :C));
select pg_temp.act(:C);
select pg_temp.t('Cora re-accepts (row revived)', $q$select accept_coowner_invite((select invite_id from get_my_coowner_invites('received') where status='proposed' limit 1))::text$q$);
select pg_temp.t('revived: can edit again', format('select (community_settings(%L) is not null)::text', :P1));
select pg_temp.act(:O);
select pg_temp.t('owner removes Cora', format('select remove_coowner(%L, %L)', :P1, :C));
select pg_temp.act(:C);
select pg_temp.t('removed: settings refused', format('select community_settings(%L)::text', :P1), 'not_operator');

-- ── decline + withdraw ──────────────────────────────────────────────────────────
select pg_temp.act(:O);
select pg_temp.t('invite Dev', format('select propose_coowner(%L, %L) is not null', :P1, :D));
select pg_temp.t('owner withdraws Dev', format('select remove_coowner(%L, %L)', :P1, :D));
select pg_temp.act(:D);
select pg_temp.t('withdrawn invite cannot be accepted', $q$select accept_coowner_invite((select invite_id from get_my_coowner_invites('received') limit 1))::text$q$, 'coowner_invite_not_found');
select pg_temp.act(:O);
select pg_temp.t('invite Dev again', format('select propose_coowner(%L, %L) is not null', :P1, :D));
select pg_temp.act(:D);
select pg_temp.t('Dev declines', $q$select decline_coowner_invite((select invite_id from get_my_coowner_invites('received') where status='proposed' limit 1))::text$q$);
select pg_temp.t('declined: no rights', format('select community_settings(%L)::text', :P1), 'not_operator');

-- ── operator-owned community (P2) ───────────────────────────────────────────────
select pg_temp.act(:X);
select pg_temp.t('P2 creator who is not operator cannot invite', format('select propose_coowner(%L, %L)::text', :P2, :C), 'not_owner');
select pg_temp.act(:OP);
select pg_temp.t('P2 operator invites Cora', format('select propose_coowner(%L, %L) is not null', :P2, :C));
select pg_temp.act(:C);
select pg_temp.t('P2 Cora accepts', $q$select accept_coowner_invite((select invite_id from get_my_coowner_invites('received') where status='proposed' limit 1))::text$q$);
select pg_temp.t('P2 co-owner can edit', format('select (community_settings(%L) is not null)::text', :P2));

reset role;
-- the owner pointer is untouched by co-owners
select pg_temp.t('P2 still exactly one operator', format(
  $q$select count(*)::text from place_managers where place_id = %L and role='operator' and removed_at is null$q$, :P2));

-- lapse: owner of P1 loses ownership (community moves to claim_pending under Op)
update public.places set governance_state = 'claim_pending' where id = :P1;
insert into public.place_managers (place_id, user_id, role, verification_method)
values (:P1, :OP, 'operator', 'test');
set role authenticated;
select pg_temp.act(:O);
select pg_temp.t('ex-owner can no longer invite', format('select propose_coowner(%L, %L)::text', :P1, :D), 'not_owner');
reset role;
insert into public.community_coowner_invites (place_id, owner_id, candidate_id) values (:P1, :O, :D) on conflict do nothing;
set role authenticated;
select pg_temp.act(:D);
select pg_temp.t('stale invite lapses on accept', $q$select accept_coowner_invite((select invite_id from get_my_coowner_invites('received') where status='proposed' limit 1))::text$q$, 'coowner_invite_lapsed');
reset role;

select n, case when ok then 'PASS' else 'FAIL' end, label, detail from pg_temp.results order by n;
select count(*) filter (where not ok) as failed, count(*) as total from pg_temp.results;

rollback;
