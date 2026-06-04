-- Lana agent context: block-scoped network snapshot (peers + events) for worker retrieval.
-- Service-role only — not exposed to browsers; lana-worker calls via service client.

create or replace function public.get_lana_block_context_for_user(p_user_id uuid)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_block_id text;
  v_cluster_id text;
  v_block_name text;
  v_member_count int;
  v_events jsonb;
  v_neighbors jsonb;
begin
  if p_user_id is null then
    raise exception 'user_id_required' using errcode = 'P0001';
  end if;

  select u.home_block_id, b.cluster_id, b.display_name
  into v_block_id, v_cluster_id, v_block_name
  from public.users u
  left join public.blocks b on b.id = u.home_block_id
  where u.id = p_user_id;

  if v_block_id is null then
    return jsonb_build_object(
      'has_block', false,
      'block_id', null,
      'cluster_id', null,
      'block_display_name', null,
      'member_count', 0,
      'open_events_count', 0,
      'upcoming_events', '[]'::jsonb,
      'neighbor_hints', '[]'::jsonb
    );
  end if;

  select count(*)::int
  into v_member_count
  from public.users u
  where u.home_block_id = v_block_id
    and u.id <> p_user_id;

  select coalesce(jsonb_agg(jsonb_build_object(
      'title', e.title,
      'starts_at', e.starts_at,
      'venue_name', e.venue_name,
      'cohort_tags', e.cohort_tags
    ) order by e.starts_at asc), '[]'::jsonb)
  into v_events
  from (
    select e.title, e.starts_at, e.venue_name, e.cohort_tags
    from public.events e
    where e.status = 'open'
      and e.cluster_id = coalesce(v_cluster_id, 'lake-nona')
      and e.starts_at between now() and now() + interval '14 days'
    order by e.starts_at asc
    limit 5
  ) e;

  select coalesce(jsonb_agg(jsonb_build_object(
      'nickname', n.nickname,
      'shared_public_claim_count', n.shared_count,
      'public_labels', n.public_labels
    ) order by n.shared_count desc, n.nickname asc nulls last), '[]'::jsonb)
  into v_neighbors
  from (
    select
      u.nickname,
      coalesce((
        select count(*)::int
        from public.user_identity_claims c1
        join public.user_identity_claims c2 on c1.concept = c2.concept
        where c1.user_id = p_user_id
          and c2.user_id = u.id
          and c1.dismissed_at is null
          and c2.dismissed_at is null
          and c1.disclosure = 'public'
          and c2.disclosure = 'public'
      ), 0) as shared_count,
      coalesce((
        select jsonb_agg(sub.label order by sub.confidence desc)
        from (
          select distinct c.label, c.confidence
          from public.user_identity_claims c
          where c.user_id = u.id
            and c.dismissed_at is null
            and c.disclosure = 'public'
          order by c.confidence desc
          limit 3
        ) sub
      ), '[]'::jsonb) as public_labels
    from public.users u
    where u.home_block_id = v_block_id
      and u.id <> p_user_id
    order by shared_count desc, u.created_at asc
    limit 5
  ) n;

  return jsonb_build_object(
    'has_block', true,
    'block_id', v_block_id,
    'cluster_id', v_cluster_id,
    'block_display_name', v_block_name,
    'member_count', v_member_count,
    'open_events_count', jsonb_array_length(v_events),
    'upcoming_events', v_events,
    'neighbor_hints', v_neighbors
  );
end;
$$;

comment on function public.get_lana_block_context_for_user(uuid) is
  'Block-scoped peer/event snapshot for Lana worker (agent retrieval). Public claims only; service role.';

revoke all on function public.get_lana_block_context_for_user(uuid) from public, anon, authenticated;
grant execute on function public.get_lana_block_context_for_user(uuid) to service_role;
