"""Text payload for capability_index embeddings (Vertex text-embedding-005).

Kept in its own module with NO imports so both callers can share one definition:
  * scripts/backfill_capability_embeddings.py — standalone, must not pull in the
    orchestrator package (circular import; see that script's docstring)
  * app/latent_extract.py — the runtime self-heal

Drift between the two would silently split the vector space: rows embedded by the
script would no longer be comparable to rows embedded by the worker.
"""


def capability_embedding_text(
    *,
    capability_name: str | None,
    description: str | None,
    entity_triggers: list[str] | None = None,
) -> str:
    """`name — description — trigger, trigger, …`

    identity_claim_triggers and required_state are deliberately excluded: they are
    post-filters, not semantics. Dev was embedded with exactly this text and works.
    """
    parts = [capability_name or "", description or ""]
    triggers = [str(t).strip() for t in (entity_triggers or []) if str(t).strip()]
    if triggers:
        parts.append(", ".join(triggers))
    return " — ".join(p for p in parts if p)
