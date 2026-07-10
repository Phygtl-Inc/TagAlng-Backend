#!/usr/bin/env python3
"""Backfill embeddings for identity claims and rapport gap questions.

Claims saved while the embedding model was unreachable (e.g. expired gcloud ADC) land with
embedding=NULL, and gap questions opened before the semantic-suppression migration have
question_embedding=NULL. Both are INVISIBLE to the rapport coverage/dedup checks
(rapport_uncovered_claims / rapport_question_max_similarity filter on `... is not null`), so
the "By the way…" tile behaves as if the user has no threads. Run this once after fixing auth
and pushing 20260809120000_rapport_gaps_semantic.sql.

Usage (from services/lana-worker, with the worker's env loaded):
    python -m scripts.backfill_rapport_embeddings                 # only NULL embeddings
    python -m scripts.backfill_rapport_embeddings --all           # re-embed everything
    python -m scripts.backfill_rapport_embeddings --user <uuid>   # limit to one user

Requires the same env as the worker: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY,
GCP_VERTEX_PROJECT (text-embedding-005 via Vertex).
"""

from __future__ import annotations

import argparse
import os
import sys

from app.auth import service_client
from app.claim_embed import claim_embedding_text


def _vertex_embed(text: str, dim: int = 768) -> list[float]:
    """Embed via Vertex text-embedding-005, inline.

    Deliberately does NOT import app.vertex_extract: that module pulls in the orchestrator
    package, whose circular import only resolves once app.main loads the graph first. As a
    standalone script we call google.genai directly to sidestep it.
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


def _backfill_claims(sb, user_id: str | None, do_all: bool) -> tuple[int, int]:
    query = sb.table("user_identity_claims").select(
        "id, concept, label, source_quote, bucket, embedding"
    ).is_("dismissed_at", "null")
    if user_id:
        query = query.eq("user_id", user_id)
    if not do_all:
        query = query.is_("embedding", "null")
    rows = query.execute().data or []
    done = 0
    for row in rows:
        text = claim_embedding_text(
            concept=row.get("concept") or "",
            label=row.get("label") or "",
            source_quote=row.get("source_quote"),
            bucket=row.get("bucket"),
        )
        try:
            vec = _vertex_embed(text)
        except Exception as exc:  # noqa: BLE001 — report and continue
            print(f"  claim FAILED {row['id']}: {exc}", file=sys.stderr)
            continue
        sb.table("user_identity_claims").update({"embedding": vec}).eq("id", row["id"]).execute()
        done += 1
        print(f"  claim embedded {row.get('label') or row['id']}")
    return done, len(rows)


def _backfill_gap_questions(sb, user_id: str | None, do_all: bool) -> tuple[int, int]:
    query = sb.table("rapport_gaps").select(
        "gap_row_id, question, question_embedding, status"
    ).neq("status", "skipped")
    if user_id:
        query = query.eq("user_id", user_id)
    if not do_all:
        query = query.is_("question_embedding", "null")
    rows = query.execute().data or []
    done = 0
    for row in rows:
        q = str(row.get("question") or "").strip()
        if not q:
            continue
        try:
            vec = _vertex_embed(q)
        except Exception as exc:  # noqa: BLE001 — report and continue
            print(f"  gap FAILED {row['gap_row_id']}: {exc}", file=sys.stderr)
            continue
        sb.table("rapport_gaps").update({"question_embedding": vec}).eq(
            "gap_row_id", row["gap_row_id"]
        ).execute()
        done += 1
        print(f"  gap embedded: {q[:60]}")
    return done, len(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true", help="re-embed all rows, not just NULLs")
    parser.add_argument("--user", help="limit to one user_id", default=None)
    args = parser.parse_args()

    sb = service_client()
    print("Backfilling identity-claim embeddings…")
    c_done, c_total = _backfill_claims(sb, args.user, args.all)
    print(f"  → {c_done}/{c_total} claims embedded.\n")
    print("Backfilling rapport gap-question embeddings…")
    g_done, g_total = _backfill_gap_questions(sb, args.user, args.all)
    print(f"  → {g_done}/{g_total} gap questions embedded.")

    failed = (c_total - c_done) + (g_total - g_done)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
