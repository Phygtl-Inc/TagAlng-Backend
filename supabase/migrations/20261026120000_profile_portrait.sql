-- ---------------------------------------------------------------------------
-- The portrait line is written once and kept, for you and for your neighbours.
-- ---------------------------------------------------------------------------
-- `mapped_summary` only ever existed for a profile that came out of an onboarding
-- intake. A profile built in chat had none, so the worker composed one with a model
-- call on every drawer open — cached in-process, which means it died with the
-- container and each pod re-bought the same sentence. The drawer also fetched it
-- AFTER rendering, so the box showed a bare list of threads and then swapped in the
-- prose a second and a half later (2026-08-18).
--
-- TWO portraits, deliberately, never one string used twice:
--   * portrait        — the caller's own, written from ALL their threads and
--                       addressed to them ("you"). Owner-only, and it may reference
--                       a thread that is about their kid, which is exactly why it
--                       must never be the one a peer reads.
--   * public_portrait — written ONLY from claims marked disclosure='public', the
--                       same set get_peer_profile already projects as chips to
--                       anyone who can open the profile. The synthesis is new; the
--                       facts in it were already on screen.
--
-- `*_key` fingerprints the claim set each line was written from. The worker rewrites
-- in the background when a claim lands or is retracted — a retraction CLEARS the
-- line first, because a stale portrait that still names a thread someone took back
-- is not merely out of date, it is false.
--
-- ROLLBACK: re-run 20261023120000 (get_my_profile_dashboard) and 20261022120000
-- (get_peer_profile), then drop the four columns below.
-- ---------------------------------------------------------------------------

alter table public.users
  add column if not exists portrait text,
  add column if not exists portrait_key text,
  add column if not exists public_portrait text,
  add column if not exists public_portrait_key text;

comment on column public.users.portrait is
  'One-line AI portrait over ALL the user''s threads, addressed to them. Owner-only: '
  'may reference a child''s thread. Never projected to a peer.';
comment on column public.users.public_portrait is
  'One-line AI portrait over the user''s PUBLIC claims only — the same facts '
  'get_peer_profile already projects as chips. Safe for any viewer of the profile.';
comment on column public.users.portrait_key is
  'Fingerprint of the threads `portrait` was written from; a mismatch means rewrite.';
comment on column public.users.public_portrait_key is
  'Fingerprint of the public claims `public_portrait` was written from.';

-- ── the owner's dashboard falls back to the stored line ──────────────────────
-- Body is 20261023120000 verbatim apart from the mapped_summary coalesce, so the
-- drawer stops making a second round trip for prose the row already has.

create or replace function public.get_my_profile_dashboard()
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
stable
as $$
declare
  v_caller uuid := auth.uid();
  v_profile jsonb;
  v_summary text;
  v_portrait text;
  v_spans jsonb;
  v_claims jsonb;
  v_stats jsonb;
  v_events_hosted int;
  v_events_attended int;
  v_check_ins int;
  v_peers_met int;
begin
  if v_caller is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;

  select public.get_my_profile() into v_profile;

  select
    s.context->>'mapped_summary',
    coalesce(s.context->'spans', '[]'::jsonb)
  into v_summary, v_spans
  from public.lana_sessions s
  where s.user_id = v_caller
    and s.purpose = 'profile_intake'
    and s.status = 'completed'
  order by s.completed_at desc nulls last, s.updated_at desc
  limit 1;

  -- The intake line wins when there is one (it is what they were shown at signup);
  -- otherwise the stored portrait, which is how a chat-built profile gets prose and
  -- how the drawer stopped making a second round trip for it.
  select u.portrait into v_portrait from public.users u where u.id = v_caller;
  v_summary := coalesce(nullif(trim(v_summary), ''), nullif(trim(v_portrait), ''));

  select coalesce(jsonb_agg(
    jsonb_build_object(
      'id', c.id,
      'concept', c.concept,
      'label', c.label,
      'tone', c.tone,
      'confidence', c.confidence,
      'disclosure', c.disclosure,
      'synonyms', c.synonyms,
      'details', coalesce(c.details, '{}'),
      'source_quote', c.source_quote,
      'bucket', c.bucket,
      'created_at', c.created_at,
      -- Who the thread is about. Owner's own wall, so the child's name and age
      -- ride along here — and only here.
      'subject_kind', c.subject_kind,
      'subject_name', c.subject_name,
      'subject_age', case
        when c.subject_birth_year is null then null
        else extract(year from now())::int - c.subject_birth_year
      end
    ) order by c.confidence desc, c.created_at desc
  ), '[]'::jsonb)
  into v_claims
  from public.user_identity_claims c
  where c.user_id = v_caller
    and c.dismissed_at is null;

  select count(*)::int
  into v_events_hosted
  from public.events e
  where e.host_id = v_caller
    and e.status in ('open', 'completed');

  select count(*)::int
  into v_events_attended
  from public.event_requests er
  where er.requester_id = v_caller
    and er.status in ('approved', 'attended');

  select count(*)::int
  into v_check_ins
  from public.thread_events te
  where te.actor_id = v_caller
    and te.event_type = 'check_in';

  select count(distinct er2.requester_id)::int
  into v_peers_met
  from public.event_requests er1
  join public.event_requests er2
    on er2.event_id = er1.event_id
   and er2.requester_id <> v_caller
  where er1.requester_id = v_caller
    and er1.status in ('approved', 'attended')
    and er2.status in ('approved', 'attended');

  v_stats := jsonb_build_object(
    'events_hosted', coalesce(v_events_hosted, 0),
    'events_attended', coalesce(v_events_attended, 0),
    'check_ins', coalesce(v_check_ins, 0),
    'peers_met', coalesce(v_peers_met, 0)
  );

  return jsonb_build_object(
    'profile', coalesce(v_profile, '{}'::jsonb),
    'mapped_summary', v_summary,
    'spans', coalesce(v_spans, '[]'::jsonb),
    'claims', coalesce(v_claims, '[]'::jsonb),
    'stats', v_stats
  );
end;
$$;

-- ── a peer's profile carries the PUBLIC line ─────────────────────────────────
-- One added key. Everything else is 20261022120000's body; the blurred
-- (signed-out) branch returns null for it, like every other real field there.

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
      'is_blurred', true,
      'is_matched', false,
      'portrait', null,
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

revoke all on function public.get_peer_profile(uuid) from public;
grant execute on function public.get_peer_profile(uuid) to anon, authenticated;
