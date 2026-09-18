"""local_world.py — pin a scenario's WORLD into a LOCAL Supabase stack, for real.

WHY
---
`decide_turn(user_id, ...)` does not take a world; it reads one (app/policy/world.py:105).
So a policy_eval scenario that pins "quiet area, unverified, no confirmed circle" is, on the
`inproc` backend, silently evaluated against whatever the sim account's REAL state is. The
harness is honest about that — `checks.check_world_fidelity` scores it UNSCORED — but UNSCORED
is a hole in the run, not a result. The two escapes that existed were:

  * `SIM_INPROC_INJECT_WORLD=1`  — monkeypatch `world_state`. The decision is real, the world
    is fabricated. Every report line says HARNESS-SUPPLIED WORLD, and it must, because the DB
    reads that decide_turn does BESIDE world_state (goals, claims, capability rows) still see
    the account's real, contradictory state.
  * nothing else. live_policy.py's docstring says outright: "There is deliberately no third
    option where the harness writes the account's real world into the DB. That is seeding a
    shared account from an eval."

That objection is about the account being SHARED, not about seeding being wrong. On a LOCAL
stack — nobody else's rows, disposable, re-creatable from `supabase start` + migrations +
seed_sim_accounts.sql — writing the world is strictly better than injecting it: the whole of
decide_turn then reads ONE consistent world, including the reads injection cannot reach.

So this module is the third option, and it is available ONLY on a local stack. Every entry
point calls `local_guard.require_local()` first (and honours the pre-existing
`SIM_ALLOW_WRITES=1` gate on top). Against the shared dev project it refuses; the escape hatch
is `SIM_ALLOW_NONLOCAL_WRITES=1`, which prints a banner.

WHAT IT CAN PIN
---------------
Exactly the inputs `world_state()` derives its `states` vocabulary from — no more, so that a
pinned world and an observed world are the same object:

    zip_unlock.unlock_state (+count/threshold)   -> area.state, the `zip_open` token
    users.home_zip                               -> `has_home_zip`, and whether an area exists
    users.phone_verified_at / email_verified_at  -> the `verified` token
    users.role, users.grammatical_gender         -> the lingo constitution's address inputs
    users.locale                                 -> prompt language
    circle_affiliations (status='confirmed')     -> `has_circle`, and the offer's concreteness

REVERSIBILITY
-------------
Rows this module creates are stamped `detail = SEED_MARKER` (circles) / `google_place_id`
prefixed `simworld:` (places), so `teardown()` deletes exactly what it made and nothing else.
`zip_unlock` and `users` are UPDATED, not created, so they cannot be "un-seeded" — teardown
restores nothing there. That is deliberate and stated: on a local stack the restore path is
`supabase db reset`, which is cheaper and more trustworthy than a bookkeeping snapshot.

USAGE (library)
    from local_world import apply_world
    note = apply_world(user_id, scenario.world)     # -> dict summary, raises if not local

USAGE (cli)
    SIM_ALLOW_WRITES=1 python local_world.py --user <uuid> --zip 32832 --unlock closed \
        --role parent --gender feminine --verified --circle fitness:Life\\ Time
    SIM_ALLOW_WRITES=1 python local_world.py --teardown --user <uuid>
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

_SIMS = Path(__file__).resolve().parent
if str(_SIMS) not in sys.path:
    sys.path.insert(0, str(_SIMS))

load_dotenv(_SIMS.parents[2] / ".env.local", override=True)  # simulations -> lana-worker -> services -> repo root

from local_guard import require_local  # noqa: E402

SEED_MARKER = "policy-sim-world"          # circle_affiliations.detail teardown key
PLACE_ID_PREFIX = "simworld:"             # places.google_place_id namespace
_TIMEOUT = 30

# circle_affiliations.circle_key CHECK, verbatim from 20260906120000:103.
_CIRCLE_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_KEY_PREFIX = "simworld_"

# circle_affiliations.circle_type CHECK (20260906120000:100).
CIRCLE_TYPES = frozenset({
    "school", "faith", "fitness", "kids_activity", "neighborhood",
    "hobby", "support", "heritage", "friends", "other",
})
# zip_unlock.unlock_state CHECK (20260906120000:204).
UNLOCK_STATES = ("closed", "warming", "open")
# users.role / users.grammatical_gender CHECKs (20260909120000:14-19).
USER_ROLES = frozenset({"parent", "expecting", "grandparent", "caregiver", "guardian", "relative"})
GENDERS = frozenset({"feminine", "masculine"})

# GUESSED: verified_active_count filler per unlock state. Nothing in the policy prompt reads it
# as more than colour (world.py:143 passes it straight through), and the REAL number is derived
# by recount_zip_unlock from actual users — which a pinned world has no way to conjure. Kept
# identical to live_policy._AREA_FILLER so the injected and seeded paths describe the same area.
_AREA_FILLER = {"closed": 1, "warming": 6, "open": 14}
_DEFAULT_THRESHOLD = 10   # the real column default (20260906120000:205)


def _url() -> str:
    return os.environ.get("SUPABASE_URL", "").rstrip("/")


def _key() -> str:
    return os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")


def _headers(extra: dict | None = None) -> dict:
    h = {"apikey": _key(), "Authorization": f"Bearer {_key()}",
         "Content-Type": "application/json", "Prefer": "return=representation"}
    if extra:
        h.update(extra)
    return h


def _guard(operation: str) -> None:
    """The two gates, in order: writes-allowed, then target-is-local.

    SIM_ALLOW_WRITES is checked FIRST and independently — this module layers on top of the
    suite's existing write gate rather than replacing it, so turning the local guard off (via
    SIM_ALLOW_NONLOCAL_WRITES) still cannot make an un-gated write happen.
    """
    if os.environ.get("SIM_ALLOW_WRITES", "").strip() != "1":
        raise RuntimeError(f"Refusing to {operation}: set SIM_ALLOW_WRITES=1.")
    if not _url() or not _key():
        raise RuntimeError(
            f"Refusing to {operation}: SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY must be set "
            "(see simulations/LOCAL_STACK.md).")
    require_local(operation)


def _post(table: str, rows: list[dict], *, params: dict | None = None,
          prefer: str | None = None) -> list[dict]:
    with httpx.Client(timeout=_TIMEOUT) as c:
        r = c.post(f"{_url()}/rest/v1/{table}",
                   headers=_headers({"Prefer": f"return=representation,{prefer}"} if prefer else None),
                   params=params or {}, json=rows)
        r.raise_for_status()
        return r.json() if r.content else []


def _patch(table: str, params: dict, patch: dict) -> list[dict]:
    with httpx.Client(timeout=_TIMEOUT) as c:
        r = c.patch(f"{_url()}/rest/v1/{table}", headers=_headers(), params=params, json=patch)
        r.raise_for_status()
        return r.json() if r.content else []


def _delete(table: str, params: dict) -> int:
    with httpx.Client(timeout=_TIMEOUT) as c:
        r = c.delete(f"{_url()}/rest/v1/{table}", headers=_headers(), params=params)
        r.raise_for_status()
        return len(r.json()) if r.content else 0


def _circle_key_for(label: str) -> str:
    """Slugify a circle label into a legal circle_key (see the CHECK at 20260906120000:103).
    Same shape as circles_zip/live_seed._circle_key_for — kept separate because the marker and
    the prefix differ, and a shared prefix would make the two tools' teardowns overlap."""
    slug = re.sub(r"[^a-z0-9_]+", "_", label.strip().lower()).strip("_")
    room = 64 - len(_KEY_PREFIX)
    key = _KEY_PREFIX + (slug[-room:] if len(slug) > room else slug)
    if not _CIRCLE_KEY_RE.match(key):
        raise ValueError(f"cannot build a legal circle_key from {label!r} (got {key!r})")
    return key


# ---------------------------------------------------------------------------
# The individual pins
# ---------------------------------------------------------------------------

def pin_zip_unlock(zip5: str, unlock_state: str, *, verified_active_count: int | None = None,
                   unlock_threshold: int = _DEFAULT_THRESHOLD) -> dict:
    """Force `zip_unlock.unlock_state` for a ZIP. Creates the row if absent.

    NOTE this deliberately writes the state DIRECTLY rather than going through
    recount_zip_unlock(): the recount derives the state from real verified-active users, which
    a pinned world has none of, so calling it would immediately undo the pin.
    """
    if unlock_state not in UNLOCK_STATES:
        raise ValueError(f"unlock_state must be one of {UNLOCK_STATES}, got {unlock_state!r}")
    if not re.fullmatch(r"\d{5}", zip5 or ""):
        raise ValueError(f"zip5 must be 5 digits (table CHECK), got {zip5!r}")
    _guard(f"set zip_unlock.unlock_state={unlock_state!r} for ZIP {zip5}")
    count = _AREA_FILLER.get(unlock_state, 0) if verified_active_count is None else verified_active_count
    row = {
        "zip5": zip5,
        "unlock_state": unlock_state,
        "verified_active_count": count,
        "unlock_threshold": unlock_threshold,
        # `open` without opened_at is a state the product never produces; keep it coherent.
        "opened_at": _now_iso() if unlock_state == "open" else None,
    }
    out = _post("zip_unlock", [row], params={"on_conflict": "zip5"},
                prefer="resolution=merge-duplicates")
    print(f"  [world] zip_unlock {zip5} -> {unlock_state} (count={count}/{unlock_threshold})")
    return (out or [row])[0]


def pin_user(user_id: str, *, home_zip: str | None = ..., role: str | None = ...,
             grammatical_gender: str | None = ..., locale: str | None = ...,
             verified: bool | None = None) -> dict:
    """Set the `users` columns world_state() reads. `...` means "leave alone"; None means
    "write SQL NULL" — the distinction matters, because clearing home_zip is how a scenario
    pins a rootless user and clearing both verified_at columns is how it pins an unverified one.
    """
    patch: dict[str, Any] = {}
    if home_zip is not ...:
        if home_zip is not None and not re.fullmatch(r"\d{5}", home_zip):
            raise ValueError(f"home_zip must be 5 digits or None, got {home_zip!r}")
        patch["home_zip"] = home_zip
    if role is not ...:
        if role is not None and role not in USER_ROLES:
            raise ValueError(f"users.role CHECK allows {sorted(USER_ROLES)} or null, got {role!r}")
        patch["role"] = role
    if grammatical_gender is not ...:
        if grammatical_gender is not None and grammatical_gender not in GENDERS:
            raise ValueError(
                f"users.grammatical_gender CHECK allows {sorted(GENDERS)} or null, "
                f"got {grammatical_gender!r}")
        patch["grammatical_gender"] = grammatical_gender
    if locale is not ...:
        patch["locale"] = locale
    if verified is not None:
        # world.py:130 — `verified` holds if EITHER timestamp is set, so pinning False must
        # clear BOTH. Clearing email_verified_at is exactly why this is local-only: on the
        # shared project it would knock a sim account back through the auth gate for everyone.
        stamp = _now_iso() if verified else None
        patch["email_verified_at"] = stamp
        patch["phone_verified_at"] = stamp
    if not patch:
        return {}
    _guard(f"update public.users {sorted(patch)} for user {user_id}")
    out = _patch("users", {"id": f"eq.{user_id}"}, patch)
    if not out:
        raise RuntimeError(
            f"users PATCH matched no row for id={user_id!r} — is the local stack seeded? "
            "(psql -f supabase/seed_sim_accounts.sql; see LOCAL_STACK.md)")
    print(f"  [world] users {user_id} <- {patch}")
    return out[0]


def _pin_place(name: str, *, place_type: str | None = None, zip5: str | None = None) -> str:
    """Upsert a namespaced place so a circle can be GROUNDED (circle_affiliations.grounded is a
    generated column — `place_ref is not null` — so grounding is not directly settable)."""
    gpid = PLACE_ID_PREFIX + re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    row = {"google_place_id": gpid, "name": name, "source": "user_grounded"}
    if place_type in CIRCLE_TYPES:
        row["place_type"] = place_type
    if zip5 and re.fullmatch(r"\d{5}", zip5):
        row["zip"] = zip5
    out = _post("places", [row], params={"on_conflict": "google_place_id"},
                prefer="resolution=merge-duplicates")
    return str(out[0]["id"])


def pin_circles(user_id: str, circles: list[dict]) -> list[dict]:
    """Replace this user's HARNESS-SEEDED circles with exactly `circles`.

    Each entry: {"circle_type": <one of CIRCLE_TYPES>, "place_name": str|None,
                 "confirmed": bool}. A place_name grounds the row (a `places` row is upserted).

    Only rows stamped with SEED_MARKER are removed — a user's organically-created circles are
    left alone, so this never destroys state some other part of the run depends on.
    """
    _guard(f"replace seeded circle_affiliations for user {user_id}")
    removed = _delete("circle_affiliations",
                      {"user_id": f"eq.{user_id}", "detail": f"eq.{SEED_MARKER}"})
    rows = []
    for i, c in enumerate(circles):
        ctype = str(c.get("circle_type") or "other")
        if ctype not in CIRCLE_TYPES:
            # FLAGGED: WorldState.Community carries the user's own framing, which the policy
            # harness allows to be free text (world_state.with_confirmed_circle defaults to
            # "gym"), but the DB CHECK does not. Map rather than 400 mid-batch.
            ctype = "fitness" if ctype in ("gym", "fitness_centre") else "other"
        place_name = c.get("place_name")
        rows.append({
            "user_id": user_id,
            "circle_type": ctype,
            "circle_key": _circle_key_for(f"{ctype}_{place_name or i}"),
            "place_ref": _pin_place(str(place_name), place_type=ctype) if place_name else None,
            "status": "confirmed" if c.get("confirmed", True) else "suggested",
            "source": "profile_add",
            "confidence": 1.0,
            "detail": SEED_MARKER,
        })
    created = _post("circle_affiliations", rows) if rows else []
    print(f"  [world] circle_affiliations for {user_id}: -{removed} seeded, +{len(created)}")
    return created


# ---------------------------------------------------------------------------
# The whole world, from a policy_eval WorldState
# ---------------------------------------------------------------------------

def world_pin_plan(world: Any) -> dict[str, Any]:
    """Pure: a policy_eval `ports.WorldState` -> the exact writes apply_world would make.

    Split out from apply_world so the mapping is unit-testable with no DB and no creds — the
    selftest asserts the plan for each fixture world, which is the only way to know the seeded
    world would carry the same state tokens the checks compare against.
    """
    home_zip = getattr(world, "home_zip", None)
    unlock = str(getattr(world, "zip_unlock_state", "closed") or "closed")
    return {
        "user": {
            "home_zip": home_zip,
            "role": getattr(world, "role", None),
            "grammatical_gender": getattr(world, "grammatical_gender", None),
            "locale": getattr(world, "locale", None),
            "verified": bool(getattr(world, "verified", False)),
        },
        # No home ZIP -> no area at all (world.py:113 keys the snapshot off users.home_zip), so
        # there is nothing to pin and pinning one would create an area the user cannot see.
        "zip_unlock": None if not home_zip else {"zip5": home_zip, "unlock_state": unlock},
        "circles": [
            {"circle_type": getattr(c, "circle_type", "other"),
             "place_name": getattr(c, "place_name", None),
             "confirmed": bool(getattr(c, "confirmed", False))}
            for c in (getattr(world, "communities", None) or [])
        ],
    }


def apply_world(user_id: str, world: Any) -> dict[str, Any]:
    """Write a scenario's WorldState into the (local) DB for `user_id`, then report what was
    pinned. Raises local_guard.NonLocalTargetError unless the target is a local stack."""
    plan = world_pin_plan(world)
    _guard(f"pin a scenario world onto user {user_id}")
    pin_user(user_id, home_zip=plan["user"]["home_zip"], role=plan["user"]["role"],
             grammatical_gender=plan["user"]["grammatical_gender"],
             locale=plan["user"]["locale"], verified=plan["user"]["verified"])
    if plan["zip_unlock"]:
        pin_zip_unlock(plan["zip_unlock"]["zip5"], plan["zip_unlock"]["unlock_state"])
    pin_circles(user_id, plan["circles"])
    return plan


def teardown(user_id: str | None = None) -> int:
    """Delete every circle_affiliations row this module created (optionally for one user).

    zip_unlock / users are UPDATES with no snapshot — `supabase db reset` is the restore path.
    """
    _guard("delete seeded circle_affiliations")
    params = {"detail": f"eq.{SEED_MARKER}"}
    if user_id:
        params["user_id"] = f"eq.{user_id}"
    n = _delete("circle_affiliations", params)
    print(f"[teardown] deleted {n} seeded circle_affiliations"
          + (f" for {user_id}" if user_id else ""))
    return n


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Pin world state into a LOCAL Supabase stack (evals/dev only).")
    ap.add_argument("--user", help="user_id to pin state onto")
    ap.add_argument("--zip", dest="zip5", help="home ZIP (5 digits); '' clears it")
    ap.add_argument("--unlock", choices=UNLOCK_STATES, help="zip_unlock.unlock_state for --zip")
    ap.add_argument("--role", choices=sorted(USER_ROLES))
    ap.add_argument("--gender", choices=sorted(GENDERS))
    ap.add_argument("--locale")
    ap.add_argument("--verified", dest="verified", action="store_true", default=None)
    ap.add_argument("--unverified", dest="verified", action="store_false")
    ap.add_argument("--circle", action="append", default=[], metavar="TYPE[:PLACE]",
                    help="confirmed circle, e.g. fitness:Life Time (repeatable)")
    ap.add_argument("--teardown", action="store_true", help="delete rows this tool created")
    args = ap.parse_args()

    if args.teardown:
        teardown(args.user)
        return 0
    if not args.user:
        ap.error("--user is required unless --teardown")

    kw: dict[str, Any] = {}
    if args.zip5 is not None:
        kw["home_zip"] = args.zip5 or None
    if args.role:
        kw["role"] = args.role
    if args.gender:
        kw["grammatical_gender"] = args.gender
    if args.locale:
        kw["locale"] = args.locale
    if args.verified is not None:
        kw["verified"] = args.verified
    if kw:
        pin_user(args.user, **kw)
    if args.zip5 and args.unlock:
        pin_zip_unlock(args.zip5, args.unlock)
    if args.circle:
        circles = []
        for spec in args.circle:
            ctype, _, place = spec.partition(":")
            circles.append({"circle_type": ctype.strip(), "place_name": place.strip() or None,
                            "confirmed": True})
        pin_circles(args.user, circles)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
