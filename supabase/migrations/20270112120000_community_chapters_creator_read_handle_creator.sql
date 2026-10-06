-- Community read surfaces: chapters from their parent, creator communities without a
-- query, and the creator on both anon handle reads.
--
-- PWA backend-asks §59(a), §58(a), §56(b). Read-only functions; nothing here writes
-- parent_place_ref (§59(d), creating a chapter, is still unspecified by product).
--
-- ── 1 · discover_community_chapters ──────────────────────────────────────────────
--
-- "Chapters in Iron Man Training". discover_communities_near drops every chapter on
-- purpose ("a chapter is found from its parent", 20270104120000), and until now nothing
-- read them from the parent, so a chapter could not be found at all.
--
-- VISIBILITY — the 20261214120000 contract, read for a DIRECTORY row (place name,
-- address, member count; never a member identity and never a chapter's content):
--
--   caller is a confirmed member of the parent P   → every chapter of P
--   caller is in some chapter of P but NOT in P    → only her own chapter(s)
--                                                     ("sees C and P and NOTHING ELSE";
--                                                     never a sibling chapter)
--   caller is in neither                            → every chapter of P — the same
--                                                     directory discover_communities_near
--                                                     offers any neighbour for a place
--                                                     with members; this is the only way
--                                                     a chapter is found at all
--
-- Same row rules as discovery: counts come from visible_place_members (blocked users are
-- not counted, and a chapter kept alive only by someone the caller blocked does not
-- appear), test and suspended places are skipped, and the caller's own chapters always
-- come back (is_member = true) even when they are test data.
--
-- ── 2 · discover_creator_communities ─────────────────────────────────────────────
--
-- The query-less half of /lana/circles/discover-topic: every creator community with
-- members, for the worker to rank by the caller's fit (community_affinity). hq_city /
-- hq_lat / hq_lng are RETURNED for rendering and are in NO predicate and NO ordering
-- (20261214120000's one rule) — the order here is only a stable candidate cut; the
-- worker re-ranks by affinity.
--
-- ── 3 · the creator on resolve_place_handle and place_claim_card ─────────────────
--
-- resolve_place_handle (body from 20270104120000) already carried
-- creator {displayName, avatarUrl} for place_type = 'creator'. §56(b) asks for `handle`
-- too, and for the same block on place_claim_card (body from 20261226120000), which only
-- had the flat creatorName / creatorAvatarUrl keys. Both keep every existing key.
--
--   displayName  place_features 'creator_name' (what the creator typed on the claim
--                form), else the operator's users.nickname
--   avatarUrl    place_features 'creator_avatar_url', else users.profile_photo_url
--   handle       the operator's users.handle (the public "@rosegold22" name)
--
-- The operator is the claimant: place_managers role 'operator', kept in step with
-- places.claimed_by by places_sync_operator (20270104120000). The block is null for any
-- other place_type, and null when none of the three is known (an unclaimed row): the
-- client then heads the card with the community itself.

-- ── 1 ───────────────────────────────────────────────────────────────────────────

create or replace function public.discover_community_chapters(
  p_user_id  uuid,
  p_place_id uuid,
  p_limit    int default 40
)
returns table (
  place_id      uuid,
  name          text,
  address       text,
  place_type    text,
  zip           text,
  lat           double precision,
  lng           double precision,
  member_count  int,
  member_types  text[],
  is_member     boolean
)
language sql
stable
security definer
set search_path = pg_catalog, public, extensions
as $$
  with chapters as (
    select c.id, c.name, c.address, c.place_type, c.zip, c.lat, c.lng,
           coalesce(c.is_test, false) as is_test
    from public.places c
    where c.parent_place_ref = p_place_id
      and c.governance_state is distinct from 'suspended'
  ),
  counted as (
    select vm.place_ref                     as pid,
           count(distinct vm.user_id)::int  as members,
           array_agg(distinct vm.circle_type) as types,
           bool_or(vm.user_id = p_user_id)  as mine
    from public.visible_place_members(p_user_id) vm
    join chapters ch on ch.id = vm.place_ref
    group by vm.place_ref
  ),
  me as (
    select
      exists (
        select 1 from public.circle_affiliations a
        where a.user_id = p_user_id
          and a.place_ref = p_place_id
          and a.status = 'confirmed'
          and a.dismissed_at is null
      ) as in_parent,
      exists (select 1 from counted k where k.mine) as in_a_chapter
  )
  select ch.id, ch.name, ch.address, ch.place_type, ch.zip, ch.lat, ch.lng,
         coalesce(k.members, 0), coalesce(k.types, '{}'::text[]),
         coalesce(k.mine, false)
  from chapters ch
  left join counted k on k.pid = ch.id
  cross join me
  where coalesce(k.members, 0) > 0
    and (coalesce(k.mine, false) or not ch.is_test)
    and (
      coalesce(k.mine, false)          -- her own chapter, always
      or me.in_parent                  -- a member of the parent sees every chapter
      or not me.in_a_chapter           -- a stranger to the family sees the directory
    )                                  -- (so a chapter-only member never sees a sibling)
  order by coalesce(k.mine, false) desc, coalesce(k.members, 0) desc, ch.name asc
  limit greatest(1, least(coalesce(p_limit, 40), 100));
$$;

comment on function public.discover_community_chapters(uuid, uuid, int) is
  'Chapters of one community as p_user_id may see them (20261214120000 contract): a '
  'parent member sees every chapter, a chapter-only member sees only her own, a stranger '
  'sees the directory. Directory rows only — no member identities. Service role only; '
  'POST /lana/circles/chapters is the reader.';

revoke all on function public.discover_community_chapters(uuid, uuid, int)
  from public, anon, authenticated;
grant execute on function public.discover_community_chapters(uuid, uuid, int)
  to service_role;

-- ── 2 ───────────────────────────────────────────────────────────────────────────

create or replace function public.discover_creator_communities(
  p_user_id uuid,
  p_limit   int default 40
)
returns table (
  place_id      uuid,
  name          text,
  place_type    text,
  hq_city       text,
  hq_lat        double precision,
  hq_lng        double precision,
  member_count  int,
  is_member     boolean
)
language sql
stable
security definer
set search_path = pg_catalog, public, extensions
as $$
  with counted as (
    select vm.place_ref                    as pid,
           count(distinct vm.user_id)::int as members,
           bool_or(vm.user_id = p_user_id) as mine
    from public.visible_place_members(p_user_id) vm
    group by vm.place_ref
  )
  select p.id, p.name, p.place_type, p.hq_city, p.hq_lat, p.hq_lng,
         c.members, coalesce(c.mine, false)
  from counted c
  join public.places p on p.id = c.pid
  where c.members > 0
    and p.place_type = 'creator'
    and not coalesce(p.is_test, false)
    and p.parent_place_ref is null
    and p.governance_state is distinct from 'suspended'
  -- A stable candidate cut, NOT the ranking: the worker orders by the caller's fit.
  -- No hq_* here, by rule (20261214120000).
  order by c.members desc, p.name asc
  limit greatest(1, least(coalesce(p_limit, 40), 100));
$$;

comment on function public.discover_creator_communities(uuid, int) is
  'Every creator community with visible members, for the worker to rank by the caller''s '
  'fit (POST /lana/circles/discover-topic with no query). hq_* are returned for rendering '
  'and appear in no predicate or ordering. Service role only.';

revoke all on function public.discover_creator_communities(uuid, int)
  from public, anon, authenticated;
grant execute on function public.discover_creator_communities(uuid, int)
  to service_role;

-- ── 3a · resolve_place_handle · body from 20270104120000, creator.handle ADDED ─────

create or replace function public.resolve_place_handle(p_handle text)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public, extensions
as $$
declare
  v_in      text := public.normalize_place_handle(p_handle);
  v_place   record;
  v_alias   boolean := false;
  v_creator_name   text;
  v_creator_avatar text;
  v_creator_handle text;                                           -- ADDED
begin
  select p.id, p.handle, p.name, p.place_type, p.zip, p.first_action,
         p.governance_state, p.blurb, p.hq_city, p.is_test
    into v_place
    from public.places p
   where p.handle = v_in
     and p.governance_state = 'operator_verified';

  if v_place.id is null then
    select p.id, p.handle, p.name, p.place_type, p.zip, p.first_action,
           p.governance_state, p.blurb, p.hq_city, p.is_test
      into v_place
      from public.place_handle_aliases a
      join public.places p on p.id = a.place_id
     where a.handle = v_in
       and p.governance_state = 'operator_verified';
    v_alias := v_place.id is not null;
  end if;

  if v_place.id is null then
    return null;
  end if;

  if v_place.place_type = 'creator' then
    select
      coalesce(
        (select f.value from public.place_features f
          where f.place_id = v_place.id and f.key = 'creator_name'
            and nullif(btrim(coalesce(f.value, '')), '') is not null
          order by f.confidence desc nulls last, f.created_at desc limit 1),
        op.nickname) as display_name,
      coalesce(
        (select f.value from public.place_features f
          where f.place_id = v_place.id and f.key = 'creator_avatar_url'
            and nullif(btrim(coalesce(f.value, '')), '') is not null
          order by f.confidence desc nulls last, f.created_at desc limit 1),
        op.profile_photo_url) as avatar_url,
      nullif(btrim(coalesce(op.handle, '')), '') as handle         -- ADDED
      into v_creator_name, v_creator_avatar, v_creator_handle
      from (select 1) one
      left join lateral (
        select u.nickname, u.profile_photo_url, u.handle           -- handle ADDED
          from public.place_managers m
          join public.users u on u.id = m.user_id
         where m.place_id = v_place.id
           and m.role = 'operator'
           and m.removed_at is null
         order by m.created_at asc
         limit 1) op on true;
  end if;

  return jsonb_build_object(
    'placeId',          v_place.id,
    'handle',           v_place.handle,
    'displayName',      v_place.name,
    'placeType',        v_place.place_type,
    'zip',              v_place.zip,
    'firstAction',      v_place.first_action,
    'governanceState',  v_place.governance_state,
    'operatorVerified', true,
    'blurb',            v_place.blurb,
    'hqCity',           v_place.hq_city,
    'viaAlias',         v_alias,
    'isTest',           coalesce(v_place.is_test, false),
    'creator',          case
                          when v_creator_name is null
                           and v_creator_avatar is null
                           and v_creator_handle is null then null   -- handle ADDED
                          else jsonb_build_object(
                                 'displayName', v_creator_name,
                                 'avatarUrl',   v_creator_avatar,
                                 'handle',      v_creator_handle)  -- ADDED
                        end);
end;
$$;

-- ── 3b · place_claim_card · body from 20261226120000, `creator` block ADDED ────────

create or replace function public.place_claim_card(p_place_id uuid)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public
as $$
declare
  p        record;
  v_count  int;
  v_noun   text;
  v_emoji  text;
  v_person text;
  v_avatar text;
  v_op_name   text;                                                -- ADDED
  v_op_avatar text;                                                -- ADDED
  v_op_handle text;                                                -- ADDED
  v_creator   jsonb;                                               -- ADDED
begin
  select pl.id, pl.name, pl.place_type, pl.zip, pl.blurb, pl.governance_state,
         pl.first_action, pl.hq_city
    into p
    from public.places pl
   where pl.id = p_place_id;

  if p.id is null then
    return null;
  end if;

  select count(*)::int into v_count
    from public.circle_affiliations a
   where a.place_ref = p_place_id
     and a.dismissed_at is null;

  select a.noun, a.emoji into v_noun, v_emoji
    from public.circle_affiliations a
   where a.place_ref = p_place_id
     and a.dismissed_at is null
     and a.noun is not null
   group by a.noun, a.emoji
   order by count(*) desc, a.noun asc
   limit 1;

  select f.value into v_person
    from public.place_features f
   where f.place_id = p_place_id and f.key = 'creator_name'
     and nullif(btrim(coalesce(f.value, '')), '') is not null
   order by f.confidence desc nulls last, f.created_at desc
   limit 1;

  select f.value into v_avatar
    from public.place_features f
   where f.place_id = p_place_id and f.key = 'creator_avatar_url'
     and nullif(btrim(coalesce(f.value, '')), '') is not null
   order by f.confidence desc nulls last, f.created_at desc
   limit 1;

  -- ADDED: the same block resolve_place_handle carries, for a creator community only.
  -- The flat creatorName / creatorAvatarUrl keys below stay exactly as they were (the
  -- feature rows alone) so no existing reader changes.
  if p.place_type = 'creator' then
    select u.nickname, u.profile_photo_url, nullif(btrim(coalesce(u.handle, '')), '')
      into v_op_name, v_op_avatar, v_op_handle
      from public.place_managers m
      join public.users u on u.id = m.user_id
     where m.place_id = p_place_id
       and m.role = 'operator'
       and m.removed_at is null
     order by m.created_at asc
     limit 1;

    if coalesce(v_person, v_op_name) is not null
       or coalesce(v_avatar, v_op_avatar) is not null
       or v_op_handle is not null then
      v_creator := jsonb_build_object(
        'displayName', coalesce(v_person, v_op_name),
        'avatarUrl',   coalesce(v_avatar, v_op_avatar),
        'handle',      v_op_handle);
    end if;
  end if;

  return jsonb_build_object(
    'placeId',         p.id,
    'displayName',     p.name,
    'placeType',       p.place_type,
    'zip',             p.zip,
    'blurb',           p.blurb,
    'noun',            v_noun,
    'emoji',           v_emoji,
    'firstAction',     p.first_action,
    'governanceState', p.governance_state,
    'hasMembers',      v_count > 0,
    'memberCount',     v_count,
    'hqCity',          p.hq_city,
    'creatorName',     v_person,
    'creatorAvatarUrl',v_avatar,
    'creator',         v_creator);                                 -- ADDED
end;
$$;

-- Both keep their existing grants (20261114120000: anon + authenticated) across
-- `create or replace` on the same signature.

-- ============================================================================
-- ROLLBACK
--   drop function if exists public.discover_community_chapters(uuid, uuid, int);
--   drop function if exists public.discover_creator_communities(uuid, int);
--   Re-run section 3 of 20270104120000 (resolve_place_handle) and
--   20261226120000 (place_claim_card): same signatures, the added keys stop appearing,
--   and every reader already null-coalesces them.
-- ============================================================================
