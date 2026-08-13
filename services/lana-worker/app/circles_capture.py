"""Circles · Stage 1 capture (extract-and-park) — master spec §A.1/§H.1.

The per-turn identity extraction pass (claims_persist -> vertex_extract) also emits:

  * circle_candidates        — real communities the user belongs to ("my Tuesday
    spin class"). Persisted as circle_affiliations rows, status='suggested',
    place_ref NULL (ungrounded). Grounding ("which place?") is a SEPARATE gated
    conversational step and is never triggered from here (§H.3: a circle
    mentioned mid-task is captured but must not interrupt that turn).
  * place_feature_candidates — objective attributes of such a place volunteered
    in passing ("we swim there" -> has_pool). Written to place_features when the
    user already has a confirmed, grounded place for that circle; otherwise
    folded into the affiliation's detail text so grounding can pick them up.

Rides the existing LLM pass — this module never makes a model call of its own
(§4.2 latency guardrail); embeddings reuse the claim embedder. Defensive by
contract: runs inside the background claims task and never raises.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from app.auth import service_client

logger = logging.getLogger(__name__)

CIRCLE_TYPES = frozenset(
    {
        "school",
        "faith",
        "fitness",
        "kids_activity",
        "neighborhood",
        "hobby",
        "support",
        "heritage",
        "friends",
        "other",
    }
)

_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_MIN_CANDIDATE_CONFIDENCE = 0.6
_MAX_CANDIDATES = 4
# Mirrors claims: a re-mention is corroboration, confidence walks up.
_CORROBORATION_BUMP = 0.05
_DETAIL_MAX = 200


@dataclass
class CircleCandidate:
    circle_type: str
    circle_key: str
    raw_phrase: str
    confidence: float = 0.6
    # The venue name the user actually SAID ("Fitness CF"), "" when they named only
    # the activity ("my gym"). Grounding searches THIS, never the whole phrase —
    # a sentence matches no place, and the type-keyword fallback then offered
    # arbitrary nearby spots as if they were the user's own (2026-08-03).
    place_name: str = ""
    # What this community IS, in the user's terms, chosen by the extractor at
    # capture. circle_type cannot supply either: it is a ten-value grouping bucket
    # where every sport is "fitness", so a table-tennis club rendered as "your gym"
    # with a 🏋️ (2026-08-07). Empty falls back to the type maps in circles_flow.
    noun: str = ""
    emoji: str = ""


@dataclass
class PlaceFeatureCandidate:
    circle_key: str
    key: str
    value: str | None
    sub_group: str = ""
    confidence: float = 0.6


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", str(text or "").lower()).strip("_")
    slug = re.sub(r"^(?:my|our|the|a|an)_", "", slug)[:64]
    if slug and not slug[0].isalpha():
        slug = "x_" + slug
    return slug if _KEY_RE.match(slug or "") else ""


def _clean_noun(raw: Any) -> str:
    """A short lower-case noun. Rejects anything sentence-shaped — it is rendered as
    "your <noun>", so a stray clause there reads as gibberish to the user."""
    text = re.sub(r"\s+", " ", str(raw or "").strip().lower())
    if not text or len(text) > 32 or len(text.split()) > 3:
        return ""
    return text if re.fullmatch(r"[a-z0-9][a-z0-9 '\-]*", text) else ""


def _clean_emoji(raw: Any) -> str:
    """Exactly one emoji, via the same validator event cover art uses."""
    try:
        from app.lana_ui import sanitize_cover_emoji

        return str(sanitize_cover_emoji(raw) or "")
    except Exception:
        return ""


def parse_circle_candidates(data: Any) -> list[CircleCandidate]:
    """Validate the extractor's circle_candidates block into clean rows."""
    if not isinstance(data, dict):
        return []
    raw = data.get("circle_candidates", [])
    if not isinstance(raw, list):
        return []
    out: list[CircleCandidate] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        circle_type = str(item.get("circle_type", "")).strip().lower()
        if circle_type not in CIRCLE_TYPES:
            continue
        phrase = str(item.get("raw_phrase", "")).strip()[:120]
        key = str(item.get("circle_key", "")).strip().lower()
        if not _KEY_RE.match(key):
            key = _slugify(phrase) or _slugify(circle_type)
        if not key or key in seen:
            continue
        try:
            conf = max(0.0, min(1.0, float(item.get("confidence", 0.6))))
        except (TypeError, ValueError):
            conf = 0.6
        seen.add(key)
        out.append(
            CircleCandidate(
                circle_type=circle_type,
                circle_key=key,
                raw_phrase=phrase or key.replace("_", " "),
                confidence=conf,
                place_name=str(item.get("place_name") or "").strip()[:120],
                noun=_clean_noun(item.get("noun")),
                emoji=_clean_emoji(item.get("emoji")),
            )
        )
    return out[:_MAX_CANDIDATES]


def parse_place_feature_candidates(data: Any) -> list[PlaceFeatureCandidate]:
    """Validate the extractor's place_feature_candidates block."""
    if not isinstance(data, dict):
        return []
    raw = data.get("place_feature_candidates", [])
    if not isinstance(raw, list):
        return []
    out: list[PlaceFeatureCandidate] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        circle_key = str(item.get("circle_key", "")).strip().lower()
        key = str(item.get("key", "")).strip().lower()
        if not _KEY_RE.match(circle_key) or not _KEY_RE.match(key):
            continue
        value_raw = item.get("value")
        value = None if value_raw is None else str(value_raw).strip()[:120]
        sub_group = str(item.get("sub_group") or "").strip().lower()[:64]
        try:
            conf = max(0.0, min(1.0, float(item.get("confidence", 0.6))))
        except (TypeError, ValueError):
            conf = 0.6
        out.append(
            PlaceFeatureCandidate(
                circle_key=circle_key,
                key=key,
                value=value,
                sub_group=sub_group,
                confidence=conf,
            )
        )
    return out[:_MAX_CANDIDATES]


def _embed_circle(cand: CircleCandidate) -> list[float] | None:
    try:
        from app.vertex_extract import vertex_embed

        return vertex_embed(f"{cand.raw_phrase} ({cand.circle_type} community)")
    except Exception:
        logger.exception("circle_embed_failed key=%s", cand.circle_key)
        return None


def _fetch_affiliation(sb: Any, user_id: str, circle_key: str) -> dict[str, Any] | None:
    res = (
        sb.table("circle_affiliations")
        .select("id, confidence, detail, status, place_ref, circle_type, place_name, noun, emoji")
        .eq("user_id", user_id)
        .eq("circle_key", circle_key)
        .is_("dismissed_at", "null")
        .limit(1)
        .execute()
    )
    return (res.data or [None])[0]


# Category words that name a KIND of place, not a particular community. A mention
# this vague is the same community as a specific one of the same type the person
# already has — "the gym" is their Fitness CF, not a second gym.
_GENERIC_KEYS = frozenset(
    {
        "gym", "restaurant", "church", "cafe", "coffee_shop", "bar", "park",
        "school", "store", "shop", "market", "library", "club", "team", "class",
        "group", "studio", "pool", "court", "field", "temple", "mosque",
    }
)


def _key_tokens(key: str) -> set[str]:
    """Meaningful words in a circle key, minus the grouping/category noise."""
    parts = [p for p in str(key or "").lower().split("_") if p]
    return {p for p in parts if p not in _GENERIC_KEYS and len(p) > 1}


def same_community(a_key: str, a_type: str, b_key: str, b_type: str) -> bool:
    """Are these two circle keys the SAME real-world community?

    Capture creates one row per phrasing, so a single gym accumulated four:
    `gym`, `fitness_cf`, `fitness_cf_st_cloud`, `crossfit_st_cloud` — and a single
    church three. Each one then queues its own "which spot is it?" question, so the
    queue was six deep for two actual communities, and one of those questions was
    asked about a gym that was ALREADY pinned (2026-08-07).

    Deliberately conservative and deterministic — merging two genuinely different
    communities is worse than leaving a duplicate, so only two cases count:
      · same type and one side is a bare category ("gym" ~ "fitness_cf")
      · same type and one side's words contain the other's
        ("fitness_cf" ~ "fitness_cf_st_cloud")
    """
    if not a_key or not b_key or a_type != b_type:
        return False
    a, b = str(a_key).lower(), str(b_key).lower()
    if a == b:
        return True
    if a in _GENERIC_KEYS or b in _GENERIC_KEYS:
        return True
    ta, tb = _key_tokens(a), _key_tokens(b)
    if not ta or not tb:
        return False
    return ta <= tb or tb <= ta


def _fetch_same_community(
    sb: Any, user_id: str, cand: CircleCandidate
) -> dict[str, Any] | None:
    """An existing circle that is the same community as this candidate.

    Prefers a GROUNDED row: merging into the one already pinned to a real place is
    what stops a fourth duplicate queueing a question we can already answer.
    """
    try:
        res = (
            sb.table("circle_affiliations")
            .select(
                "id, confidence, detail, status, place_ref, circle_type, "
                "place_name, circle_key, noun, emoji"
            )
            .eq("user_id", user_id)
            .eq("circle_type", cand.circle_type)
            .is_("dismissed_at", "null")
            .limit(20)
            .execute()
        )
        rows = [r for r in (res.data or []) if isinstance(r, dict)]
    except Exception:
        logger.exception("fetch_same_community_failed key=%s", cand.circle_key)
        return None
    hits = [
        r
        for r in rows
        if same_community(
            cand.circle_key, cand.circle_type, str(r.get("circle_key") or ""),
            str(r.get("circle_type") or ""),
        )
    ]
    if not hits:
        return None
    hits.sort(key=lambda r: (0 if r.get("place_ref") else 1, str(r.get("circle_key") or "")))
    return hits[0]


def persist_circle_candidates(user_id: str, candidates: list[CircleCandidate]) -> int:
    """Upsert suggested affiliations; a re-mention corroborates (confidence rises).

    Never touches place_ref or status — a suggested row is promoted to confirmed
    only by the grounding flow, and a confirmed row is never downgraded here.
    """
    sb = service_client()
    saved = 0
    for cand in candidates:
        if cand.confidence < _MIN_CANDIDATE_CONFIDENCE:
            continue
        try:
            existing = _fetch_affiliation(
                sb, user_id, cand.circle_key
            ) or _fetch_same_community(sb, user_id, cand)
            if existing:
                if str(existing.get("circle_key") or "") not in ("", cand.circle_key):
                    logger.info(
                        "circle_dedupe user=%s %s -> existing %s",
                        user_id, cand.circle_key, existing.get("circle_key"),
                    )
                try:
                    old_conf = float(existing.get("confidence") or 0.0)
                except (TypeError, ValueError):
                    old_conf = 0.0
                patch: dict[str, Any] = {
                    "confidence": min(
                        1.0, max(old_conf, cand.confidence) + _CORROBORATION_BUMP
                    )
                }
                # "I go to the gym" first, "it's Fitness CF" later — a mention that
                # finally names the venue fills the blank so grounding can search
                # it. Never overwrites a name we already have (they'd correct it
                # through grounding, not through a passing re-mention).
                if cand.place_name and not str(existing.get("place_name") or "").strip():
                    patch["place_name"] = cand.place_name
                # Backfill only — a re-mention fills a blank noun/emoji so rows
                # captured before 20261008 stop rendering as their bucket, but never
                # overwrites one we already have.
                if cand.noun and not str(existing.get("noun") or "").strip():
                    patch["noun"] = cand.noun
                if cand.emoji and not str(existing.get("emoji") or "").strip():
                    patch["emoji"] = cand.emoji
                sb.table("circle_affiliations").update(patch).eq(
                    "id", existing["id"]
                ).execute()
            else:
                row: dict[str, Any] = {
                    "user_id": user_id,
                    "circle_type": cand.circle_type,
                    "circle_key": cand.circle_key,
                    "status": "suggested",
                    "source": "chat_extraction",
                    "confidence": cand.confidence,
                    "detail": cand.raw_phrase[:_DETAIL_MAX],
                }
                if cand.place_name:
                    row["place_name"] = cand.place_name
                if cand.noun:
                    row["noun"] = cand.noun
                if cand.emoji:
                    row["emoji"] = cand.emoji
                embedding = _embed_circle(cand)
                if embedding is not None:
                    row["embedding"] = embedding
                sb.table("circle_affiliations").insert(row).execute()
            saved += 1
        except Exception:
            logger.exception("persist_circle_candidate_failed key=%s", cand.circle_key)
    return saved


def upsert_place_feature(
    *,
    place_id: str,
    key: str,
    value: str | None,
    sub_group: str = "",
    confidence: float = 0.6,
    source: str = "rapport",
    contributed_by: str | None = None,
    emoji: str = "",
) -> bool:
    """One truth per (place, key, sub_group). Write policy (documented on the table):
    overwrite only when the new confidence >= stored; source='owner' rows are never
    overwritten by 'rapport'/'inferred' writes. Returns True when a row was written.
    """
    sb = service_client()
    res = (
        sb.table("place_features")
        .select("id, confidence, source")
        .eq("place_id", place_id)
        .eq("key", key)
        .eq("sub_group", sub_group or "")
        .limit(1)
        .execute()
    )
    existing = (res.data or [None])[0]
    if existing:
        if existing.get("source") == "owner" and source != "owner":
            return False
        try:
            old_conf = float(existing.get("confidence") or 0.0)
        except (TypeError, ValueError):
            old_conf = 0.0
        if confidence < old_conf:
            return False
        patch: dict[str, Any] = {
            "value": value,
            "confidence": confidence,
            "source": source,
            "contributed_by": contributed_by,
        }
        # Never blank an emoji the row already has: the chat path doesn't pick one.
        if emoji:
            patch["emoji"] = emoji
        sb.table("place_features").update(patch).eq("id", existing["id"]).execute()
        return True
    sb.table("place_features").insert(
        {
            "place_id": place_id,
            "key": key,
            "value": value,
            "sub_group": sub_group or "",
            "confidence": confidence,
            "source": source,
            "contributed_by": contributed_by,
            "emoji": emoji or None,
        }
    ).execute()
    return True


def persist_place_feature_candidates(
    user_id: str, candidates: list[PlaceFeatureCandidate]
) -> int:
    """Route each feature to the user's affiliation with that circle_key.

    Grounded + confirmed -> place_features on its place (learned once, reused for
    every member). Not grounded yet -> fold into the affiliation's detail text so
    the grounding flow can flush it later. No matching affiliation -> drop (a
    feature is only trustworthy about a place the user actually belongs to).
    """
    sb = service_client()
    saved = 0
    for cand in candidates:
        if cand.confidence < _MIN_CANDIDATE_CONFIDENCE:
            continue
        try:
            aff = _fetch_affiliation(sb, user_id, cand.circle_key)
            if not aff:
                continue
            place_ref = aff.get("place_ref")
            if place_ref and aff.get("status") == "confirmed":
                if upsert_place_feature(
                    place_id=str(place_ref),
                    key=cand.key,
                    value=cand.value,
                    sub_group=cand.sub_group,
                    confidence=cand.confidence,
                    source="rapport",
                    contributed_by=user_id,
                ):
                    saved += 1
                continue
            note = f"{cand.key}={cand.value or 'true'}"
            detail = str(aff.get("detail") or "")
            if note not in detail:
                merged = f"{detail}; {note}".strip("; ")[:_DETAIL_MAX]
                sb.table("circle_affiliations").update({"detail": merged}).eq(
                    "id", aff["id"]
                ).execute()
                saved += 1
        except Exception:
            logger.exception("persist_place_feature_failed key=%s", cand.key)
    return saved


def run_circle_capture(user_id: str, data: Any) -> dict[str, int]:
    """Single entry point from the claims pass. Reads the raw extractor dict
    (same pattern as followup_topic) and persists both candidate kinds.
    Never raises."""
    result = {"circles": 0, "features": 0}
    if not user_id:
        return result
    try:
        parsed_raw = (data or {}).get("circle_candidates") if isinstance(data, dict) else None
        circles = parse_circle_candidates(data)
        if circles:
            result["circles"] = persist_circle_candidates(user_id, circles)
            if result["circles"]:
                # Queue the tile's grounding question ("which spot is it?") while the
                # mention is fresh — asking the day they said it is warm continuity;
                # three weeks later it's surveillance. Capped + idempotent inside.
                try:
                    from app.circles_flow import ensure_grounding_gaps

                    ensure_grounding_gaps(user_id)
                except Exception:
                    logger.exception("ensure_grounding_gaps_failed user=%s", user_id)
        features = parse_place_feature_candidates(data)
        if features:
            result["features"] = persist_place_feature_candidates(user_id, features)
        # Always log the verdict — "emitted nothing" and "persisted N" must both be
        # visible, or a silent no-capture turn is indistinguishable from a failure.
        # `field` separates the two ways emitted=0 happens: "absent" means the model
        # never wrote the key (it skimmed past the circles rules — a prompt/model-tier
        # problem), "empty" means it considered the message and declined (correct for
        # most turns). Without this the two were indistinguishable (2026-08-05).
        field = (
            "absent"
            if not isinstance(data, dict) or "circle_candidates" not in data
            else "empty"
            if not parsed_raw
            else "present"
        )
        logger.info(
            "circle_capture user=%s field=%s emitted=%d persisted=%d features=%d",
            user_id,
            field,
            len(circles),
            result["circles"],
            result["features"],
        )
    except Exception:
        logger.exception("run_circle_capture_failed")
    return result
