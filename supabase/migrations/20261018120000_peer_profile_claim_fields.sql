-- get_peer_profile: claim bucket + created_at, and place_id on the community rows
-- (backend asks §14a / issues #76, and §18 / issues #83, #85).
--
-- WHY THE WHOLE FUNCTION IS REWRITTEN
--   Postgres has no "add one key to an existing function's result" — the body is copied
--   verbatim from 20261011120000_peer_profile_communities.sql with exactly three
--   additions. Diff the two if in doubt.
--
-- WHAT IS ADDED
--   1. bucket on every claim row — the category of a claim the peer already discloses,
--      already projected for the owner by get_my_identity_claims. It is what the peer
--      profile groups its threads by ("Parenting stage", "Lifestyle").
--   2. created_at on every claim row — when Lana mapped the claim, which restores the
--      This week / This month / Earlier rail. Not sensitive on its own.
--   3. place_id (our public.places.id) on each community row, under the SAME gate as
--      place_name: a named row is openable, a locked row ("A gym") carries no id
--      because there is nothing to open. For a shared row this is the join key the
--      function already computed (mine.place_ref = a.place_ref), so it discloses
--      nothing new; the FE stops matching place names to guess it.
--
-- ROLLBACK: re-run 20261011120000_peer_profile_communities.sql.

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
        'confidence', c.confidence,
        -- NEW: what kind of thread it is, and when it was learned.
        'bucket', c.bucket,
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
    -- The peer's communities, disclosed by the two gates described in 20261011.
    -- Places the viewer also belongs to lead the list (those are the useful rows).
    'communities', coalesce((
      select jsonb_agg(jsonb_build_object(
        'id', c.id,
        -- NEW: our places.id, on the rows whose name travels. Null on a locked row.
        'place_id', c.place_id,
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
          case when is_matched or mine.id is not null then a.place_ref end as place_id,
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
  'claims only to matched users; each claim carries its bucket (category) and created_at '
  '(when Lana learned it). Returns location label based on peer''s chosen precision '
  'and hides home_block_id. `communities` lists the peer''s confirmed grounded '
  'affiliations: the place NAME and place_id (and their activities there) only when the '
  'pair is matched or the viewer belongs to the same place, their private detail only '
  'when matched — otherwise the row is its kind alone.';

grant execute on function public.get_peer_profile(uuid) to authenticated, anon;
