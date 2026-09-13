#!/usr/bin/env python3
"""Backfill (and repair) embeddings for meets (20261202120000).

Three jobs, one script:

  * every meet that existed before the migration has embedding=NULL,
  * a publish-time embed can fail (Vertex hiccup) and leaves the same NULL,
  * a host editing a published meet goes through update_event straight from the PWA, which
    the worker never sees — the title changes and the vector does not. events.updated_at
    moves, events.embedding_updated_at does not, and --stale is how that gets noticed.

--stale is the one worth putting on a schedule; the other two are one-offs.

Usage (from services/lana-worker, with the worker's env loaded):
    python -m scripts.backfill_event_embeddings            # only NULL embeddings
    python -m scripts.backfill_event_embeddings --stale    # + meets edited since embedding
    python -m scripts.backfill_event_embeddings --all      # re-embed every open meet
    python -m scripts.backfill_event_embeddings --probe "outdoor thing with kids"

Only status='open' meets are touched: they are the only rows search_events_semantic reads,
and embedding a finished meet is a Vertex call spent on something nobody can search for.

Requires the same env as the worker: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY,
GCP_VERTEX_PROJECT (text-embedding-005 via Vertex).
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

from app.auth import service_client
from app.event_embed import event_embedding_text

_FIELDS = "id, title, description, venue_name, cohort_tags, updated_at, embedding_updated_at"


def _vertex_embed(text: str, dim: int = 768) -> list[float]:
    """Embed via Vertex text-embedding-005, inline.

    Deliberately does NOT import app.vertex_extract — same reason as
    scripts/backfill_tip_embeddings.py: that module pulls in the orchestrator package,
    whose circular import only resolves once app.main has loaded the graph.
    """
    from google import genai

    project = os.environ.get("GCP_VERTEX_PROJECT", "")
    location = os.environ.get("GCP_VERTEX_LOCATION", "us-central1")
    if not project:
        raise RuntimeError("GCP_VERTEX_PROJECT not set")
    model = os.environ.get("VERTEX_EMBED_MODEL", "text-embedding-005")
    client = genai.Client(vertexai=True, project=project, location=location)
    result = client.models.embed_content(model=model, contents=text)
    values = list(result.embeddings[0].values)
    if len(values) != dim:
        raise ValueError(f"expected_{dim}_dims_got_{len(values)}")
    return values


def _is_stale(row: dict) -> bool:
    """Row edited after its vector was computed. A NULL stamp is handled by the NULL pass."""
    embedded_at, updated_at = row.get("embedding_updated_at"), row.get("updated_at")
    if not embedded_at or not updated_at:
        return False
    return str(updated_at) > str(embedded_at)  # both ISO-8601 UTC from PostgREST


def _targets(sb, do_all: bool, do_stale: bool) -> list[dict]:
    rows = (
        sb.table("events").select(_FIELDS).eq("status", "open").execute().data or []
    )
    if do_all:
        return rows
    # PostgREST renders the vector column as a long string, so staleness is decided from the
    # timestamps and NULL-ness is asked for separately rather than dragging 768 floats per
    # row through JSON to discover the column is empty.
    null_ids = {
        r["id"]
        for r in (
            sb.table("events")
            .select("id")
            .eq("status", "open")
            .is_("embedding", "null")
            .execute()
            .data
            or []
        )
    }
    return [r for r in rows if r["id"] in null_ids or (do_stale and _is_stale(r))]


def _backfill(sb, do_all: bool, do_stale: bool) -> tuple[int, int]:
    rows = _targets(sb, do_all, do_stale)
    done = 0
    for row in rows:
        text = event_embedding_text(
            title=row.get("title"),
            description=row.get("description"),
            venue_name=row.get("venue_name"),
            cohort_tags=row.get("cohort_tags"),
        )
        if not text.strip():
            print(f"  meet SKIPPED {row['id']}: nothing to embed", file=sys.stderr)
            continue
        try:
            vec = _vertex_embed(text)
        except Exception as exc:  # noqa: BLE001 — report and continue
            print(f"  meet FAILED {row['id']}: {exc}", file=sys.stderr)
            continue
        sb.table("events").update(
            {
                "embedding": vec,
                "embedding_updated_at": datetime.now(timezone.utc).isoformat(),
            }
        ).eq("id", row["id"]).execute()
        done += 1
        print(f"  meet embedded: {text[:70]}")
    return done, len(rows)


def _probe(sb, ask: str) -> int:
    """Score one ask against every embedded open meet, best first.

    This is what C3's floor_base gets calibrated against: the inequality admits on
    similarity, and picking 0.55 without ever seeing the real distribution of meet scores
    is how a threshold ends up either admitting everything or nothing.
    """
    vec = _vertex_embed(ask)
    rows = (
        sb.table("events")
        .select("id, title, venue_name")
        .eq("status", "open")
        .not_.is_("embedding", "null")
        .execute()
        .data
        or []
    )
    if not rows:
        print("No embedded meets — run the backfill first.", file=sys.stderr)
        return 1
    scored: list[tuple[float, dict]] = []
    for row in rows:
        raw = (
            sb.table("events")
            .select("embedding")
            .eq("id", row["id"])
            .single()
            .execute()
            .data
            or {}
        )
        try:
            peer = [float(x) for x in str(raw.get("embedding") or "").strip("[]").split(",")]
        except ValueError:
            continue
        if len(peer) != len(vec):
            continue
        dot = sum(a * b for a, b in zip(vec, peer))
        norm = (sum(a * a for a in vec) ** 0.5) * (sum(b * b for b in peer) ** 0.5)
        scored.append((dot / norm if norm else 0.0, row))
    scored.sort(key=lambda pair: pair[0], reverse=True)

    print(f'\nask: "{ask}"   (C3 floor_base = 0.55 at radius_base)\n')
    for sim, row in scored:
        mark = "✓" if sim >= 0.55 else " "
        label = row.get("title") or row["id"]
        print(f"  {mark} {sim:.3f}  {str(label)[:70]}")
    print()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true", help="re-embed every open meet")
    parser.add_argument("--stale", action="store_true",
                        help="also re-embed meets edited since their vector was computed")
    parser.add_argument("--probe", help="score this ask against every embedded meet", default=None)
    args = parser.parse_args()

    sb = service_client()
    if args.probe:
        return _probe(sb, args.probe)

    print("Backfilling meet embeddings…")
    done, total = _backfill(sb, args.all, args.stale)
    print(f"\nEmbedded {done} of {total} meet(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
