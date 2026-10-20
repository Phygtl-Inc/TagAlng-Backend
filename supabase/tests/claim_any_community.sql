-- 20270211120000_claim_any_community · behaviour checks (local validation container only)
--   psql -h 127.0.0.1 -p 55432 -U supabase_admin -d postgres -f supabase/tests/claim_any_community.sql
-- Needs public._require_verified_neighbor_comms (20260616120000 lines 1-62; that migration
-- stops early in the bare container because auth.jwt() is missing).
-- The Mizu case from prod: a customer starts the restaurant's community and takes its link
-- (self-verified), appoints a co-owner; the real owner claims it; a reviewer approves.
-- Prints one PASS/FAIL row per check and a final failed|total row.

begin;
\set ON_ERROR_STOP 1
set client_min_messages = warning;

create or replace function pg_temp.mkuser(p_id uuid, p_nick text, p_internal boolean default false)
returns void language plpgsql as $$
begin
  insert into auth.users (id, email, email_confirmed_at, is_anonymous)
  values (p_id, p_nick || '@t.test', now(), false) on conflict (id) do nothing;
  insert into public.users (id, nickname) values (p_id, p_nick)
    on conflict (id) do update set nickname = excluded.nickname;
  update public.users
     set phone_verified_at = now(),
         founder_role = case when p_internal then 'internal' end
   where id = p_id;
end $$;

select pg_temp.mkuser('00000000-0000-0000-0000-0000000000a1', 'Starter');
select pg_temp.mkuser('00000000-0000-0000-0000-0000000000a2', 'RealOwner');
select pg_temp.mkuser('00000000-0000-0000-0000-0000000000a3', 'CoOwner');
select pg_temp.mkuser('00000000-0000-0000-0000-0000000000a4', 'Reviewer', true);
select pg_temp.mkuser('00000000-0000-0000-0000-0000000000a5', 'Invitee');

\set S '''00000000-0000-0000-0000-0000000000a1'''
\set R '''00000000-0000-0000-0000-0000000000a2'''
\set K '''00000000-0000-0000-0000-0000000000a3'''
\set A '''00000000-0000-0000-0000-0000000000a4'''
\set I '''00000000-0000-0000-0000-0000000000a5'''
\set P '''20000000-0000-0000-0000-000000000001'''
\set Q '''20000000-0000-0000-0000-000000000002'''
\set H '''20000000-0000-0000-0000-000000000003'''

-- P: the restaurant community a customer (Starter) started. Q: a parent for chapter checks.
insert into public.places (id, google_place_id, name, created_by, governance_state)
values (:P, 'gp-mizu', 'Mizu Sushi & Steakhouse', :S, 'community_started'),
       (:Q, 'gp-food', 'Lake Nona Foodies', :S, 'community_started'),
       (:H, 'gp-plain', 'Corner Gym', :S, 'community_started');
insert into public.circle_affiliations (user_id, circle_type, circle_key, place_ref, status, source)
select u::uuid, 'other', 'mizu', :P, 'confirmed', 'profile_add'
from unnest(array[:S, :R, :K, :I]) u;

create table pg_temp.results (n serial, label text, ok boolean, detail text);
grant all on pg_temp.results to authenticated;
grant usage on sequence pg_temp.results_n_seq to authenticated;

create or replace function pg_temp.act(p_user text) returns void language sql as $$
  select set_config('request.jwt.claim.sub', p_user, false),
         set_config('request.jwt.claims', '{"is_anonymous":false}', false);
$$;
grant execute on function pg_temp.act(text) to authenticated;

create or replace function pg_temp.t(p_label text, p_sql text, p_err text default null)
returns text language plpgsql as $$
declare v text; v_msg text;
begin
  begin
    execute p_sql into v;
    insert into pg_temp.results (label, ok, detail) values (p_label, p_err is null, coalesce(v, '∅'));
    return v;
  exception when others then
    v_msg := sqlerrm;
    insert into pg_temp.results (label, ok, detail)
    values (p_label, p_err is not null and v_msg = p_err, 'raised: ' || v_msg);
    return null;
  end;
end $$;
grant execute on function pg_temp.t(text, text, text) to authenticated;

-- expect a value (not just "no error")
create or replace function pg_temp.eq(p_label text, p_sql text, p_want text)
returns void language plpgsql as $$
declare v text;
begin
  begin
    execute p_sql into v;
    insert into pg_temp.results (label, ok, detail)
    values (p_label, v is not distinct from p_want, coalesce(v, '∅') || ' (want ' || coalesce(p_want, '∅') || ')');
  exception when others then
    insert into pg_temp.results (label, ok, detail) values (p_label, false, 'raised: ' || sqlerrm);
  end;
end $$;
grant execute on function pg_temp.eq(text, text, text) to authenticated;

-- ── setup through the real paths ────────────────────────────────────────────────
-- Starter takes the link: self-verified, exactly what happened to Mizu on prod.
select pg_temp.eq('starter takes the link',
  format($q$select claim_community_handle_for(%L, %L, 'mizusushi')->>'status'$q$, :S, :P), 'claimed');
select pg_temp.eq('community is now operator_verified',
  format('select governance_state from places where id = %L', :P), 'operator_verified');

set role authenticated;
select pg_temp.act(:S);
select pg_temp.t('starter invites a co-owner', format('select propose_coowner(%L, %L) is not null', :P, :K));
select pg_temp.t('starter invites another (left pending)', format('select propose_coowner(%L, %L) is not null', :P, :I));
select pg_temp.act(:K);
select pg_temp.t('co-owner accepts', $q$select accept_coowner_invite((select invite_id from get_my_coowner_invites('received') where status='proposed' limit 1))::text$q$);
select pg_temp.t('co-owner can edit before takeover', format('select (community_settings(%L) is not null)::text', :P));
reset role;

-- ── 1 · submit ──────────────────────────────────────────────────────────────────
select pg_temp.t('real owner can claim a verified community',
  format($q$select submit_community_claim_for(%L, %L, 'owner@mizu.test', null, 'my restaurant')->>'status'$q$, :R, :P));
select pg_temp.t('second claim on the same community refused',
  format($q$select submit_community_claim_for(%L, %L, 'k@t.test')::text$q$, :K, :P), 'claim_open');
select pg_temp.t('owner claiming her own community: already_yours (not "taken")',
  format($q$select submit_community_claim_for(%L, %L, 's@t.test')::text$q$, :S, :Q), 'already_yours');
select pg_temp.t('self-verified owner claiming her own: already_yours',
  format($q$select submit_community_claim_for(%L, %L, 's@t.test')::text$q$, :S, :P), 'already_yours');
update public.places set governance_state = 'suspended' where id = :H;
select pg_temp.t('suspended still refused',
  format($q$select submit_community_claim_for(%L, %L, 'k@t.test')::text$q$, :K, :H), 'already_claimed');
select pg_temp.eq('pending claim leaves the verified state alone',
  format('select governance_state from places where id = %L', :P), 'operator_verified');

-- ── 2 · approve = takeover ──────────────────────────────────────────────────────
-- place_claims has no client policies; look the id up as the test owner.
select id as takeover_claim from public.place_claims
 where place_id = :P and requested_by = :R and status = 'pending_verification' \gset
set role authenticated;
select pg_temp.act(:A);
select pg_temp.eq('reviewer approves the takeover',
  format($q$select approve_place_claim(%L)->>'status'$q$, :'takeover_claim'), 'verified');
reset role;

select pg_temp.eq('link unchanged by the takeover', format('select handle from places where id = %L', :P), 'mizusushi');
select pg_temp.eq('real owner is the only live manager',
  format($q$select string_agg(u.nickname || ':' || m.role, ',') from place_managers m join users u on u.id = m.user_id where m.place_id = %L and m.removed_at is null$q$, :P),
  'RealOwner:operator');
select pg_temp.eq('claimed_by moved', format('select claimed_by::text from places where id = %L', :P), '00000000-0000-0000-0000-0000000000a2');
select pg_temp.eq('pending co-owner invite revoked',
  format($q$select status from community_coowner_invites where place_id = %L and candidate_id = %L order by created_at desc limit 1$q$, :P, :I), 'revoked');

set role authenticated;
select pg_temp.act(:R);
select pg_temp.t('new owner can edit', format('select (community_settings(%L) is not null)::text', :P));
select pg_temp.eq('new owner sees herself as owner', format($q$select community_coowners(%L)->>'you'$q$, :P), 'owner');
select pg_temp.act(:S);
select pg_temp.t('old owner can no longer edit', format('select community_settings(%L)::text', :P), 'not_operator');
select pg_temp.eq('old owner is a plain member now', format($q$select community_coowners(%L)->>'you'$q$, :P), 'member');
select pg_temp.t('old owner cannot invite co-owners', format('select propose_coowner(%L, %L)::text', :P, :I), 'not_owner');
select pg_temp.act(:K);
select pg_temp.t('old co-owner can no longer edit', format('select community_settings(%L)::text', :P), 'not_operator');
reset role;

-- ── 4 · chapters: started it ≠ runs it once someone else is verified ────────────
select pg_temp.eq('old starter is no longer the starter', format('select _community_starter(%L, %L)::text', :P, :S), 'false');
select pg_temp.eq('starter of an unverified community still is', format('select _community_starter(%L, %L)::text', :Q, :S), 'true');
select pg_temp.t('old starter cannot make it a chapter',
  format('select attach_chapter(%L, %L, %L)::text', :S, :P, :Q), 'not_your_community');
select pg_temp.t('new owner can make it a chapter? (not a member of the parent)',
  format('select attach_chapter(%L, %L, %L)::text', :R, :P, :Q), 'not_a_member_of_parent');

-- ── regression: the ordinary first claim on an unclaimed community still works ──
insert into public.places (id, google_place_id, name, created_by, governance_state)
values ('20000000-0000-0000-0000-000000000009', 'gp-cafe', 'Corner Cafe', :S, 'community_started');
select pg_temp.t('first claim on a started community',
  format($q$select submit_community_claim_for(%L, '20000000-0000-0000-0000-000000000009', 'r@t.test')->>'status'$q$, :I));
select pg_temp.eq('it goes claim_pending', $q$select governance_state from places where id = '20000000-0000-0000-0000-000000000009'$q$, 'claim_pending');
select id as first_claim from public.place_claims
 where place_id = '20000000-0000-0000-0000-000000000009' and status = 'pending_verification' \gset
set role authenticated;
select pg_temp.act(:A);
select pg_temp.eq('approved', format($q$select approve_place_claim(%L)->>'status'$q$, :'first_claim'), 'verified');
reset role;
select pg_temp.eq('gets a generated link', $q$select (handle is not null)::text from places where id = '20000000-0000-0000-0000-000000000009'$q$, 'true');
select pg_temp.eq('claimant is its operator',
  $q$select string_agg(u.nickname, ',') from place_managers m join users u on u.id = m.user_id where m.place_id = '20000000-0000-0000-0000-000000000009' and m.removed_at is null and m.role = 'operator'$q$, 'Invitee');

select n, case when ok then 'PASS' else 'FAIL' end, label, detail from pg_temp.results order by n;
select count(*) filter (where not ok) as failed, count(*) as total from pg_temp.results;

rollback;
