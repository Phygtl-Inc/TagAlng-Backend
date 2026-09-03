-- The chips under "WHY LANA SEES A FIT" on a fellows row (new card).
--
-- The card used to render Lana's sentence alone; it now leads with 2-3 short facets and
-- keeps the sentence behind them. Same authoring pass, same cache key, same rec_id for the
-- thumb — only the stored shape grows. Nullable on purpose: a row written before this
-- column existed reads back NULL and is recomposed once, while a row the model gave no
-- honest facet for stores '[]' and is served from cache like any other.
alter table public.peer_rec_lines
  add column if not exists chips jsonb;

comment on column public.peer_rec_lines.chips is
  'Short authored "why a fit" facets shown as chips above the line. NULL = authored before chips existed (recompose); [] = no honest facet.';
