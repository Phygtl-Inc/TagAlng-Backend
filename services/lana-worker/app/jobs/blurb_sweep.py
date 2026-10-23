"""Run the blurb sweep until there is nothing left to do.

WHY THIS FILE EXISTS

    The cold-start fix for community discovery is DEPLOYED AND NON-FUNCTIONAL.

    Checked in production 2026-10-01:

        places with a blurb ................ 33
        places with blurb_embedding ........ 0

    `discover_communities` has two arms. The member-claim arm works. The blurb arm —
    the one that lets a brand-new community be found at all, on the day a creator
    needs it found — matches on `blurb_embedding`, and nothing has ever written one.

    `community_blurb.sweep()` does the work and has existed since the worker PR. What
    was missing was anything that CALLS it. That is this file.

    This is the same failure that produced five dead mechanisms before it: the
    radar with zero rows, discovery wired to no surface, external identities never
    written, profile_backlink never used, provisional handles never set. Build the
    mechanism, leave the last inch.

HOW TO RUN

    python -m app.jobs.blurb_sweep              # until done
    python -m app.jobs.blurb_sweep --once       # one batch, for a cron tick
    python -m app.jobs.blurb_sweep --dry-run    # report only, writes nothing

    Idempotent and safe to run twice. Each pass picks up whatever is still missing,
    stale, or unembedded, so a partial failure self-heals on the next run.

STILL NEEDS A SCHEDULER HOOK. A blurb goes stale whenever a community is renamed
(trigger sets blurb_stale), so this is not a one-time backfill — it needs to run on a
cadence. Hourly is ample. Wiring it to whatever the worker already uses for periodic
jobs is the one thing this PR does not do, because I do not know which scheduler that
is — flagged for review.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

logger = logging.getLogger(__name__)

# Generous ceiling. A full backfill of 33 places finishes in one pass; the loop exists
# so that a backlog after a bulk rename also drains without hand-holding.
MAX_PASSES = 40
PAUSE_SECONDS = 1.0


def pending_count() -> int:
    """How many places still need a blurb or an embedding."""
    from app.auth import service_client

    try:
        res = (
            service_client()
            .table("places")
            .select("id", count="exact")
            .neq("governance_state", "suspended")
            .or_("blurb.is.null,blurb_stale.is.true,blurb_embedding.is.null")
            .limit(1)
            .execute()
        )
        return res.count or 0
    except Exception:
        logger.exception("blurb_sweep: could not count pending")
        return 0


def run(*, once: bool = False, dry_run: bool = False) -> int:
    """Returns the number of places successfully refreshed."""
    from app import community_blurb

    pending = pending_count()
    logger.info("blurb_sweep: %d places pending", pending)
    if dry_run:
        print(f"{pending} places need a blurb or an embedding. Nothing written.")
        return 0
    if pending == 0:
        return 0

    done = failed = 0
    for attempt in range(MAX_PASSES):
        result = community_blurb.sweep()
        done += result["done"]
        failed += result["failed"]

        # Nothing moved. Either we are finished, or every remaining row is failing for
        # the same reason — and in both cases another pass changes nothing.
        if result["done"] == 0:
            if result["failed"]:
                logger.warning(
                    "blurb_sweep: stopping, %d failures and no progress on pass %d",
                    result["failed"], attempt + 1,
                )
            break
        if once:
            break
        time.sleep(PAUSE_SECONDS)

    remaining = pending_count()
    logger.info("blurb_sweep: done=%d failed=%d remaining=%d", done, failed, remaining)
    print(f"refreshed {done}, failed {failed}, {remaining} still pending")
    return done


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate and embed missing community blurbs.")
    parser.add_argument("--once", action="store_true", help="one batch only")
    parser.add_argument("--dry-run", action="store_true", help="report, write nothing")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        run(once=args.once, dry_run=args.dry_run)
    except Exception:
        logger.exception("blurb_sweep: fatal")
        return 1
    # Exit 0 even with individual failures — a scheduler should not alert because one
    # blurb could not be generated. Real breakage shows as `remaining` not falling,
    # which the log line above makes visible.
    return 0


if __name__ == "__main__":
    sys.exit(main())
