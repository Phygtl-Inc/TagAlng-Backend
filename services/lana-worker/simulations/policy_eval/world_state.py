"""
world_state.py — the capability registry + the required_state gate that capability-grounding
is checked against, plus small WorldState fixture builders scenarios.py reuses.

CAPABILITY-GROUNDING, IN ONE SENTENCE (engineering §C.3 + PROMPT PART 3):
"the policy can only ever OFFER a tool the user's current state satisfies; unlock gates
CONSUMPTION, never creation." checks.check_capability_grounding() enforces exactly this: a
NextAction.tool must be (a) a REGISTERED capability_id and (b) in the AVAILABLE set for the
turn's WorldState.

THE BIG CAVEAT (why the gate below is # GUESSED):
capability_index ships in migration 20260728120000 with all 8 rows, but their required_state[]
column is EMPTY on every row (the seed INSERT omits it, so it defaults to '{}'). So the real
DB currently gates NOTHING. The gate encoded here is the engineering §C.3 INTENT
("looking.*/discovery consume others' supply -> need zip_open; sharing.* create -> always-on"),
not something read from the DB. Asjid must populate required_state before capability-grounding
failures are load-bearing against `live`. Confirm the token vocabulary too ({zip_open} vs the
migration's example {phone_verified}).
"""

from __future__ import annotations

from ports import Community, WorldState

# ---------------------------------------------------------------------------
# The registered capability set — VERBATIM the 8 capability_id values seeded in
# supabase/migrations/20260728120000_lana_latent_intent.sql. (The doc §9 plans 23; only these
# 8 are seeded today.) A NextAction.tool outside this set is an unregistered/invented tool.
# ---------------------------------------------------------------------------

REGISTERED_CAPABILITIES: dict[str, set[str]] = {
    # id                        required_state[]  (# GUESSED gate — see module docstring)
    "sharing.host":            set(),           # create/host — always-on (§C.3)
    "sharing.swap":            set(),           # offer an item — always-on
    "sharing.tip":             set(),           # share a tip — always-on
    "discovery.find_activities": set(),         # GUESSED: browsing public activities always shown
                                                #          (Asjid gate: soft mode always shows supply)
    "looking.meet":            {"zip_open"},    # GUESSED: consumes others' supply -> needs open area
    "looking.swap":            {"zip_open"},    # GUESSED: consumes others' supply
    "looking.tip":             {"zip_open"},    # GUESSED: consumes others' supply
    "discovery.find_peers":    {"zip_open"},    # engineering §C.3 explicitly gates this on zip_open
}

# Capabilities whose whole job is creation/seeding — these must ALWAYS be offerable, even in a
# closed area (PROMPT PART 6 exemplar 7: quiet area -> offer sharing.host, never discovery).
ALWAYS_ON = {cid for cid, req in REGISTERED_CAPABILITIES.items() if not req}


def is_registered(capability_id: str) -> bool:
    return capability_id in REGISTERED_CAPABILITIES


def available_capabilities(world: WorldState) -> set[str]:
    """The capability_ids the user's current state makes offerable this turn (required_state ⊆ state)."""
    state = world.current_state_tokens()
    return {cid for cid, req in REGISTERED_CAPABILITIES.items() if req <= state}


# ---------------------------------------------------------------------------
# WorldState fixture builders — keep scenarios.py terse and consistent.
# ---------------------------------------------------------------------------

def cold_area(**over) -> WorldState:
    """A user whose area is just getting started (zip closed) — the seed-forward case."""
    base = dict(user_id="u_cold", zip_unlock_state="closed", phone_verified=True)
    base.update(over)
    return WorldState(**base)


def live_area(**over) -> WorldState:
    """A user whose area is open — discovery/looking capabilities are available."""
    base = dict(user_id="u_live", zip_unlock_state="open", phone_verified=True)
    base.update(over)
    return WorldState(**base)


def warming_area(**over) -> WorldState:
    base = dict(user_id="u_warm", zip_unlock_state="warming", phone_verified=True)
    base.update(over)
    return WorldState(**base)


def with_neighbor(world: WorldState, circle_type: str, place_name: str,
                  tier: str = "stranger") -> WorldState:
    """Attach a known neighbor at a given tier for over-reveal / privacy scenarios."""
    world.extra["known_neighbor"] = {
        "circle_type": circle_type, "place_name": place_name, "tier": tier,
    }
    world.communities.append(Community(circle_type=circle_type, place_name=place_name, tier=tier))  # type: ignore[arg-type]
    return world
