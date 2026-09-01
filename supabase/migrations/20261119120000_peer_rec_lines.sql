-- The AI-authored "why this neighbour" line under a fellows row, plus a thumb on it.
--
-- /lana/fellows used to ship the templated pair ("You both: A · B") and trait_tags, and
-- the card rendered the tags as chips. The line replaces the chips with one sentence in
-- Lana's voice, authored per (viewer, neighbour, shared-claim basis, language).
--
-- Why a table and not a per-request compose: the thumb needs text to snapshot, and the
-- rule everywhere else in lana_feedback is that the rated text is read from the DB, never
-- trusted from the client. Caching also keeps a reload off the LLM — the basis_sig is the
-- shared claims the line was written from, so a NEW overlap authors a new line instead of
-- serving a stale one.
create table if not exists public.peer_rec_lines (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.users(id) on delete cascade,
  peer_user_id uuid not null references public.users(id) on delete cascade,
  -- The language the line was AUTHORED in (the viewer's users.locale at the time).
  -- Part of the key: a Spanish reader must not be served the English line.
  lang text not null default 'en',
  -- Fingerprint of the shared claims behind the line (app/peer_rec_line.py::_basis_sig).
  basis_sig text not null,
  line text not null,
  created_at timestamptz not null default now(),
  constraint peer_rec_lines_uq unique (user_id, peer_user_id, lang, basis_sig)
);

-- The read on every fellows fetch: this viewer's lines for a dozen peer ids at once.
create index if not exists peer_rec_lines_user_idx
  on public.peer_rec_lines (user_id, lang);

comment on table public.peer_rec_lines is
  'AI-authored one-line reason a neighbour is a fit, per viewer + neighbour + shared-claim basis + language. Cached so a reload costs no LLM call and so a thumb has DB text to snapshot.';

alter table public.peer_rec_lines enable row level security;
-- No policies: the worker (service_role) is the only reader/writer, same as lana_feedback.

-- A third rateable thing: one of those lines. Same toggle semantics as the other two
-- (up/down flips, clear deletes), so only the target column and the check are new.
alter table public.lana_feedback
  add column if not exists rec_id uuid references public.peer_rec_lines(id) on delete set null;

alter table public.lana_feedback
  drop constraint if exists lana_feedback_target_kind_check;
alter table public.lana_feedback
  add constraint lana_feedback_target_kind_check
  check (target_kind in ('message', 'rapport_question', 'peer_rec'));

create unique index if not exists lana_feedback_user_rec_uq
  on public.lana_feedback (user_id, rec_id) where rec_id is not null;
