-- Exact-concept peer matches are lexical facts — never gate them on embeddings.
-- QA 2026-07-16: viewer and Fish share 3 concepts (outdoor_enthusiast, long_married,
-- italian_heritage) but the card said only one, because peer_claim_pairs required
-- BOTH claims to have embeddings even for cc.concept = pc.concept, and locally the
-- embedding writer fails intermittently. Same-concept pairs now join lexically
-- (sim = cosine when both embedded, else 1.0 — an identical concept slug is maximal
-- overlap); vector similarity remains required for fuzzy (different-concept) pairs.
-- Signatures unchanged from 20260821 (shared_labels) — create or replace only.

create or replace function public.match_peers_by_claim_vectors(
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
  ),
  peer_claim_pairs as (
    select
      pc.user_id as peer_id,
      pc.concept as peer_concept,
      pc.label as peer_label,
      cc.label as my_label,
      (cc.concept = pc.concept) as exact_pair,
      case
        when cc.concept = pc.concept
          then coalesce((1 - (cc.embedding <=> pc.embedding))::real, 1.0::real)
        else (1 - (cc.embedding <=> pc.embedding))::real
      end as sim
    from caller_claims cc
    join public.user_identity_claims pc
      on pc.user_id <> v_caller
     and pc.dismissed_at is null
     and pc.disclosure = 'public'
    join public.users u on u.id = pc.user_id
    where u.home_block_id = v_block_id
      and (
        cc.concept = pc.concept
        or (
          cc.embedding is not null
          and pc.embedding is not null
          and (1 - (cc.embedding <=> pc.embedding)) >= p_min_similarity
          and not (
            coalesce(cc.bucket, 'general') in ('heritage', 'faith')
            and coalesce(pc.bucket, 'general') in ('heritage', 'faith')
            and cc.concept <> pc.concept
          )
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

create or replace function public.match_peers_by_claim_vectors_for_user(
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
  ),
  peer_claim_pairs as (
    select
      pc.user_id as peer_id,
      pc.concept as peer_concept,
      pc.label as peer_label,
      cc.label as my_label,
      (cc.concept = pc.concept) as exact_pair,
      case
        when cc.concept = pc.concept
          then coalesce((1 - (cc.embedding <=> pc.embedding))::real, 1.0::real)
        else (1 - (cc.embedding <=> pc.embedding))::real
      end as sim
    from caller_claims cc
    join public.user_identity_claims pc
      on pc.user_id <> p_user_id
     and pc.dismissed_at is null
     and pc.disclosure = 'public'
    join public.users u on u.id = pc.user_id
    where u.home_block_id = v_block_id
      and (
        cc.concept = pc.concept
        or (
          cc.embedding is not null
          and pc.embedding is not null
          and (1 - (cc.embedding <=> pc.embedding)) >= p_min_similarity
          and not (
            coalesce(cc.bucket, 'general') in ('heritage', 'faith')
            and coalesce(pc.bucket, 'general') in ('heritage', 'faith')
            and cc.concept <> pc.concept
          )
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
  'Block-scoped peer match. Same-concept claim pairs join lexically (no embedding needed); '
  'different-concept pairs require cosine >= floor. shared_labels lists every exact-concept '
  'shared claim. Heritage/faith pairs require the same concept slug.';

comment on function public.match_peers_by_claim_vectors_for_user(uuid, int, real) is
  'Peer match for a user (service role). Same semantics as the authed variant.';
