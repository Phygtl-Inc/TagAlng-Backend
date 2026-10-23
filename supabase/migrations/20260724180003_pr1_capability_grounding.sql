-- ============================================================================
-- PR 1 · Capability-grounding in data  (→ Asjid to review & push)
-- ----------------------------------------------------------------------------
-- WHY: capability_index.required_state is empty on all rows and ranking is a
--      static surface_priority int — that hardcoded-priority pattern is what
--      collided in standup (grounding question stopped firing). This moves
--      capability *availability* into data so the policy can only ever offer
--      what the user's real state allows (Circles master §D: unlock gates
--      consumption, never creation). Also scrubs a lingo leak ("mom-tested").
-- SCOPE: data update + 2 read-only SQL functions. Non-destructive. Idempotent.
-- REAL SCHEMA USED: capability_index(required_state text[], is_active),
--      blocks.state block_state{waitlist,racing,live,day_zero},
--      users.home_block_id, users.phone_verified_at, circle_affiliations(status, place_id).
-- ASSUMPTION TO CONFIRM: circle_affiliations exists with (user_id, status, place_id).
--      If the merged circle tables differ, adjust the `circ` CTE only.
-- ROLLBACK: drop functions; reset required_state to '{}'.
-- ============================================================================

begin;

-- 1) LINGO SCRUB · no "mom" in user-surfacing capability text (LANA_LINGO §10)
update capability_index
   set capability_name = 'Find a well-loved local recommendation'
 where capability_id = 'looking.tip' and capability_name ilike '%mom%';
-- (reviewer: grep all rows for stray "mom" in capability_name/description)

-- 2) STATE VOCABULARY · the set of state flags currently true for a user.
--    Maps prod block_state → supply availability. 'has_supply' = area live/day_zero OR an active circle.
create or replace function current_state_array(p_user uuid)
returns text[]
language sql stable
as $$
  with u as (select home_block_id, phone_verified_at from users where id = p_user),
       b as (select state from blocks where id = (select home_block_id from u)),
       circ as (
         select exists (
           select 1 from circle_affiliations a
           where a.user_id = p_user and a.status = 'confirmed' and a.place_id is not null
             and (select count(*) from circle_affiliations o
                    where o.place_id = a.place_id and o.status = 'confirmed') >= 2
         ) as has_active
       )
  select array_remove(array[
      case when (select phone_verified_at from u) is not null           then 'phone_verified' end,
      case when (select state from b) = 'waitlist'                       then 'zip_closed'  end,
      case when (select state from b) = 'racing'                         then 'zip_warming' end,
      case when (select state from b) in ('live','day_zero')             then 'zip_open'    end,
      case when (select state from b) in ('live','day_zero')
                or (select has_active from circ)                         then 'has_supply'  end
  ], null);
$$;

-- 3) POPULATE required_state · creation/rapport/tips ALWAYS on; discovery of others gated by has_supply.
update capability_index set required_state = '{}'
  where capability_id in ('sharing.host','sharing.swap','sharing.tip','looking.tip');
update capability_index set required_state = '{has_supply}'
  where capability_id in ('looking.meet','looking.swap','discovery.find_peers','discovery.find_activities');

-- 4) AVAILABILITY · the candidate capabilities the policy may offer this user right now.
--    required_state ⊆ current_state_array(user).  '<@' = "is contained by".
create or replace function capabilities_available(p_user uuid)
returns setof capability_index
language sql stable
as $$
  select * from capability_index
  where is_active and required_state <@ current_state_array(p_user)
  order by surface_priority;   -- weak tiebreak ONLY; the policy LLM does the real arbitration
$$;

commit;

-- ============================================================================
-- TEST PLAN (run manually after apply):
--   select capability_id from capabilities_available('<cold_user_uuid>');  -- expect sharing.* + looking.tip only
--   select capability_id from capabilities_available('<live_user_uuid>');  -- expect + discovery/looking.meet/swap
--   select current_state_array('<user_uuid>');
-- ROLLBACK:
--   drop function if exists capabilities_available(uuid);
--   drop function if exists current_state_array(uuid);
--   update capability_index set required_state = '{}';
-- ============================================================================
