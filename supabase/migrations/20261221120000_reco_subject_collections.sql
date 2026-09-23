-- Recipes, DIY tricks and "other" join the subject model — as COLLECTIONS, not blends.
--
-- 20261219/20261220 kept recipe, diy and other out of merging entirely, on the grounds
-- that their captured fields ARE the artifact: two neighbours recommending banana bread
-- have two different recipes, and folding them together would discard one author's
-- ingredients while claiming both stood behind the survivor.
--
-- That reasoning was right about the FIELDS and wrong about the SUBJECT. Standup
-- 2026-09-22 (Tommaso) settled it with the Pareto framing: within one subject, most
-- recommendations say roughly the same thing and can be aggregated, while the distinctive
-- minority must be shown standalone — the App Store reviews shape. A recipe is simply
-- always on the minority side. So "banana bread" is a perfectly good SUBJECT that three
-- neighbours can point at; what must never happen is blending their steps into one
-- unreadable hybrid, or summarising "great doctor · huge parking lot" into a sentence
-- nobody wrote.
--
-- So merging is no longer all-or-nothing. Every subject now declares how its
-- contributions may be COMBINED:
--
--   aggregate   the contributions are OBSERVATIONS about one shared thing, so the read
--               path may summarise the majority and surface outliers separately.
--               professional · restaurant · location · service · product
--
--   collection  the contributions ARE the thing, one per author. They share a subject and
--               a count, and are rendered side by side, never merged into each other.
--               recipe · diy · other
--
-- The column exists so this cannot be forgotten at read time. A renderer that blends a
-- collection is reproducing exactly the bug this migration was written to avoid, and the
-- data now says so rather than a comment in a Python module hoping to be read.

alter table public.reco_subjects
  add column if not exists merge_mode text not null default 'aggregate'
    check (merge_mode in ('aggregate', 'collection'));

comment on column public.reco_subjects.merge_mode is
  'How contributions may be COMBINED. aggregate = observations about one shared thing, '
  'safe to summarise (the Pareto majority) with outliers surfaced separately. collection '
  '= each contribution IS the artifact (a recipe, a DIY method), shown standalone and '
  'NEVER blended — merging their fields would discard one author''s work and attribute '
  'the remainder to everyone.';

-- Every row that exists today was created by the place path, which only ever runs for the
-- aggregate types — so the default is already correct and no backfill is needed.

-- Collections are read as "all the recipes filed under this subject", which is a different
-- query shape from the aggregate card's "how many stand behind this".
create index if not exists reco_subjects_merge_mode_idx
  on public.reco_subjects (merge_mode)
  where merge_mode = 'collection';


-- ----------------------------------------------------------------------------
-- set_signal_subject v3 — carries the mode.
--
-- DROPPED and recreated rather than given a defaulted extra parameter, for the third time
-- and the same reason: two overloads let PostgREST resolve to whichever matches the keys
-- sent, silently dropping the new field (20261120120000's set_signal_reco bug).
--
-- The mode is settled by the CREATING author and never revised, because it is a property
-- of the type rather than of any one recommendation: a subject cannot be observations for
-- one neighbour and artifacts for the next.
-- ----------------------------------------------------------------------------
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
  p_lng             double precision default null,
  p_merge_mode      text default 'aggregate'
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
  v_mode    text := case when lower(coalesce(p_merge_mode, '')) = 'collection'
                         then 'collection' else 'aggregate' end;
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
      (subject_key, google_place_id, display_name, category, locality, lat, lng, merge_mode)
    values
      (v_key, v_place, v_name, nullif(btrim(p_category), ''), nullif(btrim(p_locality), ''),
       p_lat, p_lng, v_mode)
    on conflict (google_place_id) do update
      set lat = coalesce(public.reco_subjects.lat, excluded.lat),
          lng = coalesce(public.reco_subjects.lng, excluded.lng),
          category = coalesce(public.reco_subjects.category, excluded.category),
          locality = coalesce(public.reco_subjects.locality, excluded.locality)
          -- merge_mode deliberately NOT updated: see the header.
    returning id into v_subject;
  else
    insert into public.reco_subjects
      (subject_key, display_name, category, locality, lat, lng, merge_mode)
    values
      (v_key, v_name, nullif(btrim(p_category), ''), nullif(btrim(p_locality), ''),
       p_lat, p_lng, v_mode)
    returning id into v_subject;
  end if;

  update public.local_signals
     set subject_ref = v_subject,
         subject_method = case when v_place is not null then 'google' else 'new' end,
         subject_confidence = null,
         subject_candidate_ref = null,
         updated_at = now()
   where id = p_signal_id and user_id = v_me;

  return v_subject;
end;
$$;

comment on function public.set_signal_subject(uuid, text, text, text, text, text, double precision, double precision, text) is
  'Create (or find by google_place_id) the subject for one tip and stamp subject_ref. '
  'v3 carries merge_mode: aggregate (observations, summarisable) vs collection (artifacts, '
  'shown standalone). Inferred attachments go through attach_signal_subject instead.';

revoke all on function public.set_signal_subject(uuid, text, text, text, text, text, double precision, double precision, text)
  from public, anon;
grant execute on function public.set_signal_subject(uuid, text, text, text, text, text, double precision, double precision, text)
  to authenticated, service_role;


-- ----------------------------------------------------------------------------
-- Candidates carry the mode too, so a caller never attaches a recipe to a dentist.
-- Rebuilt: OUT columns change (42P13).
-- ----------------------------------------------------------------------------
drop function if exists public.reco_subject_candidates(text, text, text, int);

create or replace function public.reco_subject_candidates(
  p_subject_key text,
  p_category    text default null,
  p_locality    text default null,
  p_limit       int default 10,
  p_merge_mode  text default null
)
returns table (
  id            uuid,
  subject_key   text,
  display_name  text,
  category      text,
  locality      text,
  merge_mode    text,
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
      nullif(btrim(lower(coalesce(p_locality, ''))), '')              as loc,
      nullif(btrim(lower(coalesce(p_merge_mode, ''))), '')            as mode
  )
  select
    s.id, s.subject_key, s.display_name, s.category, s.locality, s.merge_mode,
    (select count(*)::int from public.local_signals ls
      where ls.subject_ref = s.id and ls.intent = 'tip_share' and ls.status = 'listening')
  from public.reco_subjects s, needle n
  where auth.uid() is not null
    and s.google_place_id is null
    and array_length(n.words, 1) is not null
    and string_to_array(s.subject_key, ' ') && n.words
    -- A recipe and a plumber can share a word and must never be candidates for each
    -- other: what may be COMBINED is part of what the subject IS.
    and (n.mode is null or s.merge_mode = n.mode)
    -- Same town, or one of the two never said. Deliberately still applied to collections:
    -- their locality is usually null (a recipe has no address), so this rarely bites, and
    -- where an author DID name a place it is a real signal.
    and (n.loc is null or s.locality is null or lower(btrim(s.locality)) = n.loc)
  order by
    (select count(*) from public.local_signals ls
      where ls.subject_ref = s.id and ls.intent = 'tip_share' and ls.status = 'listening') desc,
    s.created_at asc
  limit greatest(1, least(coalesce(p_limit, 10), 25));
$$;

comment on function public.reco_subject_candidates(text, text, text, int, text) is
  'Blocking step for the non-place identity space. v2 filters on merge_mode as well, so a '
  'recipe is never a candidate for a plumber that happens to share a word.';

revoke all on function public.reco_subject_candidates(text, text, text, int, text) from public, anon;
grant execute on function public.reco_subject_candidates(text, text, text, int, text)
  to authenticated, service_role;
