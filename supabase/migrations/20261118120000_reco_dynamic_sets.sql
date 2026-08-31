-- Dynamic recommendation question sets — the delta on top of 20261117120000_reco_types.sql,
-- which is already applied on dev with the earlier shape.
--
-- What changed since that file: the questions are no longer seven hardcoded sets. Lana WRITES
-- the set per recommendation (app/reco_question_sets.py owns the per-type floor, the guards
-- and the two closing steps), so a recipe is asked about taste and difficulty and a dentist
-- about what she treats. Two consequences land here:
--
--   1. reco_fields becomes an ARRAY of answered steps. {field: answer} was fine against a
--      fixed set; against a generated one it loses the wording, and nothing can say later
--      that "helped_with" was asked as "What did she help with?" — the reader card would
--      have values with no labels. So each element carries its own question.
--   2. reco_subject exists, because the closing "others also said · tap to agree" step needs
--      to group what other neighbours logged about the SAME subject.
--
-- Idempotent throughout: safe whether 20261117120000 was applied by `db push` or by hand.

-- ── 1. reco_fields: object → array ────────────────────────────────────────────────────
-- Convert before constraining, or the check fails validation on every existing row (they
-- all carry the old '{}' default). Any real answers captured under the old shape are kept,
-- as {field, answer} elements — the question wording for those is genuinely unknown, and
-- inventing one would put words in a neighbour's mouth.
update public.local_signals
   set reco_fields = coalesce(
         (
           select jsonb_agg(jsonb_build_object('field', kv.key, 'answer', kv.value))
             from jsonb_each_text(reco_fields) kv
         ),
         '[]'::jsonb
       )
 where jsonb_typeof(reco_fields) = 'object';

update public.local_signals
   set reco_fields = '[]'::jsonb
 where jsonb_typeof(reco_fields) is distinct from 'array';

alter table public.local_signals
  alter column reco_fields set default '[]'::jsonb;

alter table public.local_signals
  drop constraint if exists local_signals_reco_fields_is_array;
alter table public.local_signals
  add constraint local_signals_reco_fields_is_array
  check (jsonb_typeof(reco_fields) = 'array');

comment on column public.local_signals.reco_fields is
  'The answered steps, in ask order: [{field,label,question,kind,answer}]. Self-describing '
  'because the question set is generated per recommendation — the wording is not '
  'reconstructible from a field key, so it is stored with the answer.';

-- ── 2. reco_subject: what the agree-row tallies group on ──────────────────────────────
alter table public.local_signals
  add column if not exists reco_subject text;

comment on column public.local_signals.reco_subject is
  'Normalized subject (lower/trimmed name) the "others also said" tallies group on. Exact '
  'key: "Dr Sarah" and "Dr. Sarah" are deliberately different subjects.';

-- Stamped at write time so a tally lookup is an index hit, not a scan over every
-- recommendation on the block.
create index if not exists idx_local_signals_reco_subject
  on public.local_signals (block_id, reco_subject)
  where status = 'listening' and reco_subject is not null;

-- ── 3. The writer gains the subject ───────────────────────────────────────────────────
-- The 3-arg version is DROPPED, not left beside this one: two overloads make PostgREST
-- ambiguous about which the worker meant, and it would resolve to whichever matches the
-- keys sent — silently dropping the subject on every write.
drop function if exists public.set_signal_reco(uuid, text, jsonb);

create or replace function public.set_signal_reco(
  p_signal_id uuid,
  p_reco_type text default null,
  p_reco_fields jsonb default null,
  p_reco_subject text default null
)
returns void
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

  if p_reco_type is not null and p_reco_type not in (
    'professional', 'restaurant', 'recipe', 'product', 'location', 'service', 'diy'
  ) then
    raise exception 'invalid_reco_type' using errcode = 'P0001';
  end if;

  if p_reco_fields is not null and jsonb_typeof(p_reco_fields) <> 'array' then
    raise exception 'reco_fields_must_be_array' using errcode = 'P0001';
  end if;

  update public.local_signals
     set reco_type = coalesce(p_reco_type, reco_type),
         reco_fields = coalesce(p_reco_fields, reco_fields),
         reco_subject = coalesce(nullif(btrim(lower(p_reco_subject)), ''), reco_subject),
         updated_at = now()
   where id = p_signal_id
     and user_id = v_me;
end;
$$;

revoke all on function public.set_signal_reco(uuid, text, jsonb, text) from public, anon;
grant execute on function public.set_signal_reco(uuid, text, jsonb, text) to authenticated;

-- ── 4. "Others also said · tap to agree" ──────────────────────────────────────────────
-- security definer because RLS on local_signals is own-rows-only and this is deliberately
-- about OTHER people's rows. What escapes is fenced hard: an aggregate of short answers
-- (<= 24 chars, <= 3 words) with counts — never a row, an author, a signal id, or a
-- sentence somebody wrote about their own kid.
--
-- Agreeing is not a write here: the tap lands in the agreeing user's OWN reco_fields, so
-- the next reader's count picks it up from this same group-by and there is no counter to
-- keep honest.
create or replace function public.reco_attr_tallies(
  p_block_id text,
  p_subject text,
  p_limit int default 6
)
returns table (attr text, n int)
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select lower(btrim(a.answer)) as attr, count(*)::int as n
    from public.local_signals s
    -- Split on the separators the capture actually produces: a multi-tap agree row comes
    -- back as "easy parking, books online", and an answer like "Takes insurance · open
    -- Saturdays" is two attributes a neighbour would tap, not one four-word phrase the
    -- length filter below would throw away.
    cross join lateral (
      select regexp_split_to_table(e ->> 'answer', '\s*[,;·]\s*') as answer
        from jsonb_array_elements(s.reco_fields) e
    ) a
   where auth.uid() is not null
     and s.user_id <> auth.uid()
     and s.block_id = p_block_id
     and s.reco_subject = btrim(lower(p_subject))
     and s.status = 'listening'
     and a.answer is not null
     and char_length(btrim(a.answer)) between 3 and 24
     and array_length(regexp_split_to_array(btrim(a.answer), '\s+'), 1) <= 3
   group by 1
   order by n desc, attr
   limit greatest(1, least(coalesce(p_limit, 6), 12));
$$;

revoke all on function public.reco_attr_tallies(text, text, int) from public, anon;
grant execute on function public.reco_attr_tallies(text, text, int) to authenticated;
