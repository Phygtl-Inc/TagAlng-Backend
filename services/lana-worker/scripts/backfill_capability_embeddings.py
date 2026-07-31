#!/usr/bin/env python3
"""Backfill capability_index.embedding for rows seeded without a vector.

The Layer 3 migration seeds capability_index rows with embedding=NULL (pure SQL can't
call the embedding model). Run this once after `supabase db push` so the capability
matcher (match_latent_capabilities) has vectors to compare against.

Usage (from services/lana-worker, with the worker's env loaded):
    python -m scripts.backfill_capability_embeddings           # only NULL embeddings
    python -m scripts.backfill_capability_embeddings --all     # re-embed every row

Requires the same env as the worker: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY,
GCP_VERTEX_PROJECT (text-embedding-005 via vertex_embed).
"""

from __future__ import annotations

import argparse
import os
import sys

from app.auth import service_client
from app.capability_embed import capability_embedding_text


def _vertex_embed(text: str, dim: int = 768) -> list[float]:
    """Embed via Vertex text-embedding-005, inline.

    Deliberately does NOT import app.vertex_extract: that module pulls in the orchestrator
    package, which has a circular import that only resolves when app.main loads the graph
    first. As a standalone script we call google.genai directly to sidestep it.
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


def _embedding_text(row: dict) -> str:
    """What we embed: name + description + triggers, so synonyms land near the capability.

    Shared with the worker's runtime self-heal via app.capability_embed — the two MUST
    produce identical text or the vector space splits.
    """
    return capability_embedding_text(
        capability_name=row.get("capability_name"),
        description=row.get("description"),
        entity_triggers=row.get("entity_triggers"),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true", help="re-embed all rows, not just NULLs")
    args = parser.parse_args()

    sb = service_client()
    query = sb.table("capability_index").select(
        "capability_id, capability_name, description, entity_triggers, embedding"
    )
    if not args.all:
        query = query.is_("embedding", "null")
    rows = query.execute().data or []

    if not rows:
        print("Nothing to backfill (all capability_index rows already embedded).")
        return 0

    done = 0
    for row in rows:
        cap_id = row["capability_id"]
        try:
            vec = _vertex_embed(_embedding_text(row))
        except Exception as exc:  # noqa: BLE001 — report and continue
            print(f"  FAILED {cap_id}: {exc}", file=sys.stderr)
            continue
        sb.table("capability_index").update({"embedding": vec}).eq(
            "capability_id", cap_id
        ).execute()
        done += 1
        print(f"  embedded {cap_id}")

    print(f"Backfilled {done}/{len(rows)} capability embeddings.")
    return 0 if done == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
