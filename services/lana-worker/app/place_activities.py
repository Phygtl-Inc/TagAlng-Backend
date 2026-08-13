"""What people do at a community, and what the place has (C-PROFILE-CIRCLE-SUBS).

Two member-curated lists on one place, both written from the community panel and
both readable by every member of that place:

  * activities — "Aerobics", "Weightlifting": what THIS member does here. Adding
    one also upserts the matching identity claim, so an activity shapes matching
    the same way a claim volunteered in chat does; the row here is the place↔
    activity edge a claim cannot hold (one `place_ref` per claim, and the same
    activity happens at two places). Removing an activity un-links it from the
    place and leaves the interest alone — they still do it, just not here.
  * features — "Pool", "Childcare": what the PLACE has. Shared, one truth per
    (place, key), so these reuse `place_features` and its write policy
    (`circles_capture.upsert_place_feature`), the same rows chat already learns.

MEMBERSHIP is re-checked on every call through `community_surface._resolve_place`
(§F: a place is only named to the people who go there). A feature can only be
removed by whoever contributed it — one member does not get to erase another's
statement of fact about a shared place.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.auth import service_client
from app.circles_capture import _slugify, upsert_place_feature
from app.community_surface import _resolve_place

logger = logging.getLogger(__name__)

# Enough for a real gym-goer, few enough that the chip row stays a list of things
# they actually do rather than a tag cloud.
MAX_ACTIVITIES_PER_MEMBER = 12
_MAX_LABEL = 48
# A member's own statement, added deliberately in the panel — not the 0.6 of a
# feature inferred from something they said in passing.
_PANEL_CONFIDENCE = 0.9
_FEATURE_KEY_PREFIX_RE = re.compile(r"^(has|is|offers|allows)_")


def _clean_label(raw: Any) -> str:
    text = re.sub(r"\s+", " ", str(raw or "").strip())[:_MAX_LABEL]
    return (text[:1].upper() + text[1:]) if text else ""


# ── activities ────────────────────────────────────────────────────────────────


def activities_for_places(
    place_ids: list[str], viewer_id: str | None = None
) -> dict[str, list[dict[str, Any]]]:
    """place_id → its activities, most-shared first, `mine` marking the viewer's.
    Batched: the communities list renders every row's chips from one read."""
    ids = sorted({str(p) for p in place_ids if p})
    if not ids:
        return {}
    try:
        res = (
            service_client()
            .table("place_activities")
            .select("place_id, concept, label, user_id")
            .in_("place_id", ids)
            .limit(2000)
            .execute()
        )
        rows = res.data if isinstance(res.data, list) else []
    except Exception:
        logger.exception("place_activities_read_failed places=%d", len(ids))
        return {}
    grouped: dict[str, dict[str, dict[str, Any]]] = {pid: {} for pid in ids}
    for r in rows:
        if not isinstance(r, dict):
            continue
        pid = str(r.get("place_id") or "")
        concept = str(r.get("concept") or "").strip()
        label = _clean_label(r.get("label"))
        if pid not in grouped or not concept or not label:
            continue
        row = grouped[pid].setdefault(
            concept, {"concept": concept, "label": label, "member_count": 0, "mine": False}
        )
        row["member_count"] += 1
        if viewer_id and str(r.get("user_id") or "") == viewer_id:
            row["mine"] = True
    out: dict[str, list[dict[str, Any]]] = {}
    for pid, by_concept in grouped.items():
        items = list(by_concept.values())
        items.sort(key=lambda r: (-int(r["member_count"]), str(r["label"]).lower()))
        out[pid] = items
    return out


def activities_at_place(place_id: str, viewer_id: str | None = None) -> list[dict[str, Any]]:
    """Every activity anyone does here, with `mine` for the caller's own. One list
    serves both "your activities" and the "add more" menu — what the other members
    do here is the only suggestion list that is true."""
    return activities_for_places([place_id], viewer_id).get(str(place_id), [])


def add_activity(
    user_id: str,
    *,
    label: str,
    place_id: str | None = None,
    affiliation_id: str | None = None,
) -> dict[str, Any]:
    """Record that the caller does `label` at this community. Idempotent per
    (place, user, concept). Raises ValueError('place_required' | 'not_a_member' |
    'affiliation_not_found' | 'label_required' | 'too_many_activities')."""
    pid = _resolve_place(user_id, affiliation_id=affiliation_id, place_id=place_id)
    clean = _clean_label(label)
    concept = _slugify(clean)
    if not clean or not concept:
        raise ValueError("label_required")
    sb = service_client()
    existing = (
        sb.table("place_activities")
        .select("id, concept")
        .eq("place_id", pid)
        .eq("user_id", user_id)
        .execute()
    )
    rows = existing.data if isinstance(existing.data, list) else []
    if any(str(r.get("concept")) == concept for r in rows):
        return {"place_id": pid, "concept": concept, "label": clean, "already_there": True}
    if len(rows) >= MAX_ACTIVITIES_PER_MEMBER:
        raise ValueError("too_many_activities")
    sb.table("place_activities").insert(
        {"place_id": pid, "user_id": user_id, "concept": concept, "label": clean}
    ).execute()
    _mirror_claim(user_id, concept, clean)
    return {"place_id": pid, "concept": concept, "label": clean, "already_there": False}


def remove_activity(
    user_id: str,
    *,
    concept: str,
    place_id: str | None = None,
    affiliation_id: str | None = None,
) -> None:
    """Stop listing this activity here. The identity claim stays — they still do
    it, this just isn't the place."""
    pid = _resolve_place(user_id, affiliation_id=affiliation_id, place_id=place_id)
    key = str(concept or "").strip().lower()
    if not key:
        raise ValueError("concept_required")
    service_client().table("place_activities").delete().eq("place_id", pid).eq(
        "user_id", user_id
    ).eq("concept", key).execute()


def link_activity_from_claim(user_id: str, place_id: str, label: str) -> None:
    """Chat's version of `add_activity`: the answer to "what do you enjoy most at
    {place}?" is already a place-tagged claim, so mirror it onto the panel's list.
    Best-effort — a failure here must never break the claim write."""
    try:
        clean = _clean_label(label)
        concept = _slugify(clean)
        if not (place_id and clean and concept):
            return
        service_client().table("place_activities").upsert(
            {"place_id": place_id, "user_id": user_id, "concept": concept, "label": clean},
            on_conflict="place_id,user_id,concept",
        ).execute()
    except Exception:
        logger.exception("link_activity_from_claim_failed place=%s", place_id)


def _mirror_claim(user_id: str, concept: str, label: str) -> None:
    """An activity is an interest: write it as a claim so it feeds matching the
    same way one volunteered in conversation does. Best-effort."""
    try:
        from app.claims_persist import upsert_claims
        from app.models import ExtractedClaim

        upsert_claims(
            user_id,
            [
                ExtractedClaim(
                    concept=concept,
                    label=label,
                    confidence=_PANEL_CONFIDENCE,
                    bucket="interest",
                    source_quote=None,
                )
            ],
        )
    except Exception:
        logger.exception("activity_claim_mirror_failed concept=%s", concept)


# ── features ("what it has") ──────────────────────────────────────────────────


def add_feature(
    user_id: str,
    *,
    label: str,
    place_id: str | None = None,
    affiliation_id: str | None = None,
    sub_group: str = "",
) -> dict[str, Any]:
    """A member says the place has something ("Pool"). Same rows chat learns, so
    the write policy (owner rows win, higher confidence wins) is unchanged."""
    pid = _resolve_place(user_id, affiliation_id=affiliation_id, place_id=place_id)
    clean = _clean_label(label)
    slug = _slugify(clean)
    if not clean or not slug:
        raise ValueError("label_required")
    key = slug if _FEATURE_KEY_PREFIX_RE.match(slug) else f"has_{slug}"[:64]
    written = upsert_place_feature(
        place_id=pid,
        key=key,
        value=None,
        sub_group=str(sub_group or "").strip().lower(),
        confidence=_PANEL_CONFIDENCE,
        source="rapport",
        contributed_by=user_id,
        emoji=feature_emoji(clean),
    )
    return {"place_id": pid, "key": key, "label": clean, "written": written}


def remove_feature(
    user_id: str,
    *,
    key: str,
    place_id: str | None = None,
    affiliation_id: str | None = None,
) -> None:
    """Only the member who contributed it can take it back — a feature is a claim
    about a shared place, not a personal preference. Raises ValueError('not_yours')."""
    pid = _resolve_place(user_id, affiliation_id=affiliation_id, place_id=place_id)
    k = str(key or "").strip().lower()
    if not k:
        raise ValueError("key_required")
    sb = service_client()
    res = (
        sb.table("place_features")
        .select("id, contributed_by")
        .eq("place_id", pid)
        .eq("key", k)
        .execute()
    )
    rows = [r for r in (res.data or []) if isinstance(r, dict)]
    if not rows:
        raise ValueError("feature_not_found")
    mine = [r for r in rows if str(r.get("contributed_by") or "") == user_id]
    if not mine:
        raise ValueError("not_yours")
    for r in mine:
        sb.table("place_features").delete().eq("id", r["id"]).execute()


_EMOJI_PROMPT = """You pick ONE emoji for a facility a local place has \
("pool", "childcare", "sauna", "free parking").

Output ONLY JSON: {"emoji": "X"}
- Exactly one emoji, the most literal depiction of the thing itself.
- No text, no skin tones, no flags, no faces.
- If nothing fits, {"emoji": ""}."""


def feature_emoji(label: str) -> str:
    """One emoji for the chip, chosen the way every other emoji in the product is
    (events.cover_emoji, circle_affiliations.emoji) — asked for, never mapped from
    a word list, because the set of things a place can have is open-ended."""
    text = str(label or "").strip()
    if not text:
        return ""
    try:
        from app.lana_ui import sanitize_cover_emoji
        from app.orchestrator.llm import llm_configured, llm_json, synthesizer_model

        if not llm_configured():
            return ""
        data = llm_json(
            model=synthesizer_model(),
            system=_EMOJI_PROMPT,
            user_payload=text,
            max_tokens=16,
            temperature=0.0,
        )
        raw = (data or {}).get("emoji") if isinstance(data, dict) else ""
        return str(sanitize_cover_emoji(raw) or "")
    except Exception:
        logger.exception("feature_emoji_failed label=%s", text)
        return ""
