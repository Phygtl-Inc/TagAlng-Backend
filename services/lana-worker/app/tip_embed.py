"""Text payload for tip_share embeddings (Vertex text-embedding-005).

A tip is stored across several columns — the free-text `detail_text` the neighbour typed,
plus the structured card fields (`reco_name`, `reco_place`, `reco_description`) that
20261120120000 split out of it. Embedding `detail_text` alone would miss half a modern tip,
so this folds the card back into one sentence before it is embedded.

Mirrors app/claim_embed.py: a pure string builder with no imports, so the backfill script
can use it without pulling the worker's app graph in.
"""

from __future__ import annotations


def tip_embedding_text(
    *,
    detail_text: str | None,
    category: str | None = None,
    reco_name: str | None = None,
    reco_place: str | None = None,
    reco_description: str | None = None,
    affinity_tags: list[str] | None = None,
) -> str:
    """One line describing what this recommendation IS, for the vector.

    Ordered noun-first ("Quill & Co — stationery — on Main — huge paper selection"): the
    ask being compared against is short ("art supplies"), and a lead of stopwords drags the
    similarity of every tip toward every other tip.
    """
    parts: list[str] = []
    for value in (reco_name, category, reco_place):
        text = str(value or "").strip()
        if text and text.lower() not in [p.lower() for p in parts]:
            parts.append(text)
    for value in (reco_description, detail_text):
        text = str(value or "").strip()
        # detail_text on an older row is the whole tip; on a new one it repeats the card.
        if text and not any(text.lower() in p.lower() for p in parts):
            parts.append(text)
    tags = [str(t).strip() for t in (affinity_tags or []) if str(t or "").strip()]
    if tags:
        parts.append(", ".join(tags[:8]))
    return " — ".join(parts)[:2000]
