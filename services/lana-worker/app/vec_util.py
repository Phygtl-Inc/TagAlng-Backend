"""Tiny helpers for passing embeddings to pgvector RPCs.

PostgREST casts a text argument to a `vector` param, so we hand embeddings to RPCs as the
pgvector text literal `[f1,f2,…]` rather than a JSON array (which doesn't cast cleanly)."""

from __future__ import annotations

from typing import Sequence


def to_pgvector(embedding: Sequence[float] | None) -> str | None:
    """Format an embedding as a pgvector text literal, or None if empty."""
    if not embedding:
        return None
    return "[" + ",".join(f"{float(x):.6g}" for x in embedding) + "]"
