-- A neighbour's communities on their profile (`communities` on get_peer_profile).
--
-- The PWA has rendered this section for a while (PeerCommunities) but the RPC never
-- returned the key, so it silently rendered nothing. This adds it.
--
-- WHY THE WHOLE FUNCTION IS REWRITTEN
--   Postgres has no "add one key to an existing function's result" — the body is
--   copied verbatim from 20260602000001_phase3b_location_visibility.sql with exactly
--   one key appended. Nothing else changed; diff the two if in doubt.
--
-- WHAT TRAVELS, AND WHY THAT MUCH
--   A community is a real place someone physically goes to, so the place NAME is the
--   sensitive part, not the fact that they go to a gym. Two gates, both reusing
--   signals the function already has:
--
--     reveal_place = is_matched  OR  the viewer belongs to that same place
--       Matched: the same bar that already unlocks mutual_claims.
--       Same place: the viewer is standing in it — naming it discloses nothing they
--       don't already know, and it is what makes the "Both here" pill true.
--
--     detail (the peer's own private note — "morning classes") = is_matched only.
--       It is closer to a mutual claim than to a place name, so it follows that bar.
--
--   Below the gate a row still travels as its KIND ("A gym") with place_name null —
--   the FE reads that as locked and says exact places appear once you connect. Only
--   confirmed, non-dismissed, GROUNDED affiliations are listed: an ungrounded row is
--   a place nobody has pinned yet, so there is nothing honest to show.
--
-- ROLLBACK: re-run 20260602000001_phase3b_location_visibility.sql.

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
      'is_blurred', true,
      'is_matched', false,
      'public_claims', '[]'::jsonb,
      'mutual_claims', '[]'::jsonb,
      'shared_claim_count', 0,
      'location_label', null,
      'location_precision', null,
      'block_name', null,
      'upcoming_shared_events', '[]'::jsonb,
      'communities', '[]'::jsonb
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
    'is_blurred', false,
    'is_matched', is_matched,
    -- Always show public claims
    'public_claims', coalesce((
      select jsonb_agg(jsonb_build_object(
        'concept', c.concept,
        'label', c.label,
        'tone', c.tone,
        'confidence', c.confidence
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
            'confidence', c.confidence
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
    ), '[]'::jsonb),
    -- NEW: the peer's communities, disclosed by the two gates described at the top.
    -- Places the viewer also belongs to lead the list (those are the useful rows).
    'communities', coalesce((
      select jsonb_agg(jsonb_build_object(
        'id', c.id,
        'circle_type', c.circle_type,
        'shared', c.shared,
        'place_name', c.place_name,
        'detail', c.detail,
        'sub_groups', c.sub_groups
      ) order by c.shared desc, c.created_at asc)
      from (
        select
          a.id,
          a.circle_type,
          a.created_at,
          (mine.id is not null) as shared,
          case when is_matched or mine.id is not null
            then coalesce(p.name, a.place_name) end as place_name,
          case when is_matched then a.detail end as detail,
          case when is_matched or mine.id is not null then coalesce((
            select jsonb_agg(pa.label order by pa.created_at asc)
            from public.place_activities pa
            where pa.place_id = a.place_ref
              and pa.user_id = peer.id
          ), '[]'::jsonb) else '[]'::jsonb end as sub_groups
        from public.circle_affiliations a
        join public.places p on p.id = a.place_ref
        left join public.circle_affiliations mine
          on mine.place_ref = a.place_ref
         and mine.user_id = caller
         and mine.status = 'confirmed'
         and mine.dismissed_at is null
        where a.user_id = peer.id
          and a.status = 'confirmed'
          and a.dismissed_at is null
        order by (mine.id is not null) desc, a.created_at asc
        limit 12
      ) c
    ), '[]'::jsonb)
  );

  return result;
end;
$$;
comment on function public.get_peer_profile(uuid) is
  'Returns peer profile with disclosure-aware claims. Shows public claims to all, mutual '
  'claims only to matched users. Returns location label based on peer''s chosen precision '
  'and hides home_block_id. `communities` lists the peer''s confirmed grounded '
  'affiliations: the place NAME (and their activities there) only when the pair is '
  'matched or the viewer belongs to the same place, their private detail only when '
  'matched — otherwise the row is its kind alone.';
grant execute on function public.get_peer_profile(uuid) to authenticated, anon;
