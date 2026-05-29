-- TagAlng Phase 1: waitlist RPC + atlas ticker

-- Validate cohort ids against cohorts table
create or replace function public.validate_cohort_ids(p_cohorts text[])
returns boolean
language sql
stable
as $$
  select not exists (
    select 1
    from unnest(p_cohorts) as c(id)
    where not exists (select 1 from public.cohorts co where co.id = c.id)
  );
$$;
-- Join waitlist (called from Next admin/website server or later Edge Function)
create or replace function public.join_waitlist(
  p_phone text default null,
  p_city text default null,
  p_declared_cohorts text[] default '{}',
  p_sport_sub text default null,
  p_candidate_block_id text default null,
  p_inbound_ref text default null,
  p_inbound_cohorts text[] default '{}',
  p_recaptcha_verified boolean default false
)
returns uuid
language plpgsql
security definer
set search_path = public
as $$
declare
  v_id uuid;
  v_cohorts text[];
begin
  if not p_recaptcha_verified then
    raise exception 'recaptcha_required' using errcode = 'P0001';
  end if;

  if p_candidate_block_id is null then
    raise exception 'block_required' using errcode = 'P0001';
  end if;

  if not exists (select 1 from public.blocks where id = p_candidate_block_id) then
    raise exception 'invalid_block' using errcode = 'P0001';
  end if;

  v_cohorts := coalesce(nullif(p_declared_cohorts, '{}'), p_inbound_cohorts, '{}');
  if cardinality(v_cohorts) < 1 then
    raise exception 'cohorts_required' using errcode = 'P0001';
  end if;

  if not public.validate_cohort_ids(v_cohorts) then
    raise exception 'invalid_cohort' using errcode = 'P0001';
  end if;

  if p_sport_sub is not null and not exists (
    select 1 from public.cohorts where id = p_sport_sub and kind = 'sport_subtype'
  ) then
    raise exception 'invalid_sport_sub' using errcode = 'P0001';
  end if;

  insert into public.waitlist_signups (
    phone,
    city,
    declared_cohorts,
    sport_sub,
    candidate_block_id,
    inbound_ref,
    inbound_cohorts,
    recaptcha_verified
  ) values (
    p_phone,
    p_city,
    v_cohorts,
    p_sport_sub,
    p_candidate_block_id,
    p_inbound_ref,
    coalesce(p_inbound_cohorts, '{}'),
    p_recaptcha_verified
  )
  returning id into v_id;

  return v_id;
end;
$$;
-- Bump atlas count + notify Realtime listeners
create or replace function public.bump_block_waitlist_count()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
declare
  v_count int;
begin
  insert into public.block_waitlist_counts (block_id, signup_count)
  values (new.candidate_block_id, 1)
  on conflict (block_id) do update
    set signup_count = public.block_waitlist_counts.signup_count + 1,
        updated_at = now();

  select signup_count into v_count
  from public.block_waitlist_counts
  where block_id = new.candidate_block_id;

  perform pg_notify(
    'atlas_block',
    json_build_object(
      'block_id', new.candidate_block_id,
      'signup_count', v_count,
      'event', 'waitlist_signup'
    )::text
  );

  return new;
end;
$$;
create trigger waitlist_atlas_notify
after insert on public.waitlist_signups
for each row execute function public.bump_block_waitlist_count();
-- Read atlas state (for admin dashboard + public ticker)
create or replace function public.get_atlas_snapshot(p_cluster_id text default 'lake-nona')
returns table (
  block_id text,
  display_name text,
  state public.block_state,
  signup_count int,
  updated_at timestamptz
)
language sql
stable
security definer
set search_path = public
as $$
  select
    b.id,
    b.display_name,
    b.state,
    coalesce(c.signup_count, 0),
    coalesce(c.updated_at, b.updated_at)
  from public.blocks b
  left join public.block_waitlist_counts c on c.block_id = b.id
  where b.cluster_id = p_cluster_id
  order by coalesce(c.signup_count, 0) desc;
$$;
grant execute on function public.join_waitlist to anon, authenticated;
grant execute on function public.get_atlas_snapshot to anon, authenticated;
