-- TagAlng Phase 2: identity claims + pgvector (text-embedding-005 = 768 dims)

create type public.claim_disclosure as enum ('public', 'mutual', 'private');

create table public.user_identity_claims (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.users (id) on delete cascade,
  concept text not null,
  label text not null,
  tone text,
  confidence real not null check (confidence >= 0 and confidence <= 1),
  disclosure public.claim_disclosure not null default 'public',
  synonyms text[] not null default '{}',
  embedding extensions.vector(768),
  dismissed_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint user_identity_claims_concept_format
    check (concept ~ '^[a-z][a-z0-9_]{1,63}$')
);

comment on table public.user_identity_claims is
  'AI-extracted identity threads per user. Never store race, exact age, or sex as claims.';

create unique index user_identity_claims_user_concept_active_idx
  on public.user_identity_claims (user_id, concept)
  where dismissed_at is null;

create index user_identity_claims_user_id_idx
  on public.user_identity_claims (user_id)
  where dismissed_at is null;

create index user_identity_claims_embedding_hnsw_idx
  on public.user_identity_claims
  using hnsw (embedding extensions.vector_cosine_ops)
  where dismissed_at is null and embedding is not null;

create trigger user_identity_claims_updated_at
before update on public.user_identity_claims
for each row execute function public.set_updated_at();

-- Read own active claims (for Profile built UI)
create or replace function public.get_my_identity_claims()
returns table (
  id uuid,
  concept text,
  label text,
  tone text,
  confidence real,
  disclosure public.claim_disclosure,
  synonyms text[],
  created_at timestamptz
)
language sql
stable
security definer
set search_path = public
as $$
  select
    c.id,
    c.concept,
    c.label,
    c.tone,
    c.confidence,
    c.disclosure,
    c.synonyms,
    c.created_at
  from public.user_identity_claims c
  where c.user_id = auth.uid()
    and c.dismissed_at is null
  order by c.confidence desc, c.created_at desc;
$$;

grant execute on function public.get_my_identity_claims to authenticated;
