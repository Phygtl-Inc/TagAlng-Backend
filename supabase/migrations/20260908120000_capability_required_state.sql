-- Circles §D.2 / engineering doc §C.3 — capability-grounding in DATA.
--
-- required_state has existed since 20260728 but was empty everywhere; populate it
-- so capabilities self-declare availability instead of code branches deciding.
-- The worker's discovery gate enforces this at runtime today (zip_unlock module,
-- LANA_ZIP_UNLOCK_GATE mode); the future decide_turn policy reads the same column.
--
-- The §D.2 rule: unlock gates CONSUMPTION of others' supply, never creation.
--   · looking.meet / discovery.find_peers → need an OPEN area (zip_open).
--   · sharing.* (host / swap-offer / tip-share) → always-on, explicitly {}.
--   · discovery.find_activities stays {} ON PURPOSE: browse is supply-aware —
--     real events in a not-yet-open area remain visible (hiding a host's event
--     starves the meets that make the area come alive); only its EMPTY state
--     gets the seed-forward framing, which is copy, not availability.

update public.capability_index
   set required_state = array['zip_open']
 where capability_id in ('looking.meet', 'discovery.find_peers')
   and (required_state is null or required_state = '{}');

update public.capability_index
   set required_state = '{}'
 where capability_id like 'sharing.%'
   and required_state is null;

comment on column public.capability_index.required_state is
  'States the user must satisfy for this capability to be OFFERED (e.g. {zip_open}). '
  'Empty = always available. Consumption capabilities gate on area unlock (§D.2); '
  'creation capabilities never do.';
