"""Your communities, on the look screen and behind it (C-CIRCLE-LOOK-COMMS → …-COMM-PEOPLE).

Three surfaces, one module, all read-only:

  * `communities_card`   — the "YOUR COMMUNITIES" card that rides along with the
    looking-open turn: the caller's top three places with a real status line, plus
    how many more there are ("View 4 more" → the Radar Communities tab).
  * `community_profile`  — one place: its neighbours count, the features people
    actually volunteered about it, what's coming up there, and the two CTAs
    (create a meet there / invite people).
  * `community_members`  — every neighbour at that place with a truthful shared
    line and a Nudge.

TRUTHFULNESS ([[truthful-peer-match-model]]). Nothing here scores two people, so
no member row carries stars, a band or a badge. What a row honestly claims is
what the DB proves: this neighbour is confirmed at this place (the shared fact
every row has), plus any identity concepts the caller and they actually share.
When there are no shared concepts the line says only "You both go here" — it
never invents an affinity, and it never dresses the peer's own claims up as
shared ones.

DISCLOSURE (§F). The place is named only to its own members: every read here
re-checks that the caller holds a confirmed, non-dismissed affiliation at that
place, and mutual blocks are filtered out of the member list. Member nicknames +
avatars are the same Stranger-tier fields the peer cards already show; no real
names, no addresses of people, nothing about non-members.

DB-only (no migration). Members come from `circle_affiliations` by `place_ref`,
shared concepts from the existing `count_shared_concepts_for_user` RPC, so this
ships without a `db push` — the cost is the RPC's own top-N cap (see
`_SHARED_CONCEPT_FETCH`), which only ever means a member reads as "You both go
here" instead of listing a shared thread.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from app.auth import service_client
from app.circles_flow import place_relation_emoji, place_relation_noun
from app.ui_actions import community_profile_actions, peer_card_nudge_action

logger = logging.getLogger(__name__)

# The look card shows three; everything past them is "View N more".
CARD_TOP_N = 3
# Members per page on the people panel.
MEMBERS_PAGE = 20
# How wide we ask the shared-concept RPC to look. It ranks by shared count and
# truncates, so a member below the cut simply has no shared line — never a wrong one.
_SHARED_CONCEPT_FETCH = 200
# Shared threads listed on one member row before it stops being scannable.
_MAX_SHARED_LABELS = 3
# Features are volunteered in conversation; below this confidence we don't repeat them.
_FEATURE_MIN_CONFIDENCE = 0.5
_MAX_FEATURES = 8
_MAX_PROFILE_EVENTS = 5

_FEATURE_PREFIX_RE = re.compile(r"^(has|is|offers|allows)_")
_TRUTHY_VALUES = {"true", "yes", "y", "1"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


# ── membership + place reads ──────────────────────────────────────────────────


def caller_affiliation_at(user_id: str, place_id: str) -> dict[str, Any] | None:
    """The caller's own confirmed row at `place_id` — the authorization for every
    read in this module. None means "not a member": the profile and the people
    panel are for the places you belong to, not a directory of the neighborhood."""
    if not user_id or not place_id:
        return None
    try:
        res = (
            service_client()
            .table("circle_affiliations")
            .select(
                "id, circle_type, circle_key, detail, created_at, source, confirmed_via"
            )
            .eq("user_id", user_id)
            .eq("place_ref", place_id)
            .eq("status", "confirmed")
            .is_("dismissed_at", "null")
            .limit(1)
            .execute()
        )
        rows = res.data if isinstance(res.data, list) else []
    except Exception:
        logger.exception("community_membership_check_failed user=%s place=%s", user_id, place_id)
        return None
    return rows[0] if rows else None


def _resolve_place(user_id: str, *, affiliation_id: str | None, place_id: str | None) -> str:
    """(affiliation_id | place_id) → place_id, membership proven. Raises ValueError."""
    if affiliation_id:
        try:
            res = (
                service_client()
                .table("circle_affiliations")
                .select("place_ref")
                .eq("id", affiliation_id)
                .eq("user_id", user_id)
                .is_("dismissed_at", "null")
                .limit(1)
                .execute()
            )
            rows = res.data if isinstance(res.data, list) else []
        except Exception:
            logger.exception("community_affiliation_lookup_failed id=%s", affiliation_id)
            raise ValueError("affiliation_not_found") from None
        if not rows or not rows[0].get("place_ref"):
            raise ValueError("affiliation_not_found")
        return str(rows[0]["place_ref"])
    pid = str(place_id or "").strip()
    if not pid:
        raise ValueError("place_required")
    if not caller_affiliation_at(user_id, pid):
        raise ValueError("not_a_member")
    return pid


def _place_row(place_id: str) -> dict[str, Any]:
    try:
        res = (
            service_client()
            .table("places")
            .select("id, name, address, place_type, zip")
            .eq("id", place_id)
            .limit(1)
            .execute()
        )
        rows = res.data if isinstance(res.data, list) else []
    except Exception:
        logger.exception("community_place_read_failed place=%s", place_id)
        return {}
    return rows[0] if rows else {}


def _member_rows(place_id: str) -> list[dict[str, Any]]:
    """Every confirmed, non-dismissed affiliation at the place (oldest first —
    the people who made it a community lead)."""
    try:
        res = (
            service_client()
            .table("circle_affiliations")
            .select("user_id, circle_type, created_at")
            .eq("place_ref", place_id)
            .eq("status", "confirmed")
            .is_("dismissed_at", "null")
            .order("created_at")
            .limit(500)
            .execute()
        )
        rows = res.data if isinstance(res.data, list) else []
    except Exception:
        logger.exception("community_members_read_failed place=%s", place_id)
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in rows:
        uid = str((r or {}).get("user_id") or "")
        if not uid or uid in seen:
            continue
        seen.add(uid)
        out.append(dict(r))
    return out


def _blocked_ids(user_id: str, candidates: list[str]) -> set[str]:
    """Mutual-block filter (the Python side of lana_is_blocked)."""
    if not candidates:
        return set()
    blocked: set[str] = set()
    try:
        sb = service_client()
        out = (
            sb.table("user_blocks")
            .select("blocker, blocked")
            .or_(f"blocker.eq.{user_id},blocked.eq.{user_id}")
            .limit(500)
            .execute()
        )
        for row in out.data if isinstance(out.data, list) else []:
            blocker = str((row or {}).get("blocker") or "")
            target = str((row or {}).get("blocked") or "")
            other = target if blocker == user_id else blocker
            if other:
                blocked.add(other)
    except Exception:
        # Fail CLOSED on the people panel: if we can't read blocks we'd rather show
        # nobody's name than someone the caller blocked.
        logger.exception("community_block_read_failed user=%s", user_id)
        return set(candidates)
    return blocked


def _users_by_id(user_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not user_ids:
        return {}
    try:
        res = (
            service_client()
            .table("users")
            .select("id, nickname, profile_photo_url")
            .in_("id", user_ids)
            .execute()
        )
        rows = res.data if isinstance(res.data, list) else []
    except Exception:
        logger.exception("community_users_read_failed n=%d", len(user_ids))
        return {}
    return {str(r["id"]): r for r in rows if isinstance(r, dict) and r.get("id")}


def member_count(place_id: str) -> int:
    from app.circles_flow import _member_count

    return _member_count(place_id)


# ── events at a place ─────────────────────────────────────────────────────────


def _going_counts(event_ids: list[str]) -> dict[str, int]:
    """Real going roster per event (same predicate the cancel fan-out uses)."""
    if not event_ids:
        return {}
    try:
        res = (
            service_client()
            .table("event_requests")
            .select("event_id")
            .in_("event_id", event_ids)
            .in_("status", ["approved", "attended"])
            .eq("rsvp_status", "going")
            .limit(2000)
            .execute()
        )
        rows = res.data if isinstance(res.data, list) else []
    except Exception:
        logger.exception("community_going_counts_failed n=%d", len(event_ids))
        return {}
    counts: dict[str, int] = {}
    for r in rows:
        eid = str((r or {}).get("event_id") or "")
        if eid:
            counts[eid] = counts.get(eid, 0) + 1
    return counts


def _events_at_place(place_id: str, *, limit: int, within_days: int | None = None) -> list[dict]:
    try:
        q = (
            service_client()
            .table("events")
            .select("id, title, starts_at, has_time, venue_name, host_id")
            .eq("place_ref", place_id)
            .eq("status", "open")
            .gte("starts_at", _now_iso())
        )
        if within_days:
            until = datetime.now(timezone.utc) + timedelta(days=within_days)
            q = q.lte("starts_at", until.strftime("%Y-%m-%dT%H:%M:%S"))
        res = q.order("starts_at").limit(max(limit, 1)).execute()
        rows = res.data if isinstance(res.data, list) else []
    except Exception:
        logger.exception("community_events_read_failed place=%s", place_id)
        return []
    return [r for r in rows if isinstance(r, dict)]


def meets_this_week(place_id: str) -> int:
    return len(_events_at_place(place_id, limit=20, within_days=7))


def _event_rows_for_profile(place_id: str) -> list[dict[str, Any]]:
    """Upcoming meets at the place, the best-attended first. "Popular" is a real
    going count — never a guess, and never an ordering we can't defend."""
    raw = _events_at_place(place_id, limit=20)
    counts = _going_counts([str(r.get("id")) for r in raw if r.get("id")])
    rows = [
        {
            "event_id": str(r.get("id") or ""),
            "title": str(r.get("title") or "").strip(),
            "starts_at": str(r.get("starts_at") or "") or None,
            "has_time": r.get("has_time") is not False,
            "venue_name": str(r.get("venue_name") or "").strip() or None,
            "going_count": int(counts.get(str(r.get("id") or ""), 0)),
        }
        for r in raw
        if str(r.get("title") or "").strip()
    ]
    rows.sort(key=lambda r: (-int(r["going_count"]), str(r["starts_at"] or "")))
    return rows[:_MAX_PROFILE_EVENTS]


# ── features ──────────────────────────────────────────────────────────────────


def _feature_label(key: str, value: str | None, sub_group: str) -> str:
    base = _FEATURE_PREFIX_RE.sub("", str(key or "").strip().lower()).replace("_", " ").strip()
    if not base:
        return ""
    label = base[:1].upper() + base[1:]
    val = str(value or "").strip()
    if val and val.lower() not in _TRUTHY_VALUES and val.lower() != base:
        label = f"{label}: {val}"
    group = str(sub_group or "").strip().replace("_", " ")
    if group:
        label = f"{label} ({group})"
    return label[:48]


def place_features(place_id: str) -> list[dict[str, Any]]:
    """What members volunteered about the place, best-attested first. Only what
    somebody actually said — this list is never inferred from the place type."""
    try:
        res = (
            service_client()
            .table("place_features")
            .select("key, value, sub_group, confidence, source")
            .eq("place_id", place_id)
            .order("confidence", desc=True)
            .limit(40)
            .execute()
        )
        rows = res.data if isinstance(res.data, list) else []
    except Exception:
        logger.exception("community_features_read_failed place=%s", place_id)
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in rows:
        if not isinstance(r, dict):
            continue
        try:
            conf = float(r.get("confidence") or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        if conf < _FEATURE_MIN_CONFIDENCE:
            continue
        key = str(r.get("key") or "").strip().lower()
        label = _feature_label(key, r.get("value"), str(r.get("sub_group") or ""))
        if not label or label.lower() in seen:
            continue
        seen.add(label.lower())
        out.append({"key": key, "label": label, "sub_group": str(r.get("sub_group") or "") or None})
        if len(out) >= _MAX_FEATURES:
            break
    return out


# ── the blurb ─────────────────────────────────────────────────────────────────


_BLURB_PROMPT = """You write ONE short description of a local place for the people \
who go there, in a neighborhood app.

Output ONLY JSON: {"description": "..."}

Rules:
- Ground it ONLY in the facts given. Never invent a feature, a vibe, a price, a \
schedule, or how good the place is — if a fact isn't listed, it doesn't exist.
- 1-2 short sentences, max 180 characters, warm and plain.
- Never the words "circle", "block", or "match" (backstage vocabulary).
- Never claim people are waiting for anyone, and never promise anything.
- English only (rendered into the user's language downstream)."""


def _blurb(
    *,
    place_name: str,
    relation: str,
    area: str | None,
    features: list[str],
    members: int,
) -> str | None:
    """AI-authored from true facts, with a factual template as the floor
    ([[ai-authored-copy-not-canned]]). None when we know nothing to say."""
    fallback: str | None = None
    if features:
        joined = ", ".join(f.lower() for f in features[:3])
        where = f" in {area}" if area else ""
        fallback = f"A {relation}{where} — {joined}."
    elif area:
        fallback = f"A {relation} in {area}."
    facts = [f"Place name: {place_name}", f"What it is, in the members' words: {relation}"]
    if area:
        facts.append(f"Area: {area}")
    if features:
        facts.append("Features members mentioned: " + ", ".join(features[:6]))
    else:
        facts.append("No features have been mentioned yet — do not invent any")
    facts.append(
        f"{members} neighbor(s) here call it their spot"
        if members > 1
        else "Nobody else here has called it their spot yet"
    )
    try:
        from app.orchestrator.llm import llm_configured, llm_json, synthesizer_model

        if not llm_configured():
            return fallback
        data = llm_json(
            model=synthesizer_model(),
            system=_BLURB_PROMPT,
            user_payload="\n".join(f"- {f}" for f in facts),
            max_tokens=140,
            temperature=0.3,
        )
        text = str((data or {}).get("description") or "").strip() if isinstance(data, dict) else ""
        if text:
            return text[:220]
    except Exception:
        logger.exception("community_blurb_failed place=%s", place_name)
    return fallback


# ── shared threads between the caller and the members ─────────────────────────


def _shared_concepts(user_id: str) -> dict[str, list[str]]:
    """peer user_id -> the identity concepts they and the caller both hold.

    Exact concept-id overlap from count_shared_concepts_for_user — public,
    non-dismissed claims only. Absent from this map = nothing PROVEN shared, which
    the member row states as such.
    """
    try:
        res = service_client().rpc(
            "count_shared_concepts_for_user",
            {
                "p_user_id": user_id,
                "p_limit": _SHARED_CONCEPT_FETCH,
                "p_min_shared_count": 1,
                "p_exclude_self": True,
            },
        ).execute()
        rows = res.data if isinstance(res.data, list) else []
    except Exception:
        logger.exception("community_shared_concepts_failed user=%s", user_id)
        return {}
    out: dict[str, list[str]] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        uid = str(r.get("peer_user_id") or "")
        labels = r.get("shared_concept_labels")
        if not uid or not isinstance(labels, list):
            continue
        clean = [str(x).strip() for x in labels if str(x or "").strip()]
        if clean:
            out[uid] = clean[:_MAX_SHARED_LABELS]
    return out


def _shared_line(labels: list[str], relation: str) -> str:
    """The one honest line under a member's name. Shared threads when there are
    any; otherwise the fact that IS true of every row here — you both go here."""
    if labels:
        return "You both: " + " · ".join(labels)
    return f"You both go to this {relation}"


# ── surface 1: the look-screen card ───────────────────────────────────────────


def _status_line(count: int, meets: int) -> str:
    people = "just you so far" if count <= 1 else f"{count} people"
    if meets:
        return f"{people} · {meets} meet{'s' if meets != 1 else ''} this week"
    return people


def communities_card(user_id: str, *, top: int = CARD_TOP_N) -> dict[str, Any] | None:
    """The "YOUR COMMUNITIES" card for the looking-open turn.

    Ranked by what's alive: the places with people, then with meets this week,
    then the newest. None when the user has no community yet — the card is absent
    from the turn rather than empty, so the FE renders nothing.
    """
    if not user_id:
        return None
    from app.circles_flow import list_my_circles

    try:
        circles = list_my_circles(user_id)
    except Exception:
        logger.exception("communities_card_list_failed user=%s", user_id)
        return None
    grounded = [c for c in circles if c.get("place_name") and c.get("id")]
    if not grounded:
        return None
    # list_my_circles is already newest-first; a stable sort on people-count keeps the
    # liveliest places on top and the newest of an equal-sized pair above the older one.
    grounded.sort(key=lambda c: -int(c.get("member_count") or 0))
    shown = grounded[: max(top, 1)]
    items: list[dict[str, Any]] = []
    for c in shown:
        place_id = c.get("place_id") or c.get("place_ref")
        # Only the rows actually shown pay for a meets-this-week count.
        meets = meets_this_week(str(place_id)) if place_id else 0
        count = int(c.get("member_count") or 0)
        items.append(
            {
                "affiliation_id": str(c["id"]),
                "place_id": str(place_id or "") or None,
                "place_name": c.get("place_name"),
                "place_address": c.get("place_address"),
                "circle_type": c.get("circle_type"),
                "relation": place_relation_noun(c.get("circle_type")),
                "emoji": place_relation_emoji(c.get("circle_type")),
                "member_count": count,
                "meets_this_week": meets,
                "active": bool(c.get("active")),
                "status_line": _status_line(count, meets),
            }
        )
    total = len(grounded)
    return {
        "items": items,
        "total": total,
        "more_count": max(0, total - len(items)),
    }


def stamp_communities_card(ctx: dict[str, Any], user_id: str | None) -> None:
    """Put the card on the turn context (looking-open turns only). Best-effort:
    a failure here must never cost the user the reply they asked for."""
    if not user_id:
        return
    try:
        card = communities_card(user_id)
    except Exception:
        logger.exception("stamp_communities_card_failed user=%s", user_id)
        return
    if card and card.get("items"):
        ctx["communities_card"] = card


# ── surface 2: one community's profile ────────────────────────────────────────


def community_profile(
    user_id: str,
    *,
    affiliation_id: str | None = None,
    place_id: str | None = None,
    phone_verified: bool = True,
) -> dict[str, Any]:
    """The place, for the people who go there. Raises ValueError('not_a_member' |
    'affiliation_not_found' | 'place_required' | 'place_not_found')."""
    pid = _resolve_place(user_id, affiliation_id=affiliation_id, place_id=place_id)
    mine = caller_affiliation_at(user_id, pid)
    if not mine:
        raise ValueError("not_a_member")
    place = _place_row(pid)
    if not place:
        raise ValueError("place_not_found")
    name = str(place.get("name") or "").strip()
    relation = place_relation_noun(mine.get("circle_type") or place.get("place_type"))
    features = place_features(pid)
    members = _member_rows(pid)
    count = len(members)
    preview = _member_preview(user_id, members, phone_verified=phone_verified)
    events = _event_rows_for_profile(pid)
    return {
        "place_id": pid,
        "affiliation_id": str(mine.get("id") or ""),
        "place_name": name,
        "place_address": str(place.get("address") or "").strip() or None,
        "circle_type": mine.get("circle_type"),
        # The label for a community invite link — POST /lana/invites/mint {circle_key}.
        # "Invite people" on this card is a native FE action (mint + share sheet), so the
        # key it needs travels with the profile rather than as a chat CTA.
        "circle_key": mine.get("circle_key"),
        # How this became theirs — a Lana join, or a place they told Lana about
        # (see app/community_discovery.py for the two-column model).
        "source": mine.get("source"),
        "confirmed_via": mine.get("confirmed_via"),
        "joined_via_label": _joined_via_label(mine),
        "relation": relation,
        "emoji": place_relation_emoji(mine.get("circle_type") or place.get("place_type")),
        "detail": str(mine.get("detail") or "").strip() or None,
        "member_count": count,
        "active": count >= 2,
        "status_line": _status_line(count, len(_events_at_place(pid, limit=20, within_days=7))),
        "description": _blurb(
            place_name=name,
            relation=relation,
            area=str(place.get("zip") or "").strip() or None,
            features=[f["label"] for f in features],
            members=count,
        ),
        "features": features,
        "member_preview": preview,
        "upcoming_events": events,
        "actions": community_profile_actions(place_name=name, relation=relation)
        if phone_verified
        else [],
    }


def _joined_via_label(affiliation: dict[str, Any]) -> str | None:
    from app.community_discovery import joined_via_label

    return joined_via_label(affiliation.get("confirmed_via"), affiliation.get("source"))


def _member_preview(
    user_id: str,
    members: list[dict[str, Any]],
    *,
    phone_verified: bool,
) -> list[dict[str, Any]]:
    """The avatar row on the profile card — the first few faces, never the whole
    roster (that's the people panel, one tap away)."""
    others = [str(m.get("user_id")) for m in members if str(m.get("user_id") or "") != user_id]
    if not others or not phone_verified:
        return []
    blocked = _blocked_ids(user_id, others)
    visible = [uid for uid in others if uid not in blocked][:5]
    users = _users_by_id(visible)
    out: list[dict[str, Any]] = []
    for uid in visible:
        u = users.get(uid) or {}
        out.append(
            {
                "peer_user_id": uid,
                "nickname": str(u.get("nickname") or "").strip() or None,
                "avatar_url": str(u.get("profile_photo_url") or "").strip() or None,
            }
        )
    return out


# ── surface 3: the people ─────────────────────────────────────────────────────


def community_members(
    user_id: str,
    *,
    place_id: str | None = None,
    affiliation_id: str | None = None,
    limit: int = MEMBERS_PAGE,
    offset: int = 0,
    phone_verified: bool = True,
) -> dict[str, Any]:
    """Every neighbour at the place, each with one honest shared line and a Nudge.

    Unverified callers get the count and nothing else — names and nudges are for
    people who have verified, exactly like the peer cards.
    """
    pid = _resolve_place(user_id, affiliation_id=affiliation_id, place_id=place_id)
    place = _place_row(pid)
    mine = caller_affiliation_at(user_id, pid)
    if not mine:
        raise ValueError("not_a_member")
    relation = place_relation_noun(mine.get("circle_type") or place.get("place_type"))
    members = _member_rows(pid)
    total = len(members)
    if not phone_verified:
        return {
            "place_id": pid,
            "place_name": str(place.get("name") or "").strip() or None,
            "member_count": total,
            "members": [],
            "has_more": False,
            "requires_phone_verification": True,
        }
    others = [str(m.get("user_id")) for m in members if str(m.get("user_id") or "") != user_id]
    blocked = _blocked_ids(user_id, others)
    visible = [uid for uid in others if uid not in blocked]
    page = visible[max(offset, 0) : max(offset, 0) + max(limit, 1)]
    users = _users_by_id(page)
    shared = _shared_concepts(user_id) if page else {}
    rows: list[dict[str, Any]] = []
    for uid in page:
        u = users.get(uid) or {}
        nickname = str(u.get("nickname") or "").strip()
        labels = shared.get(uid) or []
        row: dict[str, Any] = {
            "peer_user_id": uid,
            "nickname": nickname or None,
            "avatar_url": str(u.get("profile_photo_url") or "").strip() or None,
            "trait_tags": labels,
            "shared_line": _shared_line(labels, relation),
            # Deliberately absent: stars, band, badge, similarity. Nothing here
            # compared two people ([[truthful-peer-match-model]]).
            "actions": [peer_card_nudge_action(nickname=nickname, peer_user_id=uid)]
            if nickname
            else [],
        }
        rows.append(row)
    return {
        "place_id": pid,
        "place_name": str(place.get("name") or "").strip() or None,
        "member_count": total,
        "members": rows,
        "has_more": len(visible) > max(offset, 0) + len(page),
        "requires_phone_verification": False,
    }
