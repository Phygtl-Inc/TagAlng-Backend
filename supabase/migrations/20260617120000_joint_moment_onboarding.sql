-- Joint moment onboarding (Phase 2 + 3 backend): candidate card + accept → OTP → intro nudge.
-- FE renders the purple card; these RPCs supply data and gate peer actions.

-- Demo candidate for v0.1 when vector match unavailable (guest / no block / no peers).
-- Seeded in seed.sql as nickname Maria on Lake Nona block B.
create or replace function public._joint_moment_demo_user_id()
returns uuid
language sql
immutable
as $$
  select 'a0000004-0004-4000-8000-000000000004'::uuid;
$$;

create table if not exists public.joint_moment_impressions (
  id uuid primary key default gen_random_uuid(),
  viewer_user_id uuid not null references public.users (id) on delete cascade,
  candidate_user_id uuid not null references public.users (id) on delete cascade,
  lana_session_id uuid references public.lana_sessions (id) on delete set null,
  status text not null default 'proposed'
    check (status in ('proposed', 'accepted', 'declined', 'snoozed', 'intro_sent', 'expired')),
  match_reason text not null,
  shared_dimensions text[] not null default '{}',
  similarity_score real,
  is_demo boolean not null default false,
  nudge_id uuid references public.nudges (id) on delete set null,
  created_at timestamptz not null default now(),
  responded_at timestamptz,
  intro_sent_at timestamptz
);

comment on table public.joint_moment_impressions is
  'Onboarding joint-moment card: proposed match, user response, intro nudge linkage.';

create index if not exists joint_moment_impressions_viewer_created_idx
  on public.joint_moment_impressions (viewer_user_id, created_at desc);

create unique index if not exists joint_moment_impressions_one_open_per_viewer
  on public.joint_moment_impressions (viewer_user_id)
  where status in ('proposed', 'accepted');

alter table public.joint_moment_impressions enable row level security;

create policy "joint_moment_impressions_select_own"
  on public.joint_moment_impressions for select
  to authenticated
  using (viewer_user_id = auth.uid());

create policy "joint_moment_impressions_no_client_write"
  on public.joint_moment_impressions for all
  to authenticated
  using (false)
  with check (false);

create or replace function public._joint_moment_candidate_card(p_user_id uuid)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public
as $$
declare
  v_peer record;
  v_badges jsonb := '[]'::jsonb;
  v_labels jsonb := '[]'::jsonb;
  v_subtitle text;
begin
  select
    u.id,
    u.nickname,
    u.profile_photo_url,
    u.home_block_id,
    b.display_name as block_display_name
  into v_peer
  from public.users u
  left join public.blocks b on b.id = u.home_block_id
  where u.id = p_user_id;

  if not found then
    raise exception 'candidate_not_found' using errcode = 'P0001';
  end if;

  select coalesce(
    (
      select jsonb_agg(x.label)
      from (
        select c.label
        from public.user_identity_claims c
        where c.user_id = p_user_id
          and c.dismissed_at is null
          and c.disclosure = 'public'
        order by c.confidence desc
        limit 4
      ) x
    ),
    '[]'::jsonb
  )
  into v_labels;

  select c.label into v_subtitle
  from public.user_identity_claims c
  where c.user_id = p_user_id
    and c.dismissed_at is null
    and c.disclosure = 'public'
    and c.concept like '%heritage%'
  order by c.confidence desc
  limit 1;

  if v_subtitle is null then
    select c.label into v_subtitle
    from public.user_identity_claims c
    where c.user_id = p_user_id
      and c.dismissed_at is null
      and c.disclosure = 'public'
    order by c.confidence desc
    limit 1;
  end if;

  select coalesce(
    jsonb_agg(x.badge),
    '[]'::jsonb
  )
  into v_badges
  from (
    select c.label as badge
    from public.user_identity_claims c
    where c.user_id = p_user_id
      and c.dismissed_at is null
      and c.disclosure = 'public'
      and c.concept like '%stage%'
    order by c.confidence desc
    limit 1
  ) x;

  if v_peer.block_display_name is not null then
    v_badges := v_badges || jsonb_build_array('on your block');
  end if;

  return jsonb_build_object(
    'user_id', v_peer.id,
    'nickname', coalesce(v_peer.nickname, 'A neighbor'),
    'subtitle', coalesce(v_subtitle, v_peer.block_display_name, 'on your block'),
    'avatar_url', v_peer.profile_photo_url,
    'badges', v_badges,
    'public_labels', v_labels
  );
end;
$$;

create or replace function public.get_joint_moment_candidate(
  p_session_id uuid default null
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, extensions
as $$
declare
  v_viewer uuid := auth.uid();
  v_existing record;
  v_match record;
  v_candidate_id uuid;
  v_impression_id uuid;
  v_is_demo boolean := false;
  v_reason text;
  v_dims text[] := '{}';
  v_sim real;
  v_copy text;
  v_card jsonb;
  v_nick text;
begin
  if v_viewer is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;

  select j.*
  into v_existing
  from public.joint_moment_impressions j
  where j.viewer_user_id = v_viewer
    and j.status in ('proposed', 'accepted')
  order by j.created_at desc
  limit 1;

  if found then
    v_card := public._joint_moment_candidate_card(v_existing.candidate_user_id);
    v_nick := v_card->>'nickname';
    v_copy := format(
      '%s told me she''s looking for %s. Want me to put you two together?',
      v_nick,
      v_existing.match_reason
    );
    return jsonb_build_object(
      'joint_moment_id', v_existing.id,
      'status', v_existing.status,
      'candidate', v_card,
      'lana_copy', v_copy,
      'match_reason', v_existing.match_reason,
      'shared_dimensions', v_existing.shared_dimensions,
      'similarity_score', v_existing.similarity_score,
      'is_demo', v_existing.is_demo
    );
  end if;

  select
    m.peer_user_id,
    m.similarity_score,
    m.matching_peer_label,
    m.matching_peer_concept
  into v_match
  from public.match_peers_by_claim_vectors_for_user(v_viewer, 1, 0.60) m
  where m.peer_user_id <> v_viewer
  limit 1;

  if v_match.peer_user_id is not null then
    v_candidate_id := v_match.peer_user_id;
    v_sim := v_match.similarity_score;
    v_reason := coalesce(v_match.matching_peer_label, 'neighbors like you on your block');
    v_dims := array_remove(array[v_match.matching_peer_concept], null);
  else
    v_candidate_id := public._joint_moment_demo_user_id();
    v_is_demo := true;
    v_reason := 'Brazilian moms on your block';
    v_dims := array['heritage', 'stage'];
    v_sim := null;

    if not exists (select 1 from public.users u where u.id = v_candidate_id) then
      raise exception 'demo_candidate_missing_run_seed' using errcode = 'P0001';
    end if;
  end if;

  if v_candidate_id = v_viewer then
    v_candidate_id := public._joint_moment_demo_user_id();
    v_is_demo := true;
    v_reason := 'Brazilian moms on your block';
    v_dims := array['heritage', 'stage'];
    v_sim := null;
  end if;

  insert into public.joint_moment_impressions (
    viewer_user_id,
    candidate_user_id,
    lana_session_id,
    status,
    match_reason,
    shared_dimensions,
    similarity_score,
    is_demo
  )
  values (
    v_viewer,
    v_candidate_id,
    p_session_id,
    'proposed',
    v_reason,
    coalesce(v_dims, '{}'),
    v_sim,
    v_is_demo
  )
  returning id into v_impression_id;

  v_card := public._joint_moment_candidate_card(v_candidate_id);
  v_nick := v_card->>'nickname';

  if v_is_demo then
    v_copy := format(
      '%s told me two hours ago she''s looking for Brazilian moms too. Want me to put you two together?',
      v_nick
    );
  else
    v_copy := format(
      '%s looks like a strong match — %s. Want me to put you two together?',
      v_nick,
      v_reason
    );
  end if;

  return jsonb_build_object(
    'joint_moment_id', v_impression_id,
    'status', 'proposed',
    'candidate', v_card,
    'lana_copy', v_copy,
    'match_reason', v_reason,
    'shared_dimensions', coalesce(v_dims, '{}'),
    'similarity_score', v_sim,
    'is_demo', v_is_demo
  );
end;
$$;

comment on function public.get_joint_moment_candidate(uuid) is
  'Phase 2: return joint-moment card payload for onboarding. Vector match when possible; else demo Maria.';

create or replace function public.respond_joint_moment(
  p_joint_moment_id uuid,
  p_action text
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_row public.joint_moment_impressions%rowtype;
  v_action text := lower(trim(p_action));
  v_new_status text;
begin
  if auth.uid() is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;

  v_action := case v_action
    when 'accept' then 'accepted'
    when 'accepted' then 'accepted'
    when 'yes' then 'accepted'
    when 'introduce' then 'accepted'
    when 'decline' then 'declined'
    when 'declined' then 'declined'
    when 'no' then 'declined'
    when 'snooze' then 'snoozed'
    when 'later' then 'snoozed'
    when 'keep_exploring' then 'snoozed'
    else null
  end;

  if v_action is null then
    raise exception 'invalid_action' using errcode = 'P0001';
  end if;

  update public.joint_moment_impressions j
  set status = v_action,
      responded_at = now()
  where j.id = p_joint_moment_id
    and j.viewer_user_id = auth.uid()
    and j.status in ('proposed', 'accepted')
  returning j.* into v_row;

  if not found then
    raise exception 'joint_moment_not_found_or_closed' using errcode = 'P0001';
  end if;

  return jsonb_build_object(
    'joint_moment_id', v_row.id,
    'status', v_row.status,
    'candidate_user_id', v_row.candidate_user_id,
    'next_step', case
      when v_row.status = 'accepted' then 'phone_otp_then_send_intro'
      else 'none'
    end
  );
end;
$$;

comment on function public.respond_joint_moment(uuid, text) is
  'Phase 2 CTA: accept (→ OTP), decline, or snooze/keep_exploring.';

create or replace function public.send_joint_moment_intro(
  p_joint_moment_id uuid,
  p_opener_text text default null
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_row public.joint_moment_impressions%rowtype;
  v_nudge_id uuid;
  v_opener text;
begin
  perform public._require_verified_neighbor_comms();

  select j.*
  into v_row
  from public.joint_moment_impressions j
  where j.id = p_joint_moment_id
    and j.viewer_user_id = auth.uid()
  for update;

  if not found then
    raise exception 'joint_moment_not_found' using errcode = 'P0001';
  end if;

  if v_row.status = 'intro_sent' and v_row.nudge_id is not null then
    return jsonb_build_object(
      'status', 'intro_sent',
      'nudge_id', v_row.nudge_id,
      'candidate_user_id', v_row.candidate_user_id,
      'idempotent', true
    );
  end if;

  if v_row.status <> 'accepted' then
    raise exception 'joint_moment_not_accepted' using errcode = 'P0001';
  end if;

  if not exists (
    select 1 from public.users u where u.id = auth.uid() and u.home_block_id is not null
  ) then
    raise exception 'home_block_required' using errcode = 'P0001';
  end if;

  if not public._users_share_home_block(auth.uid(), v_row.candidate_user_id) then
    raise exception 'candidate_not_on_block' using errcode = 'P0001';
  end if;

  v_opener := nullif(trim(p_opener_text), '');
  if v_opener is null then
    v_opener := format(
      'Hi — Lana thought we might click. I''m new on the block and would love to connect.'
    );
  end if;

  v_nudge_id := public.send_nudge(v_row.candidate_user_id, v_opener);

  update public.joint_moment_impressions
  set status = 'intro_sent',
      nudge_id = v_nudge_id,
      intro_sent_at = now()
  where id = v_row.id;

  return jsonb_build_object(
    'status', 'intro_sent',
    'nudge_id', v_nudge_id,
    'candidate_user_id', v_row.candidate_user_id
  );
end;
$$;

comment on function public.send_joint_moment_intro(uuid, text) is
  'Phase 3: after OTP + assign_home_block, send nudge to joint-moment candidate.';

revoke all on function public.get_joint_moment_candidate(uuid) from public, anon;
grant execute on function public.get_joint_moment_candidate(uuid) to authenticated;

revoke all on function public.respond_joint_moment(uuid, text) from public, anon;
grant execute on function public.respond_joint_moment(uuid, text) to authenticated;

revoke all on function public.send_joint_moment_intro(uuid, text) from public, anon;
grant execute on function public.send_joint_moment_intro(uuid, text) to authenticated;

-- Idempotent demo neighbor Maria (joint-moment fallback). Safe on dev; skip in prod if undesired.
insert into auth.users (
  instance_id, id, aud, role, email, encrypted_password, email_confirmed_at,
  raw_app_meta_data, raw_user_meta_data, created_at, updated_at, phone, phone_confirmed_at, is_anonymous
)
values (
  '00000000-0000-0000-0000-000000000000',
  'a0000004-0004-4000-8000-000000000004',
  'authenticated', 'authenticated', null, null, null,
  '{"provider":"phone","providers":["phone"]}'::jsonb,
  '{"seed_role":"joint_moment_demo"}'::jsonb,
  now(), now(), '+15550100004', now(), false
)
on conflict (id) do update set updated_at = now();

insert into public.users (id, phone, nickname, home_block_id, home_zip, phone_verified_at, locale, consent_to_receive_intros)
values (
  'a0000004-0004-4000-8000-000000000004',
  '+15550100004', 'Maria', '8a2a1072b59ffff', '32827', now(), 'en', true
)
on conflict (id) do update set
  nickname = excluded.nickname,
  home_block_id = excluded.home_block_id,
  consent_to_receive_intros = true,
  updated_at = now();

delete from public.user_identity_claims
where user_id = 'a0000004-0004-4000-8000-000000000004'::uuid;

insert into public.user_identity_claims (user_id, concept, label, tone, confidence, disclosure, synonyms)
values
  ('a0000004-0004-4000-8000-000000000004', 'heritage_brazilian', 'Paulista', 'warm', 0.93, 'public', array['brazilian','latina']),
  ('a0000004-0004-4000-8000-000000000004', 'parents_toddlers', '14-month-old', null, 0.90, 'public', array['mom','toddler']),
  ('a0000004-0004-4000-8000-000000000004', 'faith_community', 'faith', null, 0.85, 'public', '{}');
