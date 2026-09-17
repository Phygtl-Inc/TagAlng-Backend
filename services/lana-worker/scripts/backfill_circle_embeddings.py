#!/usr/bin/env python3
"""Backfill embeddings for confirmed circle_affiliations.

`circle_affiliations.embedding` is written on insert by circles_capture, but the column
landed after the table did — so every affiliation created before it, and every one whose
embed call failed, carries NULL. On prod that is 22 of 45 confirmed circles.

A confirmed circle with no embedding is invisible to anything that matches by meaning.
The one that matters today is attester_authority() (20261209120000): the behavioural half
of domain standing asks "is this person a confirmed member of a place of that type?", and
a NULL embedding answers no for a member who has been confirmed for months. Half the
behavioural evidence on prod is simply unreadable.

Usage (from services/lana-worker, with the worker's env loaded):
    python -m scripts.backfill_circle_embeddings              # only NULL embeddings
    python -m scripts.backfill_circle_embeddings --all        # re-embed everything
    python -m scripts.backfill_circle_embeddings --dry-run    # print text, embed nothing
    python -m scripts.backfill_circle_embeddings --user <uuid>

Requires the same env as the worker: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY,
GCP_VERTEX_PROJECT (text-embedding-005 via Vertex).
"""

from __future__ import annotations

import argparse
import os
import sys

from app.auth import service_client


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


def circle_embedding_text(row: dict) -> str:
    """Rebuild what circles_capture._embed_circle() would have produced.

    The live path embeds `f"{raw_phrase} ({circle_type} community)"`. raw_phrase is the
    user's own words and is NOT a column, so the best stored stand-in is used instead —
    detail first, then the grounded place name, then the slug turned back into words.

    The "(<type> community)" suffix is not decoration and must not be dropped: the 23 rows
    that already have vectors were built with it. Embedding the backfilled rows without it
    would put them in a measurably different region of the space, and every cosine
    threshold tuned on the existing rows — 0.60 in attester_authority, the rapport
    matchers — would then mean two different things depending on when a row was written.
    """
    phrase = (
        str(row.get("detail") or "").strip()
        or str(row.get("place_name") or "").strip()
        or str(row.get("circle_key") or "").replace("_", " ").strip()
    )
    circle_type = str(row.get("circle_type") or "other").strip()
    if not phrase:
        return ""
    # "fitness (fitness community)" is what a row with no detail and no place_name
    # degrades to, and it is worse than leaving the embedding NULL: it would match every
    # fitness concept at high similarity and hand the member 0.20 of behavioural
    # authority for belonging to a slug. Thin evidence that reads as strong evidence is
    # the failure mode Stack Overflow measured — 12% of "experts" resting on one answer.
    # No information, no vector.
    if phrase.casefold() == circle_type.casefold():
        return ""
    return f"{phrase} ({circle_type} community)"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="re-embed rows that already have one")
    ap.add_argument("--dry-run", action="store_true", help="print the text, write nothing")
    ap.add_argument("--user", help="limit to one user id")
    args = ap.parse_args()

    sb = service_client()
    query = (
        sb.table("circle_affiliations")
        .select("id, circle_key, circle_type, detail, place_name, embedding")
        # Only confirmed rows. A 'suggested' affiliation is Lana's guess that nobody has
        # agreed to yet, and it must not become evidence of standing.
        .eq("status", "confirmed")
        .is_("dismissed_at", "null")
    )
    if args.user:
        query = query.eq("user_id", args.user)
    if not args.all:
        query = query.is_("embedding", "null")

    rows = query.execute().data or []
    if not rows:
        print("nothing to backfill")
        return 0

    done = 0
    for row in rows:
        text = circle_embedding_text(row)
        if not text:
            print(f"  SKIPPED {row['id']}: nothing to embed", file=sys.stderr)
            continue
        if args.dry_run:
            print(f"  would embed: {text[:70]}")
            continue
        try:
            vec = _vertex_embed(text)
        except Exception as exc:  # noqa: BLE001 — report and continue
            print(f"  FAILED {row['id']}: {exc}", file=sys.stderr)
            continue
        sb.table("circle_affiliations").update({"embedding": vec}).eq(
            "id", row["id"]
        ).execute()
        done += 1
        print(f"  embedded: {text[:70]}")

    print(f"\n{done}/{len(rows)} confirmed circles embedded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
