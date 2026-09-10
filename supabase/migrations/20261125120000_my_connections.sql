-- ---------------------------------------------------------------------------
-- §42 — "who have I actually connected with?" gets a read of its own.
--
-- The share sheet's people tier is no longer /lana/fellows (matched neighbours, most of
-- whom the viewer has never met). It is the people she has connected with — and there was
-- no RPC for that set, so the client derived it from get_my_threads(): every open 1:1
-- thread is one connection. Sound as far as it goes (_open_relationship_thread is
-- service_role-only and runs on four accept events), but wrong twice:
--
--   1. It misses the meet-attendance tier. promote_relationship_tier(
--      'rsvp_attended_same_event') sets the pair to 'acquaintance' and opens NO thread, so
--      two neighbours who met at the same meet — the pair a meet share is most likely
--      aimed at — are invisible.
--   2. A closed swap hides a live connection. 20260623120000 archives the pair thread when
--      an inquiry closes and get_my_threads filters `archived_at is null`; the relationship
--      row survives, the person disappears.
--
-- So the connection is read from the RELATIONSHIP, which is where it actually lives, and
-- the thread is a left join: `thread_id null` means "connected, no open channel", which the
-- client already handles by falling back to the nudge.
--
-- 'nudge' is NOT a connection (frontend's position, ruled 2026-09-09). A nudge sent and
-- not yet accepted is one person's hope, and putting it here would fill the sheet with
-- people whose only channel is the nudge fallback §41 exists to retire. The floor is
-- 'acquaintance' — the first tier the ladder itself only reaches on a mutual event.
-- ---------------------------------------------------------------------------

create or replace function public.get_my_connections()
returns table (
  other_user_id uuid,
  nickname text,
  avatar_url text,
  tier public.relationship_tier,
  thread_id uuid,
  connected_at timestamptz
)
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select
    other.id,
    other.nickname,
    other.profile_photo_url,
    r.tier,
    t.id,
    r.last_transition_at
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

comment on function public.get_my_connections() is
  'People the caller has actually connected with (tier >= acquaintance), newest first. '
  'thread_id is the pair''s open 1:1 thread or NULL when there is none — a meet-attendance '
  'connection never had one and an archived swap thread no longer counts. Excludes '
  '''nudge'': an unanswered nudge is not a connection.';

revoke all on function public.get_my_connections() from public, anon;
grant execute on function public.get_my_connections() to authenticated, service_role;
