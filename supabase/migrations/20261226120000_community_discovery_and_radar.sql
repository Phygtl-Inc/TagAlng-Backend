-- Discovery that works on day one · entry recording · the widen radar
--
-- THE COLD-START BUG THIS FIXES
--
--   discover_communities_semantic scores a community by its MEMBERS' public identity
--   claims, and filters `members > 0`. Etiqueta do Reino has ONE public claim in total.
--   Every creator community in production has exactly one affiliation — the creator.
--
--   So a community is undiscoverable at precisely the moment the creator needs it found,
--   and the pilot cannot work. The fix is not to lower a threshold: it is to let a
--   community be found on its OWN description as well as on who is in it.
--
--   Member claims remain the stronger signal and still win. The blurb is the floor.

-- ── 1 · discovery · two arms, member claims first, blurb as the floor ───────

create or replace function public.discover_communities(
  p_user_id         uuid,
  p_query_embedding extensions.vector(768),
  p_min_similarity  real    default 0.55,
  p_limit           int     default 10,
  p_creator_only    boolean default false,
  p_include_mine    boolean default false
)
returns table (
  place_id      uuid,
  name          text,
  place_type    text,
  hq_city       text,
  hq_lat        double precision,
  hq_lng        double precision,
  members       int,
  is_mine       boolean,
  match_label   text,
  -- 'member_claim' when a person in it matches, 'blurb' when only the description does.
  -- The UI needs this: "because Ana runs" is proof, "because it's about running" is not.
  match_kind    text,
  similarity    real,
  is_new        boolean
)
language sql
stable
security definer
set search_path = pg_catalog, public, extensions
as $$
  with visible as (
    select vm.place_ref, vm.user_id from public.visible_place_members(p_user_id) vm
  ),
  counted as (
    select v.place_ref as pid,
           count(distinct v.user_id)::int as members,
           bool_or(v.user_id = p_user_id) as mine
    from visible v group by v.place_ref
  ),
  -- ARM A · a member's own public claim. Public + subject_kind='self' only: a discovery
  -- surface is by definition strangers, and "my kid does karate" is a claim somebody
  -- HOLDS but is not about them.
  by_claim as (
    select distinct on (v.place_ref)
      v.place_ref as pid, c.label as lbl, 'member_claim'::text as kind,
      (1 - (c.embedding <=> p_query_embedding))::real as sim
    from visible v
    join public.user_identity_claims c
      on c.user_id = v.user_id
     and c.dismissed_at is null and c.transient = false
     and c.disclosure = 'public' and c.subject_kind = 'self'
     and c.embedding is not null
    order by v.place_ref, c.embedding <=> p_query_embedding
  ),
  -- ARM B · the community's own description. This is the whole point: it works with zero
  -- members, which is every community on its first day.
  by_blurb as (
    select p.id as pid, p.blurb as lbl, 'blurb'::text as kind,
           (1 - (p.blurb_embedding <=> p_query_embedding))::real as sim
    from public.places p
    where p.blurb_embedding is not null
      and p.governance_state in ('community_started','operator_verified')
  ),
  merged as (
    select * from by_claim
    union all
    select * from by_blurb b
    -- A member-claim hit already describes this place with better evidence.
    where not exists (select 1 from by_claim c where c.pid = b.pid)
  )
  select
    m.pid, p.name, p.place_type, p.hq_city, p.hq_lat, p.hq_lng,
    coalesce(c.members, 0), coalesce(c.mine, false),
    m.lbl, m.kind, m.sim,
    coalesce(c.members, 0) <= 1 as is_new
  from merged m
  join public.places p on p.id = m.pid
  left join counted c  on c.pid = m.pid
  where m.sim >= p_min_similarity
    and (not p_creator_only or p.place_type = 'creator')
    and (p_include_mine or not coalesce(c.mine, false))
    and p.governance_state <> 'suspended'
  -- Member-claim matches outrank blurb matches at equal similarity: real people beat a
  -- description. Then size, so an established community is not buried by an empty one.
  order by (m.kind = 'member_claim') desc, m.sim desc, coalesce(c.members,0) desc, p.name
  limit greatest(1, least(coalesce(p_limit, 10), 50));
$$;

revoke all on function public.discover_communities(uuid, extensions.vector, real, int, boolean, boolean)
  from public, anon;
grant execute on function public.discover_communities(uuid, extensions.vector, real, int, boolean, boolean)
  to authenticated, service_role;

comment on function public.discover_communities is
  'Community discovery with two arms. Member claims are the stronger evidence and rank '
  'first; the blurb is the floor that lets a brand-new community be found at all. '
  'is_new marks communities with one member or none so the UI can say "just started" '
  'rather than hiding them — hiding them is what makes a creator launch impossible. '
  'discover_communities_semantic is left in place; migrate callers, then retire it.';

-- ── 2 · entry recording ─────────────────────────────────────────────────────

create or replace function public.record_community_entry(
  p_place_id    uuid,
  p_entry_kind  text default 'creator_link',
  p_handle_used text default null,
  p_session_id  uuid default null
)
returns uuid
language plpgsql
volatile
security definer
set search_path = pg_catalog, public
as $$
declare
  v_uid  uuid := auth.uid();
  v_anon boolean := coalesce((auth.jwt() ->> 'is_anonymous')::boolean, true);
  v_id   uuid;
begin
  if not exists (select 1 from public.places where id = p_place_id) then
    raise exception 'place_not_found';
  end if;

  insert into public.community_entry_events
    (place_id, user_id, entry_kind, handle_used, session_id, was_anonymous)
  values (p_place_id, v_uid, coalesce(p_entry_kind,'creator_link'),
          p_handle_used, p_session_id, v_anon)
  returning id into v_id;

  return v_id;
end;
$$;

revoke all on function public.record_community_entry(uuid, text, text, uuid) from public;
-- anon included on purpose: the visitor who has not signed up is the whole point.
grant execute on function public.record_community_entry(uuid, text, text, uuid)
  to anon, authenticated, service_role;

-- Joining stamps the arrival it converted. Most recent unconverted entry for that person
-- and place: a second visit that converts should not mark the first one as the winner.
create or replace function public.mark_community_entry_joined(p_place_id uuid)
returns void
language sql
volatile
security definer
set search_path = pg_catalog, public
as $$
  update public.community_entry_events e
     set joined_at = now()
   where e.id = (
     select e2.id from public.community_entry_events e2
      where e2.place_id = p_place_id
        and e2.user_id = auth.uid()
        and e2.joined_at is null
      order by e2.created_at desc
      limit 1);
$$;

revoke all on function public.mark_community_entry_joined(uuid) from public;
grant execute on function public.mark_community_entry_joined(uuid) to anon, authenticated, service_role;

-- ── 3 · the creator's real numbers · visitors are not members ───────────────
--
-- place_claim_card counts every affiliation with dismissed_at is null — including
-- 'suggested' rows and including anonymous visitors. That number cannot be shown to a
-- creator as a membership, and it is the number handle activation would later gate on.

create or replace function public.community_audience(p_place_id uuid)
returns jsonb
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select jsonb_build_object(
    'placeId', p_place_id,
    -- Confirmed, signed-up. The honest membership number.
    'members', (
      select count(*)::int from public.circle_affiliations a
      join auth.users au on au.id = a.user_id
      where a.place_ref = p_place_id and a.status = 'confirmed'
        and a.dismissed_at is null and not au.is_anonymous),
    -- Joined, but still anonymous. Real people, not yet accounts. Promoted automatically
    -- when they sign up: Supabase keeps the same auth.users.id and flips is_anonymous,
    -- so the affiliation carries over with no migration of its own.
    'provisionalMembers', (
      select count(*)::int from public.circle_affiliations a
      join auth.users au on au.id = a.user_id
      where a.place_ref = p_place_id and a.status = 'confirmed'
        and a.dismissed_at is null and au.is_anonymous),
    'visits', (
      select count(*)::int from public.community_entry_events e
      where e.place_id = p_place_id),
    'visitsJoined', (
      select count(*)::int from public.community_entry_events e
      where e.place_id = p_place_id and e.joined_at is not null),
    'visitsLast30', (
      select count(*)::int from public.community_entry_events e
      where e.place_id = p_place_id and e.created_at > now() - interval '30 days'));
$$;

revoke all on function public.community_audience(uuid) from public, anon;
grant execute on function public.community_audience(uuid) to authenticated, service_role;

comment on function public.community_audience(uuid) is
  'The creator dashboard numbers, kept apart on purpose. members = confirmed and signed '
  'up. provisionalMembers = joined while still anonymous. visits vs visitsJoined is the '
  'conversion ratio, and it is the only number that tells a creator whether their link '
  'works. Never collapse these into one count.';

-- ── 4 · the widen radar ─────────────────────────────────────────────────────
--
-- When the community has no answer, Lana asks before looking wider. THE ASK ITSELF IS THE
-- SIGNAL — an unanswered question with a community attached and nobody in it to answer.
-- That is the highest-value row in the product and inquiry_signals, which exists for
-- exactly this, has ZERO rows.

create or replace function public.record_community_widen(
  p_place_id  uuid,
  p_free_text text,
  p_category  text default 'recommendation',
  p_session_id uuid default null,
  p_widened   boolean default true
)
returns uuid
language plpgsql
volatile
security definer
set search_path = pg_catalog, public
as $$
declare
  v_uid uuid := auth.uid();
  v_id  uuid;
begin
  if v_uid is null then
    raise exception 'not_authenticated';
  end if;
  if p_free_text is null or length(btrim(p_free_text)) < 3 then
    raise exception 'free_text_required';
  end if;

  insert into public.inquiry_signals
    (user_id, category, free_text, session_id, source_module, status, opt_in_followup)
  values (v_uid, coalesce(p_category,'recommendation'), btrim(p_free_text), p_session_id,
          -- Distinct module so these are separable from companionship capture. This is
          -- demand, not conversation.
          case when p_widened then 'community_widen' else 'community_unanswered' end,
          'open', false)
  returning id into v_id;

  -- The place the question failed in. block_id is the existing free slot on this table;
  -- a dedicated column would be cleaner and is called out in the PR for Asjid to rule on.
  update public.inquiry_signals set block_id = p_place_id::text where id = v_id;

  return v_id;
end;
$$;

revoke all on function public.record_community_widen(uuid, text, text, uuid, boolean) from public, anon;
grant execute on function public.record_community_widen(uuid, text, text, uuid, boolean)
  to authenticated, service_role;

-- What a community was asked and could not answer. The creator's most useful screen:
-- "eleven people asked your community about X and nobody answered."
create or replace function public.community_unanswered(p_place_id uuid, p_limit int default 20)
returns table (free_text text, category text, asked_at timestamptz, n int)
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select i.free_text, i.category, max(i.captured_at) as asked_at, count(*)::int as n
  from public.inquiry_signals i
  where i.block_id = p_place_id::text
    and i.source_module in ('community_widen','community_unanswered')
  group by i.free_text, i.category
  order by count(*) desc, max(i.captured_at) desc
  limit greatest(1, least(coalesce(p_limit, 20), 100));
$$;

revoke all on function public.community_unanswered(uuid, int) from public, anon;
grant execute on function public.community_unanswered(uuid, int) to authenticated, service_role;

-- ============================================================================
-- ROLLBACK
--   drop function if exists public.community_unanswered(uuid, int);
--   drop function if exists public.record_community_widen(uuid, text, text, uuid, boolean);
--   drop function if exists public.community_audience(uuid);
--   drop function if exists public.mark_community_entry_joined(uuid);
--   drop function if exists public.record_community_entry(uuid, text, text, uuid);
--   drop function if exists public.discover_communities(uuid, extensions.vector, real, int, boolean, boolean);
--   Additive throughout. discover_communities_semantic is untouched and still serves its
--   existing callers until they are migrated.
-- ============================================================================
