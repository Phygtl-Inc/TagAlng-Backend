-- ---------------------------------------------------------------------------
-- §45 (option a) — get_my_connections gains a place scope: shared_place_ids.
--
-- The community invite sheet asks three questions of the same people — "who are my
-- fellows", "which of them are already at this community", "which are not" — and only
-- the first had a read. The client swept POST /lana/circles/members (the heaviest read
-- in the communities API, capped and silently truncated at 200 ids) just to keep the
-- peer_user_id column, and an UNVERIFIED caller got [] from that sweep (members[] is
-- blanked for her) and was shown the unfiltered list.
--
-- get_my_connections(p_place_ids uuid[] default null) returns today's rows, in today's
-- order, plus shared_place_ids: the places in scope where that connection ALSO has a
-- confirmed affiliation. The scope is
--   · p_place_ids null  → every place the caller herself is confirmed at;
--   · p_place_ids given → those places INTERSECTED with the caller's own confirmed
--                         places. A place she does not belong to is silently dropped,
--                         never disclosed.
-- So the column only ever names places the caller is a confirmed member of, and only
-- for people she is already connected to — the same fact /lana/circles/members already
-- tells a verified member about her own community. No phone-verification gate: the
-- membership check is the disclosure check, so a guest sees exactly what a verified
-- member does (the acceptance asks for that).
--
-- Never null: '{}' when the connection shares none of the scoped places.
-- 'curious' rows count on neither side — curious is not membership (20261017120000).
--
-- The body below is the CURRENT one (20261125120000_my_connections.sql, the only
-- definition) with the column added; nothing else changes. The return type changes,
-- so the zero-arg function is DROPPED first — leaving it would give PostgREST two
-- candidates for a no-argument rpc('get_my_connections') call. With the default, that
-- call now resolves to this one and returns the same rows plus the new column.
--
-- ROLLBACK: re-run the create/comment/grant block of 20261125120000_my_connections.sql
-- after `drop function if exists public.get_my_connections(uuid[]);`.
-- ---------------------------------------------------------------------------

drop function if exists public.get_my_connections();

create or replace function public.get_my_connections(p_place_ids uuid[] default null)
returns table (
  other_user_id uuid,
  nickname text,
  avatar_url text,
  tier public.relationship_tier,
  thread_id uuid,
  connected_at timestamptz,
  shared_place_ids uuid[]
)
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  with scope as (
    -- Disclosure boundary: only places the CALLER is a confirmed member of.
    select distinct ca.place_ref as place_id
    from public.circle_affiliations ca
    where ca.user_id = auth.uid()
      and ca.status = 'confirmed'
      and ca.dismissed_at is null
      and ca.place_ref is not null
      and (p_place_ids is null or ca.place_ref = any (p_place_ids))
  )
  select
    other.id,
    other.nickname,
    other.profile_photo_url,
    r.tier,
    t.id,
    r.last_transition_at,
    coalesce(
      (
        select array_agg(distinct oa.place_ref order by oa.place_ref)
        from public.circle_affiliations oa
        join scope s on s.place_id = oa.place_ref
        where oa.user_id = other.id
          and oa.status = 'confirmed'
          and oa.dismissed_at is null
      ),
      '{}'::uuid[]
    )
  from public.user_relationships r
  join public.users other
    on other.id = case when r.user_low = auth.uid() then r.user_high else r.user_low end
  -- At most one row: chat_threads_relationship_uniq is one thread per pair across
  -- shielded/direct. An 'inquiry' thread is not a relationship channel and is not read
  -- here — a swap conversation ending must not take the connection with it.
  left join public.chat_threads t
    on t.user_low = r.user_low
   and t.user_high = r.user_high
   and t.kind in ('shielded', 'direct')
   and t.archived_at is null
  where auth.uid() is not null
    -- security definer: the caller sees only pairs they are a party to. This is the
    -- disclosure check, not just a filter — without it the function reads the whole graph.
    and (r.user_low = auth.uid() or r.user_high = auth.uid())
    and public._tier_rank(r.tier)
        >= public._tier_rank('acquaintance'::public.relationship_tier)
    -- Symmetric, so a block hides the pair from both sides.
    and not public.lana_is_blocked(auth.uid(), other.id)
  order by r.last_transition_at desc nulls last;
$$;

comment on function public.get_my_connections(uuid[]) is
  'People the caller has actually connected with (tier >= acquaintance), newest first. '
  'thread_id is the pair''s open 1:1 thread or NULL when there is none — a meet-attendance '
  'connection never had one and an archived swap thread no longer counts. Excludes '
  '''nudge'': an unanswered nudge is not a connection. shared_place_ids (never null) is '
  'the places in scope where that connection is also a confirmed member; the scope is '
  'p_place_ids intersected with the caller''s own confirmed places, or all of them when '
  'p_place_ids is null — a place the caller does not belong to is never disclosed.';

revoke all on function public.get_my_connections(uuid[]) from public, anon, authenticated;
grant execute on function public.get_my_connections(uuid[]) to authenticated, service_role;
