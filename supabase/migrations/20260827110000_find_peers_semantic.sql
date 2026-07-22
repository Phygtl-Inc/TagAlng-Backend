-- TagAlng: semantic attribute search for discovery.find_by_attrs.
-- "gymmer" should surface "Gym enthusiast" — cosine match between the user's ask and the
-- claim embeddings we already store, instead of growing synonym word lists. The worker
-- calls this as a fallback when the lexical (substring) filter finds no one.

create or replace function public.find_peers_by_claim_semantic(
  p_query_embedding extensions.vector(768),
  p_limit int default 5,
  p_min_similarity real default 0.55
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
  if p_query_embedding is null then
    return;
  end if;

  select u.home_block_id into v_block_id
  from public.users u
  where u.id = v_caller;

  if v_block_id is null then
    return;
  end if;

  return query
  with peer_claims as (
    select
      pc.user_id as peer_id,
      pc.concept as peer_concept,
      pc.label as peer_label,
      (1 - (pc.embedding <=> p_query_embedding))::real as sim
    from public.user_identity_claims pc
    join public.users u on u.id = pc.user_id
    where u.home_block_id = v_block_id
      and pc.user_id <> v_caller
      and pc.dismissed_at is null
      and pc.disclosure = 'public'
      and pc.embedding is not null
      and (1 - (pc.embedding <=> p_query_embedding)) >= p_min_similarity
  ),
  peer_best as (
    select distinct on (p.peer_id)
      p.peer_id,
      p.peer_concept,
      p.peer_label,
      p.sim
    from peer_claims p
    order by p.peer_id, p.sim desc
  )
  select
    pb.peer_id as peer_user_id,
    u.nickname,
    u.profile_photo_url as avatar_url,
    pb.sim as similarity_score,
    pb.peer_label as matching_peer_label,
    pb.peer_concept as matching_peer_concept,
    -- No caller-claim comparison here: the query text is the anchor, not a claim pair.
    false as has_exact_concept_match
  from peer_best pb
  join public.users u on u.id = pb.peer_id
  order by pb.sim desc, u.nickname asc nulls last
  limit greatest(1, least(coalesce(p_limit, 5), 20));
end;
$$;

comment on function public.find_peers_by_claim_semantic(extensions.vector, int, real) is
  'discovery.find_by_attrs semantic fallback — peers on caller home block whose public claim embeddings are close to the query embedding. p_min_similarity 0-1 (cosine).';

revoke all on function public.find_peers_by_claim_semantic(extensions.vector, int, real) from public, anon;
grant execute on function public.find_peers_by_claim_semantic(extensions.vector, int, real) to authenticated;
