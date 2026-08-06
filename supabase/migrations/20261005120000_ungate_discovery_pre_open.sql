-- ============================================================================
-- UNGATE DISCOVERY BEFORE THE AREA OPENS  (§D.2, as the doc actually specifies)
--
-- THE BUG THIS FIXES
--   "show all communities around me" in a warming ZIP answered:
--       "Your area's just getting started, so there aren't any local communities
--        to show yet. But you can always host something yourself…"
--   Both halves were wrong. Prod has 8 grounded communities (3 with two members
--   each) and one of them sits in the very ZIP that produced this reply — nothing
--   counted anything before saying "there aren't any".
--
--   The mechanism was HERE, not in the reply layer. capability_index gates the
--   policy's options with required_state ⊆ world_state.states, and `zip_open` is
--   only in `states` when unlock_state = 'open' (app/policy/world.py). So in a
--   warming area all three discovery capabilities vanished from decide_turn's
--   list and the only move left was the seed-forward host bridge — in EVERY gate
--   mode, including the default `soft`, whose whole point is that nothing is
--   blocked and only the EMPTY-state copy changes.
--
-- WHAT §D.2 ACTUALLY SAYS (docs/LANA_CIRCLES_BACKEND.md)
--   "Deliberately supply-aware: an area that already has events keeps them fully
--    visible in any mode — hiding a host's event from neighbors starves the meets
--    that make the area come alive."
--   soft: "Nothing is blocked."  hard: "peers is the one surface that truly locks."
--
--   A static required_state cannot express "blocked only in hard mode", so the
--   mode-aware decision belongs where it already lives and is testable:
--   zip_unlock.discovery_zip_gate (peers, hard mode only). This migration removes
--   the duplicate, mode-blind copy of that rule from the capability table.
--
-- NOT CHANGED
--   * The peers gate itself. discovery_zip_gate still returns blocked=True for
--     peers in hard mode, and the peers turn still answers with the seed-forward
--     reply — sparse-area intros stay off in hard mode.
--   * No capability row is added for community discovery: "show all communities"
--     has no chat handler yet, and offering a capability the router cannot serve
--     is worse than not offering it.
-- ============================================================================

update public.capability_index
   set required_state = '{}'::text[]
 where capability_name in (
         'Find a meet or playgroup',   -- looking.meet (search-first: real events)
         'Find local activities',      -- discovery.find_activities (browse)
         'Find similar neighbors'      -- discovery.find_peers (hard mode gates it at runtime)
       )
   and required_state @> array['zip_open']::text[];

comment on column public.capability_index.required_state is
  'World-state predicates that must ALL hold before the policy may offer this '
  'capability (required_state ⊆ world_state.states). Deliberately excludes '
  'zip_open for discovery: the unlock gate is mode-dependent (soft blocks '
  'nothing, hard blocks peers only) and a static array cannot express that — it '
  'is enforced in zip_unlock.discovery_zip_gate instead. Keep verification-style '
  'predicates here; keep area-state ones out.';


-- ============================================================================
-- ROLLBACK
--   update public.capability_index
--      set required_state = array['zip_open']::text[]
--    where capability_name in ('Find a meet or playgroup', 'Find local activities',
--                              'Find similar neighbors');
-- ============================================================================
