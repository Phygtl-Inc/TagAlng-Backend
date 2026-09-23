-- Recommendation subjects, second identity space: the things that merge but are not places.
--
-- 20261219120000 grounds a subject through google_place_id, which is exact and cannot be
-- wrong — but only reaches subjects that ARE map points. Two populations merge just as
-- honestly and have no place id at all:
--
--   product                          "Cosori gooseneck" is a SKU. Two neighbours naming it
--                                    mean one object. Grounding it through Places would
--                                    attach it to whatever shop sells it.
--   professional / service, no shop  a plumber, a nanny, a tutor who comes to you,
--                                    "Chef Ana meal prep" — reco_question_sets' own words:
--                                    "Dr Sarah is not her clinic, and a plumber has no
--                                    address at all".
--
-- recipe / diy / other stay out permanently: their captured fields ARE the artifact, so
-- two banana-bread recommendations are two different recipes (see 20261219120000's header).
--
-- Until now the no-place-id branch of set_signal_subject inserted a fresh row every time,
-- so those two populations never merged with anything. This adds the missing half:
-- candidates, and a way to attach a signal to a subject somebody else already created.
--
-- WHAT IS AND IS NOT INFERRED. A place id is a fact. Everything here is a JUDGEMENT, so
-- every attachment records HOW it was reached and how sure we were:
--
--   google       exact google_place_id match                      (20261219120000)
--   new          created its own subject, merged with nothing
--   blocked      normalized name + locality, scored past the auto floor
--   adjudicated  a model call on a shortlist said yes
--   confirmed    the sharer was asked and said yes
--   ambiguous    a candidate was found and NOT merged — left standing for a later pass
--
-- 'ambiguous' is a real outcome and not a failure: a wrong merge invents corroboration,
-- and the count a stranger reads is the one number that must not be inflated. Refusing to
-- guess keeps two rows where there might be one, which is the cheap mistake of the two.

-- ── 1. How each attachment was reached ────────────────────────────────────────────────
alter table public.local_signals
  add column if not exists subject_method text
    check (subject_method is null or subject_method in (
      'google', 'new', 'blocked', 'adjudicated', 'confirmed', 'ambiguous'
    )),
  add column if not exists subject_confidence real,
  -- The subject this row ALMOST matched. Kept so an ambiguous case can be resolved later
  -- (by a confirm-ask, or a human) without re-running the search and the model call that
  -- produced it.
  add column if not exists subject_candidate_ref uuid references public.reco_subjects (id);

comment on column public.local_signals.subject_method is
  'How subject_ref was decided. google = exact place id (a fact); blocked/adjudicated = '
  'inferred (a judgement); confirmed = the sharer said so; new = merged with nothing; '
  'ambiguous = a candidate existed and was deliberately NOT merged.';
comment on column public.local_signals.subject_confidence is
  'Score behind an inferred attachment, so a bad batch is reversible by METHOD and '
  'THRESHOLD together rather than one row at a time.';
comment on column public.local_signals.subject_candidate_ref is
  'The near-miss, kept so an ambiguous row can be settled later without re-running the '
  'search and the model call.';

-- The reversal query: "everything attached by inference below x". Partial, because the
-- exact-match rows are never what a repair pass is looking for.
create index if not exists local_signals_subject_method_idx
  on public.local_signals (subject_method, subject_confidence)
  where subject_method in ('blocked', 'adjudicated', 'ambiguous');


-- ── 2. Candidates ─────────────────────────────────────────────────────────────────────
-- Blocking, not matching: this returns the handful of rows worth LOOKING at, and Python
-- decides. Deliberately cheap and deliberately recall-biased — a candidate that turns out
-- to be wrong costs one comparison, while a candidate never returned can never be merged.
--
-- Matched on IDENTITY only: the normalized name, and the locality it sits in. Never the
-- description, and never the embedding — tip_embedding_text() covers the description, so
-- it clusters by topic and would happily hand back a different plumber who fixed a
-- similar leak (see 20261219120000's header).
--
-- Word overlap via && rather than trigram similarity: pg_trgm is not installed in this
-- database, and adding an extension to ship a blocker is a bigger commitment than the
-- blocker is worth while the corpus is this small.
-- ponytail: linear over the ungrounded rows in one locality. Fine at pilot size; when it
-- bites, the answer is pg_trgm + a GIN index on subject_key, not a bigger limit here.
create or replace function public.reco_subject_candidates(
  p_subject_key text,
  p_category    text default null,
  p_locality    text default null,
  p_limit       int default 10
)
returns table (
  id            uuid,
  subject_key   text,
  display_name  text,
  category      text,
  locality      text,
  signal_count  int
)
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  with needle as (
    select
      string_to_array(btrim(lower(coalesce(p_subject_key, ''))), ' ') as words,
      nullif(btrim(lower(coalesce(p_locality, ''))), '')              as loc
  )
  select
    s.id, s.subject_key, s.display_name, s.category, s.locality,
    -- How many neighbours already stand behind this subject. Not used to decide the
    -- match; it is what makes the shortlist worth ordering, since merging into the row
    -- that already has voices is the outcome that makes a card whole.
    (select count(*)::int from public.local_signals ls
      where ls.subject_ref = s.id and ls.intent = 'tip_share' and ls.status = 'listening')
  from public.reco_subjects s, needle n
  where auth.uid() is not null
    -- Only the second identity space. A subject with a place id is settled by exact match
    -- and must never be re-decided by a name comparison.
    and s.google_place_id is null
    and array_length(n.words, 1) is not null
    and string_to_array(s.subject_key, ' ') && n.words
    -- Same town, or one of the two never said. A shared name across localities is exactly
    -- the coincidence this is meant NOT to merge.
    and (n.loc is null or s.locality is null or lower(btrim(s.locality)) = n.loc)
  order by
    (select count(*) from public.local_signals ls
      where ls.subject_ref = s.id and ls.intent = 'tip_share' and ls.status = 'listening') desc,
    s.created_at asc
  limit greatest(1, least(coalesce(p_limit, 10), 25));
$$;

comment on function public.reco_subject_candidates(text, text, text, int) is
  'Blocking step for the non-place identity space: ungrounded subjects sharing a word and '
  'a locality with the incoming one. Recall-biased shortlist — the caller decides. Never '
  'returns subjects that already have a google_place_id: those are settled by exact match.';

revoke all on function public.reco_subject_candidates(text, text, text, int) from public, anon;
grant execute on function public.reco_subject_candidates(text, text, text, int)
  to authenticated, service_role;


-- ── 3. Attach a signal to a subject somebody else created ─────────────────────────────
-- Separate from set_signal_subject because the two do genuinely different things: that one
-- CREATES (or finds by place id) and this one JOINS an existing row. Folding them together
-- would mean one function whose behaviour depends on which arguments are null, which is
-- how set_signal_reco grew the overload problem 20261120120000 had to clean up.
create or replace function public.attach_signal_subject(
  p_signal_id  uuid,
  p_subject_id uuid,
  p_method     text,
  p_confidence real default null
)
returns boolean
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_me uuid := auth.uid();
begin
  if v_me is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;
  if p_method not in ('blocked', 'adjudicated', 'confirmed') then
    -- 'google'/'new' belong to set_signal_subject, and 'ambiguous' means NOT attached —
    -- letting it through here would record a merge that was deliberately refused.
    raise exception 'invalid_attach_method' using errcode = 'P0001';
  end if;
  if not exists (
    select 1 from public.local_signals s
    where s.id = p_signal_id and s.user_id = v_me and s.intent = 'tip_share'
  ) then
    raise exception 'tip_not_found' using errcode = 'P0001';
  end if;
  if not exists (select 1 from public.reco_subjects rs where rs.id = p_subject_id) then
    raise exception 'subject_not_found' using errcode = 'P0001';
  end if;

  update public.local_signals
     set subject_ref = p_subject_id,
         subject_method = p_method,
         subject_confidence = p_confidence,
         subject_candidate_ref = null,
         updated_at = now()
   where id = p_signal_id and user_id = v_me;
  return true;
end;
$$;

revoke all on function public.attach_signal_subject(uuid, uuid, text, real) from public, anon;
grant execute on function public.attach_signal_subject(uuid, uuid, text, real)
  to authenticated, service_role;


-- ── 4. Record a refusal ───────────────────────────────────────────────────────────────
-- The 'ambiguous' outcome: a candidate existed, we would not swear to it, so the row keeps
-- its own subject and remembers what it nearly matched.
create or replace function public.mark_signal_subject_ambiguous(
  p_signal_id   uuid,
  p_candidate   uuid,
  p_confidence  real default null
)
returns boolean
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_me uuid := auth.uid();
begin
  if v_me is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;
  update public.local_signals
     set subject_method = 'ambiguous',
         subject_confidence = p_confidence,
         subject_candidate_ref = p_candidate,
         updated_at = now()
   where id = p_signal_id and user_id = v_me and intent = 'tip_share';
  return found;
end;
$$;

revoke all on function public.mark_signal_subject_ambiguous(uuid, uuid, real) from public, anon;
grant execute on function public.mark_signal_subject_ambiguous(uuid, uuid, real)
  to authenticated, service_role;


-- ── 5. set_signal_subject carries its method ──────────────────────────────────────────
-- DROPPED and recreated rather than given defaulted extra parameters: two overloads let
-- PostgREST resolve to whichever matches the keys sent, silently dropping the new fields.
-- That exact bug is why 20261120120000 dropped set_signal_reco's 4-arg version.
drop function if exists public.set_signal_subject(
  uuid, text, text, text, text, text, double precision, double precision
);

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

  if not exists (
    select 1 from public.local_signals s
    where s.id = p_signal_id and s.user_id = v_me and s.intent = 'tip_share'
  ) then
    raise exception 'tip_not_found' using errcode = 'P0001';
  end if;

  if v_place is not null then
    insert into public.reco_subjects
      (subject_key, google_place_id, display_name, category, locality, lat, lng)
    values
      (v_key, v_place, v_name, nullif(btrim(p_category), ''), nullif(btrim(p_locality), ''),
       p_lat, p_lng)
    on conflict (google_place_id) do update
      set lat = coalesce(public.reco_subjects.lat, excluded.lat),
          lng = coalesce(public.reco_subjects.lng, excluded.lng),
          category = coalesce(public.reco_subjects.category, excluded.category),
          locality = coalesce(public.reco_subjects.locality, excluded.locality)
    returning id into v_subject;
  else
    insert into public.reco_subjects
      (subject_key, display_name, category, locality, lat, lng)
    values
      (v_key, v_name, nullif(btrim(p_category), ''), nullif(btrim(p_locality), ''), p_lat, p_lng)
    returning id into v_subject;
  end if;

  update public.local_signals
     set subject_ref = v_subject,
         -- An exact place id is a fact; a row that created its own subject merged with
         -- nothing. Neither is an inference, so neither carries a confidence.
         subject_method = case when v_place is not null then 'google' else 'new' end,
         subject_confidence = null,
         subject_candidate_ref = null,
         updated_at = now()
   where id = p_signal_id and user_id = v_me;

  return v_subject;
end;
$$;

comment on function public.set_signal_subject(uuid, text, text, text, text, text, double precision, double precision) is
  'Create (or find by google_place_id) the subject for one tip and stamp subject_ref. '
  'v2 also records subject_method: google for an exact place id, new for a subject that '
  'merged with nothing. Inferred attachments go through attach_signal_subject instead.';

revoke all on function public.set_signal_subject(uuid, text, text, text, text, text, double precision, double precision)
  from public, anon;
grant execute on function public.set_signal_subject(uuid, text, text, text, text, text, double precision, double precision)
  to authenticated, service_role;
