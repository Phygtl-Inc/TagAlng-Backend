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
import hashlib
import re
import uuid
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
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
# Portraits warmed per roster read. A page of 20 strangers is not worth 20 model calls
# in one go; the next open picks up where this left off.
_MAX_PORTRAIT_WARMS = 5

_FEATURE_PREFIX_RE = re.compile(r"^(has|is|offers|allows)_")
_TRUTHY_VALUES = {"true", "yes", "y", "1"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


# ── membership + place reads ──────────────────────────────────────────────────


def caller_affiliation_at(
    user_id: str,
    place_id: str,
    *,
    statuses: tuple[str, ...] = ("confirmed",),
) -> dict[str, Any] | None:
    """The caller's own confirmed row at `place_id` — the authorization for every
    read in this module. None means "not a member": the profile and the people
    panel are for the places you belong to, not a directory of the neighborhood.

    `statuses` widens that to 'curious' for the profile head alone (§19): someone who
    joined to watch the place can open it, but is not a member and gets no names."""
    if not user_id or not place_id:
        return None
    try:
        res = (
            service_client()
            .table("circle_affiliations")
            .select(
                "id, circle_type, circle_key, detail, created_at, source, "
                "confirmed_via, noun, emoji, status"
            )
            .eq("user_id", user_id)
            .eq("place_ref", place_id)
            .in_("status", list(statuses))
            .is_("dismissed_at", "null")
            .limit(1)
            .execute()
        )
        rows = res.data if isinstance(res.data, list) else []
    except Exception:
        logger.exception("community_membership_check_failed user=%s place=%s", user_id, place_id)
        return None
    return rows[0] if rows else None


def _resolve_place(
    user_id: str,
    *,
    affiliation_id: str | None,
    place_id: str | None,
    statuses: tuple[str, ...] = ("confirmed",),
    out_affiliation: dict[str, Any] | None = None,
    require_member: bool = True,
) -> str:
    """(affiliation_id | place_id) → place_id, membership proven. Raises ValueError.

    `out_affiliation` receives the caller's row when this resolved by place_id, so the
    caller can skip re-reading what the membership check just fetched — a wasted network
    round trip on every profile and roster open.

    `require_member=False` resolves the place without proving membership — for the profile
    head, which any viewer may open. out_affiliation is left empty in that case, which is
    how the caller tells a visitor from a member."""
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
    mine = caller_affiliation_at(user_id, pid, statuses=statuses)
    if not mine:
        if require_member:
            raise ValueError("not_a_member")
        return pid
    if out_affiliation is not None:
        out_affiliation.update(mine)
    return pid


_PLACE_FIELDS = (
    "id, name, address, place_type, zip, google_place_id, lat, lng, blurb, blurb_key"
)
# Pre-20261024 environments have no blurb columns; step down rather than fail the whole
# place read (the profile would 404 on a place that is perfectly fine).
_PLACE_FIELDS_NO_BLURB = "id, name, address, place_type, zip, google_place_id, lat, lng"


def _place_row(place_id: str) -> dict[str, Any]:
    for fields in (_PLACE_FIELDS, _PLACE_FIELDS_NO_BLURB):
        try:
            res = (
                service_client()
                .table("places")
                .select(fields)
                .eq("id", place_id)
                .limit(1)
                .execute()
            )
        except Exception:
            continue
        rows = res.data if isinstance(res.data, list) else []
        return rows[0] if rows else {}
    logger.exception("community_place_read_failed place=%s", place_id)
    return {}


_READ_POOL = ThreadPoolExecutor(max_workers=6, thread_name_prefix="community-read")


def _gather(**thunks: Any) -> dict[str, Any]:
    """Run independent reads at once and return them by name.

    These surfaces are a handful of tiny queries that were merely QUEUED behind each
    other: measured against prod, every hop costs ~300ms of round trip from a laptop
    and the roster spent 1.6s doing nothing but waiting six times in a row. Each read
    already swallows its own errors and returns a safe default, so a failure here is
    the same empty list it would have been serially.
    """
    futures = {name: _READ_POOL.submit(fn) for name, fn in thunks.items()}
    return {name: f.result() for name, f in futures.items()}


def _member_rows(place_id: str) -> list[dict[str, Any]]:
    """Everyone at the place, non-dismissed: members first (the people who made it a
    community), then curious joiners — each row keeping its `status` so the roster can
    say which it is (§19). Oldest first inside each group."""
    try:
        res = (
            service_client()
            .table("circle_affiliations")
            .select("user_id, circle_type, status, created_at")
            .eq("place_ref", place_id)
            .in_("status", ["confirmed", "curious"])
            .is_("dismissed_at", "null")
            .order("created_at")
            .limit(500)
            .execute()
        )
        rows = res.data if isinstance(res.data, list) else []
    except Exception:
        logger.exception("community_members_read_failed place=%s", place_id)
        return []
    # One person can hold both a curious and a confirmed row (they joined, then
    # confirmed): the stronger state is what they are here.
    best: dict[str, dict[str, Any]] = {}
    for r in rows:
        uid = str((r or {}).get("user_id") or "")
        if not uid:
            continue
        held = best.get(uid)
        if held is None or (
            str(held.get("status")) != "confirmed"
            and str((r or {}).get("status")) == "confirmed"
        ):
            best[uid] = dict(r)
    ordered = list(best.values())
    return [r for r in ordered if str(r.get("status")) == "confirmed"] + [
        r for r in ordered if str(r.get("status")) != "confirmed"
    ]


def shared_community_name(user_id: str, peer_user_id: str) -> str | None:
    """The place these two both belong to, named — or None if there isn't one.

    The proven tie behind a nudge sent from a community roster. Two members of one
    gym are routinely on different blocks, so "a neighbor close by" is not what is
    true about them ([[truthful-peer-match-model]]) — the shared place is."""
    if not user_id or not peer_user_id or user_id == peer_user_id:
        return None
    try:
        rows = (
            service_client()
            .table("circle_affiliations")
            .select("user_id, place_ref")
            .in_("user_id", [user_id, peer_user_id])
            .eq("status", "confirmed")
            .is_("dismissed_at", "null")
            .not_.is_("place_ref", "null")
            .limit(200)
            .execute()
        ).data or []
    except Exception:
        logger.exception("shared_community_lookup_failed peer=%s", peer_user_id)
        return None
    mine = {str(r["place_ref"]) for r in rows if str(r.get("user_id")) == user_id}
    theirs = {str(r["place_ref"]) for r in rows if str(r.get("user_id")) == peer_user_id}
    both = mine & theirs
    if not both:
        return None
    name = str(_place_row(sorted(both)[0]).get("name") or "").strip()
    return name or None


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


_USER_FIELDS = "id, nickname, profile_photo_url, public_portrait"
# Pre-20261026 environments have no portrait column; step down rather than lose the
# roster over a line of prose.
_USER_FIELDS_NO_PORTRAIT = "id, nickname, profile_photo_url"


def _users_by_id(user_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not user_ids:
        return {}
    rows: list[dict[str, Any]] = []
    for fields in (_USER_FIELDS, _USER_FIELDS_NO_PORTRAIT):
        try:
            res = (
                service_client()
                .table("users")
                .select(fields)
                .in_("id", user_ids)
                .execute()
            )
        except Exception:
            continue
        rows = res.data if isinstance(res.data, list) else []
        break
    else:
        logger.exception("community_users_read_failed n=%d", len(user_ids))
        return {}
    # Self-healing: anyone here without a public portrait gets one written behind this
    # request. Their own claim writes are the normal trigger, but a member who never
    # says another word to Lana would never get one — and peers read get_peer_profile
    # straight from Supabase, so no viewer can warm it from the client side. The first
    # person to open a community fixes it for everyone who opens it after.
    _warm_missing_portraits(rows)
    return {str(r["id"]): r for r in rows if isinstance(r, dict) and r.get("id")}


def _warm_missing_portraits(rows: list[dict[str, Any]]) -> None:
    """Queue a portrait write for listed people who have none. Background, never blocks."""
    missing = [
        str(r["id"])
        for r in rows
        if isinstance(r, dict) and r.get("id") and not str(r.get("public_portrait") or "").strip()
    ]
    if not missing:
        return
    try:
        from app.profile_portrait import schedule_portrait_refresh

        for uid in missing[:_MAX_PORTRAIT_WARMS]:
            schedule_portrait_refresh(uid)
    except Exception:  # noqa: BLE001 — prose is an upgrade, never a blocker
        logger.exception("portrait_warm_failed n=%d", len(missing))


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
    # Held here OR created for this community (setup card 2/5) — a school's picnic in the
    # park belongs on the school's screen even though the venue is the park. or_ takes a
    # formatted string, so the id must be a real uuid before it goes in: a caller-supplied
    # place ref never gets to write PostgREST syntax. Anything else falls back to the
    # single-column filter, which PostgREST parameterizes for us.
    try:
        uuid.UUID(str(place_id))
        two_column = True
    except (ValueError, AttributeError, TypeError):
        two_column = False
    try:
        q = (
            service_client()
            .table("events")
            .select("id, title, starts_at, has_time, venue_name, host_id, cover_emoji")
        )
        q = (
            q.or_(f"place_ref.eq.{place_id},circle_place_ref.eq.{place_id}")
            if two_column
            else q.eq("place_ref", place_id)
        )
        q = q.eq("status", "open").gte("starts_at", _now_iso())
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


def _this_week(rows: list[dict[str, Any]]) -> int:
    """How many of these upcoming meets land in the next 7 days. Counted from rows we
    already hold rather than re-querying — every profile open was reading the place's
    events twice."""
    until = datetime.now(timezone.utc) + timedelta(days=7)
    n = 0
    for r in rows:
        try:
            starts = datetime.fromisoformat(str(r.get("starts_at") or ""))
        except ValueError:
            continue
        if starts.tzinfo is None:
            starts = starts.replace(tzinfo=timezone.utc)
        if starts <= until:
            n += 1
    return n


def _event_rows_for_profile(place_id: str) -> tuple[list[dict[str, Any]], int]:
    """Upcoming meets at the place, the best-attended first, plus how many are inside
    the next 7 days. "Popular" is a real going count — never a guess, and never an
    ordering we can't defend. The week count comes from the FULL read, not the few rows
    the card shows, so the status line stays true when there are more."""
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
            # The meet's own cover glyph, the same one its card and the Radar show. The
            # FE has rendered this field all along and fell back to a calendar because
            # nothing ever sent it, so one meet wore two faces (2026-08-18).
            "cover_emoji": str(r.get("cover_emoji") or "").strip() or None,
        }
        for r in raw
        if str(r.get("title") or "").strip()
    ]
    rows.sort(key=lambda r: (-int(r["going_count"]), str(r["starts_at"] or "")))
    return rows[:_MAX_PROFILE_EVENTS], _this_week(rows)


# ── features ──────────────────────────────────────────────────────────────────


def _feature_label(
    key: str, value: str | None, sub_group: str, stored: str | None = None
) -> str:
    """The chip's text. `stored` is place_features.label — the member's own words — and
    wins whenever it is there: a slug cannot round-trip casing, digits or punctuation
    ("has_byob" can only come back "Byob"). Derived from the key otherwise, which is
    still right for chat-learned rows (a key, no typed text) and pre-20261030 rows."""
    base = str(stored or "").strip()
    if not base:
        base = (
            _FEATURE_PREFIX_RE.sub("", str(key or "").strip().lower()).replace("_", " ").strip()
        )
    if not base:
        return ""
    label = base[:1].upper() + base[1:]
    val = str(value or "").strip()
    if val and val.lower() not in _TRUTHY_VALUES and val.lower() != base.lower():
        label = f"{label}: {val}"
    group = str(sub_group or "").strip().replace("_", " ")
    if group:
        label = f"{label} ({group})"
    return label[:48]


_FEATURE_FIELDS = "key, value, sub_group, confidence, source, emoji, label, contributed_by"
# Pre-20261030 environments have no label column; step down rather than lose every chip
# on the card (the read would fail whole and the place would render with no features).
_FEATURE_FIELDS_NO_LABEL = "key, value, sub_group, confidence, source, emoji, contributed_by"


def place_features(place_id: str, user_id: str | None = None) -> list[dict[str, Any]]:
    """What members volunteered about the place, best-attested first. Only what
    somebody actually said — this list is never inferred from the place type.

    `mine` marks the rows `user_id` contributed — the only ones remove_feature will
    delete, so it is what decides whether the card renders an × (issues #77)."""
    rows: list[Any] = []
    for fields in (_FEATURE_FIELDS, _FEATURE_FIELDS_NO_LABEL):
        try:
            res = (
                service_client()
                .table("place_features")
                .select(fields)
                .eq("place_id", place_id)
                .order("confidence", desc=True)
                .limit(40)
                .execute()
            )
        except Exception:
            continue
        rows = res.data if isinstance(res.data, list) else []
        break
    else:
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
        label = _feature_label(key, r.get("value"), str(r.get("sub_group") or ""), r.get("label"))
        if not label or label.lower() in seen:
            continue
        seen.add(label.lower())
        out.append(
            {
                "key": key,
                "label": label,
                "sub_group": str(r.get("sub_group") or "") or None,
                "emoji": str(r.get("emoji") or "").strip() or None,
                "mine": bool(user_id) and str(r.get("contributed_by") or "") == user_id,
            }
        )
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


def _blurb_fingerprint(
    *, place_name: str, relation: str, area: str | None, features: list[str], members: int
) -> str:
    """What the description was written FROM. A different fingerprint means a fact moved
    (a feature volunteered, the second member arriving) and the line is rewritten."""
    facts = "|".join(
        [place_name, relation, area or "", ",".join(features[:6]), str(members)]
    )
    return hashlib.sha256(facts.encode("utf-8")).hexdigest()[:32]


def _blurb(
    *,
    place_name: str,
    relation: str,
    area: str | None,
    features: list[str],
    members: int,
    # The place row's stored line and what it was written from. Absent (or an
    # unmigrated environment, where the columns don't exist) simply means the
    # template ships and nothing is written back.
    place_id: str = "",
    stored: str | None = None,
    stored_key: str | None = None,
) -> str | None:
    """AI-authored from true facts, with a factual template as the floor
    ([[ai-authored-copy-not-canned]]). None when we know nothing to say.

    Written ONCE and kept on the place (migration 20261024). The model call is ~1.6s and
    the card cannot render until the description returns, so it never happens in a
    reader's request: a stored line whose fingerprint still matches is served straight
    from the row we already fetched, and anything else serves the factual template now
    while the authored line is written behind (landing for the next open).

    The previous in-process lru_cache hid this locally and hid nothing in production —
    it died with the process and every pod paid its own miss for the same sentence."""
    key = _blurb_fingerprint(
        place_name=place_name, relation=relation, area=area,
        features=features, members=members,
    )
    text = str(stored or "").strip()
    if text and str(stored_key or "") == key:
        return text
    if place_id and key not in _BLURB_INFLIGHT:
        _BLURB_INFLIGHT.add(key)
        _BLURB_POOL.submit(
            _author_blurb,
            place_id,
            key,
            dict(
                place_name=place_name,
                relation=relation,
                area=area,
                features=tuple(features or ()),
                members=members,
            ),
        )
    # A stale line still describes this place better than nothing, but it can name a
    # feature that has since been removed — the template is the one that is always true.
    return _blurb_fallback(relation=relation, area=area, features=features)


# One worker: authoring is best-effort, and a single thread keeps a burst of profile
# opens from fanning out into parallel model calls.
_BLURB_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="blurb")
# Fingerprints being authored right now, so two concurrent opens (or React's double
# effect) don't buy the same sentence twice.
_BLURB_INFLIGHT: set[str] = set()


def _author_blurb(place_id: str, key: str, facts: dict[str, Any]) -> None:
    """Write the description for these facts onto the place. Background only."""
    try:
        text = _compose_blurb(**facts)
        if not text:
            return
        service_client().table("places").update(
            {"blurb": text, "blurb_key": key}
        ).eq("id", place_id).execute()
    except Exception:  # noqa: BLE001 — the template already shipped; this is an upgrade
        logger.exception("community_blurb_author_failed place=%s", place_id)
    finally:
        _BLURB_INFLIGHT.discard(key)


def _blurb_fallback(
    *, relation: str, area: str | None, features: list[str]
) -> str | None:
    """The true sentence, from facts alone — the floor under every authored line."""
    if features:
        joined = ", ".join(f.lower() for f in features[:3])
        where = f" in {area}" if area else ""
        return f"A {relation}{where} — {joined}."
    if area:
        return f"A {relation} in {area}."
    return None


def _compose_blurb(
    *,
    place_name: str,
    relation: str,
    area: str | None,
    features: tuple[str, ...],
    members: int,
) -> str | None:
    """The authored sentence, or None if we could not write one. Background callers only."""
    features = list(features)
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
        from app.orchestrator.llm import llm_configured, llm_json, router_model

        if not llm_configured():
            return None
        # Router tier on purpose: one 140-token sentence from facts we hand it — the synth
        # model spent seconds on the same job.
        data = llm_json(
            model=router_model(),
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
    # None, never the template: the caller already shipped that, and storing it would
    # stamp a fingerprint that stops us retrying once the model is reachable again.
    return None


# ── shared threads between the caller and the members ─────────────────────────


def _shared_concepts(user_id: str) -> dict[str, list[tuple[str, str]]]:
    """peer user_id -> [(label, subject_kind)] they and the caller both hold.

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
        # shared_concept_subjects[i] belongs to labels[i] (20261022120000): a
        # 'child' entry is a fact about their kids, not about them.
        subjects = r.get("shared_concept_subjects")
        subjects = list(subjects) if isinstance(subjects, list) else []
        clean: list[tuple[str, str]] = [
            (str(x).strip(), str(subjects[i] if i < len(subjects) else "self"))
            for i, x in enumerate(labels)
            if str(x or "").strip()
        ]
        if clean:
            out[uid] = clean[:_MAX_SHARED_LABELS]
    return out


def _shared_line(labels: list[tuple[str, str]], relation: str) -> str:
    """The one honest line under a member's name. Shared threads when there are
    any; otherwise the fact that IS true of every row here — you both go here.

    Threads held about a child get their own clause: "you both" must only ever
    describe the two adults.
    """
    mine = [lb for lb, subject in labels if subject == "self"]
    kids = [lb for lb, subject in labels if subject == "child"]
    clauses = []
    if mine:
        clauses.append("You both: " + " · ".join(mine))
    if kids:
        clauses.append("Your kids both: " + " · ".join(kids))
    if clauses:
        return " · ".join(clauses)
    return f"You both go to this {relation}"


# ── surface 1: the look-screen card ───────────────────────────────────────────


def _status_line(count: int, meets: int, *, is_member: bool = True) -> str:
    if is_member:
        people = "just you so far" if count <= 1 else f"{count} people"
    else:
        # A curious joiner and a visitor are not in `count` — "just you" would be a lie.
        people = "1 person" if count == 1 else f"{count} people"
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
                "relation": place_relation_noun(
                    c.get("circle_type"), c.get("noun"), c.get("circle_key")
                ),
                "emoji": place_relation_emoji(c.get("circle_type"), c.get("emoji")),
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
    """The place, for anyone who opens it. Raises ValueError('affiliation_not_found' |
    'place_required' | 'place_not_found').

    Three viewers, one shape. A member gets the roster, her own words and the host/invite
    CTAs. A 'curious' joiner (§19) and a visitor who belongs to nothing here get the head
    — the place, what it has, how many people, what's coming up — and no names: neither of
    them says she goes here, so the roster stays with the members.

    Opening a place you are not in is not a §F leak: discover_communities_near already
    names any nearby place to any neighbour, and a peer's profile names the ones you share
    or are matched on. §F protects the PEOPLE, and they are gated on `is_member` below."""
    viewer = ("confirmed", "curious")
    resolved: dict[str, Any] = {}
    pid = _resolve_place(
        user_id,
        affiliation_id=affiliation_id,
        place_id=place_id,
        statuses=viewer,
        out_affiliation=resolved,
        require_member=False,
    )
    # Resolved by place_id, the check above already fetched the caller's row (or found none
    # — a visitor). Resolved by affiliation_id it proved the row is hers without reading
    # it, so that read happens here.
    mine = resolved
    if affiliation_id:
        mine = caller_affiliation_at(user_id, pid, statuses=viewer) or {}
    is_member = bool(mine) and str(mine.get("status") or "confirmed") == "confirmed"
    from app.place_activities import activities_at_place

    # Five reads about the same place, none of which needs another's answer. Serially
    # they were five round trips the card waited through one at a time.
    got = _gather(
        place=lambda: _place_row(pid),
        features=lambda: place_features(pid, user_id),
        activities=lambda: activities_at_place(pid, user_id),
        members=lambda: _member_rows(pid),
        events=lambda: _event_rows_for_profile(pid),
    )
    place = got["place"]
    if not place:
        raise ValueError("place_not_found")
    features, activities, members = got["features"], got["activities"], got["members"]
    events, meets_this_week_count = got["events"]
    if not mine:
        # Same rule discover_communities_near applies to the list: a place kept alive only
        # by someone the caller blocked must not surface at all, and the count a visitor
        # sees must not include them. Members skip this — they are already inside.
        blocked = _blocked_ids(user_id, [str(m.get("user_id") or "") for m in members])
        members = [m for m in members if str(m.get("user_id") or "") not in blocked]
        if not members:
            raise ValueError("place_not_found")
    name = str(place.get("name") or "").strip()
    relation = place_relation_noun(
        mine.get("circle_type") or place.get("place_type"),
        mine.get("noun"),
        mine.get("circle_key"),
    )
    count = len(members)
    # Needs the roster, so it follows it — its own three reads are the next wave.
    preview = _member_preview(
        user_id, members, phone_verified=phone_verified and is_member
    )
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
        "emoji": place_relation_emoji(
            mine.get("circle_type") or place.get("place_type"), mine.get("emoji")
        ),
        "detail": str(mine.get("detail") or "").strip() or None,
        # What the caller is to this place: 'member' counts and is counted, 'curious' is
        # watching it (POST /lana/circles/membership flips it), 'visitor' has no row here
        # at all and reached the place from a peer's profile or discovery — the FE reads
        # this to offer Join.
        "membership": "member" if is_member else "curious" if mine else "visitor",
        "member_count": count,
        "active": count >= 2,
        # Counted from the meets already fetched above — reading this place's events
        # twice was a wasted round trip on every profile open.
        "status_line": _status_line(count, meets_this_week_count, is_member=is_member),
        # Read off the place row we already fetched — no model call in this request.
        # The noun here is the PLACE's, not `relation` (which is the caller's own word
        # for it): the description is stored once for everyone, so keying it on a
        # per-member noun made two members with different words overwrite each other's
        # line on every open — the per-open model call this exists to remove.
        "description": _blurb(
            place_id=pid,
            place_name=name,
            relation=place_relation_noun(place.get("place_type")),
            area=str(place.get("zip") or "").strip() or None,
            features=[f["label"] for f in features],
            members=count,
            stored=place.get("blurb"),
            stored_key=place.get("blurb_key"),
        ),
        "features": features,
        # Everything anyone does here, `mine` marking the caller's own — one list
        # serves both "your activities" and the "add more" menu (place_activities.py).
        "activities": activities,
        "member_preview": preview,
        "upcoming_events": events,
        # The venue for "Create an event": POST this block verbatim to
        # /lana/sessions/{id}/event-venue (a plain context write) BEFORE posting the
        # chip's message, so hosting runs through chat as always but on THIS place
        # instead of a re-geocoded name. `place_id` is the GOOGLE id (not our
        # places.id), so the post-publish stamp lands on this same place_ref and the
        # meet shows up in this community's upcoming_events. Null = no google id on
        # file, so the FE lets the host flow ask for the venue as usual.
        "create_event_venue": _create_event_venue(place)
        if phone_verified and is_member
        else None,
        # Hosting and inviting are things members do here; a curious viewer gets the
        # head and the FE's own Join.
        "actions": community_profile_actions(place_name=name, relation=relation)
        if phone_verified and is_member
        else [],
    }


def _create_event_venue(place: dict[str, Any]) -> dict[str, Any] | None:
    google_id = str(place.get("google_place_id") or "").strip()
    name = str(place.get("name") or "").strip()
    if not google_id or not name:
        return None
    return {
        "name": name,
        # "" not null — EventVenueRequest.address is a plain str, so the block has to be
        # postable verbatim.
        "address": str(place.get("address") or "").strip(),
        "place_id": google_id,
        "lat": place.get("lat"),
        "lng": place.get("lng"),
        # Hosting from HERE means the meet is for this community — so the setup card's
        # picker arrives pre-selected and its members get the publish email. Our places.id,
        # not the Google one: that is what events.circle_place_ref stores.
        "circle_place_id": str(place.get("id") or "") or None,
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
    roster (that's the people panel, one tap away).

    The caller is IN this list, flagged `me` (§17): member_count has always counted her,
    so leaving her out rendered "2 members" over one face."""
    ids = [str(m.get("user_id")) for m in members if str(m.get("user_id") or "")]
    if not ids or not phone_verified:
        return []
    # Self goes in with the rest so a failed block read still fails closed — it hides
    # every row, including hers, rather than rendering a roster of one.
    blocked = _blocked_ids(user_id, ids)
    visible = [uid for uid in ids if uid not in blocked][:5]
    users = _users_by_id(visible)
    out: list[dict[str, Any]] = []
    for uid in visible:
        u = users.get(uid) or {}
        out.append(
            {
                "peer_user_id": uid,
                "nickname": str(u.get("nickname") or "").strip() or None,
                "avatar_url": str(u.get("profile_photo_url") or "").strip() or None,
                "me": uid == user_id,
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
    resolved: dict[str, Any] = {}
    pid = _resolve_place(
        user_id,
        affiliation_id=affiliation_id,
        place_id=place_id,
        out_affiliation=resolved,
    )
    # The place, the roster and the caller's concepts don't depend on each other —
    # only membership (already proven above) gates them. Shared concepts are fetched
    # only for a caller who can actually be shown names.
    got = _gather(
        place=lambda: _place_row(pid),
        members=lambda: _member_rows(pid),
        **({"shared": lambda: _shared_concepts(user_id)} if phone_verified else {}),
    )
    place, members = got["place"], got["members"]
    mine = resolved or caller_affiliation_at(user_id, pid)
    if not mine:
        raise ValueError("not_a_member")
    relation = place_relation_noun(
        mine.get("circle_type") or place.get("place_type"),
        mine.get("noun"),
        mine.get("circle_key"),
    )
    # `member_count` stays what it always was — the people who actually go here — so
    # the header can read "34 people · 28 go here in real life" without a second call.
    status_by_uid = {
        str(m.get("user_id")): str(m.get("status") or "confirmed") for m in members
    }
    total = sum(1 for s in status_by_uid.values() if s == "confirmed")
    curious_total = len(status_by_uid) - total
    if not phone_verified:
        return {
            "place_id": pid,
            "place_name": str(place.get("name") or "").strip() or None,
            "member_count": total,
            "curious_count": curious_total,
            "members": [],
            "has_more": False,
            "requires_phone_verification": True,
        }
    # The caller is one of the people here, so she is in the list, flagged `me` (§17):
    # member_count counts her, and a roster one row short of its own count reads as a bug.
    ids = [str(m.get("user_id")) for m in members if str(m.get("user_id") or "")]
    # Self goes in with the rest so a failed block read still fails closed — it hides
    # every row, including hers, rather than rendering a roster of one.
    blocked = _blocked_ids(user_id, ids)
    visible = [uid for uid in ids if uid not in blocked]
    page = visible[max(offset, 0) : max(offset, 0) + max(limit, 1)]
    users = _users_by_id(page)
    shared = got.get("shared") or {}
    from app.place_activities import activities_by_member

    activities = activities_by_member(pid)
    rows: list[dict[str, Any]] = []
    for uid in page:
        u = users.get(uid) or {}
        nickname = str(u.get("nickname") or "").strip()
        me = uid == user_id
        # Nothing is "shared with" yourself, and you cannot nudge yourself.
        labels = [] if me else (shared.get(uid) or [])
        row: dict[str, Any] = {
            "peer_user_id": uid,
            "nickname": nickname or None,
            "avatar_url": str(u.get("profile_photo_url") or "").strip() or None,
            # Tags are topic words for the chip row; the subject lives in the line.
            "trait_tags": [lb for lb, _ in labels],
            # What they are to this place, and what they do here — the row's two chip
            # kinds ("member" + "CrossFit"). A curious joiner is here too, said so.
            "membership": (
                "member" if status_by_uid.get(uid, "confirmed") == "confirmed" else "curious"
            ),
            "activities": list(activities.get(uid) or []),
            "shared_line": None if me else _shared_line(labels, relation),
            "me": me,
            # Deliberately absent: stars, band, badge, similarity. Nothing here
            # compared two people ([[truthful-peer-match-model]]).
            "actions": [],  # filled below, once we know who is already connected
        }
        rows.append(row)
    # An intro already on its way, or a connection already made, is a STATUS — not a
    # control inviting the same action again. The roster kept offering Nudge next to
    # someone Lana had just introduced you to, and the only thing a second tap could
    # do was fail the 7-day pair cooldown (2026-08-18). Same helper and same tiers the
    # peer cards use, so both surfaces agree on what "already done" means.
    from app.peer_discovery_surface import stamp_connection_state

    stamp_connection_state(rows, user_id=user_id)
    for row in rows:
        nickname = str(row.get("nickname") or "").strip()
        if row.get("me") or not nickname or row.get("connection"):
            continue
        row["actions"] = [
            peer_card_nudge_action(nickname=nickname, peer_user_id=str(row["peer_user_id"]))
        ]
    return {
        "place_id": pid,
        "place_name": str(place.get("name") or "").strip() or None,
        "member_count": total,
        "curious_count": curious_total,
        "members": rows,
        "has_more": len(visible) > max(offset, 0) + len(page),
        "requires_phone_verification": False,
    }
