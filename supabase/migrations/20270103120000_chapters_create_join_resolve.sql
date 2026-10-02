-- Chapters · create, join, resolve, list
--
-- WHAT IS BROKEN TODAY (verified 2026-10-02)
--
--   `places.parent_place_ref` has existed since 20261214120000. The depth guard exists.
--   The CHECK constraints exist. ZERO rows use it, and NOTHING IN EITHER REPO WRITES IT.
--
--   Worse: the `+ Add` button in the switch-community sheet, tapped from inside a
--   community, reads as "create a sub-community". It calls /lana/circles/add, which has
--   no parent field, and the drawer never receives the community it was opened from. So
--   it silently creates a brand-new TOP-LEVEL community — a sibling, not a child. It
--   looks like it worked.
--
--   The original migration said as much about the read half: "the contract the read side
--   must implement (NOT enforced here)". Nobody implemented either half.
--
-- WHAT A CHAPTER IS (settled 2026-10-02)
--
--   A chapter is a GEOGRAPHIC branch of a community. Etiqueta do Reino → São Paulo,
--   Lisboa. Crunch Fitness → Town Center, Narcoossee Market.
--
--   It is NOT an interest sub-group. The walkthrough's "Sunrise 5k Crew" examples are
--   misleading and need correcting in design — an interest group has no coordinates of
--   its own, and `places_chapter_has_geography` already requires a chapter to have
--   lat/lng. The constraint was right; the mockup drifted.
--
-- THE FOUR DECISIONS THIS IMPLEMENTS
--
--   1. Geographic, not interest-based        → lat/lng required, already enforced
--   2. ANY member of the parent may create   → gated on confirmed affiliation, not operator
--   3. Joining a chapter joins the parent    → two affiliations, written together
--   4. Nested URL, /c/{parent}/{chapter}     → chapter_slug, unique WITHIN the parent
--
-- WHY A SEPARATE SLUG RATHER THAN `handle`
--
--   `places_handle_needs_verification` requires governance_state = 'operator_verified'
--   for any row carrying a handle, and `places_handle_uniq` makes it globally unique. A
--   chapter is neither independently verified nor entitled to compete with real
--   communities for the namespace. So chapters keep `handle` NULL and get
--   `chapter_slug`, unique only within their parent. "lisboa" may exist under twenty
--   different communities.
--
-- SCHEMA FACTS THIS WAS WRITTEN AGAINST (checked, not assumed)
--
--   places.google_place_id  NOT NULL, UNIQUE, no default  → a chapter must synthesise one
--   places.name             NOT NULL, no default
--   governance_state CHECK  community_started | claim_pending | operator_verified | suspended
--   place_type CHECK        null allowed; creator/fitness/faith/... — inheriting is safe
--   extensions installed    postgis ONLY. cube and earthdistance are NOT installed.
--   circle_affiliations     UNIQUE (user_id, place_ref) WHERE dismissed_at IS NULL
--                           UNIQUE (user_id, circle_key) WHERE dismissed_at IS NULL
--   normalize_place_handle  strips accents: 'São Paulo' → 'sao-paulo'
--
-- WHAT THIS DELIBERATELY DOES NOT DO
--
--   PARENT → CHAPTER rollup. A parent member seeing every chapter's content, each row
--   carrying origin_place_ref/origin_place_name, is the harder half and it is not here.
--   This PR does CHAPTER → PARENT only, which is the direction a chapter member needs on
--   day one and which cannot leak between siblings.
--
--   The original contract's hard rule stands and nothing here weakens it:
--
--     CHAPTER → PARENT: yes.  PARENT → CHAPTER: yes.  CHAPTER → SIBLING CHAPTER: NO.
--
--     "If Orlando's content rolls up to the parent and the parent rolls down to Boston,
--      then Boston reads Orlando's content as local. That is the one failure this
--      product cannot ship."

-- ── 1 · the slug ────────────────────────────────────────────────────────────

alter table public.places
  add column if not exists chapter_slug text;

alter table public.places
  drop constraint if exists places_chapter_slug_shape;

alter table public.places
  add constraint places_chapter_slug_shape check (
    chapter_slug is null
    or (parent_place_ref is not null
        and chapter_slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'
        and length(chapter_slug) between 2 and 40)
  );

-- Unique WITHIN the parent. Twenty communities may each have a "lisboa".
create unique index if not exists places_chapter_slug_uniq
  on public.places (parent_place_ref, chapter_slug)
  where parent_place_ref is not null and chapter_slug is not null;

comment on column public.places.chapter_slug is
  'URL segment for a chapter, unique only within its parent: /c/{parent_handle}/{slug}. '
  'Chapters keep handle NULL — a global handle requires operator_verified and competes '
  'with real communities for the namespace, and a chapter is neither independently '
  'verified nor entitled to that.';

-- ── 2 · create ──────────────────────────────────────────────────────────────
--
-- ANY confirmed member of the parent may create a chapter. That is deliberately
-- permissionless and matches the standup ("a group created by users"). The cost is
-- fragmentation: a 340-member gym could sprout a dozen near-identical chapters. The
-- nearby-sibling count below is a nudge returned to the caller, not a block.

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
  v_parent  record;
  v_slug    text;
  v_id      uuid;
  v_near    int;
  v_gpid    text;
begin
  if v_uid is null then
    raise exception 'not_authenticated';
  end if;

  select p.id, p.name, p.handle, p.place_type, p.parent_place_ref, p.governance_state
    into v_parent
    from public.places p where p.id = p_parent_place_id;

  if v_parent.id is null then
    raise exception 'parent_not_found';
  end if;
  -- One level. The depth guard would also catch this, but failing here gives a better
  -- error than a trigger exception surfacing through the API.
  if v_parent.parent_place_ref is not null then
    raise exception 'chapter_depth_exceeded'
      using hint = 'A chapter cannot have chapters of its own.';
  end if;
  if v_parent.handle is null then
    raise exception 'parent_not_published'
      using hint = 'The parent needs a handle before it can have chapters, because a '
                   'chapter URL is /c/{parent_handle}/{slug}.';
  end if;

  -- Membership in the parent, not operatorship. Decision 2.
  if not exists (
    select 1 from public.circle_affiliations a
     where a.place_ref = p_parent_place_id
       and a.user_id = v_uid
       and a.status = 'confirmed'
       and a.dismissed_at is null)
  then
    raise exception 'not_a_member'
      using hint = 'Join the community before creating a chapter in it.';
  end if;

  if p_name is null or length(btrim(p_name)) < 2 then
    raise exception 'name_too_short';
  end if;
  -- Geographic by definition, and places_chapter_has_geography enforces it anyway.
  -- Checked here so the caller gets a named error rather than a constraint violation.
  if p_lat is null or p_lng is null then
    raise exception 'chapter_needs_location'
      using hint = 'A chapter is a place-based branch and must have coordinates.';
  end if;

  v_slug := left(public.normalize_place_handle(
              coalesce(nullif(btrim(p_slug), ''), p_name)), 40);
  if v_slug is null or v_slug !~ '^[a-z0-9]+(-[a-z0-9]+)*$' or length(v_slug) < 2 then
    raise exception 'slug_shape';
  end if;
  if exists (select 1 from public.places q
              where q.parent_place_ref = p_parent_place_id and q.chapter_slug = v_slug) then
    raise exception 'slug_taken'
      using hint = 'That chapter name is already used in this community.';
  end if;

  -- google_place_id is NOT NULL and UNIQUE on places, with no default. A chapter is not
  -- a Google Place, so it synthesises a stable key, the same way creator places use
  -- 'creator:<slug>'. Deterministic on (parent, slug), so a retry cannot create a twin.
  v_gpid := 'chapter:' || p_parent_place_id::text || ':' || v_slug;

  -- Advisory only. Fragmentation is the real risk of permissionless creation, but
  -- blocking on proximity would stop two genuinely different chapters in one city.
  -- Returned to the caller so the UI can ask "did you mean X?" before committing.
  -- PostGIS, because cube/earthdistance are not installed on this database.
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

  -- The creator is a member of their own chapter, and of the parent already.
  perform public.join_chapter(v_id);

  return jsonb_build_object(
    'chapterId',   v_id,
    'slug',        v_slug,
    'parentId',    p_parent_place_id,
    'url',         '/c/' || v_parent.handle || '/' || v_slug,
    'nearbySiblings', v_near);
end;
$$;

revoke all on function public.create_chapter(uuid, text, double precision, double precision, text, text)
  from public, anon;
grant execute on function public.create_chapter(uuid, text, double precision, double precision, text, text)
  to authenticated, service_role;

-- ── 3 · join · a chapter member is always a parent member ──────────────────
--
-- Decision 3. Two affiliation rows written together, rather than deriving parent
-- membership at read time: every existing count, roster and visibility query reads
-- circle_affiliations directly, and making parent membership implicit would mean
-- teaching all of them about the hierarchy. Two rows keeps them all correct for free.
--
-- Leaving a chapter does NOT leave the parent. Asymmetric on purpose — joining the
-- Lisboa chapter means joining Etiqueta do Reino; leaving Lisboa does not mean leaving
-- Etiqueta do Reino.
--
-- ON CONFLICT rather than WHERE NOT EXISTS: circle_affiliations carries two partial
-- unique indexes — (user_id, place_ref) and (user_id, circle_key), both WHERE
-- dismissed_at IS NULL. A check-then-insert races; two taps on Join would raise.

create or replace function public.join_chapter(p_chapter_id uuid)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public
as $$
declare
  v_uid     uuid := auth.uid();
  v_ch      record;
  v_parent  record;
  v_joined_parent boolean := false;
begin
  if v_uid is null then
    raise exception 'not_authenticated';
  end if;

  select p.id, p.name, p.parent_place_ref, p.place_type, p.chapter_slug
    into v_ch from public.places p where p.id = p_chapter_id;

  if v_ch.id is null then
    raise exception 'chapter_not_found';
  end if;
  if v_ch.parent_place_ref is null then
    raise exception 'not_a_chapter'
      using hint = 'Use the normal join path for a top-level community.';
  end if;

  select p.id, p.name, p.place_type
    into v_parent from public.places p where p.id = v_ch.parent_place_ref;

  -- Parent first, so a failure cannot leave somebody in a chapter of a community they
  -- are not in — which is the one state the visibility rules cannot describe.
  insert into public.circle_affiliations
    (user_id, circle_type, circle_key, place_ref, place_name, status, source, grounded)
  values (v_uid, coalesce(v_parent.place_type, 'other'),
          'place:' || v_parent.id::text, v_parent.id, v_parent.name,
          'confirmed', 'chapter_join', true)
  on conflict (user_id, place_ref) where dismissed_at is null do nothing;

  get diagnostics v_joined_parent = row_count;

  insert into public.circle_affiliations
    (user_id, circle_type, circle_key, place_ref, place_name, status, source, grounded)
  values (v_uid, coalesce(v_ch.place_type, 'other'),
          'place:' || v_ch.id::text, v_ch.id, v_ch.name,
          'confirmed', 'chapter_join', true)
  on conflict (user_id, place_ref) where dismissed_at is null do nothing;

  -- An existing row may be 'suggested' rather than 'confirmed'. Joining is explicit, so
  -- promote it either way.
  update public.circle_affiliations
     set status = 'confirmed', updated_at = now()
   where user_id = v_uid
     and place_ref in (v_ch.id, v_parent.id)
     and dismissed_at is null
     and status <> 'confirmed';

  return jsonb_build_object(
    'chapterId', v_ch.id,
    'parentId',  v_parent.id,
    'joinedParent', v_joined_parent);
end;
$$;

revoke all on function public.join_chapter(uuid) from public, anon;
grant execute on function public.join_chapter(uuid) to authenticated, service_role;

-- ── 4 · resolve · /c/{parent_handle}/{chapter_slug} ────────────────────────
--
-- Mirrors resolve_place_handle's posture: only a published parent resolves, and the
-- chapter is returned with its parent's identity attached so the page can say "a chapter
-- of Etiqueta do Reino" without a second round trip.

create or replace function public.resolve_chapter_handle(
  p_parent_handle text,
  p_chapter_slug  text
)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public
as $$
declare
  v_parent_in text := public.normalize_place_handle(p_parent_handle);
  v_slug      text := public.normalize_place_handle(p_chapter_slug);
  v_parent_id uuid;
  v_parent_name text;
  v_parent_handle text;
  v_ch record;
begin
  select p.id, p.name, p.handle into v_parent_id, v_parent_name, v_parent_handle
    from public.places p
   where p.handle = v_parent_in
     and p.governance_state = 'operator_verified';

  -- A retired parent handle still finds its chapters.
  if v_parent_id is null then
    select p.id, p.name, p.handle into v_parent_id, v_parent_name, v_parent_handle
      from public.place_handle_aliases a
      join public.places p on p.id = a.place_id
     where a.handle = v_parent_in
       and p.governance_state = 'operator_verified';
  end if;

  if v_parent_id is null then
    return null;
  end if;

  select p.id, p.name, p.chapter_slug, p.lat, p.lng, p.blurb, p.place_type,
         p.governance_state
    into v_ch
    from public.places p
   where p.parent_place_ref = v_parent_id
     and p.chapter_slug = v_slug
     and p.governance_state <> 'suspended';

  if v_ch.id is null then
    return null;
  end if;

  return jsonb_build_object(
    'placeId',     v_ch.id,
    'isChapter',   true,
    'slug',        v_ch.chapter_slug,
    'displayName', v_ch.name,
    'blurb',       v_ch.blurb,
    'lat',         v_ch.lat,
    'lng',         v_ch.lng,
    'placeType',   v_ch.place_type,
    'canonicalPath', '/c/' || v_parent_handle || '/' || v_ch.chapter_slug,
    'parent', jsonb_build_object(
      'placeId', v_parent_id, 'displayName', v_parent_name, 'handle', v_parent_handle));
end;
$$;

revoke all on function public.resolve_chapter_handle(text, text) from public;
-- anon, like resolve_place_handle: this is the public page for a chapter URL.
grant execute on function public.resolve_chapter_handle(text, text) to anon, authenticated, service_role;

-- ── 5 · list · "Chapters in CF Fitness" ─────────────────────────────────────

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
         (select count(*)::int from public.circle_affiliations a
           where a.place_ref = c.id and a.status = 'confirmed' and a.dismissed_at is null),
         exists (select 1 from public.circle_affiliations a
                  where a.place_ref = c.id and a.user_id = auth.uid()
                    and a.status = 'confirmed' and a.dismissed_at is null)
  from public.places c
  where c.parent_place_ref = p_place_id
    and c.governance_state <> 'suspended'
  order by c.name
  limit greatest(1, least(coalesce(p_limit, 50), 200));
$$;

revoke all on function public.community_chapters(uuid, int) from public, anon;
grant execute on function public.community_chapters(uuid, int) to authenticated, service_role;

-- ── 6 · the read direction a chapter member needs ──────────────────────────
--
-- CHAPTER → PARENT only. Given a place, returns the set of place ids whose content that
-- place's members may see:
--
--   a chapter  → {chapter, parent}
--   a parent   → {parent}            ← NOT its chapters. That is the other half.
--
-- community_scope.py currently filters on one place_id:
--
--     .or_(f"circle_place_ref.eq.{place_id},place_ref.eq.{place_id}")
--
-- It should ask this function for the set instead. Sibling leakage is impossible by
-- construction here: a chapter's set contains itself and its parent, never a sibling.

create or replace function public.scope_place_ids(p_place_id uuid)
returns table (place_id uuid, relation text)
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select p.id, 'self'::text from public.places p where p.id = p_place_id
  union all
  select p.parent_place_ref, 'parent'::text
    from public.places p
   where p.id = p_place_id and p.parent_place_ref is not null;
$$;

revoke all on function public.scope_place_ids(uuid) from public, anon;
grant execute on function public.scope_place_ids(uuid) to authenticated, service_role;

comment on function public.scope_place_ids(uuid) is
  'Which places'' content a member of p_place_id may see. A chapter sees itself and its '
  'parent; a top-level place sees only itself. The reverse direction (a parent seeing '
  'its chapters, every row carrying origin_place_ref and origin_place_name) is NOT here '
  'and must never be added by widening this function without the origin labels — an '
  'unlabelled cross-chapter row is indistinguishable from a local one.';

-- ============================================================================
-- ROLLBACK
--   drop function if exists public.scope_place_ids(uuid);
--   drop function if exists public.community_chapters(uuid, int);
--   drop function if exists public.resolve_chapter_handle(text, text);
--   drop function if exists public.join_chapter(uuid);
--   drop function if exists public.create_chapter(uuid, text, double precision, double precision, text, text);
--   drop index if exists public.places_chapter_slug_uniq;
--   alter table public.places drop constraint if exists places_chapter_slug_shape;
--   alter table public.places drop column if exists chapter_slug;
--   Additive. No existing row is modified; no existing function is replaced.
-- ============================================================================
