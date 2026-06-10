-- Heritage/faith: never vector-match across different concepts (Brazilian ≠ Pakistani).
-- Vector similarity is still used for activity, interest, stage, vicinity, etc.

create or replace function public.match_peers_by_claim_vectors(
  p_limit int default 20,
  p_min_similarity real default 0.65
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
    select distinct on (p.peer_id)
      p.peer_id,
      p.peer_concept,
      p.peer_label,
      p.sim
    from peer_claim_pairs p
    order by p.peer_id, p.sim desc
  ),
  exact_concepts as (
    select distinct pc.user_id as peer_id
    from public.user_identity_claims cc
    join public.user_identity_claims pc
      on pc.concept = cc.concept
     and pc.user_id <> v_caller
     and pc.dismissed_at is null
     and pc.disclosure = 'public'
    join public.users u on u.id = pc.user_id
    where cc.user_id = v_caller
      and cc.dismissed_at is null
      and cc.disclosure = 'public'
      and u.home_block_id = v_block_id
  )
  select
    pb.peer_id as peer_user_id,
    u.nickname,
    u.profile_photo_url as avatar_url,
    pb.sim as similarity_score,
    pb.peer_label as matching_peer_label,
    pb.peer_concept as matching_peer_concept,
    exists (select 1 from exact_concepts ec where ec.peer_id = pb.peer_id) as has_exact_concept_match
  from peer_best pb
  join public.users u on u.id = pb.peer_id
  order by pb.sim desc, u.nickname asc nulls last
  limit greatest(1, least(coalesce(p_limit, 20), 50));
end;
$$;

create or replace function public.match_peers_by_claim_vectors_for_user(
  p_user_id uuid,
  p_limit int default 10,
  p_min_similarity real default 0.65
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
      p.sim
    from peer_claim_pairs p
    order by p.peer_id, p.sim desc
  ),
  exact_concepts as (
    select distinct pc.user_id as peer_id
    from public.user_identity_claims cc
    join public.user_identity_claims pc
      on pc.concept = cc.concept
     and pc.user_id <> p_user_id
     and pc.dismissed_at is null
     and pc.disclosure = 'public'
    join public.users u on u.id = pc.user_id
    where cc.user_id = p_user_id
      and cc.dismissed_at is null
      and cc.disclosure = 'public'
      and u.home_block_id = v_block_id
  )
  select
    pb.peer_id as peer_user_id,
    u.nickname,
    u.profile_photo_url as avatar_url,
    pb.sim as similarity_score,
    pb.peer_label as matching_peer_label,
    pb.peer_concept as matching_peer_concept,
    exists (select 1 from exact_concepts ec where ec.peer_id = pb.peer_id) as has_exact_concept_match
  from peer_best pb
  join public.users u on u.id = pb.peer_id
  order by pb.sim desc, u.nickname asc nulls last
  limit greatest(1, least(coalesce(p_limit, 10), 50));
end;
$$;

comment on function public.match_peers_by_claim_vectors(int, real) is
  'Block-scoped vector peer match. Heritage/faith pairs require same concept slug; other buckets use cosine similarity.';

comment on function public.match_peers_by_claim_vectors_for_user(uuid, int, real) is
  'Vector peer match for a user (service role). Heritage/faith cross-concept pairs excluded.';
