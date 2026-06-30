-- LANA Layer 3 · capability matcher RPC (spec §4.3).
-- Given an entity embedding, return the nearest active capabilities by cosine similarity.
-- Service-role only (lana-worker calls it post-turn); ranking/gating beyond cosine is v0.3.

create or replace function public.match_latent_capabilities(
  p_query_embedding extensions.vector(768),
  p_limit int default 3,
  p_min_score real default 0.65
)
returns table (
  capability_id text,
  capability_name text,
  similarity real,
  surface_priority int,
  entity_triggers text[],
  identity_claim_triggers text[],
  required_state text[]
)
language plpgsql
stable
security definer
set search_path = public, extensions
as $$
declare
  v_limit int := greatest(1, least(coalesce(p_limit, 3), 10));
begin
  return query
  select
    ci.capability_id,
    ci.capability_name,
    (1 - (ci.embedding <=> p_query_embedding))::real as similarity,
    ci.surface_priority,
    ci.entity_triggers,
    ci.identity_claim_triggers,
    ci.required_state
  from public.capability_index ci
  where ci.is_active
    and ci.embedding is not null
    and (1 - (ci.embedding <=> p_query_embedding)) >= coalesce(p_min_score, 0.65)
  order by ci.embedding <=> p_query_embedding
  limit v_limit;
end;
$$;

comment on function public.match_latent_capabilities(extensions.vector, int, real) is
  'Layer 3b: cosine-match an entity embedding against capability_index. Service role only.';

revoke all on function public.match_latent_capabilities(extensions.vector, int, real)
  from public, anon, authenticated;

grant execute on function public.match_latent_capabilities(extensions.vector, int, real)
  to service_role;
