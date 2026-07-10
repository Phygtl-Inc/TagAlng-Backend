#!/usr/bin/env python3
"""Emit demand-triggered host nudges for a block (cron/ops hook).

Finds every unmet need >= 3 distinct moms saved on the block (listening meet_seek
signals), picks the best candidate host per app/host_nudge.py's documented heuristic
(verified · wants it herself · most active), and sends the nudge through the existing
notification machinery (push + email via app.notifications.notify_user). Each send is
persisted to host_nudges, enforcing the one-nudge-per-host-per-7-days cap.

Usage (from services/lana-worker, with the worker's env loaded):
    python -m scripts.emit_host_nudges --block-id 8a2a1072b59ffff
    python -m scripts.emit_host_nudges --block-id 8a2a1072b59ffff --dry-run

Requires SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY (and VAPID/RESEND keys for the
notifications to actually go out — notify_user no-ops cleanly without them).
"""

from __future__ import annotations

import argparse
import sys

from app.host_nudge import emit_host_nudges


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--block-id", required=True, help="H3 block id to scan for demand")
    parser.add_argument("--dry-run", action="store_true", help="report candidates, send nothing")
    args = parser.parse_args()

    results = emit_host_nudges(args.block_id, dry_run=args.dry_run)
    if not results:
        print("no demand pockets due a nudge")
        return 0
    for r in results:
        state = "would nudge" if args.dry_run else ("nudged" if r.get("sent") else "skipped (cap)")
        who = r.get("host_nickname") or r["host_user_id"]
        print(f"{state} · {who} · {r['count']}x '{r['need_label']}' · {r['copy']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
