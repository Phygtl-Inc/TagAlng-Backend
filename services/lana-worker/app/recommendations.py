"""Lana-mediated value recommendations.

This is not a public "recommended neighbors" list. It ranks heterogeneous
things Lana can safely mediate: neighbors, activities, and local need/offer
signals.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any

from app.auth import service_client

RECOMMENDATION_TYPES = frozenset({"neighbor", "event", "local_signal"})
DEFAULT_LIMIT = 5


def recommend_value_for_user(
    *,
    user_id: str,
    block_id: str | None,
    query: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> list[dict[str, Any]]:
    """Return top Lana-mediated candidates for the current user.

    Best-effort DB access: if one source is unavailable, other sources can still
    contribute candidates.
    """
    cap = max(1, min(int(limit or DEFAULT_LIMIT), 10))
    user_claims = _fetch_user_claims(user_id)
    candidates: list[dict[str, Any]] = []
    candidates.extend(_neighbor_candidates(user_id=user_id, block_id=block_id, user_claims=user_claims))
    candidates.extend(_event_candidates(user_id=user_id, block_id=block_id, query=query, user_claims=user_claims))
    candidates.extend(_local_signal_candidates(user_id=user_id, block_id=block_id, query=query, user_claims=user_claims))
    ranked = rank_recommendation_candidates(candidates, query=query)
    return ranked[:cap]


def log_recommendation_impressions(
    *,
    user_id: str,
    session_id: str | None,
    block_id: str | None,
    recommendations: list[dict[str, Any]],
    query: str | None = None,
    surface: str = "lana_chat",
) -> None:
    """Best-effort impression logging.

    If the migration is not applied yet, recommendation serving still works.
    """
    rows: list[dict[str, Any]] = []
    for rec in recommendations:
        if not isinstance(rec, dict):
            continue
        rtype = str(rec.get("type") or "")
        if rtype not in RECOMMENDATION_TYPES:
            continue
        row: dict[str, Any] = {
            "user_id": user_id,
            "session_id": session_id,
            "block_id": block_id,
            "recommendation_type": rtype,
            "score": _clamp(rec.get("score")),
            "reason_codes": [str(c)[:64] for c in rec.get("reason_codes", []) if c],
            "suggested_action": str(rec.get("suggested_action") or "unknown")[:80],
            "safe_reason": str(rec.get("safe_reason") or "")[:280] or None,
            "query": str(query or "")[:500] or None,
            "surface": surface[:80],
            "status": "shown",
            "metadata": {
                "signal_kind": rec.get("signal_kind"),
                "matching_peer_concept": rec.get("matching_peer_concept"),
                "category": rec.get("category"),
            },
        }
        if rec.get("candidate_user_id"):
            row["candidate_user_id"] = rec["candidate_user_id"]
        if rec.get("event_id"):
            row["event_id"] = rec["event_id"]
        if rec.get("signal_id"):
            row["signal_id"] = rec["signal_id"]
        if row.get("candidate_user_id") or row.get("event_id") or row.get("signal_id"):
            rows.append(row)
    if not rows:
        return
    try:
        service_client().table("recommendation_impressions").insert(rows).execute()
    except Exception:
        return


def rank_recommendation_candidates(
    candidates: list[dict[str, Any]],
    *,
    query: str | None = None,
) -> list[dict[str, Any]]:
    ranked: list[dict[str, Any]] = []
    for c in candidates:
        if not isinstance(c, dict):
            continue
        ctype = str(c.get("type") or "").strip()
        if ctype not in RECOMMENDATION_TYPES:
            continue
        if c.get("blocked") or c.get("safety_penalty") == 1:
            continue
        item = dict(c)
        score = _score_candidate(item, query=query)
        item["score"] = round(score, 4)
        item["reason_codes"] = _reason_codes(item)
        item["suggested_action"] = _suggested_action(item)
        item["safe_reason"] = _safe_reason(item)
        ranked.append(_public_shape(item))
    ranked.sort(key=lambda r: (float(r.get("score") or 0), _type_priority(str(r.get("type") or ""))), reverse=True)
    return ranked


def _score_candidate(c: dict[str, Any], *, query: str | None) -> float:
    ctype = str(c.get("type") or "")
    identity = _clamp(c.get("identity_affinity"))
    activity = _clamp(c.get("activity_affinity"))
    vicinity = _clamp(c.get("vicinity_score"))
    connector = _clamp(c.get("connector_score"))
    responsiveness = _clamp(c.get("responsiveness_score"))
    freshness = _freshness_score(c.get("last_activity_at") or c.get("starts_at") or c.get("created_at"))
    diversity = _clamp(c.get("diversity_bonus"))
    query_match = _query_match_score(query, c)
    safety_penalty = _clamp(c.get("safety_penalty"))

    if ctype == "neighbor":
        score = (
            0.30 * identity
            + 0.15 * activity
            + 0.15 * vicinity
            + 0.22 * connector
            + 0.10 * responsiveness
            + 0.05 * freshness
            + 0.03 * diversity
        )
    elif ctype == "event":
        score = (
            0.15 * identity
            + 0.35 * activity
            + 0.20 * vicinity
            + 0.10 * connector
            + 0.05 * responsiveness
            + 0.10 * freshness
            + 0.05 * query_match
        )
    else:
        score = (
            0.20 * identity
            + 0.30 * activity
            + 0.20 * vicinity
            + 0.05 * connector
            + 0.05 * responsiveness
            + 0.10 * freshness
            + 0.10 * query_match
        )
    return max(0.0, min(1.0, score - safety_penalty))


def _neighbor_candidates(
    *,
    user_id: str,
    block_id: str | None,
    user_claims: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        rows = (
            service_client()
            .rpc(
                "match_peers_by_claim_vectors_for_user",
                {"p_user_id": user_id, "p_limit": 12, "p_min_similarity": 0.55},
            )
            .execute()
            .data
            or []
        )
    except Exception:
        rows = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        peer_id = str(row.get("peer_user_id") or "").strip()
        if not peer_id or peer_id == user_id:
            continue
        sim = _clamp(row.get("similarity_score"))
        out.append(
            {
                "type": "neighbor",
                "candidate_user_id": peer_id,
                "nickname": row.get("nickname"),
                "avatar_url": row.get("avatar_url"),
                "identity_affinity": sim,
                "activity_affinity": _activity_overlap_from_label(row.get("matching_peer_label"), user_claims),
                "vicinity_score": 1.0 if block_id else 0.4,
                "connector_score": 0.45 + (0.15 if row.get("has_exact_concept_match") else 0.0),
                "responsiveness_score": 0.5,
                "freshness_score": 0.5,
                "matching_peer_label": row.get("matching_peer_label"),
                "matching_peer_concept": row.get("matching_peer_concept"),
            }
        )
    return out


def _event_candidates(
    *,
    user_id: str,
    block_id: str | None,
    query: str | None,
    user_claims: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not block_id:
        return []
    try:
        rows = (
            service_client()
            .table("events")
            .select("id,title,description,starts_at,venue_name,host_id,cohort_tags,created_at")
            .eq("block_id", block_id)
            .eq("status", "open")
            .order("starts_at")
            .limit(12)
            .execute()
            .data
            or []
        )
    except Exception:
        rows = []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("host_id") or "") == user_id:
            continue
        tags = [str(t).lower() for t in (row.get("cohort_tags") or []) if t]
        label_text = " ".join([str(row.get("title") or ""), str(row.get("description") or ""), " ".join(tags)])
        affinity = max(_claim_text_overlap(label_text, user_claims), _query_match_score(query, row))
        out.append(
            {
                "type": "event",
                "event_id": row.get("id"),
                "title": row.get("title"),
                "starts_at": row.get("starts_at"),
                "venue_name": row.get("venue_name"),
                "activity_affinity": affinity,
                "identity_affinity": _claim_text_overlap(" ".join(tags), user_claims),
                "vicinity_score": 1.0,
                "connector_score": 0.55 if row.get("host_id") else 0.35,
                "responsiveness_score": 0.6,
                "created_at": row.get("created_at"),
            }
        )
    return out


def _local_signal_candidates(
    *,
    user_id: str,
    block_id: str | None,
    query: str | None,
    user_claims: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not block_id:
        return []
    try:
        rows = (
            service_client()
            .table("inquiry_signals")
            .select("id,user_id,category,free_text,urgency,created_at,captured_at,status")
            .eq("block_id", block_id)
            .eq("status", "open")
            .order("captured_at", desc=True)
            .limit(20)
            .execute()
            .data
            or []
        )
    except Exception:
        rows = []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("user_id") or "") == user_id:
            continue
        category = str(row.get("category") or "local_need").lower()
        free_text = str(row.get("free_text") or "")
        if _is_sensitive_signal(category, free_text):
            continue
        ctype = _signal_kind(category, free_text)
        text = f"{category}: {free_text}"
        out.append(
            {
                "type": "local_signal",
                "signal_id": row.get("id"),
                "signal_kind": ctype,
                "category": category,
                "excerpt": _safe_excerpt(free_text),
                "activity_affinity": max(_query_match_score(query, row), _claim_text_overlap(text, user_claims)),
                "identity_affinity": _claim_text_overlap(text, user_claims),
                "vicinity_score": 1.0,
                "connector_score": 0.25,
                "responsiveness_score": 0.4,
                "created_at": row.get("captured_at") or row.get("created_at"),
                "urgency": row.get("urgency"),
            }
        )
    return out


def _fetch_user_claims(user_id: str) -> list[dict[str, Any]]:
    try:
        rows = (
            service_client()
            .table("user_identity_claims")
            .select("concept,label,bucket,disclosure")
            .eq("user_id", user_id)
            .is_("dismissed_at", "null")
            .execute()
            .data
            or []
        )
    except Exception:
        rows = []
    return [r for r in rows if isinstance(r, dict)]


def _reason_codes(c: dict[str, Any]) -> list[str]:
    codes: list[str] = []
    if _clamp(c.get("vicinity_score")) >= 0.9:
        codes.append("same_block")
    if _clamp(c.get("identity_affinity")) >= 0.55:
        codes.append("identity_overlap")
    if _clamp(c.get("activity_affinity")) >= 0.45:
        codes.append("activity_match")
    if _clamp(c.get("connector_score")) >= 0.55:
        codes.append("active_connector")
    if c.get("type") == "event":
        codes.append("open_activity")
    if c.get("signal_kind") in ("swap_offer", "swap_seek"):
        codes.append(c["signal_kind"])
    return codes or ["nearby_value"]


def _suggested_action(c: dict[str, Any]) -> str:
    ctype = str(c.get("type") or "")
    if ctype == "neighbor":
        return "propose_intro" if _clamp(c.get("identity_affinity")) >= 0.6 else "send_nudge"
    if ctype == "event":
        return "suggest_activity_invite"
    if c.get("signal_kind") == "swap_offer":
        return "suggest_swap_followup"
    if c.get("signal_kind") == "swap_seek":
        return "capture_or_match_need"
    return "capture_unmet_need"


def _safe_reason(c: dict[str, Any]) -> str:
    if c.get("type") == "neighbor":
        label = str(c.get("matching_peer_label") or "a nearby interest").strip()
        return f"also nearby and connected through {label[:80]}"
    if c.get("type") == "event":
        title = str(c.get("title") or "an activity").strip()
        return f"an open nearby activity that may fit: {title[:80]}"
    category = str(c.get("category") or "a local need").replace("_", " ")
    return f"a nearby {category[:80]} signal that may be useful"


def _public_shape(c: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "type",
        "candidate_user_id",
        "event_id",
        "signal_id",
        "signal_kind",
        "score",
        "reason_codes",
        "suggested_action",
        "safe_reason",
        "title",
        "starts_at",
        "venue_name",
        "nickname",
        "avatar_url",
        "matching_peer_label",
        "matching_peer_concept",
        "category",
        "excerpt",
    )
    return {k: c[k] for k in keys if c.get(k) is not None}


def _type_priority(ctype: str) -> int:
    return {"neighbor": 3, "event": 2, "local_signal": 1}.get(ctype, 0)


def _freshness_score(raw: Any) -> float:
    if raw is None:
        return 0.5
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        days = max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400)
        return math.exp(-days / 14)
    except Exception:
        return 0.5


def _query_match_score(query: str | None, item: dict[str, Any]) -> float:
    q = _tokens(query)
    if not q:
        return 0.0
    text = " ".join(str(v) for v in item.values() if isinstance(v, (str, int, float)))
    t = _tokens(text)
    if not t:
        return 0.0
    return min(1.0, len(q & t) / max(len(q), 1))


def _claim_text_overlap(text: str, claims: list[dict[str, Any]]) -> float:
    text_tokens = _tokens(text)
    if not text_tokens or not claims:
        return 0.0
    claim_tokens: set[str] = set()
    for c in claims:
        claim_tokens.update(_tokens(c.get("concept")))
        claim_tokens.update(_tokens(c.get("label")))
        claim_tokens.update(_tokens(c.get("bucket")))
    if not claim_tokens:
        return 0.0
    return min(1.0, len(text_tokens & claim_tokens) / max(min(len(claim_tokens), 8), 1))


def _activity_overlap_from_label(label: Any, claims: list[dict[str, Any]]) -> float:
    return _claim_text_overlap(str(label or ""), [c for c in claims if c.get("bucket") in ("activity", "interest", "stage")])


def _signal_kind(category: str, text: str) -> str:
    hay = f"{category} {text}".lower()
    if any(w in hay for w in ("offer", "available", "giving", "free", "swap_offer")):
        return "swap_offer"
    if any(w in hay for w in ("need", "looking", "iso", "seek", "want", "swap_seek")):
        return "swap_seek"
    return "local_need"


def _is_sensitive_signal(category: str, text: str) -> bool:
    hay = f"{category} {text}".lower()
    return any(w in hay for w in ("medical", "therapy", "legal", "immigration", "domestic", "violence", "crisis"))


def _safe_excerpt(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
    cleaned = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "[email]", cleaned)
    cleaned = re.sub(r"\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b", "[phone]", cleaned)
    return cleaned[:140]


def _tokens(raw: Any) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", str(raw or "").lower()) if len(t) > 2}


def _clamp(raw: Any) -> float:
    try:
        return max(0.0, min(1.0, float(raw)))
    except Exception:
        return 0.0
