-- 20260919120000_zip_warming_state.sql
--
-- Restore the three-state capability model from LANA_CIRCLES_ZIP_MASTER_v1 §D.2.
--
-- SPEC (§D.2, marked "THE correction, locked"):
--   | Capability                          | closed | warming | open |
--   | Receive best-available introductions | ⚠️      | ✅      | ✅   |
--   | Discover *others'* events nearby     | ❌      | ⚠️ sparse| ✅   |
--   | Reliable same-block matches          | ❌      | ❌      | ✅   |
--   "The state controls what a mom can *consume*, never what she can *create*."
--
-- WHAT SHIPPED: `zip_open` is the only area token the worker emits, so
-- `warming` behaves identically to `closed`. The middle state does not exist
-- at runtime. Every people-question in a warming area falls through to the
-- host pitch.
--
-- VERIFIED ON PROD 2026-08-05:
--   zip_unlock: 32827 warming 3/10 · 34771 warming 2/10 · 32832 closed 0/10
--   opened_at IS NULL on every row — no area has ever reached `open`.
--   So 100% of areas are permanently in the state that returns nothing.
--
-- This migration adds the `zip_warming` grounding token and re-points the
-- consumption capabilities at it. The worker emits the token in
-- app/policy/world.py (same PR). This file is inert on its own.

begin;

-- Peer discovery + meet become available at `warming`.
-- Thin results are the intended warming behaviour, not a bug: §D.2 marks these
-- "⚠️ available but thin" rather than "❌ gated".
update capability_index
   set required_state = array['zip_warming']
 where capability_id in ('discovery.find_peers', 'looking.meet');

-- Activity discovery: restore to ungated.
--
-- 20260908120000_capability_required_state.sql set this to '{}' with the
-- comment "discovery.find_activities stays {} ON PURPOSE". It is currently
-- {zip_open}. Restoring, for a reason worth stating explicitly:
--
--   Creating an event is never gated (§D.2). Events therefore exist in closed
--   and warming areas — created by people who do not themselves qualify for
--   the verified_active_count. Gating discovery hides events that are really
--   there. An empty area returns an empty list, which is the honest answer and
--   costs nothing.
update capability_index
   set required_state = '{}'
 where capability_id = 'discovery.find_activities';

commit;

-- Verify after apply:
--   select capability_id, required_state from capability_index order by 1;
-- Expect:
--   discovery.find_activities → {}
--   discovery.find_peers      → {zip_warming}
--   looking.meet              → {zip_warming}
--   looking.swap/tip, sharing.* → {}   (unchanged)
