"""
world_state.py — the capability registry + the required_state gate that capability-grounding
is checked against, plus small WorldState fixture builders scenarios.py reuses.

CAPABILITY-GROUNDING, IN ONE SENTENCE (engineering §C.3 + PROMPT PART 3):
"the policy can only ever OFFER a tool the user's current state satisfies; unlock gates
CONSUMPTION, never creation." checks.check_capability_grounding() enforces exactly this: a
NextAction.tool must be (a) a REGISTERED capability_id, (b) ACTIVE, and (c) in the AVAILABLE
set for the turn's WorldState.

WHERE THIS TABLE COMES FROM (revised 2026-08-18 — it is no longer guesswork)
---------------------------------------------------------------------------
`capability_index` is seeded with 8 rows in 20260728120000_lana_latent_intent.sql:55 and
`required_state` has been rewritten four times since. Replaying the migrations in order:

  20260728120000:55   seed, required_state default '{}' for all 8
  20260908120000:16   looking.meet, discovery.find_peers      -> {zip_open}
  20260908120000:21   sharing.*                               -> {} (explicit)
  20260917120000:17   discovery.find_activities               -> {zip_open}
  20261005120000:40   'Find a meet or playgroup',
                      'Find local activities',
                      'Find similar neighbors'                -> {}   <-- UNGATED AGAIN
  20261006120000:15   looking.swap, sharing.swap              -> is_active = false
  20261028120000:42   discovery.communities  (NEW ROW)        -> {verified}

NET RESULT: eight of the nine rows have required_state '{}', the two swap rows are switched off,
and `discovery.communities` — added 2026-10-28, after the replay above was first written — is the
one row with a live gate. The containment arm is therefore NO LONGER vacuous.

That last migration is why this table mattered so much. Its header says it plainly: gating
discovery on `zip_open` meant "in a warming area all three discovery capabilities vanished
from decide_turn's list and the only move left was the seed-forward host bridge", which
produced the "there aren't any local communities to show yet" bug in a ZIP that had them.
The unlock gate is MODE-DEPENDENT (soft blocks nothing; hard blocks peers only), a static
array cannot express that, so it moved to zip_unlock.discovery_zip_gate at runtime. This
harness previously encoded the OLD, pre-20261005 gate — so `capability_grounding` HARD_FAILed
Lana for offering discovery in a closed area, which is now the CORRECT behaviour. That is a
harness false positive of exactly the kind CLAUDE.md says poisons a gate, and it is fixed here.

WHAT STILL HAS TEETH
--------------------
The "required_state ⊆ state" arm was vacuous while every row was '{}'; 20261028120000 ended that
by shipping `discovery.communities` with {verified}, so offering it to an unverified user is now
a real HARD_FAIL. Two further arms are independent of any gate and were never vacuous:
  * REGISTRATION — an invented capability_id ("magic.teleport", or a raw engine tool name)
    is still a HARD_FAIL.
  * IS_ACTIVE — offering looking.swap / sharing.swap is a HARD_FAIL, because 20261006120000
    switched them off ("Swap is not shipped. Stop offering it."), app/policy/world.py:186
    filters `is_active` before the policy ever sees a row, and the migration exists precisely
    because Lana pitched swap in prod.
The containment arm is now live rather than aspirational: `verified` is a real gate on a real
row. NOTE for whoever reads selftest.py — `cold_area()` and `live_area()` are BOTH verified, so
comparing their availability sets cancels a {verified} gate out of both sides and can never see
it. The availability assertions must vary the token under test (`unverified_user()`), one
dimension at a time; a `has_home_zip` gate would be the next blind spot if one ever shipped.
"""

from __future__ import annotations

from ports import Community, WorldState

# ---------------------------------------------------------------------------
# The registered capability set — NINE rows: the 8 capability_id values seeded in
# supabase/migrations/20260728120000_lana_latent_intent.sql:55, plus `discovery.communities`
# added by 20261028120000:32. required_state is as of the latest migration that touches each
# row (see the module docstring for the replay; capability_drift() checks it against the SQL).
# A NextAction.tool outside this set is an unregistered/invented tool.
# ---------------------------------------------------------------------------

REGISTERED_CAPABILITIES: dict[str, set[str]] = {
    # id                          required_state[]  (source)
    "looking.meet":              set(),   # 20261005120000:40 cleared the {zip_open} gate
    "looking.swap":              set(),   # ANSWERED (Asjid 2026-08-25): do not guess a gate here.
                                          # 20261006120000 set is_active=false on looking.swap and
                                          # sharing.swap because Lana was offering an unbuilt
                                          # feature, so the row never surfaces and the gate is
                                          # moot. The load-bearing assertion is is_active=false
                                          # (INACTIVE_CAPABILITIES below), not required_state.
    "looking.tip":               set(),   # ANSWERED (Asjid 2026-08-25): '{}' is INTENTIONAL, not
                                          # an unset default. The runtime gate hard-locks exactly
                                          # one surface — discovery_zip_gate blocks only
                                          # surface == "peers" (app/zip_unlock.py:241) — and
                                          # nothing gates tip consumption. No longer GUESSED.
    "sharing.host":              set(),   # 20260908120000:21 — creation is always-on (§D.2)
    "sharing.swap":              set(),   # 20260908120000:21
    "sharing.tip":               set(),   # 20260908120000:21
    "discovery.find_peers":      set(),   # 20261005120000:40 cleared the {zip_open} gate
    "discovery.find_activities": set(),   # 20260917120000:17 set it, 20261005120000:40 cleared it
    # 20261028120000_communities_capability.sql:42 — added AFTER 20261005 deliberately reversed
    # itself ("no chat handler yet" expired once discovery_route._try_layer1_intent_turn ->
    # communities_chat_turn shipped). This is the FIRST non-empty required_state since 20261005
    # cleared them all, so the state-token gate is live again rather than vacuous. NOT zip_open:
    # the migration is explicit that communities are never area-gated (§D.2) — a warming ZIP with
    # grounded communities is the exact case 20261005 existed to fix. `verified` because the
    # handler gates the read on verification, the same way find-peers does: member counts and the
    # place read are neighbours' data.
    "discovery.communities":     {"verified"},  # 20261028120000:42
}

# Rows with is_active = false. app/policy/world.py:186 (`capabilities_available`) filters on
# is_active BEFORE required_state, and app/policy/goals.py:416 drops any goal naming an inactive
# capability whatever queue it came from — so an inactive capability can never reach the policy's
# menu, and naming one is an invented offer just as surely as an unregistered id would be.
INACTIVE_CAPABILITIES: set[str] = {
    "looking.swap",   # 20261006120000_swap_capability_truthful.sql:15
    "sharing.swap",   # ditto — "Swap is not shipped. Stop offering it."
}

# Capabilities whose whole job is creation/seeding — these must ALWAYS be offerable, even in a
# closed area (PROMPT PART 6 exemplar 7: quiet area -> offer sharing.host, never discovery).
ALWAYS_ON = {
    cid for cid, req in REGISTERED_CAPABILITIES.items()
    if not req and cid not in INACTIVE_CAPABILITIES
}


def is_registered(capability_id: str) -> bool:
    return capability_id in REGISTERED_CAPABILITIES


def is_active(capability_id: str) -> bool:
    return capability_id not in INACTIVE_CAPABILITIES


def available_capabilities(world: WorldState) -> set[str]:
    """The capability_ids the user's current state makes offerable this turn.

    Mirrors app/policy/world.py:172-192: filter is_active, then keep rows whose
    required_state ⊆ world.states. `world.current_state_tokens()` now emits the REAL
    vocabulary (verified / has_home_zip / zip_open / has_circle), so the containment
    comparison is against the same token namespace the DB column is written in.
    """
    state = world.current_state_tokens()
    return {
        cid for cid, req in REGISTERED_CAPABILITIES.items()
        if cid not in INACTIVE_CAPABILITIES and req <= state
    }


# ---------------------------------------------------------------------------
# WorldState fixture builders — keep scenarios.py terse and consistent.
#
# All three give the user a home ZIP and a verified account, because that is the only shape
# in which an `area` exists at all: app/policy/world.py:113 reads the zip snapshot from
# users.home_zip, and with no home ZIP there is no unlock row and `zip_open` can never hold.
# A fixture claiming zip_unlock_state='open' with home_zip=None would be an impossible world.
# ---------------------------------------------------------------------------

# Central-Florida ZIPs, matching the prod QA notes the migrations cite (Narcoossee/Lake Nona).
_ZIP = "32832"


def cold_area(**over) -> WorldState:
    """A user whose area is just getting started (zip closed) — the seed-forward case."""
    base = dict(user_id="u_cold", zip_unlock_state="closed", verified=True, home_zip=_ZIP)
    base.update(over)
    return WorldState(**base)


def live_area(**over) -> WorldState:
    """A user whose area is open. NOTE (post-20261005): no capability requires `zip_open` any
    more — 20261005120000 cleared that gate on all three discovery rows and 20261028120000
    deliberately did NOT re-add it (communities are never area-gated) — so this differs from
    cold_area only in the `zip_open` token and in what the copy should sound like. It is NOT
    true that every required_state is empty: `discovery.communities` requires {verified}, which
    both of these fixtures happen to satisfy. It is kept because the runtime gate
    (zip_unlock.discovery_zip_gate, hard mode) still reads area state."""
    base = dict(user_id="u_live", zip_unlock_state="open", verified=True, home_zip=_ZIP)
    base.update(over)
    return WorldState(**base)


def warming_area(**over) -> WorldState:
    base = dict(user_id="u_warm", zip_unlock_state="warming", verified=True, home_zip=_ZIP)
    base.update(over)
    return WorldState(**base)


def unverified_user(**over) -> WorldState:
    """No phone AND no email verification -> the `verified` token is absent (world.py:130).
    Exists so a scenario can exercise the token that the harness used to mis-name."""
    base = dict(user_id="u_unverified", zip_unlock_state="closed", verified=False, home_zip=_ZIP)
    base.update(over)
    return WorldState(**base)


def rootless_user(**over) -> WorldState:
    """No home ZIP -> no `has_home_zip`, and no area at all, so no `zip_open` either."""
    base = dict(user_id="u_rootless", zip_unlock_state="closed", verified=True, home_zip=None)
    base.update(over)
    return WorldState(**base)


def with_confirmed_circle(world: WorldState, circle_type: str = "gym",
                          place_name: str | None = None) -> WorldState:
    """Give the user a circle_affiliations row with status='confirmed' — the exact condition
    app/policy/world.py:134 uses to emit `has_circle`."""
    world.communities.append(
        Community(circle_type=circle_type, place_name=place_name,
                  confirmed=True, grounded=bool(place_name))
    )
    return world


def with_neighbor(world: WorldState, circle_type: str, place_name: str,
                  tier: str = "stranger") -> WorldState:
    """Attach a known neighbor at a given tier for over-reveal / privacy scenarios.

    Deliberately confirmed=False: this models someone ELSE's affiliation that the matcher
    surfaced, not a confirmed circle of the user's own, so it must not grant `has_circle`.
    """
    world.extra["known_neighbor"] = {
        "circle_type": circle_type, "place_name": place_name, "tier": tier,
    }
    world.communities.append(Community(circle_type=circle_type, place_name=place_name, tier=tier))  # type: ignore[arg-type]
    return world




# ---------------------------------------------------------------------------
# Drift detection against the migrations (offline, deterministic, no DB)
# ---------------------------------------------------------------------------
# WHY THIS EXISTS: REGISTERED_CAPABILITIES is a hand-maintained mirror of
# public.capability_index. On 2026-08-24 a pull added `discovery.communities`
# (20261028120000) and NOTHING in this suite noticed — every selftest stayed green while
# capability_grounding would have HARD_FAILed real Lana for offering it, exactly the class of
# false positive this harness exists to avoid. A selftest that pins the harness's own dict
# proves the dict equals itself; it cannot see a row added upstream.
#
# The migrations are IN THE REPO, so the real registry is checkable offline with no DB and no
# network. There are TWO arms, deliberately different in ambition:
#
#   ID ARM — capability_ids_in_migrations(). A loose literal scan for 'namespace.name' in
#     non-comment SQL. Over-inclusive by construction (it counts an id mentioned only in a
#     description scrub as "real"), which is the SAFE direction for `missing`: it can only
#     ever make us notice more ids, never fewer. This is the arm that would have caught the
#     2026-08-24 miss.
#
#   VALUE ARM — capability_required_state_replay(). A strict statement-level replay that
#     recomputes required_state per id. Added 2026-08-25: the ID arm compares ids ONLY, so the
#     hand-transcribed {"verified"} above was load-bearing with nothing checking it, and a
#     wrong value there does not read as an error — it silently mis-gates a real capability.
#     The replay parses ONLY unambiguous shapes and REFUSES to guess at anything else:
#
#       parsed   `insert into public.capability_index (<cols>) values (...)[, (...)]`
#                  + `on conflict ... do nothing` / `do update set ...`
#                `update public.capability_index set ..., required_state = <array literal>
#                  where capability_id = '<id>'` / `where capability_id in ('<id>', ...)`
#                `delete from public.capability_index where capability_id = '<id>'|in (...)`
#
#       UNPARSED (reported, never guessed):
#                * any WHERE that is not exactly the capability_id predicate above. That
#                  includes 20261005120000's `where capability_name in (...)` (keyed on the
#                  NAME, so the id is not even in the statement) and every
#                  `... and (required_state is null or required_state = '{}')` conditional
#                  guard (20260908120000, 20260917120000). A guard means the statement MAY
#                  not have applied; assuming it did is the false positive that poisons a gate.
#                * any required_state literal outside `array['a','b'](::text[])?` / `'{a,b}'`.
#                * a write to capability_index inside a $$-quoted body. A read-only function
#                  body like 20260729120000's matcher is NOT flagged — it cannot mutate a row.
#
#     An UNPARSED statement is not merely skipped: it POISONS everything it could have
#     touched. An id's value is reported VERIFIABLE only when its last parseable assignment
#     comes AFTER the last unparsed statement — because an unparsed statement keyed on
#     capability_name could have rewritten any row. Today that means exactly one of the nine
#     ids (`discovery.communities`, inserted by 20261028120000, after every unparsed
#     statement) is verified and eight are reported unverified. "I could not check these" is
#     an honest result; silently reporting them clean is not, so selftest.py PRINTS them.
#
# KNOWN LIMITS (documented rather than half-fixed):
#   * A row inserted without a required_state column is replayed as '{}' — the column default
#     declared at 20260728120000:26 (`required_state text[] not null default '{}'`). The
#     replay does not parse DDL defaults. Not load-bearing today: the only id that reaches
#     VERIFIABLE status (`discovery.communities`) names required_state explicitly.
#   * DELETION: a strict-form `delete from public.capability_index where capability_id = '...'`
#     IS replayed, so a deleted-but-still-registered id is reported. The ID arm alone is blind
#     to this — the id literal survives inside the DELETE statement itself, so the loose scan
#     still counts it as real. A delete keyed on capability_name, or carrying extra WHERE
#     conditions, is UNPARSED: reported, not guessed at.
#   * is_active is NOT replayed. INACTIVE_CAPABILITIES stays hand-maintained. The shapes there
#     (20261006120000) would parse, but nothing has gone wrong there yet and every extra
#     mechanical arm is a new false-positive surface.

import re as _re
from pathlib import Path as _Path

_MIGRATIONS_DIR = _Path(__file__).resolve().parents[4] / "supabase" / "migrations"
# A capability_id literal: 'namespace.name'. Ids are lowercase/underscore by convention.
_CAP_ID_RE = _re.compile(r"'([a-z][a-z_]*\.[a-z][a-z_]*)'")
_LINE_COMMENT_RE = _re.compile(r"--[^\n]*")
_BLOCK_COMMENT_RE = _re.compile(r"/\*.*?\*/", _re.S)
# $$ ... $$ / $tag$ ... $tag$ function bodies.
_DOLLAR_BLOCK_RE = _re.compile(r"\$(\w*)\$.*?\$\1\$", _re.S)
# A MUTATION of capability_index (`from public.capability_index` in a select is not one).
_CAP_WRITE_RE = _re.compile(
    r"\b(?:insert\s+into|update|delete\s+from)\s+(?:public\.)?capability_index\b", _re.I)


def capability_ids_in_migrations(migrations_dir: _Path | None = None) -> set[str]:
    """Every capability_id literal appearing in real (non-comment) migration SQL.

    Line comments are stripped first: migrations discuss capability ids in prose constantly,
    including rollback recipes like `--   delete from public.capability_index where
    capability_id = 'discovery.communities';`. Counting those would make the check fire on
    documentation.
    """
    d = migrations_dir or _MIGRATIONS_DIR
    found: set[str] = set()
    for path in sorted(d.glob("*.sql")):
        sql = _LINE_COMMENT_RE.sub("", path.read_text(encoding="utf-8", errors="replace"))
        if "capability_index" not in sql:
            continue
        found.update(_CAP_ID_RE.findall(sql))
    return found


# --- the strict statement parser (the value arm) -------------------------------------------

def _split_top_level(text: str, sep: str = ",") -> list[str]:
    """Split on `sep` at bracket depth 0, respecting single-quoted strings ('' escapes).

    Needed because a VALUES row, an array literal and a SET clause are all comma-separated
    and all carry commas INSIDE quotes ('{community,group,gym}') and brackets (array['a','b']).
    """
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    in_str = False
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if in_str:
            if ch == "'":
                if i + 1 < n and text[i + 1] == "'":
                    buf.append("''")
                    i += 2
                    continue
                in_str = False
            buf.append(ch)
            i += 1
            continue
        if ch == "'":
            in_str = True
        elif ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        elif ch == sep and depth == 0:
            parts.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return parts


# NON-GREEDY on purpose: with a greedy `.*` the trailing `]` of a `::text[]` cast is what
# closes the bracket, and `array['verified']::text[]` silently fails to parse.
_ARRAY_CTOR_RE = _re.compile(r"^array\s*\[(?P<items>.*?)\]\s*(?:::\s*text\s*\[\s*\])?\s*$",
                             _re.I | _re.S)
_ARRAY_STR_RE = _re.compile(r"^'(?P<body>[^']*)'\s*(?:::\s*text\s*\[\s*\])?$", _re.S)
_QUOTED_ITEM_RE = _re.compile(r"^'([^']*)'\s*(?:::\s*text)?$", _re.I)


def _parse_text_array(literal: str):
    """array['a','b']::text[] or '{a,b}'::text[] -> {'a','b'}. None means: do not guess."""
    lit = literal.strip()
    m = _ARRAY_CTOR_RE.match(lit)
    if m:
        out: set[str] = set()
        for part in _split_top_level(m.group("items"), ","):
            part = part.strip()
            if not part:
                continue
            q = _QUOTED_ITEM_RE.match(part)
            if not q:
                return None
            out.add(q.group(1))
        return out
    m = _ARRAY_STR_RE.match(lit)
    if m:
        body = m.group("body").strip()
        if not (body.startswith("{") and body.endswith("}")):
            return None
        inner = body[1:-1].strip()
        if not inner:
            return set()
        # Quoted / escaped / space-bearing array text has real Postgres parsing rules.
        # Refuse rather than approximate them.
        if any(c in inner for c in '"\\ '):
            return None
        return {p.strip() for p in inner.split(",") if p.strip()}
    return None


_INSERT_RE = _re.compile(
    r"^insert\s+into\s+(?:public\.)?capability_index\s*\((?P<cols>[^()]*)\)\s*values\s*(?P<rest>.*)$",
    _re.I | _re.S)
_UPDATE_RE = _re.compile(
    r"^update\s+(?:public\.)?capability_index\s+set\s+(?P<sets>.*?)\s+where\s+(?P<where>.*)$",
    _re.I | _re.S)
_DELETE_RE = _re.compile(
    r"^delete\s+from\s+(?:public\.)?capability_index\s+where\s+(?P<where>.*)$", _re.I | _re.S)
_ON_CONFLICT_RE = _re.compile(
    r"^on\s+conflict\s*(?:\([^()]*\))?\s*(?:do\s+nothing|do\s+update\s+set\s+(?P<sets>.*))$",
    _re.I | _re.S)
# Statement heads that mention required_state but cannot change any row's value.
_IGNORABLE_RE = _re.compile(r"^(?:create|comment|grant|revoke|drop|select)\b", _re.I)
_WHERE_EQ_RE = _re.compile(r"^capability_id\s*=\s*'([a-z0-9_.]+)'$", _re.I)
_WHERE_IN_RE = _re.compile(r"^capability_id\s+in\s*\((?P<ids>[^()]*)\)$", _re.I)
_PLAIN_ID_RE = _re.compile(r"^\s*'([a-z0-9_.]+)'\s*$", _re.I)

_NOT_ASSIGNED = object()  # "this SET clause does not assign required_state at all"


def _assigned_required_state(sets: str):
    """-> set[str] | 'excluded' | None (unparseable) | _NOT_ASSIGNED (not in the SET list)."""
    for assignment in _split_top_level(sets, ","):
        key, sep, val = assignment.partition("=")
        if not sep:
            continue
        if key.strip().lower() not in ("required_state", "capability_index.required_state"):
            continue
        v = val.strip().rstrip(";").strip()
        if _re.fullmatch(r"excluded\s*\.\s*required_state", v, _re.I):
            return "excluded"
        return _parse_text_array(v)
    return _NOT_ASSIGNED


def _where_capability_ids(where: str):
    """The id list of an EXACT `capability_id = '..'` / `capability_id in (..)` predicate.

    None for anything else — a capability_name key, a LIKE, or ANY additional AND/OR
    condition. A conditional guard means the statement may not have applied, and a replay
    that assumes it did is precisely the false positive CLAUDE.md rules out.
    """
    w = " ".join(where.split()).strip().rstrip(";").strip()
    m = _WHERE_EQ_RE.match(w)
    if m:
        return [m.group(1)]
    m = _WHERE_IN_RE.match(w)
    if m:
        ids: list[str] = []
        for part in _split_top_level(m.group("ids"), ","):
            q = _PLAIN_ID_RE.match(part)
            if not q:
                return None
            ids.append(q.group(1))
        return ids or None
    return None


def _consume_value_rows(rest: str):
    """Peel the `(..), (..)` row groups off the front of a VALUES tail -> (rows, remainder)."""
    rows: list[str] = []
    i, n = 0, len(rest)
    while True:
        while i < n and rest[i].isspace():
            i += 1
        if i >= n or rest[i] != "(":
            break
        depth, j, in_str, closed = 0, i, False, False
        while j < n:
            ch = rest[j]
            if in_str:
                if ch == "'":
                    if j + 1 < n and rest[j + 1] == "'":
                        j += 2
                        continue
                    in_str = False
            elif ch == "'":
                in_str = True
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    j += 1
                    closed = True
                    break
            j += 1
        if not closed:
            return None
        rows.append(rest[i + 1:j - 1])
        i = j
        while i < n and rest[i].isspace():
            i += 1
        if i < n and rest[i] == ",":
            i += 1
            continue
        break
    if not rows:
        return None
    return rows, rest[i:]


def _apply_statement(stmt: str, values: dict, set_at: dict, deleted_at: dict, idx: int):
    """Replay one statement. None if applied or irrelevant, else an UNPARSED reason string."""
    m = _INSERT_RE.match(stmt)
    if m:
        cols = [c.strip().lower() for c in _split_top_level(m.group("cols"), ",")]
        peeled = _consume_value_rows(m.group("rest"))
        if peeled is None:
            return f"INSERT whose VALUES list is not a plain row form: {stmt[:110]}"
        rows, tail = peeled
        tail = tail.strip().rstrip(";").strip()
        conflict_sets = None
        if tail:
            c = _ON_CONFLICT_RE.match(tail)
            if not c:
                return f"INSERT with an unrecognised conflict clause: {tail[:110]}"
            conflict_sets = c.group("sets")
        for body in rows:
            vals = _split_top_level(body, ",")
            if len(vals) != len(cols):
                return f"INSERT row with {len(vals)} values for {len(cols)} columns: {body[:80]}"
            row = dict(zip(cols, vals))
            q = _PLAIN_ID_RE.match(row.get("capability_id", ""))
            if not q:
                return f"INSERT row whose capability_id is not a plain literal: {body[:80]}"
            cid = q.group(1)
            if "required_state" in row:
                new = _parse_text_array(row["required_state"])
                if new is None:
                    return f"INSERT sets required_state to an unparseable literal for {cid}"
            else:
                # GUESSED: a row inserted WITHOUT a required_state column is replayed as
                # '{}' — the DDL default at 20260728120000:26. The replay does not parse
                # column defaults, so this reads the default off the migration by hand.
                # Not load-bearing today: the only id that reaches VERIFIABLE status
                # (discovery.communities) names required_state explicitly.
                new = set()
            if cid not in values:
                values[cid] = set(new)
                set_at[cid] = idx
                deleted_at.pop(cid, None)
                continue
            if conflict_sets is None:
                continue  # a bare INSERT onto an existing PK errors; nothing to replay
            assigned = _assigned_required_state(conflict_sets)
            if assigned is _NOT_ASSIGNED:
                continue  # DO UPDATE that leaves required_state alone
            if assigned == "excluded":
                values[cid] = set(new)
                set_at[cid] = idx
                continue
            if assigned is None:
                return f"ON CONFLICT sets required_state to an unparseable literal for {cid}"
            values[cid] = set(assigned)
            set_at[cid] = idx
        return None

    m = _UPDATE_RE.match(stmt)
    if m:
        assigned = _assigned_required_state(m.group("sets"))
        if assigned is _NOT_ASSIGNED:
            return None  # touches is_active/description/... — cannot change required_state
        ids = _where_capability_ids(m.group("where"))
        if ids is None:
            return ("UPDATE of required_state whose WHERE is not a bare capability_id predicate "
                    "(conditional guard, LIKE, or a non-id key): where "
                    + " ".join(m.group("where").split())[:120])
        if assigned is None or assigned == "excluded":
            return f"UPDATE sets required_state to an unparseable expression: {m.group('sets')[:110]}"
        for cid in ids:
            if cid in values:
                values[cid] = set(assigned)
                set_at[cid] = idx
        return None

    m = _DELETE_RE.match(stmt)
    if m:
        ids = _where_capability_ids(m.group("where"))
        if ids is None:
            return ("DELETE whose WHERE is not a bare capability_id predicate: where "
                    + " ".join(m.group("where").split())[:120])
        for cid in ids:
            values.pop(cid, None)
            set_at.pop(cid, None)
            deleted_at[cid] = idx
        return None

    if _CAP_WRITE_RE.search(stmt):
        return f"unrecognised capability_index write: {stmt[:110]}"
    if "required_state" in stmt.lower() and not _IGNORABLE_RE.match(stmt):
        return f"unrecognised statement touching required_state: {stmt[:110]}"
    return None


def capability_required_state_replay(migrations_dir: _Path | None = None) -> dict:
    """Replay every capability_index statement the parser understands, in migration order.

    Returns
      values        {id: required_state} after the replay (parseable statements only)
      verifiable    the subset whose last assignment POST-DATES every UNPARSED statement, so
                    nothing unparsed could have rewritten it afterwards
      unverifiable  {id: why we will not vouch for its value}
      deleted       {id: statement index} removed by a parseable DELETE, never re-added, and
                    not shadowed by a later UNPARSED statement
      unparsed      ["<file>: <reason>", ...] — the statements we refused to guess at
    """
    d = migrations_dir or _MIGRATIONS_DIR
    values: dict[str, set[str]] = {}
    set_at: dict[str, int] = {}
    deleted_at: dict[str, int] = {}
    unparsed: list[str] = []
    last_unparsed = -1
    idx = 0
    for path in sorted(d.glob("*.sql")):
        sql = _BLOCK_COMMENT_RE.sub(" ", _LINE_COMMENT_RE.sub(
            "", path.read_text(encoding="utf-8", errors="replace")))
        # A $$-quoted body is opaque to the statement splitter. Flag it only if it MUTATES the
        # table — 20260729120000's matcher function merely selects from it, which is harmless.
        for block in _DOLLAR_BLOCK_RE.finditer(sql):
            if _CAP_WRITE_RE.search(block.group(0)):
                idx += 1
                unparsed.append(f"{path.name}: capability_index write inside a $$-quoted body")
                last_unparsed = idx
        sql = _DOLLAR_BLOCK_RE.sub(" ", sql)
        if "capability_index" not in sql:
            continue
        for raw_stmt in _split_top_level(sql, ";"):
            stmt = " ".join(raw_stmt.split())
            if not stmt or "capability_index" not in stmt.lower():
                continue
            idx += 1
            reason = _apply_statement(stmt, values, set_at, deleted_at, idx)
            if reason:
                unparsed.append(f"{path.name}: {reason}")
                last_unparsed = idx

    verifiable: dict[str, set[str]] = {}
    unverifiable: dict[str, str] = {}
    for cid, req in values.items():
        if set_at.get(cid, -1) > last_unparsed:
            verifiable[cid] = set(req)
        else:
            unverifiable[cid] = (
                f"its last parseable assignment (stmt #{set_at.get(cid, -1)}) predates UNPARSED "
                f"statement #{last_unparsed}, which could have rewritten any row")
    deleted = {cid: i for cid, i in deleted_at.items()
               if cid not in values and i > last_unparsed}
    return {"values": values, "verifiable": verifiable, "unverifiable": unverifiable,
            "deleted": deleted, "unparsed": unparsed}


def capability_drift(migrations_dir: _Path | None = None) -> dict:
    """What this harness's capability mirror gets wrong about the migrations.

      missing                ids the migrations define but this harness does not know
      extra                  ids this harness claims that no migration mentions
      value_mismatch         {id: (real_required_state, ours)} for ids whose required_state
                             the strict replay could VERIFY and which disagree with the mirror
      deleted_but_registered ids a parseable DELETE removed that the mirror still registers
                             (the id-literal scan alone cannot see this: the literal survives
                             inside the DELETE statement itself, so drift reads clean)
      unverified_values      registered ids whose required_state the replay refused to vouch
                             for, because an UNPARSED statement could have changed it
      unparsed               the statements the replay would not guess at, with reasons

    `missing` means we would fail Lana for offering a real capability; `extra` means we would
    let an invented one through as registered; `value_mismatch` means we would gate a real
    capability on the wrong state token, which misfires silently in both directions.
    `unverified_values` / `unparsed` are NOT failures — they are the honest size of the blind
    spot, and selftest.py prints them instead of swallowing them.
    """
    real = capability_ids_in_migrations(migrations_dir)
    ours = set(REGISTERED_CAPABILITIES)
    replay = capability_required_state_replay(migrations_dir)
    mismatch = {
        cid: (sorted(req), sorted(REGISTERED_CAPABILITIES[cid]))
        for cid, req in sorted(replay["verifiable"].items())
        if cid in REGISTERED_CAPABILITIES and set(req) != set(REGISTERED_CAPABILITIES[cid])
    }
    return {
        "missing": real - ours,
        "extra": ours - real,
        "value_mismatch": mismatch,
        "deleted_but_registered": {cid for cid in replay["deleted"] if cid in ours},
        "unverified_values": sorted(ours - set(replay["verifiable"])),
        "unparsed": list(replay["unparsed"]),
    }
