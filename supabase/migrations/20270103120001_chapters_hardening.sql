-- Chapters · hardening pass
--
-- Written after re-reading 20270103120000 as three different people. Each found
-- something, and one of them found something this migration CANNOT fix without
-- colliding with an open PR — see the blocker at the bottom.

-- ════════════════════════════════════════════════════════════════════════════
-- SECURITY
-- ════════════════════════════════════════════════════════════════════════════

-- ── S1 · an anonymous guest could create chapters ──────────────────────────
--
-- In Supabase an anonymous session runs as `authenticated` with a real uid — Asjid's own
-- note in #172: "anonymous-session guests still count, since they run as authenticated".
--
-- So `grant execute ... to authenticated` on create_chapter meant anybody who opened a
-- creator link, joined, and never signed up could mint chapters. 315 of 352 users in
-- production are anonymous. That is a spam surface, not an edge case.
--
-- Joining stays open to guests. CREATING does not: a chapter is durable structure inside
-- somebody else's community, and durable structure needs an account behind it.

-- ── S2 · no throttle ───────────────────────────────────────────────────────
--
-- One member could create two hundred chapters. There is no cost and no limit. Five per
-- person per day across the whole system, which no honest user will reach.

create or replace function public.chapters_created_today(p_user_id uuid)
returns int
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select count(*)::int
    from public.places p
   where p.parent_place_ref is not null
     and p.created_at > now() - interval '24 hours'
     and exists (
       select 1 from public.circle_affiliations a
        where a.place_ref = p.id and a.user_id = p_user_id
          and a.source = 'chapter_join' and a.dismissed_at is null);
$$;

revoke all on function public.chapters_created_today(uuid) from public, anon;
grant execute on function public.chapters_created_today(uuid) to authenticated, service_role;

-- ── S3 · a chapter is a side door into its parent ──────────────────────────
--
-- NOT FIXED HERE — flagged for a product decision.
--
-- join_chapter() grants parent membership by design (decision 3). That means anyone who
-- can reach a chapter URL can join the parent, without the parent's consent. If a
-- community is ever meant to be invite-only, chapters bypass it.
--
-- Nothing in the schema expresses "this community is invite-only" today, so there is
-- nothing to check against. Raising it rather than inventing a gate.

-- ════════════════════════════════════════════════════════════════════════════
-- ENGINEERING
-- ════════════════════════════════════════════════════════════════════════════

-- ── E1 · deleting a parent raised a confusing CHECK violation ──────────────
--
-- places_parent_place_ref_fkey is ON DELETE SET NULL. The slug CHECK required
-- parent_place_ref IS NOT NULL whenever chapter_slug is set. So deleting a community
-- with chapters tried to null the parent, which then violated the slug CHECK, and the
-- delete failed with an error naming the wrong thing entirely.
--
-- Relaxed: the slug keeps its shape rule, loses the coupling. An orphaned chapter keeps
-- a harmless slug, and places_chapter_slug_uniq already scopes uniqueness with
-- `where parent_place_ref is not null`, so nothing collides.

alter table public.places
  drop constraint if exists places_chapter_slug_shape;

alter table public.places
  add constraint places_chapter_slug_shape check (
    chapter_slug is null
    or (chapter_slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'
        and length(chapter_slug) between 2 and 40)
  );

-- ── E2 · create_chapter · anon block, throttle, blurb cap, friendly retry ──

create or replace function public.create_chapter(
  p_parent_place_id uuid,
  p_name            text,
  p_lat             double precision,
  p_lng             double precision,
  p_slug            text default null,
  p_blurb           text default null
)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, extensions
as $$
declare
  v_uid     uuid := auth.uid();
  v_anon    boolean;
  v_parent  record;
  v_slug    text;
  v_id      uuid;
  v_near    int;
  v_gpid    text;
begin
  if v_uid is null then
    raise exception 'not_authenticated';
  end if;

  -- S1. From auth.users, not the JWT — the row is the truth and needs no token config.
  select coalesce(au.is_anonymous, false) into v_anon from auth.users au where au.id = v_uid;
  if v_anon then
    raise exception 'account_required'
      using hint = 'Creating a chapter needs an account. Joining one does not.';
  end if;

  -- S2.
  if public.chapters_created_today(v_uid) >= 5 then
    raise exception 'rate_limited'
      using hint = 'You have created several chapters today. Try again tomorrow.';
  end if;

  select p.id, p.name, p.handle, p.place_type, p.parent_place_ref, p.governance_state
    into v_parent
    from public.places p where p.id = p_parent_place_id;

  if v_parent.id is null then
    raise exception 'parent_not_found';
  end if;
  if v_parent.parent_place_ref is not null then
    raise exception 'chapter_depth_exceeded'
      using hint = 'A chapter cannot have chapters of its own.';
  end if;
  if v_parent.handle is null then
    raise exception 'parent_not_published'
      using hint = 'The parent needs a handle before it can have chapters, because a '
                   'chapter URL is /c/{parent_handle}/{slug}.';
  end if;
  -- A suspended parent darkens every chapter under it (resolve_chapter_handle requires
  -- the parent to be operator_verified), so do not let one be created there.
  if v_parent.governance_state = 'suspended' then
    raise exception 'parent_suspended';
  end if;

  if not exists (
    select 1 from public.circle_affiliations a
     where a.place_ref = p_parent_place_id and a.user_id = v_uid
       and a.status = 'confirmed' and a.dismissed_at is null)
  then
    raise exception 'not_a_member'
      using hint = 'Join the community before creating a chapter in it.';
  end if;

  if p_name is null or length(btrim(p_name)) < 2 then
    raise exception 'name_too_short';
  end if;
  if length(btrim(p_name)) > 120 then
    raise exception 'name_too_long';
  end if;
  -- Was uncapped. update_community_settings caps blurb at 600; match it.
  if p_blurb is not null and length(p_blurb) > 600 then
    raise exception 'blurb_too_long';
  end if;
  if p_lat is null or p_lng is null then
    raise exception 'chapter_needs_location'
      using hint = 'A chapter is a place-based branch and must have coordinates.';
  end if;
  if p_lat not between -90 and 90 or p_lng not between -180 and 180 then
    raise exception 'coordinates_out_of_range';
  end if;

  v_slug := left(public.normalize_place_handle(
              coalesce(nullif(btrim(p_slug), ''), p_name)), 40);
  if v_slug is null or v_slug !~ '^[a-z0-9]+(-[a-z0-9]+)*$' or length(v_slug) < 2 then
    raise exception 'slug_shape';
  end if;

  v_gpid := 'chapter:' || p_parent_place_id::text || ':' || v_slug;

  -- E3 · retry is idempotent, not an error.
  --
  -- google_place_id is deterministic on (parent, slug), so a double submit or a client
  -- retry hits the UNIQUE. Returning the existing chapter is what the caller wanted;
  -- surfacing a raw 23505 is not.
  select p.id into v_id from public.places p where p.google_place_id = v_gpid;
  if v_id is not null then
    perform public.join_chapter(v_id);
    return jsonb_build_object(
      'chapterId', v_id, 'slug', v_slug, 'parentId', p_parent_place_id,
      'url', '/c/' || v_parent.handle || '/' || v_slug,
      'nearbySiblings', 0, 'alreadyExisted', true);
  end if;

  if exists (select 1 from public.places q
              where q.parent_place_ref = p_parent_place_id and q.chapter_slug = v_slug) then
    raise exception 'slug_taken'
      using hint = 'That chapter name is already used in this community.';
  end if;

  select count(*) into v_near
    from public.places q
   where q.parent_place_ref = p_parent_place_id
     and q.lat is not null and q.lng is not null
     and extensions.ST_DWithin(
           extensions.ST_SetSRID(extensions.ST_MakePoint(q.lng, q.lat), 4326)::extensions.geography,
           extensions.ST_SetSRID(extensions.ST_MakePoint(p_lng, p_lat), 4326)::extensions.geography,
           2000);

  insert into public.places
    (google_place_id, name, parent_place_ref, chapter_slug, lat, lng, blurb, place_type,
     governance_state, created_at, updated_at)
  values
    (v_gpid, btrim(p_name), p_parent_place_id, v_slug, p_lat, p_lng,
     nullif(btrim(p_blurb), ''), v_parent.place_type,
     'community_started', now(), now())
  returning id into v_id;

  perform public.join_chapter(v_id);

  return jsonb_build_object(
    'chapterId', v_id, 'slug', v_slug, 'parentId', p_parent_place_id,
    'url', '/c/' || v_parent.handle || '/' || v_slug,
    'nearbySiblings', v_near, 'alreadyExisted', false);
end;
$$;

revoke all on function public.create_chapter(uuid, text, double precision, double precision, text, text)
  from public, anon;
grant execute on function public.create_chapter(uuid, text, double precision, double precision, text, text)
  to authenticated, service_role;

-- ── E4 · community_chapters was N+1 ────────────────────────────────────────
--
-- Two correlated subqueries per row, up to 200 rows — 400 scans of
-- circle_affiliations for one screen. One aggregate instead.

create or replace function public.community_chapters(
  p_place_id uuid,
  p_limit    int default 50
)
returns table (
  place_id   uuid,
  name       text,
  slug       text,
  lat        double precision,
  lng        double precision,
  blurb      text,
  members    int,
  is_mine    boolean
)
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select c.id, c.name, c.chapter_slug, c.lat, c.lng, c.blurb,
         coalesce(m.members, 0)::int,
         coalesce(m.mine, false)
  from public.places c
  left join lateral (
    select count(*) as members,
           bool_or(a.user_id = auth.uid()) as mine
      from public.circle_affiliations a
     where a.place_ref = c.id
       and a.status = 'confirmed'
       and a.dismissed_at is null
  ) m on true
  where c.parent_place_ref = p_place_id
    and c.governance_state <> 'suspended'
  order by c.name
  limit greatest(1, least(coalesce(p_limit, 50), 200));
$$;

revoke all on function public.community_chapters(uuid, int) from public, anon;
grant execute on function public.community_chapters(uuid, int) to authenticated, service_role;

-- Supports both the member count above and every other per-place roster query.
create index if not exists circle_affiliations_place_active_idx
  on public.circle_affiliations (place_ref)
  where dismissed_at is null and place_ref is not null;

-- ════════════════════════════════════════════════════════════════════════════
-- 🔴 BLOCKER · NOT FIXABLE IN THIS PR
-- ════════════════════════════════════════════════════════════════════════════
--
-- CHAPTERS LEAK INTO DISCOVERY AS STANDALONE COMMUNITIES.
--
-- discover_communities' blurb arm reads:
--
--     from public.places p
--     where p.blurb_embedding is not null
--       and p.governance_state in ('community_started','operator_verified')
--
-- There is no parent filter. A new chapter is created with 'community_started', so the
-- moment the blurb sweep embeds it, "Etiqueta do Reino · Lisboa" surfaces in discovery
-- alongside — and competing with — Etiqueta do Reino itself. A chapter is not something
-- a stranger should find cold; it is found from inside its parent.
--
-- THE FIX IS ONE LINE:
--
--     and p.parent_place_ref is null
--
-- It is not here because PR #174 already re-emits discover_communities (to add
-- `and not p.is_test`), and a second re-emission would conflict — or worse, one would
-- silently revert the other, which is a mistake I already made once on this function.
--
-- @asjid9 — whichever of #174 / #176 merges LAST must carry both predicates. Flagging
-- rather than racing.
--
-- Related, same cause: community_blurb.sweep() will generate a blurb for a chapter from
-- its name and type with no knowledge of its parent, so "Lisboa" gets described as if it
-- were a community in its own right. The sweep should either skip chapters or compose
-- from the parent. Worker-side, so also not here.

-- ============================================================================
-- ROLLBACK
--   drop index if exists public.circle_affiliations_place_active_idx;
--   drop function if exists public.chapters_created_today(uuid);
--   -- create_chapter and community_chapters: restore from 20270103120000.
--   alter table public.places drop constraint if exists places_chapter_slug_shape;
--   alter table public.places add constraint places_chapter_slug_shape check (
--     chapter_slug is null or (parent_place_ref is not null
--       and chapter_slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'
--       and length(chapter_slug) between 2 and 40));
-- ============================================================================
