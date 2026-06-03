-- TagAlng Phase 3b: Update get_cluster_peers with mutual visibility + is_matched flag
-- Purpose: Show peer list with separate public/mutual claim counts and matching status
-- Backward compatibility: keep old columns `match_score` and `total_threads` as aliases for shared public claim count and shared event count.

drop function if exists public.get_cluster_peers(text);
create or replace function public.get_cluster_peers(p_cluster_id text)
returns table (
  user_id uuid,
  nickname text,
  avatar_url text,
  match_score int,
  total_threads int,
  is_blurred bool,
  public_claim_count int,
  mutual_claim_count int,
  shared_event_count int,
  is_matched bool
)
language plpgsql
security definer
set search_path = pg_catalog, public
stable
as $$
declare
  caller uuid := auth.uid();
begin
  -- Anonymous: return blurred placeholders (no personal data)
  if caller is null then
    return query
    select
      null::uuid,
      null::text,
      null::text,
      0,
      0,
      0,
      true,
      false
    from generate_series(1, 10);
    return;
  end if;

  -- Authenticated: return real peers with visibility controls
  return query
  with cluster_users as (
    select u.id, u.nickname, u.profile_photo_url
    from public.users u
    join public.blocks b on b.id = u.home_block_id
    where b.cluster_id = p_cluster_id
      and u.id <> caller
  ),
  caller_public_concepts as (
    select c.concept
    from public.user_identity_claims c
    where c.user_id = caller
      and c.dismissed_at is null
      and c.disclosure = 'public'
  ),
  peer_data as (
    select
      cu.id,
      cu.nickname,
      cu.profile_photo_url,
      -- Count public claims shared with caller
      coalesce((
        select count(*)::int
        from public.user_identity_claims pc
        join caller_public_concepts cc on cc.concept = pc.concept
        where pc.user_id = cu.id
          and pc.dismissed_at is null
          and pc.disclosure = 'public'
      ), 0) as public_claims_count,
      -- Count mutual claims (visible only if matched, but return count for UI)
      coalesce((
        select case
          when public.are_users_matched(caller, cu.id) then
            count(*)::int
          else 0
        end
        from public.user_identity_claims pc
        where pc.user_id = cu.id
          and pc.dismissed_at is null
          and pc.disclosure = 'mutual'
      ), 0) as mutual_claims_count,
      -- Count shared approved/attended events
      coalesce((
        select count(distinct er1.event_id)::int
        from public.event_requests er1
        join public.event_requests er2 on er2.event_id = er1.event_id
        where er1.requester_id = caller
          and er2.requester_id = cu.id
          and er1.status in ('approved', 'attended')
          and er2.status in ('approved', 'attended')
      ), 0) as shared_events,
      -- Is matched?
      public.are_users_matched(caller, cu.id) as is_matched
    from cluster_users cu
  )
  select
    pd.id,
    pd.nickname,
    pd.profile_photo_url,
    pd.public_claims_count as match_score,
    pd.shared_events as total_threads,
    false,  -- is_blurred = false for authenticated users
    pd.public_claims_count,
    pd.mutual_claims_count,
    pd.shared_events,
    pd.is_matched
  from peer_data pd
  -- Order by: most public matches → most shared events → alphabetical
  order by pd.public_claims_count desc, pd.shared_events desc, pd.nickname asc nulls last;
end;
$$;
comment on function public.get_cluster_peers(text) is
  'Returns list of peers in a cluster with separate public/mutual claim counts and is_matched flag. Anon sees blurred placeholders.';
grant execute on function public.get_cluster_peers(text) to authenticated, anon;
