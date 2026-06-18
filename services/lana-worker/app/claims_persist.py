"""Incremental identity claims: extract from each user turn and upsert to Postgres."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from app.auth import service_client
from app.claim_embed import claim_embedding_text
from app.models import ExtractedClaim
from app.vertex_extract import (
    parse_incremental_claims_data,
    incremental_claims_from_utterance,
    vertex_embed,
    vertex_extract_claims_from_utterance,
)

logger = logging.getLogger(__name__)

MIN_CLAIM_CONFIDENCE = 0.65
_SKIP_OK = frozenset({"ok", "okay", "yes", "no", "yep", "nope", "sure", "thanks", "thank you"})

_NEGATIVE_CLAIM_RE = re.compile(
    r"\b(?:no|not|without|non[-\s]?)\s+(?:\w+\s+){0,3}"
    r"(?:heritage|background|from|speaker|brazilian|italian|pakistani|latino|latina)",
    re.I,
)
_HERITAGE_ROOT_TERMS: dict[str, tuple[str, ...]] = {
    "american": ("american", "america", "usa", "u.s."),
    "british": ("british", "britain", "uk", "english"),
    "canadian": ("canadian", "canada"),
    "pakistani": ("pakistani", "pakistan"),
    "brazilian": ("brazilian", "brazil", "latina", "latino", "paulista"),
    "italian": ("italian", "italy"),
    "portuguese": ("portuguese", "portugal"),
    "indian": ("indian", "india"),
    "mexican": ("mexican", "mexico"),
    "chinese": ("chinese", "china"),
    "korean": ("korean", "korea"),
    "colombian": ("colombian", "colombia"),
}

_HERITAGE_CORRECTION_RE = re.compile(
    r"\b(?:not|no|remove|delete|drop|clear|get rid of|i(?:'m| am) not)\b",
    re.I,
)

_NAME_INTRO_PATTERNS = (
    re.compile(
        r"\b(?:my name is|call me|they call me|name'?s)\s+([A-Za-z][A-Za-z'-]{1,28})\b",
        re.I,
    ),
    re.compile(
        r"\b(?:[Ii]'?m|[Ii] am|[Tt]his is)\s+([A-Z][a-z]{1,28})(?:\s+and\b|[.,!?\s]*$)",
    ),
    re.compile(
        r"\b(?:change my name to|update my name to|rename me to)\s+([A-Za-z][A-Za-z'-]{1,28})\b",
        re.I,
    ),
    re.compile(
        r"\badd my name(?:\s+as)?\s+([A-Za-z][A-Za-z'-]{1,28})\b",
        re.I,
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
    if (
        bare
        and bare.group(0).lower() not in _NOT_NAMES
        and bare.group(0).lower() not in _SKIP_OK
    ):
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


@dataclass
class ClaimExtractResult:
    saved: int = 0
    heritage_conflict: dict[str, Any] | None = None
    nickname: str | None = None


def _heritage_root(concept: str, label: str) -> str | None:
    blob = f"{concept} {label}".lower()
    for root, terms in _HERITAGE_ROOT_TERMS.items():
        if any(t in blob for t in terms):
            return root
    return None


def heritage_claim_key(concept: str, label: str) -> str:
    root = _heritage_root(concept, label)
    if root:
        return root
    return str(concept or label).strip().lower()


def is_explicit_heritage_correction(message: str) -> bool:
    """User is correcting or removing heritage — apply without confirmation."""
    text = str(message or "")
    if _HERITAGE_CORRECTION_RE.search(text):
        return True
    low = text.lower()
    return bool(re.search(r"\bi told you\b", low) and message_might_assert_heritage(text))


def message_might_assert_heritage(message: str) -> bool:
    low = str(message or "").lower()
    if not re.search(r"\b(?:i(?:'m| am)|my heritage|we(?:'re| are))\b", low):
        return False
    for terms in _HERITAGE_ROOT_TERMS.values():
        if any(re.search(rf"\b{re.escape(t)}\b", low) for t in terms):
            return True
    return False


def fetch_active_heritage_claims(user_id: str) -> list[tuple[str, str]]:
    """Return (concept, label) for active heritage rows."""
    try:
        res = (
            service_client()
            .table("user_identity_claims")
            .select("concept, label")
            .eq("user_id", user_id)
            .eq("bucket", "heritage")
            .is_("dismissed_at", "null")
            .execute()
        )
    except Exception:
        logger.exception("fetch_active_heritage_claims_failed")
        return []
    out: list[tuple[str, str]] = []
    for row in res.data or []:
        if not isinstance(row, dict):
            continue
        concept = str(row.get("concept") or "").strip()
        label = str(row.get("label") or "").strip()
        if not concept and not label:
            continue
        if is_negative_claim(
            ExtractedClaim(concept=concept, label=label, confidence=1.0)
        ):
            continue
        out.append((concept, label))
    return out


def detect_heritage_conflict(
    user_id: str,
    new_claims: list[ExtractedClaim],
) -> tuple[str, ExtractedClaim] | None:
    """When user asserts a new heritage that contradicts stored heritage."""
    existing = fetch_active_heritage_claims(user_id)
    if not existing:
        return None
    positives = [
        c for c in new_claims if c.bucket == "heritage" and not is_negative_claim(c)
    ]
    if not positives:
        return None
    if len(positives) > 1:
        return None
    new = positives[0]
    new_key = heritage_claim_key(new.concept, new.label)
    for ex_concept, ex_label in existing:
        if heritage_claim_key(ex_concept, ex_label) == new_key:
            return None
        return (ex_label, new)
    return None


def heritage_conflict_prompt(from_label: str, new_claim: ExtractedClaim) -> str:
    return (
        f"I thought your heritage was {from_label}. "
        f"Should I change it to {new_claim.label}? "
        f"Say yes to update, or no to keep {from_label}."
    )


def pending_heritage_from_claim(from_label: str, claim: ExtractedClaim) -> dict[str, Any]:
    return {
        "from_label": from_label,
        "label": claim.label,
        "claim": claim.model_dump(),
    }


def claim_from_pending(pending: dict[str, Any]) -> ExtractedClaim:
    raw = pending.get("claim") or {}
    return ExtractedClaim(**raw)


def is_negative_claim(claim: ExtractedClaim) -> bool:
    blob = " ".join(
        str(x or "")
        for x in (claim.concept, claim.label, claim.source_quote)
    ).lower()
    if _NEGATIVE_CLAIM_RE.search(blob):
        return True
    if re.search(r"\bno\s+\w+\s+heritage\b", blob):
        return True
    if re.search(r"\bnot\s+\w+\s+heritage\b", blob):
        return True
    return bool(re.match(r"^(no|not)\b", blob.strip()))


def filter_extracted_claims(
    message: str,
    claims: list[ExtractedClaim],
) -> list[ExtractedClaim]:
    """Drop negative and vague junk before persist."""
    out: list[ExtractedClaim] = []
    for c in claims:
        if is_negative_claim(c):
            continue
        label = str(c.label or "").strip().lower()
        if label in {"nearby", "near me", "on my block", "lives on my block"}:
            continue
        if c.bucket == "heritage" and not _heritage_root(c.concept, c.label):
            if re.search(r"\b(?:speaker|heritage)\b", label) and len(label) > 24:
                continue
        out.append(c)
    return out[:4]


def _dismiss_claims_by_ids(sb: Any, claim_ids: list[str]) -> None:
    if not claim_ids:
        return
    from datetime import datetime, timezone

    sb.table("user_identity_claims").update(
        {"dismissed_at": datetime.now(timezone.utc).isoformat()}
    ).in_("id", claim_ids).execute()


def reconcile_heritage_claims(user_id: str, batch: list[ExtractedClaim]) -> None:
    """One heritage slot per user — new batch replaces prior rows (dual in one line kept)."""
    positive = [
        c for c in batch if c.bucket == "heritage" and not is_negative_claim(c)
    ]
    if not positive:
        return
    sb = service_client()
    res = (
        sb.table("user_identity_claims")
        .select("id, concept, label, bucket")
        .eq("user_id", user_id)
        .eq("bucket", "heritage")
        .is_("dismissed_at", "null")
        .execute()
    )
    to_dismiss: list[str] = []
    batch_concepts = {c.concept for c in positive}
    for row in res.data or []:
        if not isinstance(row, dict):
            continue
        cid = str(row.get("id") or "")
        concept = str(row.get("concept") or "")
        label = str(row.get("label") or "")
        if is_negative_claim(
            ExtractedClaim(concept=concept, label=label, confidence=1.0)
        ):
            to_dismiss.append(cid)
            continue
        if concept in batch_concepts:
            continue
        to_dismiss.append(cid)
    _dismiss_claims_by_ids(sb, to_dismiss)


def dismiss_claims_from_edit_message(user_id: str, message: str) -> int:
    """Dismiss claims the user explicitly asked to remove."""
    low = message.lower()
    if not re.search(r"\b(?:remove|delete|drop|clear|get rid of)\b", low):
        return 0
    roots_to_remove: set[str] = set()
    for m in re.finditer(
        r"\b(?:remove|delete|drop|clear|get rid of)\b[^.;!?]{0,80}",
        low,
    ):
        chunk = m.group(0)
        for root, terms in _HERITAGE_ROOT_TERMS.items():
            if any(re.search(rf"\b{re.escape(t)}\b", chunk) for t in terms):
                roots_to_remove.add(root)
    sb = service_client()
    res = (
        sb.table("user_identity_claims")
        .select("id, concept, label, bucket")
        .eq("user_id", user_id)
        .is_("dismissed_at", "null")
        .execute()
    )
    to_dismiss: list[str] = []
    for row in res.data or []:
        if not isinstance(row, dict):
            continue
        cid = str(row.get("id") or "")
        concept = str(row.get("concept") or "")
        label = str(row.get("label") or "")
        if is_negative_claim(
            ExtractedClaim(concept=concept, label=label, confidence=1.0)
        ):
            to_dismiss.append(cid)
            continue
        root = _heritage_root(concept, label)
        if root and root in roots_to_remove:
            to_dismiss.append(cid)
    _dismiss_claims_by_ids(sb, list(dict.fromkeys(to_dismiss)))
    return len(to_dismiss)


def scrub_negative_heritage_claims(user_id: str) -> None:
    """Remove invalid negative heritage rows (e.g. 'Not Brazilian Heritage')."""
    reconcile_heritage_claims(user_id, [])


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
    """Merge claims by concept; reconcile heritage bucket per batch."""
    sb = service_client()
    saved = 0
    heritage_batch = [c for c in claims if c.bucket == "heritage"]
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
    reconcile_heritage_claims(user_id, heritage_batch)
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


def regex_claims_from_message(message: str) -> list[ExtractedClaim]:
    """Rule-based fallback when Flash extract returns nothing for clear self-claims."""
    text = str(message or "").strip()
    low = text.lower()
    if not text or not re.search(r"\b(?:i(?:'m| am)|i have(?: a)?|we(?:'re| are))\b", low):
        return []
    quote = text[:120]
    out: list[ExtractedClaim] = []

    for root, terms in _HERITAGE_ROOT_TERMS.items():
        if not any(re.search(rf"\b{re.escape(t)}\b", low) for t in terms):
            continue
        if not re.search(r"\b(?:i(?:'m| am)|my heritage)\b", low):
            continue
        label = "American" if root == "american" else f"{root.title()} Heritage"
        out.append(
            ExtractedClaim(
                concept=f"{root}_heritage",
                label=label,
                confidence=0.88,
                disclosure="public",
                source_quote=quote,
                bucket="heritage",
            )
        )
        break

    if re.search(r"\b(?:young child|toddler|newborn|infant)\b", low):
        out.append(
            ExtractedClaim(
                concept="parent_young_child",
                label="Parent of young child",
                confidence=0.88,
                disclosure="public",
                source_quote=quote,
                bucket="stage",
            )
        )
    elif re.search(r"\bi have\b", low) and re.search(r"\b(?:child|kid)\b", low):
        out.append(
            ExtractedClaim(
                concept="parent",
                label="Parent",
                confidence=0.85,
                disclosure="public",
                source_quote=quote,
                bucket="stage",
            )
        )

    if re.search(r"\bteacher\b", low):
        out.append(
            ExtractedClaim(
                concept="teacher",
                label="Teacher",
                confidence=0.88,
                disclosure="public",
                source_quote=quote,
                bucket="activity",
            )
        )

    return out[:4]


def try_upsert_claims_from_message(
    user_id: str,
    message: str,
    *,
    force_heritage_replace: bool = False,
    skip_heritage: bool = False,
) -> ClaimExtractResult:
    """Flash extract from one user line → upsert claims; confirm heritage conflicts."""
    stated_nick = persist_nickname_if_stated(user_id, message)
    if not should_extract_claims_from_message(message):
        return ClaimExtractResult(nickname=stated_nick)
    try:
        data = incremental_claims_from_utterance(message)
        nickname, claims = parse_incremental_claims_data(data)
    except Exception:
        logger.exception("incremental_claim_extract_failed")
        return ClaimExtractResult(nickname=stated_nick)
    if nickname and not stated_nick:
        nickname = _normalize_nickname(nickname)
        persist_profile_patch(user_id, {"nickname": nickname})
        stated_nick = nickname
    if not claims:
        return ClaimExtractResult(nickname=stated_nick)
    claims = filter_extracted_claims(message, claims)
    if not claims:
        return ClaimExtractResult(nickname=stated_nick)

    heritage = [c for c in claims if c.bucket == "heritage"]
    other = [c for c in claims if c.bucket != "heritage"]

    if skip_heritage:
        claims = other
        heritage = []
    elif heritage and not force_heritage_replace and not is_explicit_heritage_correction(message):
        conflict = detect_heritage_conflict(user_id, heritage)
        if conflict:
            from_label, new_claim = conflict
            saved = upsert_claims(user_id, other) if other else 0
            return ClaimExtractResult(
                saved=saved,
                heritage_conflict=pending_heritage_from_claim(from_label, new_claim),
                nickname=stated_nick,
            )

    saved = upsert_claims(user_id, claims)
    return ClaimExtractResult(saved=saved, nickname=stated_nick)


def extract_and_upsert_claims_from_message(
    user_id: str,
    message: str,
    *,
    skip_heritage: bool = False,
) -> int:
    """Background job: Flash extract from one user line → upsert claims + nickname."""
    return try_upsert_claims_from_message(
        user_id,
        message,
        skip_heritage=skip_heritage,
    ).saved
