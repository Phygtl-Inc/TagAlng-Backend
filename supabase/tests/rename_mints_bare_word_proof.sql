-- 20270202120000_rename_mints_bare_word_proof · behaviour checks
-- (local validation container only — never against dev or prod)
--   psql -h 127.0.0.1 -p 55432 -U supabase_admin -d postgres -v ON_ERROR_STOP=1 \
--        -f supabase/tests/rename_mints_bare_word_proof.sql
-- Everything runs in one transaction and is rolled back. Any failed assert aborts it.

begin;

insert into auth.users (id, email, email_confirmed_at, is_anonymous) values
  ('00000000-0000-0000-0000-0000000ab001', 'pod-op@example.com',     now(), false),
  ('00000000-0000-0000-0000-0000000ab002', 'unconfirmed@example.com', null,  false),
  ('00000000-0000-0000-0000-0000000ab003', 'stranger2@example.com',  now(), false),
  ('00000000-0000-0000-0000-0000000ab004', 'bare-op@example.com',    now(), false)
on conflict (id) do nothing;

update public.users set handle = 'mayaruns' where id = '00000000-0000-0000-0000-0000000ab003';

insert into public.protected_handles (normalized_handle, reason, active)
values ('nikeword', 'test brand', true)
on conflict (normalized_handle) do update set active = true;

-- ab101 Podcasters, on a dashed link, run by a confirmed operator.
-- ab102 a community whose operator never confirmed an email.
-- ab103 already on a proven bare word, renaming to another.
-- A handle needs a verified place (places_handle_needs_verification), as the real
-- Podcasters community is: a verified claim, then the handle.
insert into public.places (id, google_place_id, name, lat, lng, created_by) values
  ('00000000-0000-0000-0000-0000000ab101', 'rb-pod',  'Podcasters', 37.33, -121.88, '00000000-0000-0000-0000-0000000ab001'),
  ('00000000-0000-0000-0000-0000000ab102', 'rb-unc',  'Quiet Club', 37.33, -121.88, '00000000-0000-0000-0000-0000000ab002'),
  ('00000000-0000-0000-0000-0000000ab103', 'rb-bare', 'Bare Club',  37.33, -121.88, '00000000-0000-0000-0000-0000000ab004');
insert into public.place_claims (place_id, requested_by, status, verification_method, submitted_at, resolved_at) values
  ('00000000-0000-0000-0000-0000000ab101', '00000000-0000-0000-0000-0000000ab001', 'verified', 'manual_founder', now(), now()),
  ('00000000-0000-0000-0000-0000000ab102', '00000000-0000-0000-0000-0000000ab002', 'verified', 'manual_founder', now(), now());
update public.places set governance_state = 'operator_verified', verified_at = now(),
       claimed_by = '00000000-0000-0000-0000-0000000ab001', handle = 'podcasters-sj'
 where id = '00000000-0000-0000-0000-0000000ab101';
update public.places set governance_state = 'operator_verified', verified_at = now(),
       claimed_by = '00000000-0000-0000-0000-0000000ab002', handle = 'quiet-club-sj'
 where id = '00000000-0000-0000-0000-0000000ab102';
update public.places set claimed_by = '00000000-0000-0000-0000-0000000ab004' where id = '00000000-0000-0000-0000-0000000ab103';

-- ab103 takes "bareclub" the way "Claim your link" does: that is its existing proof.
select set_config('request.jwt.claim.sub', '00000000-0000-0000-0000-0000000ab004', true);
do $$
declare j jsonb;
begin
  j := public.claim_community_handle('00000000-0000-0000-0000-0000000ab103', 'bareclub');
  assert j->>'status' = 'claimed', 'setup claim ' || j::text;
end;
$$;

-- ── 1 · check_community_link ─────────────────────────────────────────────────

do $$
declare j jsonb;
begin
  perform set_config('request.jwt.claim.sub', '00000000-0000-0000-0000-0000000ab001', true);
  j := public.check_community_link('00000000-0000-0000-0000-0000000ab101', 'podcaster');
  assert j->>'status' = 'available', 'bare word, confirmed operator ' || j::text;
  j := public.check_community_link('00000000-0000-0000-0000-0000000ab101', 'mayaruns');
  assert j->>'status' = 'unavailable' and j->>'reason' = 'member', 'member handle ' || j::text;
  j := public.check_community_link('00000000-0000-0000-0000-0000000ab101', 'nikeword');
  assert j->>'status' = 'unavailable' and j->>'reason' = 'protected', 'protected ' || j::text;
  j := public.check_community_link('00000000-0000-0000-0000-0000000ab101', 'bareclub');
  assert j->>'status' = 'unavailable' and j->>'reason' = 'taken', 'taken bare ' || j::text;

  perform set_config('request.jwt.claim.sub', '00000000-0000-0000-0000-0000000ab002', true);
  j := public.check_community_link('00000000-0000-0000-0000-0000000ab102', 'quietclub');
  assert j->>'status' = 'invalid' and j->>'reason' = 'sign_in_required',
    'bare word, unconfirmed operator ' || j::text;
  j := public.check_community_link('00000000-0000-0000-0000-0000000ab102', 'quiet-club-orl');
  assert j->>'status' = 'available', 'dashed word, unconfirmed operator ' || j::text;
end;
$$;

-- ── 2 · refusals write nothing ───────────────────────────────────────────────

do $$
declare
  n_res int := (select count(*) from public.place_handle_reservations);
  n_cl  int := (select count(*) from public.place_claims);
  refused text;
begin
  -- Not an operator.
  perform set_config('request.jwt.claim.sub', '00000000-0000-0000-0000-0000000ab003', true);
  begin
    perform public.rename_community_handle('00000000-0000-0000-0000-0000000ab101', 'podcaster');
    refused := null;
  exception when others then refused := sqlerrm;
  end;
  assert refused = 'not_operator', 'stranger refused: ' || coalesce(refused, 'renamed');

  -- Unconfirmed email.
  perform set_config('request.jwt.claim.sub', '00000000-0000-0000-0000-0000000ab002', true);
  begin
    perform public.rename_community_handle('00000000-0000-0000-0000-0000000ab102', 'quietclub');
    refused := null;
  exception when others then refused := sqlerrm;
  end;
  assert refused = 'handle_needs_confirmed_email', 'unconfirmed refused: ' || coalesce(refused, 'renamed');
  -- A member's handle is the reason, whatever the email: it is refused first.
  begin
    perform public.rename_community_handle('00000000-0000-0000-0000-0000000ab102', 'mayaruns');
    refused := null;
  exception when others then refused := sqlerrm;
  end;
  assert refused = 'handle_taken_by_user', 'unconfirmed, member: ' || coalesce(refused, 'renamed');

  -- A member's handle, and a protected word.
  perform set_config('request.jwt.claim.sub', '00000000-0000-0000-0000-0000000ab001', true);
  begin
    perform public.rename_community_handle('00000000-0000-0000-0000-0000000ab101', 'mayaruns');
    refused := null;
  exception when others then refused := sqlerrm;
  end;
  assert refused = 'handle_taken_by_user', 'member refused: ' || coalesce(refused, 'renamed');
  begin
    perform public.rename_community_handle('00000000-0000-0000-0000-0000000ab101', 'nikeword');
    refused := null;
  exception when others then refused := sqlerrm;
  end;
  assert refused = 'handle_protected', 'protected refused: ' || coalesce(refused, 'renamed');

  assert (select count(*) from public.place_handle_reservations) = n_res, 'no reservation written';
  assert (select count(*) from public.place_claims) = n_cl, 'no claim written';
  assert (select handle_renamed_at from public.places
           where id = '00000000-0000-0000-0000-0000000ab101') is null, 'rename not spent';
end;
$$;

-- ── 3 · the screenshot: Podcasters → podcaster ──────────────────────────────

do $$
declare
  j jsonb;
  refused text;
  pod constant uuid := '00000000-0000-0000-0000-0000000ab101';
begin
  perform set_config('request.jwt.claim.sub', '00000000-0000-0000-0000-0000000ab001', true);
  j := public.rename_community_handle(pod, 'podcaster');
  assert j->>'handle' = 'podcaster' and j->>'previousHandle' = 'podcasters-sj', 'renamed ' || j::text;
  assert (select handle from public.places where id = pod) = 'podcaster', 'place holds the word';
  assert public._place_handle_proven(pod, 'podcaster'), 'proof minted';
  assert exists (select 1 from public.place_claims c
                   join public.place_handle_reservations r on r.id = c.reservation_id
                  where c.place_id = pod and c.status = 'verified'
                    and c.verification_method = 'reservation_email'
                    and c.requested_by = '00000000-0000-0000-0000-0000000ab001'
                    and r.normalized_handle = 'podcaster' and r.status = 'consumed'),
    'claim + consumed reservation';
  assert exists (select 1 from public.place_handle_aliases
                  where handle = 'podcasters-sj' and place_id = pod), 'old link kept alive';

  -- Still once only.
  j := public.check_community_link(pod, 'podcast-club');
  assert j->>'status' = 'rename_used', 'check says used ' || j::text;
  begin
    perform public.rename_community_handle(pod, 'podcastclub');
    refused := null;
  exception when others then refused := sqlerrm;
  end;
  assert refused = 'rename_already_used', 'second rename refused: ' || coalesce(refused, 'renamed');
end;
$$;

-- ── 4 · from one proven bare word to another: the old proof stays ────────────

do $$
declare
  j jsonb;
  bare constant uuid := '00000000-0000-0000-0000-0000000ab103';
begin
  perform set_config('request.jwt.claim.sub', '00000000-0000-0000-0000-0000000ab004', true);
  j := public.rename_community_handle(bare, 'bareclubs');
  assert j->>'handle' = 'bareclubs', 'renamed ' || j::text;
  assert public._place_handle_proven(bare, 'bareclubs'), 'new word proven';
  assert public._place_handle_proven(bare, 'bareclub'), 'old word still proven';
  assert exists (select 1 from public.place_handle_aliases
                  where handle = 'bareclub' and place_id = bare), 'old bare link kept alive';
  -- Nobody else can take the retired word.
  perform set_config('request.jwt.claim.sub', '00000000-0000-0000-0000-0000000ab002', true);
  j := public.check_community_link('00000000-0000-0000-0000-0000000ab102', 'bareclub');
  assert j->>'status' = 'unavailable' and j->>'reason' = 'retired', 'retired word ' || j::text;
end;
$$;

-- ── 5 · a dashed rename writes no proof ──────────────────────────────────────

do $$
declare
  n_cl int := (select count(*) from public.place_claims);
begin
  perform set_config('request.jwt.claim.sub', '00000000-0000-0000-0000-0000000ab002', true);
  perform public.rename_community_handle('00000000-0000-0000-0000-0000000ab102', 'quiet-club-orl');
  assert (select handle from public.places where id = '00000000-0000-0000-0000-0000000ab102') = 'quiet-club-orl',
    'dashed rename works without a confirmed email';
  assert (select count(*) from public.place_claims) = n_cl, 'no claim for a dashed word';
end;
$$;

rollback;
\echo 'rename_mints_bare_word_proof: all checks passed'
