"""Text payloads for identity claim embeddings (Vertex text-embedding-005)."""


def claim_embedding_text(
    *,
    concept: str,
    label: str,
    source_quote: str | None = None,
    bucket: str | None = None,
    details: list[str] | None = None,
) -> str:
    """
    Build embed input so distinct heritages/faiths don't cluster on generic labels.

    Heritage/faith: embed the user's words (source_quote), not "Brazilian Heritage" vs
    "Pakistani Heritage" alone — those collapse in vector space.

    Accumulated details ("Swims every weekend; Competes at state level") fold into the
    vector so enrichment sharpens semantic matching, not just the profile card.
    """
    quote = str(source_quote or "").strip()
    b = str(bucket or "").strip().lower()
    extra = "; ".join(str(d).strip() for d in (details or []) if str(d).strip())
    if b in ("heritage", "faith") and quote:
        return f"{concept}: {quote}"
    core = f"{concept}: {label} — {quote}" if quote else f"{concept}: {label}"
    return f"{core} — {extra}" if extra else core
