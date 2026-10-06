#!/usr/bin/env python3
"""Backfill places.blurb_embedding for every community that has a blurb and no vector.

Nothing wrote blurb_embedding before app/community_embeddings.py: on prod (2026-10-07) 36
places had a blurb and 0 had an embedding, so no community could be found by meaning. The
worker now fills NULLs on every community search (community_embeddings.kick); this script
does the whole backlog in one go instead of waiting for searches to drain it.

Usage (from services/lana-worker, with the worker's env loaded):
    python -m scripts.backfill_community_embeddings --dry-run   # list what would be embedded
    python -m scripts.backfill_community_embeddings             # embed every NULL

Requires: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, GCP_VERTEX_PROJECT.
"""

from __future__ import annotations

import argparse
import sys

from app.community_embeddings import _write, embedding_text, missing


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=1000)
    args = ap.parse_args(argv)

    rows = missing(args.limit)
    print(f"{len(rows)} communities with a blurb and no embedding")
    written = failed = 0
    for row in rows:
        text = embedding_text(row.get("name"), row.get("blurb"))
        print(f"- {row['id']}  {text[:110]!r}")
        if args.dry_run:
            continue
        try:
            if _write(row):
                written += 1
        except Exception as exc:  # noqa: BLE001 — report and carry on
            failed += 1
            print(f"  FAILED: {exc}", file=sys.stderr)
    if not args.dry_run:
        print(f"written={written} failed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
