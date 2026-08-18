-- ============================================================================
-- COMMUNITY DISCOVERY IS A CAPABILITY  (engineering doc §C.3)
--
-- THE BUG THIS FIXES
--   "can u show me communities around me" answered:
--       "Yep — I can look for people and activities around you. Want me to find
--        nearby people who share one of your interests?"   (2026-08-18)
--   Not a classifier miss: discovery_slots names that exact sentence as a
--   discovery.communities example. decide_turn runs ahead of the engines and its
--   entire menu of things Lana can do IS this table — which had no communities
--   row — so the policy pitched the two nearest capabilities it could see,
--   find_peers and find_activities, and communities_chat_turn never ran.
--
--   20261005120000 deliberately left this row out, with a reason that has since
--   expired: "no capability row is added for community discovery: 'show all
--   communities' has no chat handler yet, and offering a capability the router
--   cannot serve is worse than not offering it." The handler shipped with
--   20261004/05 — discovery_route._try_layer1_intent_turn → communities_chat_turn,
--   real circle_affiliations rows and joinable neighbours' spots.
--
-- required_state = {verified}
--   The handler gates the READ on verification the same way find-peers does: the
--   block/place read and the member counts are neighbours' data. Declaring it
--   here keeps the policy from pitching what the handler would then refuse.
--   NOT zip_open: communities are never area-gated (§D.2 — a warming ZIP with 8
--   grounded communities is exactly the case 20261005 was written to fix).
--
-- embedding stays null, like every row seeded in 20260728: it only feeds Phase-1
-- latent suggestion matching, which is collect-only and surfaces nothing.
-- ============================================================================

insert into public.capability_index
  (capability_id, capability_name, description,
   entity_triggers, identity_claim_triggers, required_state, surface_priority)
values
  ('discovery.communities', 'Show communities nearby',
   'Show the communities around them — the gyms, churches, schools, clubs and '
   'other spots neighbours belong to, both the ones they are already part of and '
   'nearby ones they could join',
   '{community,group,gym,church,school,club,team,studio,class}',
   '{}', array['verified']::text[], 6)
on conflict (capability_id) do update
   set capability_name = excluded.capability_name,
       description     = excluded.description,
       entity_triggers = excluded.entity_triggers,
       required_state  = excluded.required_state,
       surface_priority = excluded.surface_priority,
       is_active       = true,
       updated_at      = now();

-- ============================================================================
-- ROLLBACK
--   delete from public.capability_index where capability_id = 'discovery.communities';
-- ============================================================================
