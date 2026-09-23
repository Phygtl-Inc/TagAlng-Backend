#!/usr/bin/env python3
"""Backfill reco_subjects / local_signals.subject_ref for tips posted before 20261219120000.

Every recommendation captured before that migration has subject_ref=NULL, so three
neighbours who all recommend Dr. Sarah stay three unrelated rows and the subject-grouped
read has nothing to group. Run this once after pushing the migration.

Two passes, mirroring the live path:
  1. PLACES  — the groundable types, searched and accepted only above MATCH_FLOOR.
  2. IDENTITY — everything left with no place, plus every product (a SKU is never a map
     point), scored against existing ungrounded subjects and adjudicated in the middle band.

recipe, diy and other are skipped permanently and by design — their captured fields ARE the
artifact, so two banana-bread recommendations are two different recipes and merging them
would discard one author's ingredients (docs/LANA_RECO_SUBJECT_MERGE.md).

Pass 1 is search-only: there is no draft here, so no tapped place id. Anything below a floor
is left alone rather than guessed at — a wrong merge invents corroboration, and the vouch
count a stranger reads is the one number that must not be inflated.

DRY RUN IS THE DEFAULT. Merges are the point of this script and a bad batch is expensive to
unpick, so nothing is written until --apply is passed.

Usage (from services/lana-worker, with the worker's env loaded):
    python -m scripts.backfill_reco_subjects                    # preview, writes nothing
    python -m scripts.backfill_reco_subjects --apply            # actually stamp
    python -m scripts.backfill_reco_subjects --user <uuid>      # one author
    python -m scripts.backfill_reco_subjects --limit 50 --apply
    python -m scripts.backfill_reco_subjects --no-identity        # places pass only

Requires the worker's env: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, GOOGLE_MAPS_API_KEY.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from app.auth import service_client
from app.reco_subject import (
    GROUNDABLE_TYPES,
    MATCH_FLOOR,
    MERGEABLE_TYPES,
    name_match_score,
    normalize_subject_name,
)


def _ground_one(row: dict[str, Any]) -> dict[str, Any] | None:
    """Path B for one stored row: search, score, accept only above the floor."""
    from app.places import search_places

    name = str(row.get("reco_name") or "").strip()
    if not name:
        return None
    category = str(row.get("category") or "").strip() or None
    locality = str(row.get("reco_place") or "").strip() or None
    query = " ".join(p for p in (name, category, locality) if p)
    try:
        results = search_places(
            query=query,
            block_id=row.get("block_id"),
            # The author's own home is the search centre — a tip is grounded from where it
            # was posted, not from wherever this script happens to run.
            user_id=row.get("user_id"),
            limit=5,
        )
    except Exception as exc:  # noqa: BLE001 — report and continue
        print(f"  SEARCH FAILED {row['id']}: {exc}", file=sys.stderr)
        return None

    best, best_score = None, 0.0
    for r in results or []:
        score = name_match_score(name, r.get("name"))
        if score > best_score:
            best, best_score = r, score
    if not best or best_score < MATCH_FLOOR or not str(best.get("place_id") or "").strip():
        return None
    return {
        "place_id": str(best["place_id"]).strip(),
        "matched_name": best.get("name"),
        "score": best_score,
        "lat": best.get("lat"),
        "lng": best.get("lng"),
    }


def _subject_id(sb, found: dict[str, Any], row: dict[str, Any]) -> str:
    """The existing subject for this place, or a new one. Mirrors set_signal_subject's
    conflict branch: never overwrite what a first author already recorded, only complete it."""
    existing = (
        sb.table("reco_subjects")
        .select("id, category, locality, lat, lng")
        .eq("google_place_id", found["place_id"])
        .limit(1)
        .execute()
        .data
        or []
    )
    category = str(row.get("category") or "").strip() or None
    locality = str(row.get("reco_place") or "").strip() or None
    if existing:
        sub = existing[0]
        patch = {
            k: v
            for k, v in (
                ("category", category), ("locality", locality),
                ("lat", found.get("lat")), ("lng", found.get("lng")),
            )
            if v is not None and sub.get(k) is None
        }
        if patch:
            sb.table("reco_subjects").update(patch).eq("id", sub["id"]).execute()
        return str(sub["id"])
    return str(
        sb.table("reco_subjects")
        .insert({
            "subject_key": normalize_subject_name(row.get("reco_name")),
            "google_place_id": found["place_id"],
            "display_name": str(row.get("reco_name") or "").strip(),
            "category": category,
            "locality": locality,
            "lat": found.get("lat"),
            "lng": found.get("lng"),
        })
        .execute()
        .data[0]["id"]
    )


def _identity_pass(sb, rows: list[dict[str, Any]], apply: bool) -> int:
    """Stage 2 for stored rows: attach what is left to a subject somebody already made.

    Reuses the live scorer and adjudicator (they are pure), but reads candidates straight
    off the table rather than through reco_subject_candidates — this runs as service_role,
    and that RPC keys on auth.uid(), which a script does not have.
    """
    from app.reco_subject import ADJUDICATE_FLOOR, AUTO_MERGE_FLOOR, _adjudicate, score_candidate

    attached = 0
    for row in rows:
        name = str(row.get("reco_name") or "").strip()
        key = normalize_subject_name(name)
        locality = str(row.get("reco_place") or "").strip() or None
        if not key:
            continue
        words = [w for w in key.split(" ") if w]
        # Same blocking rule as the RPC: ungrounded only, shares a word, same locality or
        # one of the two never said it.
        query = sb.table("reco_subjects").select(
            "id, subject_key, display_name, category, locality"
        ).is_("google_place_id", "null")
        candidates = [
            c for c in (query.execute().data or [])
            if c["id"] != row.get("subject_ref")
            and set(str(c.get("subject_key") or "").split(" ")) & set(words)
            and (
                not locality or not c.get("locality")
                or str(c["locality"]).strip().lower() == locality.lower()
            )
        ]
        if not candidates:
            continue
        scored = sorted(
            ((score_candidate(name, row.get("category"), c), c) for c in candidates),
            key=lambda p: p[0], reverse=True,
        )
        best_score, best = scored[0]
        if best_score < ADJUDICATE_FLOOR:
            continue
        method = "blocked"
        if best_score < AUTO_MERGE_FLOOR:
            if _adjudicate(name, row.get("category"), locality, best) is not True:
                print(f"  ambiguous: {name[:36]!r} ~ {str(best['display_name'])[:36]!r} "
                      f"({best_score:.2f}) — left alone")
                continue
            method = "adjudicated"
        print(f"  {name[:34]!r} -> {str(best['display_name'])[:34]!r} "
              f"({best_score:.2f}, {method})")
        if apply:
            sb.table("local_signals").update({
                "subject_ref": best["id"],
                "subject_method": method,
                "subject_confidence": float(best_score),
            }).eq("id", row["id"]).execute()
        attached += 1
    return attached


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write; omit to preview only")
    ap.add_argument("--user", help="limit to one author's tips")
    ap.add_argument("--limit", type=int, default=0, help="stop after N rows")
    ap.add_argument("--all", action="store_true", help="include already-grounded rows")
    ap.add_argument("--no-identity", action="store_true",
                    help="skip the Stage 2 pass (places only)")
    args = ap.parse_args()

    sb = service_client()
    query = (
        sb.table("local_signals")
        .select("id, user_id, block_id, reco_name, reco_place, category, reco_type, subject_ref")
        .eq("intent", "tip_share")
        .in_("reco_type", sorted(GROUNDABLE_TYPES))
        .not_.is_("reco_name", "null")
    )
    if args.user:
        query = query.eq("user_id", args.user)
    if not args.all:
        query = query.is_("subject_ref", "null")
    rows = query.execute().data or []
    if args.limit:
        rows = rows[: args.limit]

    if not args.apply:
        print(f"DRY RUN — {len(rows)} candidate rows, nothing will be written.\n")

    grounded = 0
    for row in rows:
        found = _ground_one(row)
        if not found:
            print(f"  ungrounded: {str(row.get('reco_name'))[:48]!r} (below floor or no hit)")
            continue
        line = (
            f"  {str(row.get('reco_name'))[:36]!r} -> {str(found['matched_name'])[:40]!r} "
            f"({found['score']:.2f}, {found['place_id']})"
        )
        if not args.apply:
            print(line)
            grounded += 1
            continue
        try:
            sid = _subject_id(sb, found, row)
            sb.table("local_signals").update({"subject_ref": sid}).eq("id", row["id"]).execute()
        except Exception as exc:  # noqa: BLE001
            print(f"  WRITE FAILED {row['id']}: {exc}", file=sys.stderr)
            continue
        grounded += 1
        print(f"{line} -> {sid}")

    verb = "would ground" if not args.apply else "grounded"
    print(f"\n{verb} {grounded}/{len(rows)} to a place")

    # Stage 2: whatever Google could not settle, plus every product (never a map point).
    if not args.no_identity:
        left = (
            sb.table("local_signals")
            .select("id, user_id, reco_name, reco_place, category, reco_type, subject_ref")
            .eq("intent", "tip_share")
            .in_("reco_type", sorted(MERGEABLE_TYPES))
            .not_.is_("reco_name", "null")
            .is_("subject_ref", "null")
            .execute()
            .data
            or []
        )
        if args.user:
            left = [r for r in left if r.get("user_id") == args.user]
        print(f"\nidentity space — {len(left)} rows with no place:")
        n = _identity_pass(sb, left, args.apply)
        print(f"{'would attach' if not args.apply else 'attached'} {n}/{len(left)}")

    if not args.apply:
        print("\nRe-run with --apply to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
