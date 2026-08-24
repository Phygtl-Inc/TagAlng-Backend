-- get_peer_profile: restore `portrait` and subject-aware claims (regression fix).
--
-- WHY
--   20261101120000 was meant to drop ONE key (`communities`) but was written by copying
--   the pre-portrait body of 20261018120000, so it also reverted everything 20261022 and
--   20261026 had added to this RPC:
--     * 'portrait'  -> users.public_portrait  (the profile drawer's about line; the FE
--                      reads `portrait` and has been getting null in prod ever since)
--     * 'subject_kind' on public_claims and mutual_claims
--     * subject_kind equality on the shared_claim_count join (a peer's kid's swim class
--       stopped counting as an overlap with the caller's own claim)
--
-- HOW
--   Body = 20261026120000's get_peer_profile with the `communities` and `is_blurred` keys
--   removed (the communities drop stands; POST /lana/circles/list owns that list). Diff
--   this against 20261026120000 and those two keys are the ONLY differences.
--
-- ALSO: drop `is_blurred`. The FE's only test for it was `is_blurred || !user_id`, and the
--   anonymous branch is the only place it was ever true — where user_id is null anyway. One
--   signal for "this viewer gets the locked shell", not two that can disagree.
--
-- ROLLBACK: re-run 20261101120000_peer_profile_drop_communities.sql (loses portrait again).

create or replace function public.get_peer_profile(p_user_id uuid)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
stable
as $$
declare
  caller uuid := auth.uid();
  peer record;
  is_matched boolean;
  location_label text;
  location_precision text;
  result jsonb;
begin
  -- Fetch peer profile
  select u.id, u.nickname, u.profile_photo_url, u.home_block_id, u.home_location_visibility,
         u.public_portrait,
         b.display_name, b.cluster_id
  into peer
  from public.users u
  left join public.blocks b on b.id = u.home_block_id
  where u.id = p_user_id;

  if not found then
    raise exception 'peer_not_found' using errcode = 'P0001';
  end if;

  -- Anonymous visitor: blurred profile (no sensitive data)
  if caller is null then
    return jsonb_build_object(
      'user_id', null,
      'nickname', null,
      'avatar_url', null,
      'is_matched', false,
      'portrait', null,
      'public_claims', '[]'::jsonb,
      'mutual_claims', '[]'::jsonb,
      'shared_claim_count', 0,
      'location_label', null,
      'location_precision', null,
      'block_name', null,
      'upcoming_shared_events', '[]'::jsonb
    );
  end if;

  -- Choose label based on peer preference
  location_label := case peer.home_location_visibility
    when 'block' then peer.display_name
    when 'cluster' then peer.cluster_id
  end;
  location_precision := peer.home_location_visibility::text;

  -- Check if caller and peer are matched (same event or shared public claims)
  is_matched := public.are_users_matched(caller, p_user_id);

  -- Build authenticated response
  result := jsonb_build_object(
    'user_id', peer.id,
    'nickname', peer.nickname,
    'avatar_url', peer.profile_photo_url,
    'is_matched', is_matched,
    -- Written from the public claims listed directly below, and from nothing else.
    'portrait', peer.public_portrait,
    -- Always show public claims
    'public_claims', coalesce((
      select jsonb_agg(jsonb_build_object(
        'concept', c.concept,
        'label', c.label,
        'tone', c.tone,
        'confidence', c.confidence,
        -- NEW: what kind of thread it is, and when it was learned.
        'bucket', c.bucket,
        'subject_kind', c.subject_kind,
        'created_at', c.created_at
      ) order by c.confidence desc)
      from public.user_identity_claims c
      where c.user_id = peer.id
        and c.dismissed_at is null
        and c.disclosure = 'public'
    ), '[]'::jsonb),
    -- Show mutual claims only if matched (silent omission otherwise)
    'mutual_claims', case
      when is_matched then
        coalesce((
          select jsonb_agg(jsonb_build_object(
            'concept', c.concept,
            'label', c.label,
            'tone', c.tone,
            'confidence', c.confidence,
            'bucket', c.bucket,
        'subject_kind', c.subject_kind,
            'created_at', c.created_at
          ) order by c.confidence desc)
          from public.user_identity_claims c
          where c.user_id = peer.id
            and c.dismissed_at is null
            and c.disclosure = 'mutual'
        ), '[]'::jsonb)
      else '[]'::jsonb
    end,
    'shared_claim_count', (
      select count(*)::int
      from public.user_identity_claims c1
      join public.user_identity_claims c2
        on c1.concept = c2.concept
       and c1.subject_kind = c2.subject_kind
      where c1.user_id = caller
        and c2.user_id = peer.id
        and c1.dismissed_at is null
        and c2.dismissed_at is null
        and c1.disclosure = 'public'
        and c2.disclosure = 'public'
    ),
    'location_label', location_label,
    'location_precision', location_precision,
    'block_name', case when peer.home_location_visibility = 'block' then peer.display_name else null end,
    -- home_block_id is HIDDEN from peer views (only owner sees it via get_my_profile)
    'upcoming_shared_events', coalesce((
      select jsonb_agg(jsonb_build_object(
        'event_id', e.id,
        'title', e.title,
        'starts_at', e.starts_at
      ) order by e.starts_at asc)
      from public.events e
      where e.status = 'open'
        and e.starts_at > now()
        and exists (
          select 1 from public.event_requests r
          where r.event_id = e.id
            and r.requester_id = caller
            and r.status in ('approved', 'attended')
        )
        and exists (
          select 1 from public.event_requests r
          where r.event_id = e.id
            and r.requester_id = peer.id
            and r.status in ('approved', 'attended')
        )
    ), '[]'::jsonb)
  );

  return result;
end;
$$;

comment on function public.get_peer_profile(uuid) is
  'Returns peer profile with disclosure-aware claims. An anonymous caller gets a shell whose '
  'user_id is null — that null IS the blurred signal; there is no is_blurred key (2026-08-19). '
  'Shows public claims to all, mutual claims only to matched users; each claim carries its bucket, subject_kind and created_at. '
  'portrait is users.public_portrait — one line written from the public claims below it. '
  'Returns location label based on peer''s chosen precision and hides home_block_id. '
  'Communities are NOT here: POST /lana/circles/list {user_id} owns that list.';

revoke execute on function public.get_peer_profile(uuid) from public;
grant execute on function public.get_peer_profile(uuid) to authenticated, anon;
