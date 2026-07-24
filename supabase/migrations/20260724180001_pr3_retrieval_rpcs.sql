-- ============================================================================
-- PR 3 · Retrieval RPCs for context assembly  (→ Asjid to review & push)
-- ----------------------------------------------------------------------------
-- WHY: HNSW cosine indexes already exist on lana_messages, user_identity_claims,
--      and capability_index — but the worker has no clean retrieval entrypoints,
--      so context is at risk of full-history stuffing → context-rot (30-50%
--      mid-window accuracy loss). These are the read-only recall functions the
--      policy's context-assembler calls each turn.
-- SCOPE: 3 STABLE read-only SQL functions. No schema change. Non-destructive.
-- NOTE: `<=>` = cosine distance (pairs with vector_cosine_ops HNSW). Functions
--       run as the worker's role (service) — they do NOT bypass RLS for other
--       callers; callers pass p_user/p_session they're authorized for.
-- ============================================================================

begin;

-- durable-memory recall (excludes dismissed; uses user_identity_claims HNSW)
create or replace function match_claims(p_user uuid, p_emb vector, p_k int default 8)
returns setof user_identity_claims
language sql stable
as $$
  select * from user_identity_claims
  where user_id = p_user and dismissed_at is null and embedding is not null
  order by embedding <=> p_emb
  limit greatest(p_k, 1);
$$;

-- semantic recall within a session (uses lana_messages HNSW)
create or replace function match_messages(p_session uuid, p_emb vector, p_k int default 6)
returns setof lana_messages
language sql stable
as $$
  select * from lana_messages
  where session_id = p_session and embedding is not null
  order by embedding <=> p_emb
  limit greatest(p_k, 1);
$$;

-- capability recall by meaning (complements the trigger/state filter in PR1)
create or replace function match_capabilities(p_emb vector, p_k int default 5)
returns setof capability_index
language sql stable
as $$
  select * from capability_index
  where is_active and embedding is not null
  order by embedding <=> p_emb
  limit greatest(p_k, 1);
$$;

commit;

-- ============================================================================
-- CONTEXT-ASSEMBLY CONTRACT (how the worker uses these):
--   ctx = recent_messages(session, k=12  via (session_id,created_at) idx)   -- recency
--       + match_claims(user, q_emb, 8)                                      -- durable memory
--       + match_messages(session, q_emb, 6)                                 -- semantic recall
--       + lana_sessions.context.rolling_summary                             -- compressed history
--       + world_state(user)                                                 -- blocks.state, circles, tier, role/gender
--   -> assemble under a token budget; summarize older turns into rolling_summary.
-- ROLLBACK:
--   drop function if exists match_claims(uuid, vector, int);
--   drop function if exists match_messages(uuid, vector, int);
--   drop function if exists match_capabilities(vector, int);
-- ============================================================================
