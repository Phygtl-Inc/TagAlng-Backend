-- MemGPT Tier 2: message embeddings, neighbor_facts, block_context, recall RPC

alter table public.lana_messages
  add column if not exists embedding extensions.vector(768);

comment on column public.lana_messages.embedding is
  'text-embedding-005 vector for archival recall (self scope).';

create index if not exists lana_messages_embedding_hnsw_idx
  on public.lana_messages
  using hnsw (embedding extensions.vector_cosine_ops)
  where embedding is not null;

create table if not exists public.neighbor_facts (
  id uuid primary key default gen_random_uuid(),
  owner_user_id uuid not null references public.users (id) on delete cascade,
  about_user_id uuid not null references public.users (id) on delete cascade,
  fact text not null check (char_length(fact) >= 1 and char_length(fact) <= 500),
  consent_tier public.relationship_tier not null default 'acquaintance',
  source text not null default 'inferred'
    check (source in ('self_disclosed', 'inferred', 'intro_accepted', 'cohost')),
  embedding extensions.vector(768),
  created_at timestamptz not null default now(),
  constraint neighbor_facts_not_self check (owner_user_id <> about_user_id)
);

comment on table public.neighbor_facts is
  'Per-neighbor learned facts for Lana recall (neighbors scope). Tier-gated by consent_tier.';

create index if not exists neighbor_facts_owner_idx
  on public.neighbor_facts (owner_user_id, created_at desc);

create index if not exists neighbor_facts_embedding_hnsw_idx
  on public.neighbor_facts
  using hnsw (embedding extensions.vector_cosine_ops)
  where embedding is not null;

alter table public.neighbor_facts enable row level security;

create policy "neighbor_facts_select_own"
  on public.neighbor_facts for select
  to authenticated
  using (owner_user_id = auth.uid());

create policy "neighbor_facts_no_client_write"
  on public.neighbor_facts for all
  to authenticated
  using (false)
  with check (false);

create table if not exists public.block_context (
  id uuid primary key default gen_random_uuid(),
  block_id text not null references public.blocks (id) on delete cascade,
  fact text not null check (char_length(fact) >= 1 and char_length(fact) <= 500),
  category text,
  embedding extensions.vector(768),
  updated_at timestamptz not null default now()
);

comment on table public.block_context is
  'Block-level public-safe patterns for Lana recall (block scope).';

create index if not exists block_context_block_idx
  on public.block_context (block_id, updated_at desc);

create index if not exists block_context_embedding_hnsw_idx
  on public.block_context
  using hnsw (embedding extensions.vector_cosine_ops)
  where embedding is not null;

alter table public.block_context enable row level security;

create policy "block_context_select_home_block"
  on public.block_context for select
  to authenticated
  using (
    block_id = (
      select u.home_block_id
      from public.users u
      where u.id = auth.uid()
    )
  );

create policy "block_context_no_client_write"
  on public.block_context for all
  to authenticated
  using (false)
  with check (false);

-- Archival retrieval (service role / lana-worker only)
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

comment on function public.lana_recall_memories(uuid, text, extensions.vector, text, int) is
  'MemGPT archival recall. Scopes: self (claims, inquiries, messages), neighbors (tier-gated facts), block (public block patterns). Service role only.';

revoke all on function public.lana_recall_memories(uuid, text, extensions.vector, text, int)
  from public, anon, authenticated;

grant execute on function public.lana_recall_memories(uuid, text, extensions.vector, text, int)
  to service_role;
