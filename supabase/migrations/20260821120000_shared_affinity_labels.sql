-- Peer matches: surface ALL shared identities, not just the single best pair.
-- QA follow-up on 20260820 (truthful peer match): a card showed one "You both: X"
-- even when several claims matched exactly. Both matchers now also return
-- shared_labels — the peer-side labels of every exact-concept pair — so surfaces
-- can say "You both: Soccer Fan · Enjoys outdoor activities". The single best
-- pair (matching_my_label / matching_peer_label) stays for fuzzy-only matches.
-- Mirrors app/peer_discovery_surface.py compose_match_reason — keep in sync.

drop function if exists public.match_peers_by_claim_vectors(int, real);
drop function if exists public.match_peers_by_claim_vectors_for_user(uuid, int, real);

create function public.match_peers_by_claim_vectors(
  p_limit int default 20,
  p_min_similarity real default 0.70
)
returns table (
  peer_user_id uuid,
  nickname text,
  avatar_url text,
  similarity_score real,
  matching_peer_label text,
  matching_peer_concept text,
  has_exact_concept_match boolean,
  matching_my_label text,
  shared_labels text[]
)
language plpgsql
security definer
set search_path = pg_catalog, public, extensions
stable
as $$
declare
  v_caller uuid := auth.uid();
  v_block_id text;
begin
  if v_caller is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;

  select u.home_block_id into v_block_id
  from public.users u
  where u.id = v_caller;

  if v_block_id is null then
    return;
  end if;

  return query
  with caller_claims as (
    select c.concept, c.label, c.bucket, c.embedding
    from public.user_identity_claims c
    where c.user_id = v_caller
      and c.dismissed_at is null
      and c.disclosure = 'public'
      and c.embedding is not null
  ),
  peer_claim_pairs as (
    select
      pc.user_id as peer_id,
      pc.concept as peer_concept,
      pc.label as peer_label,
      cc.label as my_label,
      (cc.concept = pc.concept) as exact_pair,
      (1 - (cc.embedding <=> pc.embedding))::real as sim
    from caller_claims cc
    join public.user_identity_claims pc
      on pc.user_id <> v_caller
     and pc.dismissed_at is null
     and pc.disclosure = 'public'
     and pc.embedding is not null
    join public.users u on u.id = pc.user_id
    where u.home_block_id = v_block_id
      and (1 - (cc.embedding <=> pc.embedding)) >= p_min_similarity
      and (
        cc.concept = pc.concept
        or not (
          coalesce(cc.bucket, 'general') in ('heritage', 'faith')
          and coalesce(pc.bucket, 'general') in ('heritage', 'faith')
          and cc.concept <> pc.concept
        )
      )
  ),
  peer_best as (
    -- Display pair: prefer an exact shared concept over a marginally higher
    -- fuzzy pair — the shown reason must be defensible from both profiles.
    select distinct on (p.peer_id)
      p.peer_id,
      p.peer_concept,
      p.peer_label,
      p.my_label,
      p.exact_pair,
      p.sim
    from peer_claim_pairs p
    order by p.peer_id, p.exact_pair desc, p.sim desc
  ),
  peer_shared as (
    -- Every exact-concept pair = a claim both profiles genuinely carry.
    select s.peer_id, array_agg(s.peer_label order by s.best_sim desc) as shared_labels
    from (
      select p.peer_id, p.peer_label, max(p.sim) as best_sim
      from peer_claim_pairs p
      where p.exact_pair
      group by p.peer_id, p.peer_label
    ) s
    group by s.peer_id
  )
  select
    pb.peer_id as peer_user_id,
    u.nickname,
    u.profile_photo_url as avatar_url,
    pb.sim as similarity_score,
    pb.peer_label as matching_peer_label,
    pb.peer_concept as matching_peer_concept,
    pb.exact_pair as has_exact_concept_match,
    pb.my_label as matching_my_label,
    coalesce(ps.shared_labels, '{}') as shared_labels
  from peer_best pb
  join public.users u on u.id = pb.peer_id
  left join peer_shared ps on ps.peer_id = pb.peer_id
  order by pb.sim desc, u.nickname asc nulls last
  limit greatest(1, least(coalesce(p_limit, 20), 50));
end;
$$;

create function public.match_peers_by_claim_vectors_for_user(
  p_user_id uuid,
  p_limit int default 10,
  p_min_similarity real default 0.70
)
returns table (
  peer_user_id uuid,
  nickname text,
  avatar_url text,
  similarity_score real,
  matching_peer_label text,
  matching_peer_concept text,
  has_exact_concept_match boolean,
  matching_my_label text,
  shared_labels text[]
)
language plpgsql
security definer
set search_path = pg_catalog, public, extensions
stable
as $$
declare
  v_block_id text;
begin
  if p_user_id is null then
    raise exception 'user_id_required' using errcode = 'P0001';
  end if;

  select u.home_block_id into v_block_id
  from public.users u
  where u.id = p_user_id;

  if v_block_id is null then
    return;
  end if;

  return query
  with caller_claims as (
    select c.concept, c.label, c.bucket, c.embedding
    from public.user_identity_claims c
    where c.user_id = p_user_id
      and c.dismissed_at is null
      and c.disclosure = 'public'
      and c.embedding is not null
  ),
  peer_claim_pairs as (
    select
      pc.user_id as peer_id,
      pc.concept as peer_concept,
      pc.label as peer_label,
      cc.label as my_label,
      (cc.concept = pc.concept) as exact_pair,
      (1 - (cc.embedding <=> pc.embedding))::real as sim
    from caller_claims cc
    join public.user_identity_claims pc
      on pc.user_id <> p_user_id
     and pc.dismissed_at is null
     and pc.disclosure = 'public'
     and pc.embedding is not null
    join public.users u on u.id = pc.user_id
    where u.home_block_id = v_block_id
      and (1 - (cc.embedding <=> pc.embedding)) >= p_min_similarity
      and (
        cc.concept = pc.concept
        or not (
          coalesce(cc.bucket, 'general') in ('heritage', 'faith')
          and coalesce(pc.bucket, 'general') in ('heritage', 'faith')
          and cc.concept <> pc.concept
        )
      )
  ),
  peer_best as (
    select distinct on (p.peer_id)
      p.peer_id,
      p.peer_concept,
      p.peer_label,
      p.my_label,
      p.exact_pair,
      p.sim
    from peer_claim_pairs p
    order by p.peer_id, p.exact_pair desc, p.sim desc
  ),
  peer_shared as (
    select s.peer_id, array_agg(s.peer_label order by s.best_sim desc) as shared_labels
    from (
      select p.peer_id, p.peer_label, max(p.sim) as best_sim
      from peer_claim_pairs p
      where p.exact_pair
      group by p.peer_id, p.peer_label
    ) s
    group by s.peer_id
  )
  select
    pb.peer_id as peer_user_id,
    u.nickname,
    u.profile_photo_url as avatar_url,
    pb.sim as similarity_score,
    pb.peer_label as matching_peer_label,
    pb.peer_concept as matching_peer_concept,
    pb.exact_pair as has_exact_concept_match,
    pb.my_label as matching_my_label,
    coalesce(ps.shared_labels, '{}') as shared_labels
  from peer_best pb
  join public.users u on u.id = pb.peer_id
  left join peer_shared ps on ps.peer_id = pb.peer_id
  order by pb.sim desc, u.nickname asc nulls last
  limit greatest(1, least(coalesce(p_limit, 10), 50));
end;
$$;

comment on function public.match_peers_by_claim_vectors(int, real) is
  'Block-scoped vector peer match. Best pair per peer prefers exact shared concepts; '
  'matching_my_label is the caller-side claim of that pair; shared_labels lists every '
  'exact-concept shared claim. Heritage/faith pairs require the same concept slug.';

comment on function public.match_peers_by_claim_vectors_for_user(uuid, int, real) is
  'Vector peer match for a user (service role). Same semantics as the authed variant.';

revoke all on function public.match_peers_by_claim_vectors(int, real) from public, anon;
grant execute on function public.match_peers_by_claim_vectors(int, real) to authenticated;

revoke all on function public.match_peers_by_claim_vectors_for_user(uuid, int, real)
  from public, anon, authenticated;
grant execute on function public.match_peers_by_claim_vectors_for_user(uuid, int, real)
  to service_role;

-- Fellows drawer: compose the label from every shared claim when there are any.
create or replace function public.find_my_fellows(
  p_limit int default 5,
  p_min_similarity real default 0.70
)
returns table (
  peer_user_id uuid,
  nickname text,
  avatar_url text,
  similarity_score real,
  matching_peer_label text,
  matching_peer_concept text,
  has_exact_concept_match boolean,
  preview boolean,
  match_stars int,
  match_band text,
  match_badge text,
  trait_tags text[]
)
language plpgsql
security definer
set search_path = pg_catalog, public, extensions
stable
as $$
declare
  v_caller uuid := auth.uid();
  v_block_id text;
  v_verified boolean;
begin
  if v_caller is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;

  select u.home_block_id into v_block_id
  from public.users u
  where u.id = v_caller;

  if v_block_id is null then
    raise exception 'home_block_missing' using errcode = 'P0001';
  end if;

  v_verified := (not public.auth_is_anonymous()) and public.auth_is_phone_verified();

  return query
  with matches as (
    select m.*
    from public.match_peers_by_claim_vectors_for_user(v_caller, p_limit, p_min_similarity) m
  ),
  scored as (
    select
      m.peer_user_id as m_peer_user_id,
      m.nickname as m_nickname,
      m.avatar_url as m_avatar_url,
      coalesce(m.similarity_score, 0)::real as m_sim,
      m.matching_peer_label as m_peer_label,
      nullif(btrim(coalesce(m.matching_my_label, '')), '') as m_my_label,
      (coalesce(m.shared_labels, '{}'))[1:3] as m_shared,
      m.matching_peer_concept as m_concept,
      m.has_exact_concept_match as m_exact,
      -- Stars/band mirror peer_discovery_surface.py (score_to_stars / match_band);
      -- score only — no exact-concept boost.
      case
        when coalesce(m.similarity_score, 0) >= 0.90 then 5
        when coalesce(m.similarity_score, 0) >= 0.80 then 4
        when coalesce(m.similarity_score, 0) >= 0.65 then 3
        when coalesce(m.similarity_score, 0) >= 0.50 then 2
        else 1
      end as m_stars,
      case
        when coalesce(m.similarity_score, 0) >= 0.80 then 'strong'
        when coalesce(m.similarity_score, 0) >= 0.65 then 'partial'
        else 'weak'
      end as m_band
    from matches m
  )
  select
    case when v_verified then s.m_peer_user_id end as peer_user_id,
    case when v_verified then s.m_nickname end as nickname,
    case when v_verified then s.m_avatar_url end as avatar_url,
    s.m_sim as similarity_score,
    -- Truthful reason: every genuinely shared claim, else both sides of the pair.
    case
      when cardinality(s.m_shared) > 0
        then concat('You both: ', array_to_string(s.m_shared, ' · '))
      when s.m_exact or lower(coalesce(s.m_my_label, '')) = lower(coalesce(s.m_peer_label, ''))
        then concat('You both: ', s.m_peer_label)
      when s.m_my_label is not null
        then concat('You: ', s.m_my_label, ' · Them: ', s.m_peer_label)
      else s.m_peer_label
    end as matching_peer_label,
    case when v_verified then s.m_concept end as matching_peer_concept,
    s.m_exact as has_exact_concept_match,
    ((not v_verified) or s.m_nickname is null) as preview,
    s.m_stars as match_stars,
    s.m_band as match_band,
    case
      when s.m_band = 'strong' and s.m_stars >= 5 then 'PERFECT FIT'
      when s.m_band = 'strong' then 'STRONG'
      when s.m_band = 'partial' then 'PARTIAL'
      else 'WEAK'
    end as match_badge,
    (
      select coalesce(array_agg(tt.tag), '{}')
      from (
        select distinct btrim(part) as tag
        from unnest(
          case
            when cardinality(s.m_shared) > 0 then s.m_shared
            when s.m_exact or s.m_my_label is null then array[coalesce(s.m_peer_label, '')]
            else array[s.m_my_label, coalesce(s.m_peer_label, '')]
          end
        ) as part
        where length(btrim(part)) >= 2
          and lower(btrim(part)) not in
            ('block resident', 'lives on my block', 'neighborhood connection')
        limit 5
      ) tt
    ) as trait_tags
  from scored s
  order by s.m_stars desc, s.m_sim desc, s.m_nickname asc nulls last;
end;
$$;

comment on function public.find_my_fellows(int, real) is
  'Profile "Find my fellows" CTA: ranked peer matches on the caller''s home block, card-ready '
  '(stars/band/badge/trait_tags from the similarity score alone). Labels list every shared '
  'claim ("You both: A · B") or both sides of the best pair ("You: X · Them: Y"). Identity '
  'fields masked (preview=true) until the caller is a verified non-anonymous neighbor. '
  'Raises home_block_missing when no home block is set.';

revoke all on function public.find_my_fellows(int, real) from public, anon;
grant execute on function public.find_my_fellows(int, real) to authenticated;
