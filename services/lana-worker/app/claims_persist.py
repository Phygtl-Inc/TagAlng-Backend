"""Incremental identity claims: extract from each user turn and upsert to Postgres."""

from __future__ import annotations

import logging
import re
from typing import Any

from app.auth import service_client
from app.claim_embed import claim_embedding_text
from app.models import ExtractedClaim
from app.vertex_extract import (
    parse_incremental_claims_data,
    vertex_embed,
    vertex_extract_claims_from_utterance,
)

logger = logging.getLogger(__name__)

MIN_CLAIM_CONFIDENCE = 0.65
_SKIP_OK = frozenset({"ok", "okay", "yes", "no", "yep", "nope", "sure", "thanks", "thank you"})

_NAME_INTRO_PATTERNS = (
    re.compile(
        r"\b(?:my name is|call me|they call me|name'?s)\s+([A-Za-z][A-Za-z'-]{1,28})\b",
        re.I,
    ),
    re.compile(
        r"\b(?:i'?m|this is)\s+([A-Z][a-z]{1,28})(?:\s+and\b|[.,!?\s]*$)",
    ),
)

_NOT_NAMES = frozenset(
    {
        "italian",
        "brazilian",
        "latino",
        "latin",
        "mom",
        "mother",
        "father",
        "dad",
        "new",
        "a",
        "an",
        "the",
        "very",
        "really",
        "so",
        "just",
        "happy",
        "glad",
        "here",
        "there",
        "interested",
        "looking",
        "trying",
        "going",
        "living",
        "from",
        "awesome",
        "great",
        "good",
        "fine",
        "nice",
    }
)


def _normalize_nickname(name: str) -> str:
    n = name.strip()[:30]
    if len(n) >= 2:
        return n[0].upper() + n[1:]
    return n


def extract_nickname_from_message(message: str) -> str | None:
    """Explicit name intros only — avoids treating 'I am italian mom' as a name."""
    text = message.strip()
    if not text:
        return None
    for pat in _NAME_INTRO_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        raw = m.group(1).strip()
        if raw.lower() in _NOT_NAMES:
            continue
        return _normalize_nickname(raw)
    return None


def extract_display_name_reply(message: str) -> str | None:
    """When we asked for a name, accept a single-word reply (e.g. 'Brigade')."""
    nick = extract_nickname_from_message(message)
    if nick:
        return nick
    text = message.strip()
    bare = re.fullmatch(r"[A-Za-z][A-Za-z'-]{1,28}", text)
    if bare and bare.group(0).lower() not in _NOT_NAMES:
        return _normalize_nickname(bare.group(0))
    return None


def user_needs_display_name(user_id: str | None, session_ctx: dict[str, Any]) -> bool:
    if not user_id:
        return False
    if session_ctx.get("display_name_saved"):
        return False
    try:
        res = (
            service_client()
            .table("users")
            .select("nickname, full_name")
            .eq("id", user_id)
            .limit(1)
            .execute()
        )
        row = (res.data or [None])[0]
        if not row:
            return True
        nick = str(row.get("nickname") or "").strip()
        full = str(row.get("full_name") or "").strip()
        return not nick and not full
    except Exception:
        logger.exception("user_needs_display_name_failed")
        return False


def persist_nickname_if_stated(user_id: str, message: str) -> str | None:
    """Sync write to users.nickname when the user states their name."""
    nick = extract_nickname_from_message(message)
    if not nick:
        return None
    persist_profile_patch(user_id, {"nickname": nick})
    return nick


def should_extract_claims_from_message(message: str) -> bool:
    """Skip auth gates and non-identity one-liners."""
    text = message.strip()
    if not text:
        return False
    if text.lower() in _SKIP_OK:
        return False
    if len(text) < 6:
        return False
    if re.fullmatch(r"\d{5}", text):
        return False
    if re.fullmatch(r"\d{4,8}", text):
        return False
    digits = re.sub(r"\D", "", text)
    if digits and len(digits) >= 10 and re.fullmatch(r"[\d\s+\-().]+", text):
        return False
    return True


def persist_profile_patch(user_id: str, patch: dict[str, str]) -> None:
    row: dict[str, Any] = {}
    if patch.get("nickname"):
        row["nickname"] = patch["nickname"][:30]
    if patch.get("full_name"):
        row["full_name"] = patch["full_name"][:80]
    if not row:
        return
    service_client().table("users").update(row).eq("id", user_id).execute()


def _embed_claim(c: ExtractedClaim) -> list[float] | None:
    try:
        text = claim_embedding_text(
            concept=c.concept,
            label=c.label,
            source_quote=c.source_quote,
            bucket=c.bucket,
        )
        return vertex_embed(text)
    except Exception:
        logger.exception("claim_embed_failed concept=%s", c.concept)
        return None


def _claim_row(user_id: str, c: ExtractedClaim) -> dict[str, Any]:
    row: dict[str, Any] = {
        "user_id": user_id,
        "concept": c.concept,
        "label": c.label,
        "tone": c.tone,
        "confidence": c.confidence,
        "disclosure": c.disclosure,
        "synonyms": c.synonyms,
        "source_quote": c.source_quote,
        "bucket": c.bucket,
    }
    embedding = _embed_claim(c)
    if embedding is not None:
        row["embedding"] = embedding
    return row


def upsert_claims(user_id: str, claims: list[ExtractedClaim]) -> int:
    """Merge claims by concept; never deletes sibling claims."""
    sb = service_client()
    saved = 0
    for c in claims:
        if c.confidence < MIN_CLAIM_CONFIDENCE:
            continue
        row = _claim_row(user_id, c)
        existing = (
            sb.table("user_identity_claims")
            .select("id")
            .eq("user_id", user_id)
            .eq("concept", c.concept)
            .is_("dismissed_at", "null")
            .limit(1)
            .execute()
        )
        if existing.data:
            claim_id = existing.data[0]["id"]
            sb.table("user_identity_claims").update(row).eq("id", claim_id).execute()
        else:
            sb.table("user_identity_claims").insert(row).execute()
        saved += 1
    return saved


def replace_all_claims(user_id: str, claims: list[ExtractedClaim]) -> None:
    """Full session complete: replace active claims for user."""
    sb = service_client()
    sb.table("user_identity_claims").delete().eq("user_id", user_id).is_(
        "dismissed_at", "null"
    ).execute()
    rows = [_claim_row(user_id, c) for c in claims]
    if rows:
        sb.table("user_identity_claims").insert(rows).execute()


def extract_and_upsert_claims_from_message(user_id: str, message: str) -> int:
    """Background job: Flash extract from one user line → upsert claims + nickname."""
    persist_nickname_if_stated(user_id, message)
    if not should_extract_claims_from_message(message):
        return 0
    try:
        data = vertex_extract_claims_from_utterance(message)
        nickname, claims = parse_incremental_claims_data(data)
    except Exception:
        logger.exception("incremental_claim_extract_failed")
        return 0
    if nickname:
        persist_profile_patch(user_id, {"nickname": _normalize_nickname(nickname)})
    if not claims:
        return 0
    return upsert_claims(user_id, claims)
