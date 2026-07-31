"""LANA Layer 3b · latent intent: extract entities from each turn, match capabilities.

Phase 1 = COLLECT, don't surface. This runs as a post-turn background task (sibling of
the identity-claim extractor in main.py) and writes two tables:
  * latent_signals   — every entity mentioned, regardless of classified intent
  * suggestion_queue — cosine matches against capability_index (never surfaced in Phase 1)

Mirrors the claims_persist / vertex_extract pattern: LLM-via-orchestrator-or-Vertex,
defensive (never raises into the request path), service-role writes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from app.auth import service_client

logger = logging.getLogger(__name__)

# Subject vocabulary is fixed (matches the CHECK on latent_signals.subject).
_SUBJECTS = frozenset({"self", "child", "partner", "household", "other", "unknown"})
# entity_type stays open (spec §10-Q1, Yunchao); we only sanity-bound it.
_MAX_ENTITIES = 6
_MIN_ENTITY_CONFIDENCE = 0.5
# Calibrated for Vertex text-embedding-005: a short entity vs a longer capability description
# tops out ~0.55-0.60 for true matches (vs ~0.35-0.40 for unrelated). The spec's 0.65 assumed
# OpenAI 1536-dim embeddings and never cleared here, leaving suggestion_queue empty.
_MIN_MATCH_SCORE = 0.45
_SUGGESTION_TTL_DAYS = 30
_SKIP_SHORT = frozenset({"ok", "okay", "yes", "no", "yep", "nope", "sure", "thanks", "thank you"})


# Capability-catalog self-heal (see _kick_capability_catalog_selfheal). The catalog is 8
# static reference rows, so the probe is rate-limited hard: at most one count query per
# hour per process, and only from the empty-match path.
_CATALOG_SELFHEAL_COOLDOWN_S = 3600.0
_CATALOG_SELFHEAL_MAX_ROWS = 50
_catalog_selfheal_at: float = 0.0

LATENT_EXTRACT_PROMPT = """You extract latent signals from ONE user message in a TagAlng block chat \
(a neighborhood app for local families). These are things the user MENTIONED but did not explicitly ask for — \
activities, places, gear, needs, life events — that might map to something the app could help with.

Output ONLY valid JSON (no markdown):
{
  "entities": [
    {
      "text": "karate",
      "type": "activity",
      "subject": "child",
      "confidence": 0.9,
      "attributes": {"child_age": 5, "frequency": "weekly"}
    }
  ]
}

Rules:
- Extract at most 6 entities; only concrete, meaningful mentions (skip filler/greetings).
- "type": a short lowercase noun for the entity kind, e.g. activity, sport, place, gear,
  service, interest, life_event, need. One or two words.
- "subject": WHO it is about. MUST be one of: self, child, partner, household, other, unknown.
  "my kid does karate" -> child;  "I do yoga" -> self;  "my husband travels" -> partner.
- "attributes": optional object with extra structured detail (age, frequency, place name). {} if none.
- "confidence": 0.0-1.0.
- If the message contains no latent signal (pure chit-chat, a question, an explicit request
  already handled elsewhere), return {"entities": []}.

Message:
"""


@dataclass
class ExtractedEntity:
    text: str
    type: str
    subject: str = "unknown"
    confidence: float = 0.8
    attributes: dict[str, Any] = field(default_factory=dict)


def should_extract_entities(message: str) -> bool:
    """Cheap guard: skip greetings, OTP codes, phone numbers, trivially short lines.

    Mirrors should_extract_claims_from_message so we don't pay an LLM call on "ok thanks".
    """
    import re

    text = (message or "").strip()
    if not text:
        return False
    if text.lower() in _SKIP_SHORT:
        return False
    if len(text) < 6:
        return False
    if re.fullmatch(r"\d{4,8}", text):  # OTP / zip
        return False
    digits = re.sub(r"\D", "", text)
    if digits and len(digits) >= 10 and re.fullmatch(r"[\d\s+\-().]+", text):
        return False
    return True


def parse_entities(data: Any) -> list[ExtractedEntity]:
    """Validate the LLM JSON into clean ExtractedEntity rows."""
    if not isinstance(data, dict):
        return []
    raw = data.get("entities", [])
    if not isinstance(raw, list):
        return []
    out: list[ExtractedEntity] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()[:120]
        etype = str(item.get("type", "")).strip().lower()[:40]
        if not text or not etype:
            continue
        subject = str(item.get("subject", "unknown")).strip().lower()
        if subject not in _SUBJECTS:
            subject = "unknown"
        try:
            conf = max(0.0, min(1.0, float(item.get("confidence", 0.8))))
        except (TypeError, ValueError):
            conf = 0.8
        attrs = item.get("attributes", {})
        if not isinstance(attrs, dict):
            attrs = {}
        out.append(
            ExtractedEntity(
                text=text, type=etype, subject=subject, confidence=conf, attributes=attrs
            )
        )
    return out[:_MAX_ENTITIES]


def extract_entities_from_message(message: str) -> list[ExtractedEntity]:
    """Run the entity extractor via orchestrator LLM when configured, else Vertex Flash."""

    text = (message or "").strip()
    try:
        from app.orchestrator.llm import llm_configured, llm_json, router_model

        if llm_configured():
            data = llm_json(
                model=router_model(),
                system=LATENT_EXTRACT_PROMPT,
                user_payload=text,
                max_tokens=512,
                temperature=0.2,
            )
            return parse_entities(data)
    except Exception:
        logger.exception("latent_entity_extract_llm_failed")

    # Fallback: direct Vertex Flash call (same model the claim extractor falls back
    # to), with the same 512-token budget and timeout as the OpenAI call above.
    try:
        import os

        from app.orchestrator.llm import vertex_generate_json

        return parse_entities(
            vertex_generate_json(
                model=os.environ.get("VERTEX_EXTRACT_MODEL", "gemini-2.5-flash"),
                system=None,
                user_payload=LATENT_EXTRACT_PROMPT + text,
                max_tokens=512,
                temperature=0.2,
            )
        )
    except Exception:
        logger.exception("latent_entity_extract_vertex_failed")
        return []


def _embed_entity(entity: ExtractedEntity) -> list[float] | None:
    """Embed the entity text (plus type for disambiguation), like claim embeddings."""
    try:
        from app.vertex_extract import vertex_embed

        return vertex_embed(f"{entity.text} ({entity.type})")
    except Exception:
        logger.exception("latent_entity_embed_failed")
        return None


def _insert_latent_signal(
    *,
    user_id: str,
    session_id: str,
    turn_id: str | None,
    block_id: str | None,
    utterance_excerpt: str,
    entity: ExtractedEntity,
    embedding: list[float] | None,
) -> None:
    row: dict[str, Any] = {
        "user_id": user_id,
        "session_id": session_id,
        "turn_id": turn_id,
        "block_id": block_id,
        "utterance_excerpt": utterance_excerpt[:500],
        "entity_text": entity.text,
        "entity_type": entity.type,
        "subject": entity.subject,
        "attributes": entity.attributes,
        "entity_confidence": entity.confidence,
    }
    if embedding is not None:
        row["embedding"] = embedding
    service_client().table("latent_signals").insert(row).execute()


def _embed_capability_row(row: dict[str, Any]) -> list[float] | None:
    """Embed one capability_index row with the SAME text the backfill script uses."""
    from app.capability_embed import capability_embedding_text
    from app.vertex_extract import vertex_embed

    try:
        text = capability_embedding_text(
            capability_name=row.get("capability_name"),
            description=row.get("description"),
            entity_triggers=row.get("entity_triggers"),
        )
        return vertex_embed(text) if text else None
    except Exception:
        logger.exception("capability_embed_failed id=%s", row.get("capability_id"))
        return None


def _kick_capability_catalog_selfheal() -> None:
    """Self-heal an un-embedded capability_index, in the background.

    Why this exists: capability_index rows are seeded by migration with embedding=NULL
    (pure SQL can't call the embedding model) and filled in by
    scripts/backfill_capability_embeddings.py as a post-deploy step. When that step is
    skipped for an environment, match_latent_capabilities' `embedding is not null` guard
    drops every row, the matcher returns an empty set, and the failure is
    indistinguishable from "no match" — no error, no log, no metric. That is exactly how
    prod ran with zero suggestion_queue rows for a month.

    Cheap by construction: only reachable from the empty-match path, and then at most
    once per _CATALOG_SELFHEAL_COOLDOWN_S per process. Once the catalog is whole the
    probe finds nothing and returns without embedding anything, so this is emphatically
    NOT per-turn work. The reference catalog is 8 static rows, not user data.
    """
    import threading
    import time

    global _catalog_selfheal_at
    now = time.time()
    if now - _catalog_selfheal_at < _CATALOG_SELFHEAL_COOLDOWN_S:
        return
    # Stamp before the thread starts so concurrent turns can't stampede the probe.
    _catalog_selfheal_at = now

    def _run() -> None:
        try:
            sb = service_client()
            rows = (
                sb.table("capability_index")
                .select("capability_id, capability_name, description, entity_triggers")
                .is_("embedding", "null")
                .eq("is_active", True)
                .limit(_CATALOG_SELFHEAL_MAX_ROWS)
                .execute()
                .data
                or []
            )
            if not rows:
                # Healthy catalog — the empty match was a genuine no-match.
                return
            # Loud on purpose: this is the log line whose absence hid the outage.
            logger.error(
                "capability_index_unembedded rows=%d — semantic capability routing is "
                "returning empty; self-healing now. Run "
                "`python -m scripts.backfill_capability_embeddings` if this repeats.",
                len(rows),
            )
            fixed = 0
            for row in rows:
                vec = _embed_capability_row(row)
                if vec is None:
                    continue
                sb.table("capability_index").update({"embedding": vec}).eq(
                    "capability_id", row["capability_id"]
                ).execute()
                fixed += 1
            logger.warning(
                "capability_index_selfheal fixed=%d/%d", fixed, len(rows)
            )
        except Exception:
            logger.exception("capability_catalog_selfheal_failed")

    threading.Thread(
        target=_run, daemon=True, name="capability-catalog-selfheal"
    ).start()


def _queue_capability_matches(
    *,
    user_id: str,
    entity: ExtractedEntity,
    embedding: list[float],
    utterance_excerpt: str,
) -> int:
    """Cosine-match the entity against capability_index and queue candidates. Phase 1: no surfacing."""
    sb = service_client()
    try:
        resp = sb.rpc(
            "match_latent_capabilities",
            {
                "p_query_embedding": embedding,
                "p_limit": 3,
                "p_min_score": _MIN_MATCH_SCORE,
            },
        ).execute()
    except Exception:
        logger.exception("latent_capability_match_failed")
        return 0

    matches = resp.data or []
    if not matches:
        # An empty set here is ambiguous: a real no-match, or a catalog with NULL
        # embeddings that the RPC's guard filtered out entirely. Disambiguate.
        _kick_capability_catalog_selfheal()
        return 0

    expires_at = (datetime.now(timezone.utc) + timedelta(days=_SUGGESTION_TTL_DAYS)).isoformat()
    queued = 0
    for m in matches:
        capability_id = m.get("capability_id")
        similarity = float(m.get("similarity") or 0.0)
        if not capability_id:
            continue
        service_client().table("suggestion_queue").insert(
            {
                "user_id": user_id,
                "trigger_layer": "3b",
                "trigger_context": {
                    "entity_text": entity.text,
                    "entity_type": entity.type,
                    "subject": entity.subject,
                    "utterance": utterance_excerpt[:500],
                    "similarity": similarity,
                },
                "capability_id": capability_id,
                "confidence": similarity,
                # Phase 1 never surfaces; default to next_session so v0.3 can pick it up.
                "surface_when": "next_session",
                "expires_at": expires_at,
            }
        ).execute()
        queued += 1
    return queued


def run_latent_intent(
    user_id: str,
    session_id: str,
    turn_id: str | None,
    block_id: str | None,
    message: str,
) -> dict[str, int]:
    """Background entrypoint: extract entities -> store latent_signals -> queue capability matches.

    Defensive by contract: this runs as a fire-and-forget background task and must never raise.
    Returns counts for logging/tests.
    """
    result = {"entities": 0, "signals": 0, "suggestions": 0}
    try:
        text = (message or "").strip()
        if not should_extract_entities(text):
            return result

        entities = extract_entities_from_message(text)
        result["entities"] = len(entities)

        for entity in entities:
            if entity.confidence < _MIN_ENTITY_CONFIDENCE:
                continue
            embedding = _embed_entity(entity)
            _insert_latent_signal(
                user_id=user_id,
                session_id=session_id,
                turn_id=turn_id,
                block_id=block_id,
                utterance_excerpt=text,
                entity=entity,
                embedding=embedding,
            )
            result["signals"] += 1
            if embedding is not None:
                result["suggestions"] += _queue_capability_matches(
                    user_id=user_id,
                    entity=entity,
                    embedding=embedding,
                    utterance_excerpt=text,
                )
    except Exception:
        logger.exception("run_latent_intent_failed")
    return result
