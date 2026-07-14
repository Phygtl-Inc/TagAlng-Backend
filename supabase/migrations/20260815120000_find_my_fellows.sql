-- TagAlng: "Find my fellows" (profile sheet CTA) — one FE-callable RPC returning ranked,
-- render-ready peer matches for the signed-in user.
--
-- Why a wrapper over match_peers_by_claim_vectors:
--   * The verify gate lives IN SQL here — the raw matcher returns nicknames + peer ids to
--     any authenticated session (including anonymous/unverified ones); this wrapper masks
--     identity fields until the caller is a verified, non-anonymous neighbor, mirroring
--     the worker-side peers_to_match_rows gating.
--   * Card enrichment (stars / band / badge / trait tags) previously existed only in the
--     lana-worker Python (peer_discovery_surface.py); computing it here means the FE can
--     render match cards from this one call with zero scoring logic. Thresholds MUST stay
--     in sync with peer_discovery_surface.py.

create or replace function public.find_my_fellows(
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
    -- Distinct from "no matches yet" (empty result): the user has no home block, so the
    -- FE should route them to set it (ZIP) instead of showing an empty-state.
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
      m.matching_peer_label as m_label,
      m.matching_peer_concept as m_concept,
      m.has_exact_concept_match as m_exact,
      -- Stars/band mirror peer_discovery_surface.py (score_to_stars / match_band).
      case
        when m.has_exact_concept_match and coalesce(m.similarity_score, 0) >= 0.75 then 5
        when coalesce(m.similarity_score, 0) >= 0.90 then 5
        when coalesce(m.similarity_score, 0) >= 0.80 then 4
        when coalesce(m.similarity_score, 0) >= 0.65 then 3
        when coalesce(m.similarity_score, 0) >= 0.50 then 2
        else 1
      end as m_stars,
      case
        when coalesce(m.similarity_score, 0) >= 0.80
          or (m.has_exact_concept_match and coalesce(m.similarity_score, 0) >= 0.75)
          then 'strong'
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
    s.m_label as matching_peer_label,
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
        select btrim(part) as tag
        from unnest(string_to_array(coalesce(s.m_label, ''), '·')) as part
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
  '(stars/band/badge/trait_tags). Identity fields masked (preview=true) until the caller is a '
  'verified non-anonymous neighbor. Raises home_block_missing when no home block is set.';

revoke all on function public.find_my_fellows(int, real) from public, anon;
grant execute on function public.find_my_fellows(int, real) to authenticated;
