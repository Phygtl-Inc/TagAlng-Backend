-- Typed recommendations (the C-4-RECO capture).
--
-- A recommendation is not one shape: a dentist, a recipe and a sound machine answer
-- different questions, and the reader's card shows different rows. The type is what the
-- neighbor is meant to DO with it (eat there / cook it / buy it / visit it / hire them /
-- book them / do it themselves) — not the topic, so food doesn't collapse into one bucket.
--
-- reco_fields holds the per-type answers as {field: answer}. Deliberately jsonb and NOT
-- seven sets of columns: the question sets are still churning with product, and detail_text
-- is capped at 500 chars so the answers cannot just live inside the sentence.

alter table public.local_signals
  add column if not exists reco_type text
    check (reco_type is null or reco_type in (
      'professional', 'restaurant', 'recipe', 'product', 'location', 'service', 'diy'
    )),
  add column if not exists reco_fields jsonb not null default '{}'::jsonb;

comment on column public.local_signals.reco_type is
  'Which question set the recommendation was captured with. Null for tips captured before '
  'typed capture shipped, and for any non-tip_share intent — those keep the six loose '
  'category buckets and are shown as-is (no backfill).';
comment on column public.local_signals.reco_fields is
  '{field: answer} for the reco_type''s question set. Keys are the stable snake_case step '
  'fields, never the question wording, so re-wording a question does not orphan an answer.';

-- Browsing "all the recipes near me" is the point of typing them.
create index if not exists idx_local_signals_reco_type
  on public.local_signals (block_id, reco_type)
  where status = 'listening' and reco_type is not null;

-- Narrow writer instead of a general "update own signal" policy: the author stamps the
-- type and the answers, and cannot touch status/detail_text/expiry through this path.
create or replace function public.set_signal_reco(
  p_signal_id uuid,
  p_reco_type text default null,
  p_reco_fields jsonb default null
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

  update public.local_signals
     set reco_type = coalesce(p_reco_type, reco_type),
         reco_fields = coalesce(p_reco_fields, reco_fields),
         updated_at = now()
   where id = p_signal_id
     and user_id = v_me;
end;
$$;

revoke all on function public.set_signal_reco(uuid, text, jsonb) from public, anon;
grant execute on function public.set_signal_reco(uuid, text, jsonb) to authenticated;
