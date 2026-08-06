-- Circles §D.2 (as amended by LANA_RAPPORT_BRIDGE_SPEC_v1, 2026-07-30) —
-- discovery.find_activities must stop advertising itself before unlock.
--
-- 20260908 deliberately left this row's required_state empty because browse was
-- supply-aware: real events in a not-yet-open area stayed visible. That stance
-- was retired for browse on 2026-07-30 — discovery_zip_gate now returns
-- blocked=True for surface="browse" in EVERY gating mode, and
-- run_activity_browse_turn routes those turns to hosting instead of listing.
--
-- The capability row never followed, so decide_turn still read find_activities
-- as available and offered it (QA 2026-07-31: a "Find local activities" chip in
-- a waitlist ZIP, where tapping it lands on "your area is still waking up").
-- A capability the policy may name must be one the engines will actually run.
--
-- Creation stays always-on (sharing.*) — this gates consumption only.

update public.capability_index
   set required_state = array['zip_open']
 where capability_id = 'discovery.find_activities'
   and (required_state is null or required_state = '{}');
