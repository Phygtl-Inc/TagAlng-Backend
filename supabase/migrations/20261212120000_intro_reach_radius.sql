-- ---------------------------------------------------------------------------
-- Reach must cover everyone discovery can surface.
-- ---------------------------------------------------------------------------
-- 20261025120000 widened nudge/intro reach from "same home block" to "same home
-- block OR shared confirmed place". It did not widen it to the way peers are
-- actually FOUND: `match_peers_within_radius` (20260921120000) searches an
-- st_dwithin radius around the two users' coarse points — 30 km in prod.
--
-- So the card and the RPC disagreed. Prod 2026-09-16: Lana showed Tommaso five
-- neighbours, four reachable and one — Daniel, 15.5 km away in Lake Nona with no
-- confirmed shared place — who was not. She featured that one ("would you like me
-- to introduce you two?"), and every answer to it (two "Yes" and an explicit
-- "introduce me to Daniel") raised candidate_not_on_block and killed the turn.
-- The product offered an action it then refused, exactly as the previous
-- migration set out to stop it doing.
--
-- The rule becomes: not mutually blocked, and tied by a shared home block, a
-- shared confirmed place, OR proximity within the same radius discovery uses.
-- If Lana can find you, Lana can introduce you.
--
-- peer_reach_radius_meters() is the single source of truth for that distance.
-- The worker reads it (app/peer_radius.py::radius_meters) instead of its own
-- LANA_PEER_RADIUS_METERS, so see-distance and send-distance cannot drift apart
-- again — which is the bug above, not a detail of it. Changing the policy is a
-- one-line migration here, and both sides move together.
--
-- SAFETY. Reach is now wide enough that the block rule no longer doubles as the
-- coarse "can these two touch each other" gate. What still stands between a
-- stranger and a nudge: lana_is_blocked (either direction, checked first),
-- users.consent_to_receive_intros, the 30-day duplicate-intro rule, the tier
-- ladder, and enforce_nudge_limits' daily + 7-day-per-pair cooldowns. None are
-- touched here.
--
-- ROLLBACK: re-run 20261025120000_community_reachability.sql (restores the
-- two-arm _users_can_reach), then drop the three functions below.
-- ---------------------------------------------------------------------------

create or replace function public.peer_reach_radius_meters()
returns double precision
language sql
immutable
set search_path = pg_catalog, public
as $$
  select 30000::double precision;
$$;

comment on function public.peer_reach_radius_meters() is
  'How far a nudge/intro reaches, in metres. The one source of truth: the reach '
  'test below and the worker''s peer search both read it, so people who can be '
  'found can always be contacted.';

create or replace function public._users_within_reach(
  p_a uuid,
  p_b uuid,
  p_meters double precision default null
)
returns boolean
language sql
stable
security definer
set search_path = pg_catalog, public, extensions
as $$
  select coalesce(
    (
      select extensions.st_dwithin(
        a.origin,
        b.origin,
        coalesce(p_meters, public.peer_reach_radius_meters())
      )
      from public.user_origin_point(p_a) a,
           public.user_origin_point(p_b) b
      where a.origin is not null
        and b.origin is not null
    ),
    false
  );
$$;

comment on function public._users_within_reach(uuid, uuid, double precision) is
  'True when the two users'' coarse points (block centroid, else ZIP centroid) are '
  'within the reach radius. Same st_dwithin test match_peers_within_radius uses to '
  'surface them. A user with no locatable point is never in reach — false, not null.';

create or replace function public._users_can_reach(p_a uuid, p_b uuid)
returns boolean
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select not public.lana_is_blocked(p_a, p_b)
     and (
       public._users_share_home_block(p_a, p_b)
       or public._users_share_community(p_a, p_b)
       or public._users_within_reach(p_a, p_b)
     );
$$;

comment on function public._users_can_reach(uuid, uuid) is
  'The one reachability test behind nudges and intros: not mutually blocked, and '
  'tied by a shared home block, a shared confirmed place, or proximity inside '
  'peer_reach_radius_meters().';

-- Batch form for the card surface: the worker stamps every peer row with the
-- SAME predicate the send enforces, so a Nudge button is never rendered on a row
-- the RPC would refuse. One round trip per list.
create or replace function public.lana_users_can_reach(
  p_user_id uuid,
  p_other_user_ids uuid[]
)
returns table(other_user_id uuid, can_reach boolean)
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select o.id, public._users_can_reach(p_user_id, o.id)
  from unnest(coalesce(p_other_user_ids, '{}'::uuid[])) as o(id)
  where p_user_id is not null
    and o.id is not null
    and o.id <> p_user_id;
$$;

comment on function public.lana_users_can_reach(uuid, uuid[]) is
  'Reachability for a list of peers at once, for stamping can_nudge on peer cards. '
  'Same predicate as the send path — the button and the RPC cannot disagree.';

revoke all on function public.peer_reach_radius_meters() from public, anon;
grant execute on function public.peer_reach_radius_meters() to authenticated, service_role;
revoke all on function public._users_within_reach(uuid, uuid, double precision)
  from public, anon;
grant execute on function public._users_within_reach(uuid, uuid, double precision)
  to authenticated, service_role;
revoke all on function public._users_can_reach(uuid, uuid) from public, anon;
grant execute on function public._users_can_reach(uuid, uuid) to authenticated, service_role;
revoke all on function public.lana_users_can_reach(uuid, uuid[])
  from public, anon, authenticated;
grant execute on function public.lana_users_can_reach(uuid, uuid[]) to service_role;
