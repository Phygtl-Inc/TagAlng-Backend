-- Structured claim AND-search for discovery.find_by_attrs
-- p_filters: [{"bucket":"heritage","terms":["pakistani"]},{"bucket":"stage","terms":["mom","mother"]}]

create or replace function public.find_peers_by_claim_filters(
  p_filters jsonb,
  p_limit int default 5
)
returns table (
  peer_user_id uuid,
  nickname text,
  avatar_url text,
  similarity_score real,
  matching_peer_label text,
  matching_peer_concept text,
  has_exact_concept_match boolean
)
language plpgsql
security definer
set search_path = pg_catalog, public, extensions
stable
as $$
declare
  v_caller uuid := auth.uid();
  v_block_id text;
  v_n_filters int;
begin
  if v_caller is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;

  select u.home_block_id into v_block_id
  from public.users u where u.id = v_caller;

  if v_block_id is null then
    return;
  end if;

  v_n_filters := coalesce(jsonb_array_length(p_filters), 0);
  if v_n_filters < 1 then
    return;
  end if;

  return query
  with filter_spec as (
    select
      nullif(f->>'bucket', '') as bucket,
      (
        select coalesce(array_agg(lower(t)), '{}')
        from jsonb_array_elements_text(f->'terms') t
        where length(trim(t)) >= 2
      ) as terms
    from jsonb_array_elements(p_filters) f
  ),
  valid_filters as (
    select * from filter_spec where coalesce(array_length(terms, 1), 0) > 0
  ),
  block_peers as (
    select u.id as peer_id
    from public.users u
    where u.home_block_id = v_block_id
      and u.id <> v_caller
  ),
  peer_ok as (
    select bp.peer_id
    from block_peers bp
    where (
      select count(*) from valid_filters vf
    ) = (
      select count(*)
      from valid_filters vf
      where exists (
        select 1
        from public.user_identity_claims c
        where c.user_id = bp.peer_id
          and c.dismissed_at is null
          and c.disclosure = 'public'
          and (vf.bucket is null or c.bucket = vf.bucket)
          and exists (
            select 1 from unnest(vf.terms) term
            where lower(c.concept) like '%' || term || '%'
               or lower(c.label) like '%' || term || '%'
               or term = any(
                 select lower(s) from unnest(coalesce(c.synonyms, '{}')) s
               )
          )
      )
    )
  ),
  peer_labels as (
    select
      po.peer_id,
      string_agg(distinct c.label, ' · ' order by c.label) as labels
    from peer_ok po
    join public.user_identity_claims c on c.user_id = po.peer_id
      and c.dismissed_at is null
      and c.disclosure = 'public'
    group by po.peer_id
  )
  select
    po.peer_id,
    u.nickname,
    u.profile_photo_url,
    0.92::real,
    coalesce(pl.labels, 'shared traits'),
    null::text,
    true
  from peer_ok po
  join public.users u on u.id = po.peer_id
  left join peer_labels pl on pl.peer_id = po.peer_id
  order by u.nickname asc nulls last
  limit greatest(1, least(coalesce(p_limit, 5), 20));
end;
$$;

comment on function public.find_peers_by_claim_filters(jsonb, int) is
  'Layer 1 discovery.find_by_attrs — AND match on user_identity_claims per filter slot.';

revoke all on function public.find_peers_by_claim_filters(jsonb, int) from public, anon;
grant execute on function public.find_peers_by_claim_filters(jsonb, int) to authenticated;
