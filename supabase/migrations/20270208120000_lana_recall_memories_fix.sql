-- ============================================================================
-- lana_recall_memories: it has never returned a row
-- ============================================================================
-- WHY
--   Every call since 20260614120000 fails with
--     42702: column reference "similarity" is ambiguous
--   The function RETURNS TABLE (..., similarity real, ...), which makes `similarity` a
--   PL/pgSQL variable too, and the body filters and orders by the subquery's column of the
--   same name. The worker swallows the error (orchestrator/recall.py returns []), so the
--   memory Lana prefetches before EVERY chat reply has always been empty, and nothing she
--   was told in earlier conversations reached her. Found 2026-10-14 tracing a repeated
--   rapport question (todiba was asked where he gets a pizza he had already named).
--
-- WHAT THIS DOES
--   Recreates the function unchanged except for `#variable_conflict use_column`, which
--   tells PL/pgSQL that a bare name in a query means the column. Signature, scopes,
--   grants and security are identical.
-- ============================================================================

create or replace function public.lana_recall_memories(
  p_user_id uuid,
  p_block_id text,
  p_query_embedding extensions.vector(768),
  p_scope text,
  p_limit int default 5
)
returns table (
  source_type text,
  source_id uuid,
  content text,
  similarity real,
  captured_at timestamptz
)
language plpgsql
stable
security definer
set search_path = public, extensions
as $$
#variable_conflict use_column
declare
  v_limit int := greatest(1, least(coalesce(p_limit, 5), 10));
begin
  if p_scope not in ('self', 'neighbors', 'block') then
    raise exception 'invalid_recall_scope';
  end if;

  return query
  with ranked as (
    select * from (
      select
        'claim'::text as source_type,
        c.id as source_id,
        (c.label || ' (' || c.concept || ')')::text as content,
        (1 - (c.embedding <=> p_query_embedding))::real as similarity,
        coalesce(c.updated_at, c.created_at) as captured_at
      from public.user_identity_claims c
      where p_scope = 'self'
        and c.user_id = p_user_id
        and c.dismissed_at is null
        and c.embedding is not null

      union all

      select
        'inquiry'::text,
        i.id,
        (i.category || ': ' || left(i.free_text, 200))::text,
        (1 - (i.embedding <=> p_query_embedding))::real,
        i.captured_at
      from public.inquiry_signals i
      where p_scope = 'self'
        and i.user_id = p_user_id
        and i.embedding is not null

      union all

      select
        'message'::text,
        m.id,
        (m.role || ': ' || left(m.content, 200))::text,
        (1 - (m.embedding <=> p_query_embedding))::real,
        m.created_at
      from public.lana_messages m
      join public.lana_sessions s on s.id = m.session_id
      where p_scope = 'self'
        and s.user_id = p_user_id
        and m.embedding is not null

      union all

      select
        'neighbor_fact'::text,
        nf.id,
        nf.fact::text,
        (1 - (nf.embedding <=> p_query_embedding))::real,
        nf.created_at
      from public.neighbor_facts nf
      join public.user_relationships ur on (
        (ur.user_low = p_user_id and ur.user_high = nf.about_user_id)
        or (ur.user_high = p_user_id and ur.user_low = nf.about_user_id)
      )
      where p_scope = 'neighbors'
        and nf.owner_user_id = p_user_id
        and nf.embedding is not null
        and public._tier_rank(ur.tier) >= public._tier_rank(nf.consent_tier)

      union all

      select
        'block_context'::text,
        bc.id,
        bc.fact::text,
        (1 - (bc.embedding <=> p_query_embedding))::real,
        bc.updated_at
      from public.block_context bc
      where p_scope = 'block'
        and p_block_id is not null
        and bc.block_id = p_block_id
        and bc.embedding is not null
    ) sub
    where similarity is not null
    order by similarity desc
    limit v_limit
  )
  select * from ranked;
end;
$$;

revoke all on function public.lana_recall_memories(uuid, text, extensions.vector, text, int)
  from public, anon, authenticated;

grant execute on function public.lana_recall_memories(uuid, text, extensions.vector, text, int)
  to service_role;

-- ============================================================================
-- ROLLBACK
--   Re-apply the definition in 20260614120000_lana_memgpt_memory.sql (it errors on every call).
-- ============================================================================
