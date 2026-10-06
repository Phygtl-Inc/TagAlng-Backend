-- ============================================================================
-- `other` is a reco_type the TABLE accepts, not only the writer.
--
-- 20261130120000 made `other` the eighth bucket and the floor for every typeless
-- recommendation: app/tip_share.py writes `reco_type or 'other'`, and set_signal_reco
-- (latest body 20270115120000) accepts it. But that migration never touched the column's
-- own CHECK from 20261117120000, which still lists seven values — so every write of
-- 'other' raised 23514:
--   · capture: set_signal_reco failed AFTER the insert and local_signals.py swallows it
--     (deliberately — a posted tip must not read as failed), so a typeless tip silently
--     lost reco_type AND every reco_* card field;
--   · edit: POST /lana/tips/update {reco_type: "other"} and the chat re-arm post
--     (_update_posted_tip) surfaced it as a 502.
-- The RPC's allow-list is the intended taxonomy; this brings the table in line with it.
-- Strictly a superset of the old check, so no existing row can fail it.
--
-- ROLLBACK (only once no row holds 'other'):
--   alter table public.local_signals drop constraint local_signals_reco_type_check;
--   alter table public.local_signals add constraint local_signals_reco_type_check
--     check (reco_type is null or reco_type in ('professional','restaurant','recipe',
--       'product','location','service','diy'));
-- ============================================================================

alter table public.local_signals
  drop constraint if exists local_signals_reco_type_check;

-- NOT VALID + VALIDATE: the add takes only a brief lock; the scan runs under a weaker one.
alter table public.local_signals
  add constraint local_signals_reco_type_check
  check (reco_type is null or reco_type in (
    'professional', 'restaurant', 'recipe', 'product', 'location', 'service', 'diy',
    'other'
  )) not valid;

alter table public.local_signals
  validate constraint local_signals_reco_type_check;
