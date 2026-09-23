-- Recommendation SUBJECTS: the thing recommended, apart from the recommending.
--
-- Today three neighbours recommending Dr. Sarah produce three rows, and an ask returns
-- PEOPLE. A reader has to notice for herself that rows 1, 3 and 5 are the same dentist.
-- This is the spine of returning SUBJECTS instead — one card per recommended thing,
-- carrying how many neighbours stand behind it (docs/LANA_RECO_SUBJECT_MERGE.md).
--
-- NOTHING READS subject_ref YET. This migration only lets the capture path record which
-- subject a tip is about; the subject-grouped read path is a later stage. Landing it now
-- means the column is populated and backfilled before any surface depends on it.
--
-- WHAT MERGES, AND WHAT MUST NOT. The dividing line is not "can we find it on a map" —
-- that is only the easiest mechanism. It is whether a type's reco_fields are OBSERVATIONS
-- ABOUT a shared referent or the ARTIFACT ITSELF:
--
--   professional  gentle · walk-in · takes insurance    three witnesses to one dentist
--   recipe        ingredients · steps · 45 min · easy   this IS the recipe
--
-- Two neighbours recommending Dr. Sarah accumulate evidence about one person. Two
-- neighbours recommending banana bread have two DIFFERENT recipes, and merging them would
-- mean discarding one author's ingredients and then claiming both vouched for the one we
-- kept. So recipe, diy and other never ground here — they are not in the type list below,
-- and they keep one card per author permanently. reco_question_sets.py already draws this
-- line for the Places picker ("a recipe or a DIY trick is never a place however
-- confidently the model says so"); this is the same line, for the same reason.
--
-- WHY NOT CLUSTER local_signals.embedding. tip_embedding_text() covers name + category +
-- place + description + fields, so the vector measures what a tip is ABOUT, not WHO it is
-- about. "Dr. Sarah · pediatric dentist · Lake Nona · gentle with toddlers" and
-- "Dr. Ahmed · pediatric dentist · Lake Nona · so gentle with little ones" are near
-- identical vectors and different dentists. Every signal used here is drawn from the
-- subject's IDENTITY (google_place_id, name, category, locality) and never from the
-- recommendation's content.

-- ----------------------------------------------------------------------------
-- 1. The subject.
-- ----------------------------------------------------------------------------
create table if not exists public.reco_subjects (
  id              uuid primary key default gen_random_uuid(),
  -- Normalized identity string. NOT unique on its own: two "Cosori gooseneck" rows in
  -- different towns are different subjects, and the locality is what separates them.
  -- Stage 2 (products, storefront-less providers) is what builds on this; Stage 1 grounds
  -- only through google_place_id below.
  subject_key     text not null,
  -- The merge key when the subject is a real place. Unique, so attaching a second tip is
  -- a LOOKUP, not a search: same id = same subject, no threshold, no inference, no way to
  -- be wrong. Null for subjects Stage 2 will resolve some other way.
  google_place_id text unique,
  -- The author's casing, from the first tip that created the row — what a card titles
  -- itself with. subject_key stays the lowercased grouping key and is never display copy.
  display_name    text not null,
  category        text,
  locality        text,
  -- The SUBJECT's coordinates, which is not the same question as where the recommender
  -- lives. Free with Google grounding; a merged card's distance needs them.
  lat             double precision,
  lng             double precision,
  created_at      timestamptz not null default now()
);

comment on table public.reco_subjects is
  'One recommended THING, independent of who recommended it. Tips about the same subject '
  'share a row, which is what lets an ask return one card reading "3 vouched" instead of '
  'three rows about one dentist. Only types whose reco_fields are OBSERVATIONS about a '
  'shared referent ground here — never recipe/diy/other, whose fields ARE the artifact.';
comment on column public.reco_subjects.google_place_id is
  'Unique: attaching a tip to an existing subject is a lookup, not a fuzzy match.';
comment on column public.reco_subjects.display_name is
  'Author casing for the card title. subject_key is the grouping key and is not display copy.';
comment on column public.reco_subjects.lat is
  'The SUBJECT''s position, not the recommender''s — a merged card''s distance is to the place.';

create index if not exists reco_subjects_key_idx on public.reco_subjects (subject_key);

alter table public.reco_subjects enable row level security;
-- No policies, same as peer_rec_lines: every read is through a security-definer function,
-- so nobody needs direct table access to render a card.
revoke all on public.reco_subjects from anon, authenticated;


-- ----------------------------------------------------------------------------
-- 2. Which subject a tip is about.
-- ----------------------------------------------------------------------------
alter table public.local_signals
  add column if not exists subject_ref uuid references public.reco_subjects (id);

comment on column public.local_signals.subject_ref is
  'The reco_subjects row this tip recommends. NULL = ungrounded (typed rather than tapped, '
  'below the search floor, or a type that never merges) — such a tip renders exactly as it '
  'does today, as its own card. Distinct from circle_place_ref, which is the community the '
  'tip was shared INTO, not the thing it is about.';

-- The Stage 3 read: every tip for a set of subjects, live rows only.
create index if not exists local_signals_subject_ref_idx
  on public.local_signals (subject_ref)
  where subject_ref is not null and intent = 'tip_share' and status = 'listening';


-- ----------------------------------------------------------------------------
-- 3. Merge decisions, as an audit trail.
--
-- Not optional. Entity resolution of this kind produces bad merges, and an unmerge must
-- not mean hand-rewriting signal rows. Recording HOW each decision was reached is what
-- makes a bad batch reversible by method rather than one row at a time.
-- ----------------------------------------------------------------------------
create table if not exists public.reco_subject_merges (
  from_subject uuid not null references public.reco_subjects (id) on delete cascade,
  into_subject uuid not null references public.reco_subjects (id) on delete cascade,
  -- 'google'      exact google_place_id match (Stage 1 — no judgement involved)
  -- 'blocked'     normalized name + category + locality (Stage 2)
  -- 'adjudicated' an LLM call on a blocked shortlist (Stage 2)
  -- 'confirmed'   the sharer was asked and said yes (Stage 2)
  method       text not null check (method in ('google', 'blocked', 'adjudicated', 'confirmed')),
  confidence   real,
  decided_at   timestamptz not null default now(),
  primary key (from_subject, into_subject),
  constraint reco_subject_merges_not_self check (from_subject <> into_subject)
);

comment on table public.reco_subject_merges is
  'Audit trail for subject merges, so a bad batch can be reversed by METHOD rather than '
  'row by row. Stage 1 writes nothing here — a google_place_id match involves no judgement '
  'to audit; it exists for the inferred merges Stage 2 adds.';

alter table public.reco_subject_merges enable row level security;
revoke all on public.reco_subject_merges from anon, authenticated;


-- ----------------------------------------------------------------------------
-- 4. Ground one tip: upsert the subject, stamp the signal, return the id.
--
-- One function rather than an upsert plus a separate stamp: the two must not be able to
-- come apart, and a caller that has to remember the second call will eventually forget
-- (same reasoning as the mark_answered drain in save_local_signal).
--
-- security definer because reco_subjects has no policies, but the signal ownership check
-- is the real gate: the caller may only ever stamp her OWN tip.
-- ----------------------------------------------------------------------------
create or replace function public.set_signal_subject(
  p_signal_id       uuid,
  p_subject_key     text,
  p_display_name    text,
  p_google_place_id text default null,
  p_category        text default null,
  p_locality        text default null,
  p_lat             double precision default null,
  p_lng             double precision default null
)
returns uuid
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_me      uuid := auth.uid();
  v_key     text := nullif(btrim(lower(p_subject_key)), '');
  v_name    text := nullif(btrim(p_display_name), '');
  v_place   text := nullif(btrim(p_google_place_id), '');
  v_subject uuid;
begin
  if v_me is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;
  if v_key is null or v_name is null then
    raise exception 'subject_required' using errcode = 'P0001';
  end if;

  -- Own tip_share rows only. An id that is not yours must not even be confirmable as
  -- existing, so this is one check with no separate "does it exist" branch.
  if not exists (
    select 1 from public.local_signals s
    where s.id = p_signal_id and s.user_id = v_me and s.intent = 'tip_share'
  ) then
    raise exception 'tip_not_found' using errcode = 'P0001';
  end if;

  if v_place is not null then
    -- The whole point of the unique key: the second neighbour to recommend this place
    -- ATTACHES rather than creating a near-duplicate. do update (not do nothing) so the
    -- returning clause fires on the conflict path too; the stored values are deliberately
    -- left alone, because the first author's casing is as good as the second's and
    -- churning display_name would make a merged card's title flicker per new tip.
    insert into public.reco_subjects
      (subject_key, google_place_id, display_name, category, locality, lat, lng)
    values
      (v_key, v_place, v_name, nullif(btrim(p_category), ''), nullif(btrim(p_locality), ''),
       p_lat, p_lng)
    on conflict (google_place_id) do update
      -- Backfill only what the creating row did not know. coalesce keeps the existing
      -- value whenever there is one, so this never overwrites, only completes.
      set lat = coalesce(public.reco_subjects.lat, excluded.lat),
          lng = coalesce(public.reco_subjects.lng, excluded.lng),
          category = coalesce(public.reco_subjects.category, excluded.category),
          locality = coalesce(public.reco_subjects.locality, excluded.locality)
    returning id into v_subject;
  else
    -- Ungrounded: Stage 2's identity space does not exist yet, so a subject with no place
    -- id gets its own row and merges with nothing. Deliberately not keyed on subject_key
    -- alone — that would merge two towns' "Cosori gooseneck" on a bare string match, which
    -- is precisely the guess this design refuses to make.
    insert into public.reco_subjects
      (subject_key, display_name, category, locality, lat, lng)
    values
      (v_key, v_name, nullif(btrim(p_category), ''), nullif(btrim(p_locality), ''), p_lat, p_lng)
    returning id into v_subject;
  end if;

  update public.local_signals
     set subject_ref = v_subject,
         updated_at = now()
   where id = p_signal_id and user_id = v_me;

  return v_subject;
end;
$$;

comment on function public.set_signal_subject(uuid, text, text, text, text, text, double precision, double precision) is
  'Ground one tip to its subject: upsert reco_subjects (on google_place_id where there is '
  'one) and stamp local_signals.subject_ref. Own tip_share rows only. Best-effort at the '
  'call site — a tip that posted must never fail on its subject.';

revoke all on function public.set_signal_subject(uuid, text, text, text, text, text, double precision, double precision)
  from public, anon;
grant execute on function public.set_signal_subject(uuid, text, text, text, text, text, double precision, double precision)
  to authenticated, service_role;
