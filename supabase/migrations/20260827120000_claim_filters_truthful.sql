-- TagAlng: truthful find_peers_by_claim_filters.
-- Two lies fixed: (1) matching_peer_label aggregated EVERY public claim of a matched
-- peer, so a "likes to swim" card dumped the whole profile instead of the swim claim;
-- (2) similarity_score was a fabricated 0.92 and has_exact_concept_match a hardcoded
-- true — no caller-claim comparison ever happened. Labels are now only the claims that
-- satisfied a filter term; score and exact-flag report what we actually know.

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
  -- Only the claims that satisfied a filter term — the card shows WHY they matched,
  -- not their whole public profile.
  peer_labels as (
    select
      po.peer_id,
      string_agg(distinct c.label, ' · ' order by c.label) as labels,
      min(c.concept) as concept
    from peer_ok po
    join public.user_identity_claims c on c.user_id = po.peer_id
      and c.dismissed_at is null
      and c.disclosure = 'public'
    where exists (
      select 1
      from valid_filters vf
      where (vf.bucket is null or c.bucket = vf.bucket)
        and exists (
          select 1 from unnest(vf.terms) term
          where lower(c.concept) like '%' || term || '%'
             or lower(c.label) like '%' || term || '%'
             or term = any(
               select lower(s) from unnest(coalesce(c.synonyms, '{}')) s
             )
        )
    )
    group by po.peer_id
  )
  select
    po.peer_id,
    u.nickname,
    u.profile_photo_url,
    -- Lexical term matches are binary — no cosine ran, so no invented score.
    null::real,
    coalesce(pl.labels, 'shared traits'),
    pl.concept,
    -- Query-anchored search: no caller-claim comparison happened, so never claim one.
    false
  from peer_ok po
  join public.users u on u.id = po.peer_id
  left join peer_labels pl on pl.peer_id = po.peer_id
  order by u.nickname asc nulls last
  limit greatest(1, least(coalesce(p_limit, 5), 20));
end;
$$;

comment on function public.find_peers_by_claim_filters(jsonb, int) is
  'Layer 1 discovery.find_by_attrs — AND match on user_identity_claims per filter slot; label lists only term-matching claims, no fabricated score/exact-flag.';

-- Same two lies in the token-AND fallback RPC (its labels were already term-scoped).
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
    null::real,
    l.labels,
    null::text,
    false
  from peer_ok p
  join public.users u on u.id = p.peer_id
  join labels l on l.peer_id = p.peer_id
  order by u.nickname asc nulls last
  limit greatest(1, least(coalesce(p_limit, 5), 20));
end;
$$;

comment on function public.find_peers_by_attr_filter(text, int) is
  'Layer 1 discovery.find_by_attrs — multi-token AND match on same block; no fabricated score/exact-flag.';
