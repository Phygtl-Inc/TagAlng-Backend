-- ============================================================================
-- §50(b) event_fit_lines — the cache behind a meet's "why Lana sees a fit" sentence.
--
-- app/event_fit_line.py authors ONE line (+ up to three chips) per (viewer, meet) from
-- the proven overlap public.event_viewer_fit returns (20270110120000) — never from the
-- meet's own tags — the same shape app/peer_rec_line.py authors for a neighbour and
-- app/community_fit_line.py for a community. `peer_rec_lines.peer_user_id` is a users FK
-- and `community_fit_lines.place_ref` a places FK, so a meet needs its own table, keyed
-- the same way: a reload costs no LLM call, a NEW overlap authors a NEW line, and a
-- Spanish reader is never served the English one.
--
-- `id` is returned as `rec_id`, so a future 👍/👎 on the meet card has a stored row to
-- snapshot (app/feedback.py snapshots rated text from the DB, never from the client).
--
-- ROLLBACK: drop table if exists public.event_fit_lines;
-- ============================================================================

create table if not exists public.event_fit_lines (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.users(id) on delete cascade,
  event_id uuid not null references public.events(id) on delete cascade,
  -- The language the line was AUTHORED in. Part of the key.
  lang text not null default 'en',
  -- Fingerprint of the overlap behind the line (app/peer_rec_line.py::_basis_sig).
  basis_sig text not null,
  line text not null,
  -- Short authored facets. [] = the model found no honest facet.
  chips jsonb,
  created_at timestamptz not null default now(),
  constraint event_fit_lines_uq unique (user_id, event_id, lang, basis_sig)
);

comment on table public.event_fit_lines is
  'AI-authored "why Lana sees a fit" line + chips for one meet, per viewer + meet + overlap '
  'basis + language, composed only over the viewer/meet proven intersection. Worker-only.';

alter table public.event_fit_lines enable row level security;
-- No policies, and no client grants: the worker (service_role) is the only reader/writer,
-- same as peer_rec_lines / community_fit_lines.
revoke all on table public.event_fit_lines from public, anon, authenticated;
grant select, insert, update, delete on table public.event_fit_lines to service_role;
