-- TagAlng Phase 3b: Mutual matching helper for privacy model
-- Purpose: Determine if two users are "matched" for mutual disclosure

-- Helper: are two users "matched" (for mutual disclosure)?
-- Matched if:
--   1. Both attended same open/completed event (approved or attended status), OR
--   2. Both have at least one shared public identity claim
create or replace function public.are_users_matched(
  p_user_a uuid,
  p_user_b uuid
)
returns boolean
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select (
    exists (
      select 1
      from public.event_requests er_a
      join public.event_requests er_b
        on er_a.event_id = er_b.event_id
      where er_a.requester_id = p_user_a
        and er_b.requester_id = p_user_b
        and er_a.status in ('approved', 'attended')
        and er_b.status in ('approved', 'attended')
    )
    or
    exists (
      select 1
      from public.user_identity_claims c_a
      join public.user_identity_claims c_b
        on c_a.concept = c_b.concept
      where c_a.user_id = p_user_a
        and c_b.user_id = p_user_b
        and c_a.dismissed_at is null
        and c_b.dismissed_at is null
        and c_a.disclosure = 'public'
        and c_b.disclosure = 'public'
    )
  );
$$;

comment on function public.are_users_matched(uuid, uuid) is
  'Returns true if two users are matched (same event or shared public claims). Used for mutual disclosure visibility.';

grant execute on function public.are_users_matched(uuid, uuid) to authenticated;
