#!/usr/bin/env python3
"""Backfill embeddings for tip_share signals (20261126120000).

Every tip posted before that migration has embedding=NULL, which find_neighbor_tips reads
as "lexical only" — those tips stay invisible to any ask that doesn't reuse their words,
which is the whole thing the migration fixed. Run this once after pushing it.

Usage (from services/lana-worker, with the worker's env loaded):
    python -m scripts.backfill_tip_embeddings                 # only NULL embeddings
    python -m scripts.backfill_tip_embeddings --all           # re-embed everything
    python -m scripts.backfill_tip_embeddings --user <uuid>   # limit to one author

Requires the same env as the worker: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY,
GCP_VERTEX_PROJECT (text-embedding-005 via Vertex).
"""

from __future__ import annotations

import argparse
import os
import sys

from app.auth import service_client
from app.tip_embed import tip_embedding_text


def _vertex_embed(text: str, dim: int = 768) -> list[float]:
    """Embed via Vertex text-embedding-005, inline.

    Deliberately does NOT import app.vertex_extract — same reason as
    scripts/backfill_rapport_embeddings.py: that module pulls in the orchestrator package,
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


def _backfill_tips(sb, user_id: str | None, do_all: bool) -> tuple[int, int]:
    query = (
        sb.table("local_signals")
        .select(
            "id, detail_text, category, affinity_tags, reco_name, reco_place, "
            "reco_description, embedding"
        )
        .eq("intent", "tip_share")
    )
    if user_id:
        query = query.eq("user_id", user_id)
    if not do_all:
        query = query.is_("embedding", "null")
    rows = query.execute().data or []
    done = 0
    for row in rows:
        text = tip_embedding_text(
            detail_text=row.get("detail_text"),
            category=row.get("category"),
            reco_name=row.get("reco_name"),
            reco_place=row.get("reco_place"),
            reco_description=row.get("reco_description"),
            affinity_tags=row.get("affinity_tags"),
        )
        if not text.strip():
            print(f"  tip SKIPPED {row['id']}: nothing to embed", file=sys.stderr)
            continue
        try:
            vec = _vertex_embed(text)
        except Exception as exc:  # noqa: BLE001 — report and continue
            print(f"  tip FAILED {row['id']}: {exc}", file=sys.stderr)
            continue
        sb.table("local_signals").update({"embedding": vec}).eq("id", row["id"]).execute()
        done += 1
        print(f"  tip embedded: {text[:70]}")
    return done, len(rows)


def _probe(sb, ask: str) -> int:
    """Score one ask against every embedded tip, best first.

    This is the calibration tool LANA_TIP_MIN_SIM needs: the floor should sit above the
    best score of the tips that should NOT answer, and below the worst of the tips that
    should. Guessing it from the peer matcher's 0.55 is a starting point, not an answer.
    """
    vec = _vertex_embed(ask)
    rows = (
        sb.table("local_signals")
        .select("id, detail_text, category, reco_name")
        .eq("intent", "tip_share")
        .not_.is_("embedding", "null")
        .execute()
        .data
        or []
    )
    if not rows:
        print("No embedded tips — run the backfill first.", file=sys.stderr)
        return 1
    # Refetched one at a time: PostgREST renders vector columns as strings, and asking for
    # the column above would drag every 768-float row through JSON for nothing.
    scored: list[tuple[float, dict]] = []
    for row in rows:
        raw = (
            sb.table("local_signals")
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

    floor = float(os.environ.get("LANA_TIP_MIN_SIM", "0.55"))
    print(f'\nask: "{ask}"   (floor LANA_TIP_MIN_SIM={floor})\n')
    for sim, row in scored:
        mark = "✓" if sim >= floor else " "
        label = row.get("reco_name") or row.get("detail_text") or row["id"]
        print(f"  {mark} {sim:.3f}  {str(label)[:70]}")
    print()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true", help="re-embed all rows, not just NULLs")
    parser.add_argument("--user", help="limit to one author user_id", default=None)
    parser.add_argument("--probe", help="score this ask against every embedded tip", default=None)
    args = parser.parse_args()

    sb = service_client()
    if args.probe:
        return _probe(sb, args.probe)

    print("Backfilling tip_share embeddings…")
    done, total = _backfill_tips(sb, args.user, args.all)
    print(f"  → {done}/{total} tips embedded.")
    return 0 if done == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
