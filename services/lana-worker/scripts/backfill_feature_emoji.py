#!/usr/bin/env python3
"""Backfill place_features.emoji for rows written without one (backend-asks §24(b)).

Only the panel's add path (/lana/circles/features/add) ever picked a glyph. Every other
writer — features learned in chat, features parked before grounding, the answers a
creator gave on community create — wrote NULL, so a live profile rendered "Charging
station" bare beside glyphed chips. The worker now picks one for every row it writes and
heals any bare row a profile read serves (app/place_activities.py); this script does the
same for every bare row at once instead of waiting for each profile to be opened.

The glyph is the model's pick, through the SAME function the live paths use
(place_activities.fill_feature_emoji -> feature_emoji). There is no word->emoji table:
the set of things a place can have is open-ended (issues #77 ruled out local mapping).

Each write is conditional on the column still being NULL, so a glyph somebody chose in
the meantime is never overwritten, and re-running is safe.

Usage (from services/lana-worker, with the worker's env loaded — it calls the LLM and
writes to whichever database SUPABASE_URL names, so CHECK WHICH ONE before running):
    python -m scripts.backfill_feature_emoji --dry-run      # list bare rows + labels
    python -m scripts.backfill_feature_emoji                # pick and write
    python -m scripts.backfill_feature_emoji --limit 50
    python -m scripts.backfill_feature_emoji --place <uuid>

Requires: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY and the worker's LLM env.
"""

from __future__ import annotations

import argparse
import sys

from app.auth import service_client
from app.place_activities import _feature_chip_label, fill_feature_emoji

_PAGE = 500


def bare_rows(*, place_id: str | None = None, limit: int | None = None) -> list[dict]:
    """Every place_features row with no emoji, oldest first."""
    out: list[dict] = []
    offset = 0
    while True:
        q = (
            service_client()
            .table("place_features")
            .select("id, place_id, key, value, sub_group, label, emoji")
            .is_("emoji", "null")
            .order("created_at")
        )
        if place_id:
            q = q.eq("place_id", place_id)
        page = q.range(offset, offset + _PAGE - 1).execute().data or []
        out.extend(r for r in page if isinstance(r, dict))
        if limit and len(out) >= limit:
            return out[:limit]
        if len(page) < _PAGE:
            return out
        offset += _PAGE


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="list, pick nothing, write nothing")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--place", default=None, help="only this place id")
    args = ap.parse_args(argv)

    rows = bare_rows(place_id=args.place, limit=args.limit)
    print(f"{len(rows)} feature row(s) without an emoji")
    filled = skipped = 0
    for r in rows:
        label = _feature_chip_label(r)
        if args.dry_run:
            print(f"  {r['id']}  place={r.get('place_id')}  {label!r}")
            continue
        try:
            emoji = fill_feature_emoji(str(r["id"]))
        except Exception as exc:  # noqa: BLE001 — one bad row must not stop the run
            print(f"  ! {r['id']} {label!r}: {exc}", file=sys.stderr)
            skipped += 1
            continue
        if emoji:
            filled += 1
            print(f"  {emoji}  {label}")
        else:
            skipped += 1
            print(f"  -   {label}  (no pick: model unavailable or declined)")
    if not args.dry_run:
        print(f"filled {filled}, left bare {skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
