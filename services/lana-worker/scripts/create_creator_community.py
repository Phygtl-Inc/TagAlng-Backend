#!/usr/bin/env python3
"""Create a creator community (20261207120000).

A creator community is a place with a name and no geography — see the migration for why
that, rather than a place-less community. There is no endpoint for this yet: the link-in-bio
onboarding does not exist, and Rule 1 of the contract is migrations-only, no dashboard edits.
So this is how one gets made until a creator can make their own.

Usage (from services/lana-worker, with the worker's env loaded):
    python -m scripts.create_creator_community --handle ironman_official --name "Iron Man Training"
    python -m scripts.create_creator_community --handle ironman_official --name "..." --dry-run

The handle is the creator's, and it becomes the place's key as `creator:<handle>` — unique,
stable, and readable in a query, which a generated uuid is not. Re-running with the same
handle reports the existing row rather than making a second one: two places for one creator
would split the roster in half with no error anywhere.
"""

from __future__ import annotations

import argparse
import re
import sys

from app.auth import service_client

_HANDLE = re.compile(r"^[a-zA-Z0-9._]{1,64}$")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--handle", required=True, help="the creator's handle, without @")
    ap.add_argument("--name", required=True, help="what members see, e.g. 'Iron Man Training'")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    handle = args.handle.strip().lstrip("@")
    if not _HANDLE.match(handle):
        print(f"bad handle: {handle!r} (letters, digits, dot, underscore)", file=sys.stderr)
        return 2

    key = f"creator:{handle}"
    name = args.name.strip()
    if not name:
        print("name is required", file=sys.stderr)
        return 2

    sb = service_client()
    existing = (
        sb.table("places").select("id, name, place_type")
        .eq("google_place_id", key).limit(1).execute().data
    )
    if existing:
        row = existing[0]
        print(f"already exists: {row['id']}  {row['name']!r}  type={row['place_type']}")
        return 0

    if args.dry_run:
        print(f"would create: google_place_id={key!r} name={name!r} place_type='creator'")
        return 0

    created = sb.table("places").insert({
        "google_place_id": key,
        "name": name,
        "place_type": "creator",
        # lat/lng/zip/h3 stay null — the CHECK requires it, and it is what keeps this out
        # of discover_communities_near.
        "source": "import",
    }).execute().data[0]

    print(f"created {created['id']}  {name!r}  ({key})")
    print("members join it like any other community; it will not appear in Communities Near Me.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
