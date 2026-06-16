-- Multi-token AND matching for discovery.find_by_attrs

create or replace function public.find_peers_by_attr_filter(
  p_filter_text text,
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
  v_filter text := lower(trim(coalesce(p_filter_text, '')));
  v_tokens text[];
  v_need int;
begin
  if v_caller is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;
  if char_length(v_filter) < 2 then
    raise exception 'filter_too_short' using errcode = 'P0001';
  end if;

  select u.home_block_id into v_block_id
  from public.users u where u.id = v_caller;

  if v_block_id is null then
    return;
  end if;

  select coalesce(array_agg(tok), '{}') into v_tokens
  from (
    select distinct tok
    from unnest(regexp_split_to_array(v_filter, '[^a-z0-9]+')) tok
    where length(tok) >= 2
      and tok not in (
        'find','a','an','the','with','on','my','block','which','are','of',
        'near','me','who','speak','for','to','looking','some','any','good',
        'know','want','please','can','you','help','moms','dads'
      )
  ) t;

  v_need := coalesce(array_length(v_tokens, 1), 0);
  if v_need = 0 then
    return;
  end if;

  return query
  with token_hits as (
    select distinct c.user_id as peer_id, tok as hit_tok, c.label as match_label
    from unnest(v_tokens) tok
    cross join lateral (
      select c.*
      from public.user_identity_claims c
      join public.users u on u.id = c.user_id
      where u.home_block_id = v_block_id
        and c.user_id <> v_caller
        and c.dismissed_at is null
        and c.disclosure = 'public'
        and (
          lower(c.label) like '%' || tok || '%'
          or lower(c.concept) like '%' || tok || '%'
          or exists (
            select 1 from unnest(coalesce(c.synonyms, '{}')) s
            where lower(s) like '%' || tok || '%'
          )
        )
    ) c
  ),
  peer_ok as (
    select peer_id
    from token_hits
    group by peer_id
    having count(distinct hit_tok) >= v_need
  ),
  labels as (
    select th.peer_id,
           string_agg(distinct th.match_label, ' · ' order by th.match_label) as labels
    from token_hits th
    join peer_ok p on p.peer_id = th.peer_id
    group by th.peer_id
  )
  select
    p.peer_id,
    u.nickname,
    u.profile_photo_url,
    0.9::real,
    l.labels,
    null::text,
    true
  from peer_ok p
  join public.users u on u.id = p.peer_id
  join labels l on l.peer_id = p.peer_id
  order by u.nickname asc nulls last
  limit greatest(1, least(coalesce(p_limit, 5), 20));
end;
$$;

comment on function public.find_peers_by_attr_filter(text, int) is
  'Layer 1 discovery.find_by_attrs — multi-token AND match on same block.';

revoke all on function public.find_peers_by_attr_filter(text, int) from public, anon;
grant execute on function public.find_peers_by_attr_filter(text, int) to authenticated;
