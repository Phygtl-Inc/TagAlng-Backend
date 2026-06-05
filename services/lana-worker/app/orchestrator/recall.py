"""MemGPT archival retrieval — prefetch + recall tool backend."""

from typing import Any

from app.auth import service_client
from app.vertex_extract import vertex_embed

RECALL_SCOPES = frozenset({"self", "neighbors", "block"})
PREFETCH_SCOPES = ("self", "neighbors")
DEFAULT_K = 5
PREFETCH_K = 5


def embed_query(text: str) -> list[float] | None:
    q = text.strip()
    if not q:
        return None
    try:
        return vertex_embed(q[:2000])
    except Exception:
        return None


def recall_memories(
    *,
    user_id: str,
    block_id: str | None,
    query: str,
    scope: str,
    k: int = DEFAULT_K,
    query_embedding: list[float] | None = None,
) -> list[dict[str, Any]]:
    scope_norm = str(scope or "self").strip().lower()
    if scope_norm not in RECALL_SCOPES:
        return []

    embedding = query_embedding if query_embedding is not None else embed_query(query)
    if not embedding:
        return []

    try:
        sb = service_client()
        res = sb.rpc(
            "lana_recall_memories",
            {
                "p_user_id": user_id,
                "p_block_id": block_id,
                "p_query_embedding": embedding,
                "p_scope": scope_norm,
                "p_limit": max(1, min(int(k), 10)),
            },
        ).execute()
        rows = res.data or []
        if not isinstance(rows, list):
            return []
        return [
            {
                "source_type": r.get("source_type"),
                "source_id": r.get("source_id"),
                "content": r.get("content"),
                "similarity": r.get("similarity"),
                "captured_at": r.get("captured_at"),
                "scope": scope_norm,
            }
            for r in rows
            if r.get("content")
        ]
    except Exception:
        return []


def prefetch_turn_memories(
    *,
    user_id: str,
    block_id: str | None,
    utterance: str,
    k: int = PREFETCH_K,
) -> list[dict[str, Any]]:
    """Pre-turn retrieval (Architecture §4): embed utterance, top-k self + neighbors."""
    if not utterance.strip():
        return []
    embedding = embed_query(utterance)
    if not embedding:
        return []
    combined: list[dict[str, Any]] = []
    seen: set[str] = set()
    per_scope = max(1, k // len(PREFETCH_SCOPES))
    for scope in PREFETCH_SCOPES:
        if scope == "neighbors" and not block_id:
            continue
        hits = recall_memories(
            user_id=user_id,
            block_id=block_id,
            query=utterance,
            scope=scope,
            k=per_scope,
            query_embedding=embedding,
        )
        for hit in hits:
            key = f"{hit.get('source_type')}:{hit.get('source_id')}"
            if key in seen:
                continue
            seen.add(key)
            hit["prefetch"] = True
            combined.append(hit)
    combined.sort(key=lambda h: float(h.get("similarity") or 0), reverse=True)
    return combined[:k]


def execute_recall_tool(*, user_id: str, block_id: str | None, args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query") or args.get("q") or "").strip()
    if not query:
        return {"status": "error", "tool": "recall", "reason": "query_required"}
    scope = str(args.get("scope") or "self").strip().lower()
    if scope not in RECALL_SCOPES:
        return {"status": "error", "tool": "recall", "reason": "invalid_scope"}
    k = int(args.get("k") or args.get("limit") or DEFAULT_K)
    memories = recall_memories(
        user_id=user_id,
        block_id=block_id,
        query=query,
        scope=scope,
        k=k,
    )
    return {
        "status": "ok",
        "tool": "recall",
        "query": query,
        "scope": scope,
        "memories": memories,
        "count": len(memories),
    }
