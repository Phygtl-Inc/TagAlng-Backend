"""Text payloads for identity claim embeddings (Vertex text-embedding-005)."""


def claim_embedding_text(
    *,
    concept: str,
    label: str,
    source_quote: str | None = None,
    bucket: str | None = None,
) -> str:
    """
    Build embed input so distinct heritages/faiths don't cluster on generic labels.

    Heritage/faith: embed the user's words (source_quote), not "Brazilian Heritage" vs
    "Pakistani Heritage" alone — those collapse in vector space.
    """
    quote = str(source_quote or "").strip()
    b = str(bucket or "").strip().lower()
    if b in ("heritage", "faith") and quote:
        return f"{concept}: {quote}"
    if quote:
        return f"{concept}: {label} — {quote}"
    return f"{concept}: {label}"
