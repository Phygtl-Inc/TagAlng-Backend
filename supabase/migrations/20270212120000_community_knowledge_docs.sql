-- Community knowledge documents — owners upload MD / DOCX / PDF, Lana answers from them.
--
-- The owner (or a co-owner) uploads a file. The worker converts it to Markdown, splits it
-- at headings into sections, and has the model write a "covers:" line per section. When a
-- person inside the community asks something those sections answer, Lana answers from
-- the full section text and the worker checks every quoted line against the source
-- before the reply goes out.
--
-- Everything goes through the worker (service role). Clients never touch these tables:
-- the worker checks the caller's role with community_docs_role() first.
--
-- WHAT IS NEW
--   storage bucket community-docs              private, 10 MB, md / docx / pdf
--   community_documents                        one row per uploaded file
--   community_doc_sections                     one row per heading section (full text)
--   community_doc_answers                      audit: one row per answered question
--   community_docs_role(place, user)           manager | member | visitor | place_not_found
--   community_doc_replace_sections(doc, …)     swaps a document's sections in one go
--   community_doc_find(place, docs, query, n)  full-text lookup used by the answer loop

-- ── 1 · storage ─────────────────────────────────────────────────────────────

insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values
  ('community-docs', 'community-docs', false, 10485760,
   array[
     'text/markdown', 'text/x-markdown', 'text/plain',
     'application/pdf',
     'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
   ])
on conflict (id) do update set
  public = excluded.public,
  file_size_limit = excluded.file_size_limit,
  allowed_mime_types = excluded.allowed_mime_types;

-- ── 2 · documents ───────────────────────────────────────────────────────────

create table if not exists public.community_documents (
  id            uuid primary key default gen_random_uuid(),
  place_id      uuid not null references public.places(id) on delete cascade,
  title         text not null check (length(btrim(title)) between 1 and 200),
  source_mime   text not null,
  storage_path  text not null,
  uploaded_by   uuid references auth.users(id) on delete set null,
  -- uploaded → processing → ready | failed
  status        text not null default 'uploaded'
                check (status in ('uploaded', 'processing', 'ready', 'failed')),
  error         text,
  -- members: confirmed members + managers only. public: anyone who opens the community.
  visibility    text not null default 'members'
                check (visibility in ('members', 'public')),
  -- The converted Markdown, kept so the owner can preview what Lana reads.
  markdown      text,
  summary       text,
  token_count   integer not null default 0,
  version       integer not null default 1,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);

create index if not exists community_documents_place_idx
  on public.community_documents (place_id, created_at desc);

alter table public.community_documents enable row level security;
revoke all on public.community_documents from anon, authenticated;

-- ── 3 · sections ────────────────────────────────────────────────────────────

-- The generated tsv column needs an immutable expression; array_to_string is only stable.
-- 'simple' config: no stemming, no stop words, works the same in every language.
create or replace function public._community_doc_section_tsv(p_heading text[], p_body text)
returns tsvector
language sql
immutable
parallel safe
set search_path = pg_catalog
as $$
  select to_tsvector('simple'::regconfig,
           coalesce(array_to_string(p_heading, ' '), '') || ' ' || coalesce(p_body, ''));
$$;

create table if not exists public.community_doc_sections (
  id            uuid primary key default gen_random_uuid(),
  doc_id        uuid not null references public.community_documents(id) on delete cascade,
  place_id      uuid not null references public.places(id) on delete cascade,
  ord           integer not null,
  heading_path  text[] not null default '{}',
  body          text not null,
  covers        text,
  token_count   integer not null default 0,
  tsv           tsvector generated always as (
                  public._community_doc_section_tsv(heading_path, body)
                ) stored,
  unique (doc_id, ord)
);

create index if not exists community_doc_sections_place_idx
  on public.community_doc_sections (place_id);
create index if not exists community_doc_sections_tsv_idx
  on public.community_doc_sections using gin (tsv);

alter table public.community_doc_sections enable row level security;
revoke all on public.community_doc_sections from anon, authenticated;

-- ── 4 · answer audit ────────────────────────────────────────────────────────

create table if not exists public.community_doc_answers (
  id               uuid primary key default gen_random_uuid(),
  place_id         uuid not null references public.places(id) on delete cascade,
  user_id          uuid references auth.users(id) on delete set null,
  question         text not null,
  mode             text not null check (mode in ('full', 'navigate')),
  sections_read    uuid[] not null default '{}',
  coverage         text not null check (coverage in ('full', 'partial', 'none')),
  claims_total     integer not null default 0,
  claims_dropped   integer not null default 0,
  retried          boolean not null default false,
  latency_ms       integer,
  created_at       timestamptz not null default now()
);

-- The owners' gaps report reads partial / none rows per community.
create index if not exists community_doc_answers_gaps_idx
  on public.community_doc_answers (place_id, created_at desc)
  where coverage <> 'full';

alter table public.community_doc_answers enable row level security;
revoke all on public.community_doc_answers from anon, authenticated;

-- ── 5 · who is asking ───────────────────────────────────────────────────────

-- manager  = may upload / delete / change visibility (owner or co-owner, or the starter
--            of a still-community_started place — same people _community_manage_status lets in)
-- member   = confirmed member: reads members + public docs
-- visitor  = anyone else: reads public docs only
create or replace function public.community_docs_role(p_place_id uuid, p_user_id uuid)
returns text
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select case
    when not exists (select 1 from public.places where id = p_place_id) then 'place_not_found'
    when p_user_id is null then 'visitor'
    when public._community_manage_status(p_user_id, p_place_id) is null then 'manager'
    when public._is_confirmed_member(p_place_id, p_user_id) then 'member'
    else 'visitor'
  end;
$$;

revoke all on function public.community_docs_role(uuid, uuid) from public, anon, authenticated;
grant execute on function public.community_docs_role(uuid, uuid) to service_role;

-- ── 6 · swap a document's sections ──────────────────────────────────────────

-- p_sections: [{ "ord": 0, "heading_path": ["…"], "body": "…", "covers": "…",
--                "token_count": 123 }, …]
-- Old sections go and new ones arrive in the same transaction, so a question asked
-- mid-reingest never sees half a document.
create or replace function public.community_doc_replace_sections(
  p_doc_id      uuid,
  p_sections    jsonb,
  p_markdown    text,
  p_summary     text,
  p_token_count integer
)
returns integer
language plpgsql
volatile
security definer
set search_path = pg_catalog, public
as $$
declare
  v_place uuid;
  v_n     integer;
begin
  select place_id into v_place from public.community_documents where id = p_doc_id for update;
  if v_place is null then
    raise exception 'doc_not_found';
  end if;

  delete from public.community_doc_sections where doc_id = p_doc_id;

  insert into public.community_doc_sections
    (doc_id, place_id, ord, heading_path, body, covers, token_count)
  select p_doc_id,
         v_place,
         (s->>'ord')::integer,
         coalesce(array(select jsonb_array_elements_text(s->'heading_path')), '{}'),
         s->>'body',
         nullif(btrim(coalesce(s->>'covers', '')), ''),
         coalesce((s->>'token_count')::integer, 0)
    from jsonb_array_elements(coalesce(p_sections, '[]'::jsonb)) s
   where btrim(coalesce(s->>'body', '')) <> '';
  get diagnostics v_n = row_count;

  update public.community_documents
     set markdown    = p_markdown,
         summary     = nullif(btrim(coalesce(p_summary, '')), ''),
         token_count = coalesce(p_token_count, 0),
         status      = 'ready',
         error       = null,
         updated_at  = now()
   where id = p_doc_id;

  return v_n;
end;
$$;

revoke all on function public.community_doc_replace_sections(uuid, jsonb, text, text, integer)
  from public, anon, authenticated;
grant execute on function public.community_doc_replace_sections(uuid, jsonb, text, text, integer)
  to service_role;

-- ── 7 · full-text lookup for the answer loop ────────────────────────────────

-- Only searches the documents the worker already filtered for the asker (p_doc_ids).
create or replace function public.community_doc_find(
  p_place_id uuid,
  p_doc_ids  uuid[],
  p_query    text,
  p_limit    integer default 8
)
returns table (section_id uuid, doc_id uuid, heading_path text[], rank real)
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select s.id, s.doc_id, s.heading_path,
         ts_rank(s.tsv, websearch_to_tsquery('simple'::regconfig, p_query)) as rank
    from public.community_doc_sections s
   where s.place_id = p_place_id
     and s.doc_id = any(p_doc_ids)
     and btrim(coalesce(p_query, '')) <> ''
     and s.tsv @@ websearch_to_tsquery('simple'::regconfig, p_query)
   order by rank desc, s.doc_id, s.ord
   limit least(greatest(coalesce(p_limit, 8), 1), 20);
$$;

revoke all on function public.community_doc_find(uuid, uuid[], text, integer)
  from public, anon, authenticated;
grant execute on function public.community_doc_find(uuid, uuid[], text, integer) to service_role;
