-- Circles · grounding questions on the rapport tile.
--
-- A suggested affiliation with no place_ref is invisible to the onion matcher —
-- only confirmed + grounded rows match (§C). This wires the one surface that
-- reliably reaches every user (the "By the way…" tile) into the grounding flow:
-- an ungrounded affiliation opens ONE rapport gap ("You mentioned a gym — which
-- spot is it?"), interleaved with normal rapport questions by the ranker.
--
-- Additive only. Deploy BEFORE the worker (same rule as 20260906): the worker
-- reads/writes both new columns.

-- Which affiliation this gap grounds. Cascade: if the affiliation row is ever
-- hard-deleted the question is meaningless (soft-delete via dismissed_at is the
-- normal removal path and is filtered at read time instead).
alter table public.rapport_gaps
  add column if not exists affiliation_ref uuid
    references public.circle_affiliations (id) on delete cascade;
comment on column public.rapport_gaps.affiliation_ref is
  'Set on place-grounding questions: the ungrounded circle_affiliations row this gap '
  'asks about. The ranker uses it to interleave (at most 1 circle ask per N tile '
  'questions) and the answer path grounds this affiliation.';

-- Serve-time place chips ("which spot — X, or somewhere else?"), fetched from
-- Google on FIRST serve and cached here so pending re-shows are pure lookups.
-- Shape: [{"label","address","google_place_id","send"}].
alter table public.rapport_gaps
  add column if not exists grounding_options jsonb;
comment on column public.rapport_gaps.grounding_options is
  'Cached grounding chips for affiliation_ref gaps, fetched once at first serve. '
  'Null = not fetched yet; [] = searched and found nothing (do not refetch).';

-- Cap-count ("≤2 open grounding questions") + cadence lookups.
create index if not exists rapport_gaps_affiliation_idx
  on public.rapport_gaps (user_id, status)
  where affiliation_ref is not null;
