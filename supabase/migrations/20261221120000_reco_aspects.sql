-- A statement, broken into its sections, each quantified in words.
--
-- THE MODEL (2026-09-24 standup, verbatim where it matters)
--
--   "A rating today is given to a STATEMENT. I could say 'this restaurant sucks' but give
--    it five stars because it's from my friend. So it's contradictory to what the
--    statement is."
--
--   So: break the statement into its parts — the atmosphere, the porcelain, the service,
--   the owner — and quantify EACH ONE, "not through the old style of star system but
--   through words". The user answers in their own language ("the owner really sucked",
--   "the porcelain was amazing and unique") and we "allocate on each section a quantified
--   second axis based on what was said".
--
--   The point of the granularity is the OTHER side of the loop:
--     "I'm looking for a restaurant where the owner actually speaks Italian and the
--      porcelain is unique."
--   Create and Find share one vocabulary, or the granularity buys nothing.
--
-- ============================================================================
-- WHAT IS ALREADY BUILT (do not rebuild)
-- ============================================================================
--   reco_subjects · local_signals.subject_ref/subject_confidence/subject_method ·
--   reco_subject_merges · reco_adjudications · reco_subject_digests.themes
--
--   themes already aggregates across people, and already carries the count the UI shows
--   as "4 out of 8":
--       {"n": 2, "total": 2, "label": "delivery available",
--        "quote": "Delivery: On time", "signal_ids": [...]}
--
-- ============================================================================
-- THE TWO THINGS THAT ARE MISSING, AND WHY THIS TABLE EXISTS
-- ============================================================================
-- 1. THE SECTIONS COME FROM A TEMPLATE, NOT FROM THE STATEMENT.
--    reco_fields today is a category-driven set — a furniture store gets used_for,
--    delivery, assembly, quality. Useful, but it is not what was said. The model is
--    explicit: "if the user mentioned six sections within the statement, we need
--    definitely six sections in the pipeline to be asked." One question per thing THEY
--    raised. Nobody mentions "assembly" and gets asked about the porcelain.
--
-- 2. THE VERBAL QUANTIFICATION IS THROWN AWAY AT AGGREGATION.
--    Look at the live digest: two people answered "Quality: Excellent" and
--    "Quality: Decent", and BOTH became the theme "good quality". The second axis —
--    the whole point — is discarded at exactly the step where it would have mattered.
--
--    So sentiment is stored per aspect, per person, next to the words they used.
--
-- ============================================================================
-- THE RULE THAT KEEPS THIS FROM BECOMING STARS
-- ============================================================================
--   answer_verbatim IS WHAT IS DISPLAYED. sentiment IS NEVER DISPLAYED.
--
--   The words are the product: "the porcelain was amazing and unique" is the thing
--   another person wants to read. The band exists to rank, filter and match — to answer
--   "find me somewhere the owner is good" — and the moment it is rendered as a number or
--   a row of icons we have rebuilt the thing this replaces.

create table if not exists public.reco_aspect (
  id             uuid primary key default gen_random_uuid(),
  signal_id      uuid not null references public.local_signals(id) on delete cascade,
  subject_ref    uuid references public.reco_subjects(id) on delete set null,
  author_id      uuid not null references public.users(id) on delete cascade,

  -- Canonical key, resolved by embedding against aspects already seen for this subject
  -- kind. Without it "the front desk" / "reception" / "the desk staff" are three aspects
  -- with n=1 each and nothing ever accumulates.
  aspect_key     text not null,
  -- As the user actually framed it, kept verbatim. This is what Lana echoes back:
  -- "you mentioned the owner — how was it?"
  aspect_label   text not null,
  -- The fragment of their statement this was lifted from. Provenance: an aspect nobody
  -- can trace to a sentence is an aspect we invented.
  source_span    text,

  -- Their answer, in their words. THIS is what gets displayed.
  answer_verbatim text,
  answer_source  text not null default 'voice'
                   check (answer_source in ('voice','tap','text','skipped')),

  -- The second axis. -2..+2, internal only. Null when skipped or unreadable — a null
  -- band is honest; a zero would claim we read neutrality where we read nothing.
  sentiment      smallint check (sentiment between -2 and 2),
  sentiment_confidence real,

  embedding      extensions.vector(768),
  created_at     timestamptz not null default now(),

  -- One row per aspect per statement. Re-answering updates.
  unique (signal_id, aspect_key),

  -- A skipped aspect carries no band, and a band needs words behind it.
  constraint reco_aspect_skipped_has_no_sentiment check (
    answer_source <> 'skipped' or sentiment is null),
  constraint reco_aspect_sentiment_has_answer check (
    sentiment is null or answer_verbatim is not null)
);

create index if not exists reco_aspect_subject_idx
  on public.reco_aspect(subject_ref) where subject_ref is not null;
create index if not exists reco_aspect_signal_idx
  on public.reco_aspect(signal_id);
create index if not exists reco_aspect_key_idx
  on public.reco_aspect(aspect_key);

-- Find-side: "somewhere the owner speaks Italian and the porcelain is unique" is an
-- aspect-level query, so aspects have to be searchable as text, not just as labels.
create index if not exists reco_aspect_embedding_hnsw
  on public.reco_aspect using hnsw (embedding extensions.vector_cosine_ops)
  where embedding is not null;

comment on table public.reco_aspect is
  'One row per section of a recommendation statement, per person. The section is '
  'EXTRACTED from what they said (never a category template), and quantified in their own '
  'words. answer_verbatim is displayed; sentiment never is.';

comment on column public.reco_aspect.sentiment is
  'Internal band, -2..+2, derived from answer_verbatim. NEVER RENDERED — not as a number, '
  'not as icons, not as a bar. It exists to rank, filter and match. Rendering it rebuilds '
  'the star system this replaces. Null means skipped or unreadable, never neutral.';

comment on column public.reco_aspect.answer_source is
  '''skipped'' is a first-class answer and is stored. That someone raised the front desk '
  'and then declined to grade it is information — it tells us the aspect is salient here '
  'even when this person would not say more.';

-- ---------------------------------------------------------------------------
-- Read model: aspects of a subject, across everyone, for one viewer.
--
-- Returns the distribution, never an average. "9 of 12 said the wait was bad" survives
-- scrutiny; "2.4/5 on wait" does not, and cannot be argued with.
-- ---------------------------------------------------------------------------
create or replace function public.subject_aspects(
  p_subject_ref uuid,
  p_viewer_id   uuid,
  p_min_n       int default 1
)
returns table (
  aspect_key     text,
  aspect_label   text,
  n_people       int,
  n_positive     int,
  n_mixed        int,
  n_negative     int,
  n_skipped      int,
  n_shared_community int,
  sample_quotes  text[],
  last_said_at   timestamptz
)
language sql
stable
security definer
set search_path = pg_catalog, public, extensions
as $$
  with live as (
    select a.aspect_key, a.aspect_label, a.author_id, a.answer_verbatim,
           a.sentiment, a.answer_source, a.created_at
    from public.reco_aspect a
    where a.subject_ref = p_subject_ref
  ),
  -- Same shape as everywhere else: who the viewer shares a place with. A neighbour's
  -- word and a stranger's word are not the same object.
  shared as (
    select distinct l.aspect_key, l.author_id
    from live l
    join public.visible_place_members(p_viewer_id) vm on vm.user_id = l.author_id
    join (
      select distinct vm2.place_ref
      from public.visible_place_members(p_viewer_id) vm2
      where vm2.user_id = p_viewer_id
    ) mine on mine.place_ref = vm.place_ref
  )
  select
    l.aspect_key,
    max(l.aspect_label)                                                as aspect_label,
    count(distinct l.author_id)::int                                   as n_people,
    count(*) filter (where l.sentiment >= 1)::int                      as n_positive,
    count(*) filter (where l.sentiment = 0)::int                       as n_mixed,
    count(*) filter (where l.sentiment <= -1)::int                     as n_negative,
    count(*) filter (where l.answer_source = 'skipped')::int           as n_skipped,
    (select count(distinct s.author_id)::int
       from shared s where s.aspect_key = l.aspect_key)                as n_shared_community,
    (array_agg(l.answer_verbatim order by length(l.answer_verbatim) desc)
       filter (where l.answer_verbatim is not null))[1:3]              as sample_quotes,
    max(l.created_at)                                                  as last_said_at
  from live l
  group by l.aspect_key
  having count(distinct l.author_id) >= p_min_n
  order by count(distinct l.author_id) desc, max(l.created_at) desc;
$$;

revoke all on function public.subject_aspects(uuid, uuid, int) from public, anon;
grant execute on function public.subject_aspects(uuid, uuid, int) to authenticated, service_role;

comment on function public.subject_aspects(uuid, uuid, int) is
  'Aspects of a subject across everyone who spoke about it, for one viewer. Returns a '
  'DISTRIBUTION and quotes — never a mean. n_shared_community is what makes "4 of 8, and '
  '2 of them from your church" possible, which is the number the UI shows as "4 out of 8".';

-- ============================================================================
-- ROLLBACK
--   drop function if exists public.subject_aspects(uuid, uuid, int);
--   drop table if exists public.reco_aspect;
--   Additive: reco_fields, reco_subject_digests.themes and every existing read path are
--   untouched and keep working. Aspects run alongside until the UI moves over.
-- ============================================================================
