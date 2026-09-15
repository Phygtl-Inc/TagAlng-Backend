"""
live_seed.py — seed a controlled circle-overlap scenario into the dev DB so the LIVE onion
matcher can be scored + diffed against the stub. Service-role (from .env.local), WRITE-gated,
namespaced, and reversible.

SAFETY (read before running):
  * Writes ONLY happen with SIM_ALLOW_WRITES=1. Every call refuses otherwise.
  * DEV ONLY. Never point this at prod (SUPABASE_URL must be the dev project). Asjid's rule:
    never seed prod with synthetic data.
  * Reversible: every seeded row is tagged `detail = SEED_MARKER`; `teardown()` deletes exactly
    those. Run `--teardown` when done.
  * UNTESTED end-to-end until ONION_RPC_BUG.md is fixed (the RPC throws today), so run
    `--teardown` first if a prior partial run left rows.

SCOPE: this seeds `circle_affiliations` only — that is exactly what the MATCHER needs (candidates
are confirmed + grounded + non-dismissed rows; the score keys on shared place_ref (+3) / circle_type
(+1) / shared concepts). It seeds circles for **existing** user_ids you pass in (defaults to the
sim personas), because `public.users.id` is FK-coupled to `auth.users` — creating brand-new
synthetic users needs the Supabase Auth Admin API (`POST /auth/v1/admin/users`), which is out of
scope here. The concept arm (+1/concept) and the ZIP recount (verified-active = phone_verified +
recent lana_session + a confirmed thing) need extra rows (claim_concept_links / lana_sessions /
intros) whose exact NOT-NULL columns should be confirmed against the live schema before seeding —
noted as a follow-up, not built here.

USAGE:
  export SIM_ALLOW_WRITES=1
  python live_seed.py --list                       # show current seeded rows
  python live_seed.py --seed P1 P2 P3              # give these users a shared place + type
  # ... fix the RPC, then: SIM_BACKEND=live python sweep.py (or score P1 and expect P2/P3) ...
  python live_seed.py --teardown                   # delete everything this tool created
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import httpx

from live_impl import _ALLOW_WRITES, _URL, _headers, _require_creds, _select

SEED_MARKER = "circles-sim-seed"  # stamped on circle_affiliations.detail for safe teardown
_SIMS = Path(__file__).resolve().parents[1]  # services/lana-worker/simulations
_TIMEOUT = 30

# circle_affiliations.circle_key CHECK, verbatim from
# 20260906120000_circles_places_phase_a.sql:103 — `circle_key ~ '^[a-z][a-z0-9_]{1,63}$'`.
# Postgres REJECTS the insert otherwise, so the key can never be built from an arbitrary
# label: `--seed <raw-uuid>` (which _persona_user_ids explicitly allows) would have produced
# `simseed_gym_3f9a-…`, and hyphens are not in the character class.
_CIRCLE_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_KEY_PREFIX = "simseed_fitness_"  # matches circle_type='fitness' below — keep them in sync


def _circle_key_for(label: str) -> str:
    """Slugify an arbitrary --seed label (persona id OR raw uuid) into a legal circle_key.

    Lowercase, every non-[a-z0-9_] run collapsed to '_', prefixed so the result always starts
    with a letter (the regex's first-char rule) and stays namespaced/greppable, then truncated
    to the 64-char limit. Uniqueness per user is what the partial unique index
    `(user_id, circle_key) where dismissed_at is null` needs; each seeded user gets one row, so
    a per-label key is enough — but the truncation keeps the label's TAIL, since uuids differ
    at the front only by a few chars and colliding heads would be silently rejected as dupes."""
    slug = re.sub(r"[^a-z0-9_]+", "_", label.strip().lower()).strip("_")
    room = 64 - len(_KEY_PREFIX)
    key = _KEY_PREFIX + (slug[-room:] if len(slug) > room else slug)
    if not _CIRCLE_KEY_RE.match(key):  # fail loudly rather than let Postgres 400 mid-batch
        raise SystemExit(f"cannot build a legal circle_key from {label!r} (got {key!r})")
    return key


def _guard_writes() -> None:
    _require_creds()
    if not _ALLOW_WRITES:
        raise RuntimeError("Refusing to write: set SIM_ALLOW_WRITES=1 (dev DB only).")


def _insert(table: str, rows: list[dict]) -> list[dict]:
    _guard_writes()
    with httpx.Client(timeout=_TIMEOUT) as c:
        r = c.post(f"{_URL}/rest/v1/{table}",
                   headers=_headers({"Prefer": "return=representation"}), json=rows)
        r.raise_for_status()
        return r.json()


def _delete(table: str, filters: dict) -> int:
    _guard_writes()
    with httpx.Client(timeout=_TIMEOUT) as c:
        r = c.delete(f"{_URL}/rest/v1/{table}",
                     headers=_headers({"Prefer": "return=representation"}), params=filters)
        r.raise_for_status()
        return len(r.json())


def _persona_user_ids(labels: list[str]) -> dict[str, str]:
    """Resolve persona ids (P1..P6) → real user_ids from personas.json."""
    data = json.loads((_SIMS / "personas.json").read_text(encoding="utf-8"))
    by_id = {p["id"]: p.get("profile", {}).get("user_id") for p in data["personas"]}
    out = {}
    for lab in labels:
        uid = by_id.get(lab.upper(), lab)  # allow raw uuids too — see _circle_key_for, which
        # is what makes that legal: the label reaches circle_key, and a raw uuid is not a
        # legal circle_key on its own.
        if not uid:
            raise SystemExit(f"no user_id for {lab!r} (personas.json may hold stub ids)")
        out[lab] = uid
    return out


def _an_existing_place_ref() -> str:
    """Reuse a real grounded place so the place_ref FK holds (no need to mint a places row)."""
    rows = _select("places", {"select": "id", "limit": "1"})
    if not rows:
        raise SystemExit("no rows in `places` to attach — seed a place first or pass one in.")
    return rows[0]["id"]


def seed_scenario(user_labels: list[str]) -> list[dict]:
    """Give every passed user a confirmed+grounded circle at the SAME place + SAME type, so the
    matcher scores each pair at +3 (place) — enough to prove the circle arm end-to-end. Returns
    the created rows."""
    users = _persona_user_ids(user_labels)
    place_ref = _an_existing_place_ref()
    rows = [{
        "user_id": uid,
        # MUST be one of the 10 values in circle_affiliations' CHECK constraint
        # (20260906120000_circles_places_phase_a.sql): school | faith | fitness | kids_activity |
        # neighborhood | hobby | support | heritage | friends | other.  "gym" is NOT valid and the
        # insert would be rejected by Postgres.
        "circle_type": "fitness",                  # shared type (place already gives the +3)
        "circle_key": _circle_key_for(lab),         # slugified — see _circle_key_for (the raw
        # label cannot be used: a uuid's hyphens fail the ^[a-z][a-z0-9_]{1,63}$ CHECK). The
        # prefix says "fitness", matching circle_type above — it used to say "gym", which is
        # not even a legal circle_type value.
        "place_ref": place_ref,                    # SHARED → +3
        "status": "confirmed",
        "source": "profile_add",
        "confidence": 1.0,
        "detail": SEED_MARKER,                      # teardown key
    } for lab, uid in users.items()]
    created = _insert("circle_affiliations", rows)
    print(f"[seed] inserted {len(created)} circle_affiliations at place {place_ref} for {list(users)}")
    return created


def list_seeded() -> list[dict]:
    return _select("circle_affiliations",
                   {"detail": f"eq.{SEED_MARKER}", "select": "id,user_id,circle_type,place_ref,status"})


def teardown() -> int:
    n = _delete("circle_affiliations", {"detail": f"eq.{SEED_MARKER}"})
    print(f"[teardown] deleted {n} seeded circle_affiliations")
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description="Seed/teardown a live circle-overlap scenario (dev only).")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--seed", nargs="+", metavar="USER", help="persona ids (P1..) or raw user_ids")
    g.add_argument("--teardown", action="store_true")
    g.add_argument("--list", action="store_true")
    args = ap.parse_args()
    if args.list:
        for r in list_seeded():
            print(" ", r)
    elif args.teardown:
        teardown()
    else:
        seed_scenario(args.seed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
