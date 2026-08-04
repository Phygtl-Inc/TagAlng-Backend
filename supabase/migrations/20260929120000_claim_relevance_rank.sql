-- Rank a user's identity claims by relevance to what they just said.
--
-- QA 2026-08-03: decide_turn was handed the user's claim threads as a flat list
-- (first 40 rows, no ordering, top 8 kept) with no notion of what the current
-- turn was about. The prompt then asks any topic change to be justified by
-- "something THEY told you" — so the model reached into that bag and produced
-- "is there a favorite blue thing that cheers you up?" while the person was
-- explaining that they were in pain and hadn't slept. The relevant fact they had
-- JUST said (food is their comfort, they were dieting) carried no more weight
-- than a colour mentioned weeks earlier.
--
-- Ordering by cosine distance against the turn's own embedding lets the policy
-- see WHICH of its stored facts actually bear on this moment, and the returned
-- similarity lets it see when the honest answer is "none of them".
--
-- Mirrors public.rapport_uncovered_claims (20260809120000): security definer,
-- service-role only, uses the existing HNSW index on user_identity_claims.

create or replace function public.rank_claims_by_relevance(
  p_user_id uuid,
  p_embedding extensions.vector(768),
  p_limit int default 6
) returns table (concept text, label text, details text[], bucket text, similarity real)
language sql
stable
security definer
set search_path = pg_catalog, public, extensions
as $$
  select
    c.concept,
    c.label,
    c.details,
    c.bucket,
    (1 - (c.embedding <=> p_embedding))::real as similarity
  from public.user_identity_claims c
  where c.user_id = p_user_id
    and c.dismissed_at is null
    and c.embedding is not null
  order by c.embedding <=> p_embedding
  limit greatest(1, least(coalesce(p_limit, 6), 20));
$$;

comment on function public.rank_claims_by_relevance(uuid, extensions.vector, int) is
  'Policy: the user''s active identity claims ordered by cosine similarity to the '
  'current turn, most relevant first, with the similarity score. Claims without an '
  'embedding are omitted — callers fall back to an unranked read. Service role only.';

revoke all on function public.rank_claims_by_relevance(uuid, extensions.vector, int)
  from public, anon, authenticated;
