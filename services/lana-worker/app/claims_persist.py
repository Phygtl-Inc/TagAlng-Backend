"""Incremental identity claims: extract from each user turn and upsert to Postgres."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from app.auth import service_client
from app.claim_embed import claim_embedding_text
from app.models import ExtractedClaim
from app.pii import redact_pii
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
    kids_count: int | None = None
    followup_question: str | None = None
    # The richest claim this turn — used to frame the rapport follow-up tile.
    primary_label: str | None = None
    primary_bucket: str | None = None


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


def fetch_active_claim_labels(user_id: str) -> list[str]:
    """Active claim labels, so the extractor can MERGE instead of spawning a
    near-duplicate thread (e.g. 'English Speaker' next to 'Speaks 10 languages')."""
    try:
        res = (
            service_client()
            .table("user_identity_claims")
            .select("label, concept")
            .eq("user_id", user_id)
            .is_("dismissed_at", "null")
            .limit(40)
            .execute()
        )
    except Exception:
        logger.exception("fetch_active_claim_labels_failed")
        return []
    out: list[str] = []
    for row in res.data or []:
        if not isinstance(row, dict):
            continue
        label = str(row.get("label") or row.get("concept") or "").strip()
        if label:
            out.append(label)
    return out


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


_HERITAGE_RELATION_PROMPT = """You compare two heritage/ancestry statements from the SAME person: one already saved, one they just mentioned. Decide how the new one relates to the saved one.

Output ONLY JSON: {"relation": "same" | "refine" | "broaden" | "additional" | "conflict"}

- "same": identical heritage, different wording (e.g. saved "Italian", new "Italy").
- "refine": the NEW one is a more specific region/subculture WITHIN the saved one (e.g. saved "Italian", new "Sicilian"; saved "German", new "Bavarian").
- "broaden": the NEW one is a broader region that CONTAINS the saved one (e.g. saved "Sicilian", new "Italian"; saved "Paulista", new "Brazilian").
- "additional": a second, compatible heritage a person can hold at once (e.g. saved "Italian", new "Brazilian" → dual heritage).
- "conflict": genuinely incompatible — the new one replaces the saved one with an UNRELATED nationality/culture (e.g. saved "Italian", new "Korean").

Regional or sub-cultural variants (Sicilian/Italian, Bavarian/German, Catalan/Spanish, Paulista/Brazilian) are NEVER "conflict" — they are "refine" or "broaden". Only use "conflict" for a real contradiction between unrelated heritages."""


def resolve_heritage_relation(existing_label: str, new_label: str) -> str:
    """AI verdict on how a newly stated heritage relates to the stored one.

    Regional heritages (Sicilian↔Italian, Bavarian↔German) are NOT contradictions, so
    we must not prompt the user to 'swap'. Returns one of same/refine/broaden/additional/
    conflict. Falls back to a regex root-match ('same') or, when unresolved, 'conflict'
    (the conservative pre-AI behavior) if the model is unavailable.
    """
    ex = str(existing_label or "").strip()
    new = str(new_label or "").strip()
    if not ex or not new:
        return "additional"
    try:
        from app.orchestrator.llm import llm_configured, llm_json, router_model

        if llm_configured():
            data = llm_json(
                model=router_model(),
                system=_HERITAGE_RELATION_PROMPT,
                user_payload=f'saved: "{ex}"\nnew: "{new}"',
                max_tokens=200,
                temperature=0.0,
            )
            rel = str((data or {}).get("relation", "")).strip().lower()
            if rel in ("same", "refine", "broaden", "additional", "conflict"):
                return rel
    except Exception:
        logger.exception("heritage_relation_resolve_failed")
    if heritage_claim_key("", ex) == heritage_claim_key("", new):
        return "same"
    return "conflict"


def plan_heritage_write(
    user_id: str,
    heritage_claims: list[ExtractedClaim],
) -> tuple[list[ExtractedClaim], tuple[str, ExtractedClaim] | None]:
    """Decide what to persist for a newly stated heritage, given what's stored.

    Returns (heritage_claims_to_persist, pending_conflict). Only a genuine AI-verdict
    'conflict' surfaces a swap prompt; regional refinements/broadenings resolve silently:
      - refine   → keep the NEW (more specific) heritage; it replaces the broader one.
      - same / broaden / additional → keep what's stored; drop the new heritage write
        (nothing to prompt, existing row is preserved — reconcile has no new positive).
      - conflict → drop the new positive, return it as a pending swap prompt.
    Negative heritage claims always pass through (scrubbed downstream).
    """
    negatives = [c for c in heritage_claims if is_negative_claim(c)]
    positives = [c for c in heritage_claims if not is_negative_claim(c)]
    existing = fetch_active_heritage_claims(user_id)
    if not existing or not positives:
        return heritage_claims, None
    if len(positives) > 1:
        # Multiple new heritages at once — let the batch reconcile as before.
        return heritage_claims, None
    new = positives[0]
    new_key = heritage_claim_key(new.concept, new.label)
    for ex_concept, ex_label in existing:
        if heritage_claim_key(ex_concept, ex_label) == new_key:
            return heritage_claims, None  # same root — just refresh the row
        relation = resolve_heritage_relation(ex_label, new.label)
        if relation == "conflict":
            return negatives, (ex_label, new)
        if relation == "refine":
            return heritage_claims, None  # more specific replaces the broader
        # same / broaden / additional → preserve stored heritage, skip the new write
        return negatives, None
    return heritage_claims, None


def detect_heritage_conflict(
    user_id: str,
    new_claims: list[ExtractedClaim],
) -> tuple[str, ExtractedClaim] | None:
    """When user asserts a new heritage that genuinely contradicts stored heritage.

    AI-gated: a regional variant (Sicilian vs Italian) is not a conflict, so it never
    prompts. Thin wrapper over plan_heritage_write for callers that only want the prompt.
    """
    heritage = [c for c in new_claims if c.bucket == "heritage"]
    _, pending = plan_heritage_write(user_id, heritage)
    return pending


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


_UNCERTAINTY_RE = re.compile(
    r"\b(?:unsure|uncertain|not\s+sure|dunno|don'?t\s+know|no\s+idea|"
    r"not\s+certain|what\s+to\s+call|figuring\s+(?:it\s+)?out)\b",
    re.I,
)

# Bare topic headings the model sometimes emits instead of a real thread — e.g.
# "Health" with a lone "wellness" synonym, or "Lifestyle". These are categories,
# not identity claims.
_GENERIC_TOPIC_LABELS = frozenset(
    {
        "health",
        "wellness",
        "wellbeing",
        "well-being",
        "lifestyle",
        "general",
        "misc",
        "other",
        "stuff",
        "things",
        "life",
        "hobbies",
        "activities",
        "interests",
        "interest",
        "activity",
        "about you",
        "about me",
    }
)


def is_noise_claim(claim: ExtractedClaim) -> bool:
    """Degenerate 'claims': bare topic headings and reified uncertainty.

    Catches the extractor emitting a category word ("Health") or its own hedging
    ("Unsure what to call", "Unsure about time") as if it were an identity thread.
    """
    label = str(claim.label or "").strip().lower()
    concept = str(claim.concept or "").strip().lower()
    if not label:
        return True
    if label in _GENERIC_TOPIC_LABELS:
        return True
    if _UNCERTAINTY_RE.search(label) or _UNCERTAINTY_RE.search(concept.replace("_", " ")):
        return True
    return False


def _normalized_label_key(label: str) -> str:
    """Collapse casing/punctuation/trailing-plural so near-identical labels merge."""
    text = re.sub(r"[^a-z0-9\s]", " ", str(label or "").lower())
    tokens = [t[:-1] if len(t) > 3 and t.endswith("s") else t for t in text.split()]
    return " ".join(tokens).strip()


def dedupe_claims(claims: list[ExtractedClaim]) -> list[ExtractedClaim]:
    """Collapse claims describing the same thread (same normalized label).

    The DB unique index is on `concept`, so near-duplicate rows with identical labels
    but different slugs (e.g. 'hosting_neighbor_meetings' vs 'neighbor_gatherings', both
    labeled 'Interested in Hosting Informal Neighbor Meetings') would all persist. Merge
    them: keep the highest-confidence instance and union synonyms.
    """
    by_key: dict[str, ExtractedClaim] = {}
    for c in claims:
        key = _normalized_label_key(c.label) or str(c.concept or "").lower()
        winner = by_key.get(key)
        if winner is None:
            by_key[key] = c
            continue
        keep = winner if winner.confidence >= c.confidence else c
        keep.synonyms = list(dict.fromkeys([*winner.synonyms, *c.synonyms]))[:6]
        # A thread is durable if ANY instance of it was durable.
        keep.transient = winner.transient and c.transient
        by_key[key] = keep
    return list(by_key.values())


def clean_claims_for_persist(claims: list[ExtractedClaim]) -> list[ExtractedClaim]:
    """Single guard before any write: drop negatives + noise, redact PII, dedupe repeats."""
    kept = [c for c in claims if not is_negative_claim(c) and not is_noise_claim(c)]
    # Deterministic PII backstop on every persisted text field (never `concept` — a slug).
    for c in kept:
        c.label = redact_pii(c.label) or c.label
        if c.source_quote:
            c.source_quote = redact_pii(c.source_quote)
        if c.synonyms:
            c.synonyms = [redact_pii(s) or s for s in c.synonyms]
    return dedupe_claims(kept)


def filter_extracted_claims(
    message: str,
    claims: list[ExtractedClaim],
) -> list[ExtractedClaim]:
    """Drop negative, noise, and vague junk before persist; collapse near-duplicates."""
    out: list[ExtractedClaim] = []
    for c in claims:
        if is_negative_claim(c) or is_noise_claim(c):
            continue
        label = str(c.label or "").strip().lower()
        if label in {"nearby", "near me", "on my block", "lives on my block"}:
            continue
        if c.bucket == "heritage" and not _heritage_root(c.concept, c.label):
            if re.search(r"\b(?:speaker|heritage)\b", label) and len(label) > 24:
                continue
        out.append(c)
    return dedupe_claims(out)[:6]


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


def persist_kids_count(user_id: str, kids_count: int | None) -> None:
    """Store the stated number of children (count only — never name/age/school)."""
    if kids_count is None or not (1 <= kids_count <= 20):
        return
    service_client().table("users").update({"kids_count": kids_count}).eq("id", user_id).execute()


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
        "transient": c.transient,
    }
    embedding = _embed_claim(c)
    if embedding is not None:
        row["embedding"] = embedding
    return row


def upsert_claims(user_id: str, claims: list[ExtractedClaim]) -> int:
    """Merge claims by concept; reconcile heritage bucket per batch."""
    claims = clean_claims_for_persist(claims)
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
    claims = clean_claims_for_persist(claims)
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


def _open_rapport_gap(
    user_id: str,
    message_id: str | None,
    followup: str | None,
    label: str | None,
    bucket: str | None,
    teaser: str | None = None,
) -> None:
    """Open one contextual rapport gap from the extractor's follow-up question (best-effort).

    Sensitivity is gated upstream by AI signals, not keywords here: the extractor returns
    followup=null on sensitive/help-seeking topics, so there's nothing to open.
    """
    if not followup:
        return
    try:
        from app.rapport_gaps import open_semantic_gap

        open_semantic_gap(
            user_id, message_id, followup, label=label, bucket=bucket, teaser=teaser
        )
    except Exception:
        logger.exception("rapport: semantic gap open failed")


def try_upsert_claims_from_message(
    user_id: str,
    message: str,
    *,
    force_heritage_replace: bool = False,
    skip_heritage: bool = False,
    message_id: str | None = None,
    allow_rapport_gap: bool = True,
) -> ClaimExtractResult:
    """Flash extract from one user line → upsert claims; confirm heritage conflicts.

    Also opens ONE contextual rapport follow-up gap from the extractor's own warm question.
    This is the SHARED entry point (background task AND the inline discovery/identity path
    call it), so the gap opens regardless of which path handled the turn.
    """
    stated_nick = persist_nickname_if_stated(user_id, message)
    if not should_extract_claims_from_message(message):
        return ClaimExtractResult(nickname=stated_nick)
    try:
        existing_labels = fetch_active_claim_labels(user_id)
        # Recent rapport questions so the extractor's follow-up isn't a near-duplicate.
        recent_questions: list[str] = []
        if allow_rapport_gap:
            try:
                from app.rapport_gaps import recent_gap_questions

                recent_questions = recent_gap_questions(user_id)
            except Exception:
                logger.debug("rapport: recent_gap_questions unavailable")
        data = incremental_claims_from_utterance(message, existing_labels, recent_questions)
        nickname, claims, kids_count, followup = parse_incremental_claims_data(data)
        # AI-written teaser for the tile ("about your reading…") — read straight off the raw
        # dict so we don't churn parse_incremental_claims_data's tuple arity.
        followup_topic = (
            (str(data.get("followup_topic") or "").strip()[:80] or None)
            if isinstance(data, dict)
            else None
        )
    except Exception:
        logger.exception("incremental_claim_extract_failed")
        return ClaimExtractResult(nickname=stated_nick)
    if nickname and not stated_nick:
        nickname = _normalize_nickname(nickname)
        persist_profile_patch(user_id, {"nickname": nickname})
        stated_nick = nickname
    # Kids count is private (count only) — persist regardless of whether other claims survive.
    persist_kids_count(user_id, kids_count)
    if not claims:
        return ClaimExtractResult(
            nickname=stated_nick, kids_count=kids_count, followup_question=followup
        )
    claims = filter_extracted_claims(message, claims)
    if not claims:
        return ClaimExtractResult(
            nickname=stated_nick, kids_count=kids_count, followup_question=followup
        )

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
                kids_count=kids_count,
                followup_question=followup,
            )

    saved = upsert_claims(user_id, claims)
    primary = max(claims, key=lambda c: c.confidence, default=None)
    if allow_rapport_gap:
        _open_rapport_gap(
            user_id,
            message_id,
            followup,
            primary.label if primary else None,
            primary.bucket if primary else None,
            teaser=followup_topic,
        )
    return ClaimExtractResult(
        saved=saved,
        nickname=stated_nick,
        kids_count=kids_count,
        followup_question=followup,
        primary_label=primary.label if primary else None,
        primary_bucket=primary.bucket if primary else None,
    )


def extract_and_upsert_claims_from_message(
    user_id: str,
    message: str,
    *,
    skip_heritage: bool = False,
    message_id: str | None = None,
    allow_rapport_gap: bool = True,
) -> int:
    """Background job: Flash extract from one user line → upsert claims + nickname, and open
    ONE contextual rapport follow-up gap from the extractor's own warm question (semantic,
    not a static template). `allow_rapport_gap` lets the caller defer to the turn's safety
    verdict (crisis / out-of-scope / medical) and suppress the gap while still capturing claims."""
    return try_upsert_claims_from_message(
        user_id,
        message,
        skip_heritage=skip_heritage,
        message_id=message_id,
        allow_rapport_gap=allow_rapport_gap,
    ).saved


def latest_claim_id(user_id: str) -> str | None:
    """The user's most recently created active claim — used to link a rapport answer to the
    claim the extractor just made from it (best-effort). Returns None if there's none."""
    if not user_id:
        return None
    try:
        res = (
            service_client()
            .table("user_identity_claims")
            .select("id")
            .eq("user_id", user_id)
            .is_("dismissed_at", "null")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        return res.data[0]["id"] if res.data else None
    except Exception:
        logger.exception("latest_claim_id_failed for %s", user_id)
        return None
