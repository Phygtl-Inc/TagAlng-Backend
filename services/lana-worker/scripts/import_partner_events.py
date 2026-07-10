#!/usr/bin/env python3
"""Import partner-sourced anchor events (library storytime, YMCA swim, …) into `events`.

Reads a simple JSON or CSV file of occurrences and upserts them with source='partner' +
source_name attribution ("via Lake Nona Library" in previews). Idempotent by
(source_name, title, starts_at): re-running the same file inserts nothing and only
patches rows whose description/venue/ends_at/tags changed.

Usage (from services/lana-worker, with the worker's env loaded):
    python -m scripts.import_partner_events partners.json --host-id <uuid>
    python -m scripts.import_partner_events partners.csv --block-id 8a2a1072b59ffff --dry-run

File rows (JSON: a list of objects; CSV: a header row with the same names):
    title, starts_at (ISO; naive = event-local wall clock), source_name   REQUIRED
    ends_at, description, venue_name (defaults to source_name),
    cohort_tags ("parents|family"), block_id, cluster_id                  optional

--host-id (or PARTNER_EVENTS_HOST_ID env) is the house account that owns imports —
events.host_id is not null. Requires SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY.
NOTE: the events table has no is_test column on this branch, so the importer sets none.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from pathlib import Path

from app.partner_events import normalize_partner_items, upsert_partner_events


def _read_items(path: Path) -> list[dict]:
    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8") as fh:
            return [dict(row) for row in csv.DictReader(fh)]
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):  # tolerate {"events": [...]}
        data = data.get("events") or []
    if not isinstance(data, list):
        raise ValueError("JSON file must be a list of event objects")
    return [r for r in data if isinstance(r, dict)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path, help="JSON or CSV file of partner events")
    parser.add_argument(
        "--host-id",
        default=os.environ.get("PARTNER_EVENTS_HOST_ID", ""),
        help="house user id that owns imported events (or PARTNER_EVENTS_HOST_ID env)",
    )
    parser.add_argument("--block-id", default=None, help="default block for rows without one")
    parser.add_argument("--cluster-id", default="lake-nona", help="default cluster_id")
    parser.add_argument("--dry-run", action="store_true", help="report the plan, write nothing")
    args = parser.parse_args()

    host_id = str(args.host_id or "").strip()
    if not host_id:
        print("error: --host-id (or PARTNER_EVENTS_HOST_ID) is required", file=sys.stderr)
        return 2

    items = _read_items(args.file)
    rows, problems = normalize_partner_items(
        items, default_block_id=args.block_id, default_cluster_id=args.cluster_id
    )
    for problem in problems:
        print(f"skipped · {problem}", file=sys.stderr)
    if not rows:
        print("nothing to import")
        return 0
    missing_block = sum(1 for r in rows if not r.get("block_id"))
    if missing_block:
        print(
            f"warning: {missing_block} row(s) have no block_id — they won't surface in "
            "block feeds (pass --block-id or add it per row)",
            file=sys.stderr,
        )

    stats = upsert_partner_events(rows, host_id=host_id, dry_run=args.dry_run)
    mode = "dry-run · would be " if args.dry_run else ""
    print(
        f"{mode}inserted={stats['inserted']} updated={stats['updated']} "
        f"unchanged={stats['unchanged']} (skipped={len(problems)})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
