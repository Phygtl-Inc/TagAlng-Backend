"""Find a community that already exists nearby, and join it.

The three pre-existing ways into a community all started from the user: they
mentioned a place in conversation (`chat_extraction`, closed by Lana's "which
spot is it?" ask), they added one in the Communities panel (`profile_add`), or
they redeemed an invite and self-confirmed their own place (`invite_confirmed`).
None of them let a user SEE a community that already exists and join it. This
module is that path.

PROVENANCE (the product ask). Two facts, because one column cannot answer both
questions:

  * `source`        — how the community first ENTERED the system. Never
                      overwritten, so "they mentioned it in chat" stays true
                      forever.
  * `confirmed_via` — the action that turned a candidate into a REAL (confirmed +
                      grounded) community.

They diverge in exactly the interesting case: a place captured from something the
user said, parked as a candidate, and later confirmed by tapping Join on the
discovery panel — `source='chat_extraction'`, `confirmed_via='community_join'`.
A fresh join is `source='community_join'` on both counts.

DISCLOSURE (§F). Discovery returns a place, a member count and a coarse
distance — never who is there. The people panel stays members-only
(`app/community_surface.py`), so joining is what earns you the names, and the SQL
counts only members the caller may be counted alongside (a place kept alive
solely by a blocked user is not returned at all).

JOINING IS A SELF-CLAIM, NOT A REQUEST. These are real-world places, so "I go
here too" is a statement about yourself: it takes effect immediately, with no
approval and nobody to notify. Leaving is the existing soft-delete
(`/lana/circles/remove`), which drops the row from matching at once.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from collections import Counter
from typing import Any

from app.auth import service_client
from app.circles_capture import CIRCLE_TYPES, _slugify
from app.circles_flow import place_relation_emoji, place_relation_noun

logger = logging.getLogger(__name__)

# Same coarse radius as radius peer-matching: wide enough that the adjacent block
# is in, narrow enough that the next town is not. Overridable per deploy.
_DEFAULT_RADIUS_M = 8000.0
_MAX_LIMIT = 40

# What made the row real. Mirrors the DB check constraint (migration 20261004).
CONFIRMED_VIA_GROUNDING = "grounding_ask"
CONFIRMED_VIA_PROFILE = "profile_add"
CONFIRMED_VIA_INVITE = "invite_self_confirm"
CONFIRMED_VIA_JOIN = "community_join"

# How each row is described back to the user, in one honest phrase. The product
# question "did they join it in Lana, or did we add it after they mentioned it?"
# is answered by this pair, not by guesswork over timestamps.
JOINED_VIA_LABELS = {
    CONFIRMED_VIA_JOIN: "Joined in Lana",
    CONFIRMED_VIA_GROUNDING: "From something you told Lana",
    CONFIRMED_VIA_PROFILE: "You added it",
    CONFIRMED_VIA_INVITE: "From an invite",
}


def radius_meters() -> float:
    raw = os.environ.get("LANA_COMMUNITY_DISCOVERY_RADIUS_METERS", "").strip()
    if not raw:
        return _DEFAULT_RADIUS_M
    try:
        return max(100.0, min(float(raw), 200000.0))
    except ValueError:
        logger.warning("community_discovery_bad_radius value=%r — using default", raw)
        return _DEFAULT_RADIUS_M


def joined_via_label(confirmed_via: str | None, source: str | None) -> str | None:
    """One phrase for how this community came to be theirs.

    `confirmed_via` is the authority (it records the closing action). A row from
    before that column existed falls back to its `source`, which for those rows
    implies the same thing — the grounding ask was the only path that could have
    confirmed a chat-captured community.
    """
    via = str(confirmed_via or "").strip()
    if via in JOINED_VIA_LABELS:
        return JOINED_VIA_LABELS[via]
    legacy = {
        "profile_add": JOINED_VIA_LABELS[CONFIRMED_VIA_PROFILE],
        "invite_confirmed": JOINED_VIA_LABELS[CONFIRMED_VIA_INVITE],
        "chat_extraction": JOINED_VIA_LABELS[CONFIRMED_VIA_GROUNDING],
        "community_join": JOINED_VIA_LABELS[CONFIRMED_VIA_JOIN],
    }
    return legacy.get(str(source or "").strip())


# ── discover ──────────────────────────────────────────────────────────────────


def discover_communities(
    user_id: str,
    *,
    limit: int = 20,
    query: str | None = None,
    locale: str = "en",
    radius_m: float | None = None,
) -> list[dict[str, Any]]:
    """Communities near the caller with at least one visible member.

    Ordered by how alive the place is (members), then how close. Returns [] on any
    error — a discovery panel that fails must read as "nothing yet", never as a
    stack trace. `is_member` marks the caller's own places rather than hiding
    them, so the panel can say "you're in this one".
    """
    if not user_id:
        return []
    try:
        res = service_client().rpc(
            "discover_communities_near",
            {
                "p_user_id": user_id,
                "p_radius_meters": float(radius_m if radius_m else radius_meters()),
                "p_limit": max(1, min(int(limit or 20), _MAX_LIMIT)),
                "p_locale": (locale or "en"),
                "p_query": (str(query).strip() or None) if query else None,
            },
        ).execute()
        rows = res.data if isinstance(res.data, list) else []
    except Exception:
        logger.exception("discover_communities_failed user=%s", user_id)
        return []
    out: list[dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict) or not r.get("place_id"):
            continue
        name = str(r.get("name") or "").strip()
        if not is_joinable_place_name(name):
            # An address or a bare ZIP that got grounded as somebody's "community".
            # Their own row keeps working; it is just never offered to anyone else.
            continue
        types = r.get("member_types")
        primary = str(r.get("place_type") or "").strip() or _first_type(types)
        out.append(
            {
                "place_id": str(r["place_id"]),
                "place_name": str(r.get("name") or "").strip() or None,
                "place_address": str(r.get("address") or "").strip() or None,
                "place_type": primary or None,
                # What members call it, in the app's own vocabulary — never "circle".
                # Derived from the PLACE's type, not from a circle: this lists
                # communities the viewer is NOT in, so there is no row of theirs to
                # take a noun/emoji from.
                "relation": place_relation_noun(primary),
                "emoji": place_relation_emoji(primary),
                "zip": str(r.get("zip") or "").strip() or None,
                "member_count": int(r.get("member_count") or 0),
                "distance_text": str(r.get("distance_text") or "").strip() or None,
                "is_member": bool(r.get("is_member")),
                "status_line": _discovery_status_line(
                    int(r.get("member_count") or 0),
                    str(r.get("distance_text") or "").strip() or None,
                    bool(r.get("is_member")),
                ),
            }
        )
    return out


def _first_type(types: Any) -> str:
    if isinstance(types, list):
        for t in types:
            s = str(t or "").strip()
            if s in CIRCLE_TYPES:
                return s
    return ""


def _discovery_status_line(members: int, distance_text: str | None, is_member: bool) -> str:
    """Only the two facts on the row. `member_count` includes the caller when they
    are already in, so an "N people" line must not read as N strangers."""
    if is_member:
        head = "You're in" if members <= 1 else f"You + {members - 1} others"
    else:
        head = "1 person" if members == 1 else f"{members} people"
    return f"{head} · {distance_text}" if distance_text else head


# ── join ──────────────────────────────────────────────────────────────────────


# Places that are not communities however they got grounded: a bare street address,
# an apartment/unit line, a ZIP typed as a name. The chat extractor parks whatever the
# user said and grounding pins whatever Google returned, so dev already holds rows like
# "10057 Selten Way #328" and a place literally named "32827". They are legitimate
# `places` rows (an event can be there) but offering one as a community to JOIN is
# nonsense, so discovery filters them out. Deliberately name-shape only — nothing here
# guesses at quality, and a real place with a number in its name ("FIT 407") survives.
_ADDRESSISH_RE = re.compile(
    r"""^(?:
          \#?\d+\s+\w+.*            # 373 Tampa Ct, 692 Olde Camelot Cir #3282
        | \d{5}(?:-\d{4})?          # a ZIP as a name
        )$""",
    re.IGNORECASE | re.VERBOSE,
)


def is_joinable_place_name(name: str) -> bool:
    """False for address-shaped / ZIP-shaped names — see _ADDRESSISH_RE."""
    text = str(name or "").strip()
    if len(text) < 2:
        return False
    return not _ADDRESSISH_RE.match(text)


def _place_row(place_id: str) -> dict[str, Any]:
    try:
        res = (
            service_client()
            .table("places")
            .select("id, name, address, place_type")
            .eq("id", place_id)
            .limit(1)
            .execute()
        )
        rows = res.data if isinstance(res.data, list) else []
    except Exception:
        logger.exception("community_join_place_read_failed place=%s", place_id)
        return {}
    return rows[0] if rows else {}


def _existing_rows(user_id: str) -> list[dict[str, Any]]:
    try:
        res = (
            service_client()
            .table("circle_affiliations")
            .select("id, circle_key, circle_type, place_ref, status, source, confirmed_via, noun, emoji")
            .eq("user_id", user_id)
            .is_("dismissed_at", "null")
            .limit(60)
            .execute()
        )
        rows = res.data if isinstance(res.data, list) else []
    except Exception:
        logger.exception("community_join_existing_read_failed user=%s", user_id)
        return []
    return [r for r in rows if isinstance(r, dict)]


def _place_noun_emoji(place_id: str) -> dict[str, str]:
    """The noun/emoji this community already answers to, from its members' rows.

    Only capture (the LLM) ever mints these; a Join tap never did, so joiners
    inherit rather than guess. Most-used wins so one odd row cannot rename a
    place. Empty dict on nothing stored or any error — the caller falls back to
    the type word, exactly as before.
    """
    try:
        res = (
            service_client()
            .table("circle_affiliations")
            .select("noun, emoji")
            .eq("place_ref", place_id)
            .is_("dismissed_at", "null")
            .limit(200)
            .execute()
        )
        rows = res.data if isinstance(res.data, list) else []
    except Exception:
        logger.exception("community_place_noun_read_failed place=%s", place_id)
        return {}
    out: dict[str, str] = {}
    for field in ("noun", "emoji"):
        counts = Counter(
            str((r or {}).get(field) or "").strip()
            for r in rows
            if str((r or {}).get(field) or "").strip()
        )
        if counts:
            out[field] = counts.most_common(1)[0][0]
    return out


def _unique_key(base: str, taken: set[str]) -> str:
    key = base or "spot"
    if key not in taken:
        return key
    for n in range(2, 20):
        candidate = f"{key}_{n}"[:64]
        if candidate not in taken:
            return candidate
    return f"{key}_x"[:64]


def join_community(
    user_id: str,
    place_id: str,
    *,
    circle_type: str | None = None,
    membership: str = "member",
) -> dict[str, Any]:
    """Join a community the user found in Lana.

    `membership` is the joiner's own answer to "do you actually go here?" (§19):
    'member' is membership as always; 'curious' parks the row as status='curious', which
    every member count, roster and matcher excludes — she gets the place in her own list
    and nobody gets her as a neighbour they have never met. Tapping Join again as a
    member promotes the same row.

    Three cases, in order:
      1. already a confirmed member  → no-op, `already_member: True`
      2. a candidate of theirs (a place they mentioned, or an ungrounded row with
         the same key) → CONFIRM that row and pin it here. `source` keeps its
         original value — the community really did come from what they said — and
         `confirmed_via` records that the Join tap is what closed it.
      3. otherwise → a fresh row, `source` and `confirmed_via` both
         `community_join`.

    Raises ValueError('place_not_found' | 'place_required').
    """
    if not place_id:
        raise ValueError("place_required")
    place = _place_row(place_id)
    if not place:
        raise ValueError("place_not_found")

    joined_status = "curious" if str(membership or "").strip().lower() == "curious" else "confirmed"

    sb = service_client()
    rows = _existing_rows(user_id)
    mine_here = next((r for r in rows if str(r.get("place_ref") or "") == place_id), None)
    if mine_here and str(mine_here.get("status") or "") == joined_status:
        return {
            "affiliation_id": str(mine_here["id"]),
            "place_id": place_id,
            "place_name": place.get("name"),
            "status": joined_status,
            "already_member": True,
            "source": mine_here.get("source"),
            "confirmed_via": mine_here.get("confirmed_via"),
        }

    resolved_type = (
        (circle_type or "").strip().lower()
        or str(place.get("place_type") or "").strip().lower()
        or "other"
    )
    if resolved_type not in CIRCLE_TYPES:
        resolved_type = "other"
    base_key = _slugify(str(place.get("name") or "")) or resolved_type

    # A candidate to promote: the row already pointing here, else an ungrounded row
    # the user parked under the same name (the "you mentioned a gym" case).
    candidate = mine_here or next(
        (
            r
            for r in rows
            if not r.get("place_ref") and str(r.get("circle_key") or "") == base_key
        ),
        None,
    )

    patch: dict[str, Any] = {
        "place_ref": place_id,
        "status": joined_status,
        "confidence": 1.0,  # a deliberate tap, not an inference
        "confirmed_via": CONFIRMED_VIA_JOIN,
    }
    # A join never asked an LLM for a noun, so every joiner's row went in with
    # noun NULL and place_relation_noun fell through to the TYPE word — which is
    # "spot" for a place_type of 'other'. Seven neighbours joined one restaurant
    # and every fellow card read "You both: your spot" (prod, 2026-08-31).
    # The community already knows what it is called: take the noun/emoji a member
    # who was asked at capture already stored. Blank when nobody has one — a wrong
    # noun is worse than the bucket word.
    inherited = _place_noun_emoji(place_id)
    if candidate:
        affiliation_id = str(candidate["id"])
        for field, value in inherited.items():
            if value and not str(candidate.get(field) or "").strip():
                patch[field] = value
        try:
            sb.table("circle_affiliations").update(patch).eq("id", affiliation_id).execute()
        except Exception:
            logger.exception("community_join_promote_failed aff=%s", affiliation_id)
            raise ValueError("join_failed") from None
        origin_source = str(candidate.get("source") or "community_join")
        promoted_from_candidate = True
    else:
        taken = {str(r.get("circle_key") or "") for r in rows}
        row = {
            "user_id": user_id,
            "circle_type": resolved_type,
            "circle_key": _unique_key(base_key, taken),
            "source": CONFIRMED_VIA_JOIN,
            **inherited,
            **patch,
        }
        try:
            res = sb.table("circle_affiliations").insert(row).execute()
            affiliation_id = str(res.data[0]["id"]) if res.data else ""
        except Exception:
            logger.exception("community_join_insert_failed user=%s place=%s", user_id, place_id)
            raise ValueError("join_failed") from None
        if not affiliation_id:
            raise ValueError("join_failed")
        origin_source = CONFIRMED_VIA_JOIN
        promoted_from_candidate = False

    # Housekeeping the grounding path already does: flush any feature notes parked
    # on the candidate, close its "which spot?" ask so the tile stops asking, and
    # open the one enrichment question ("what do you enjoy most at X?"). Skipped for a
    # curious join — she just said she does NOT go there, so asking what she enjoys
    # most about it would be Lana not listening.
    if joined_status == "confirmed":
        _after_join(user_id, affiliation_id, candidate, place_id, str(place.get("name") or ""))
        # The members already there hear about it — same roster the community's meets mail.
        notify_members_of_join(place_id, str(place.get("name") or ""), user_id)

    logger.info(
        "community_joined user=%s place=%s source=%s promoted=%s",
        user_id,
        place_id,
        origin_source,
        promoted_from_candidate,
    )
    return {
        "affiliation_id": affiliation_id,
        "place_id": place_id,
        "place_name": place.get("name"),
        "status": joined_status,
        "already_member": False,
        "source": origin_source,
        "confirmed_via": CONFIRMED_VIA_JOIN,
        "promoted_from_candidate": promoted_from_candidate,
    }


def set_membership(user_id: str, affiliation_id: str, membership: str) -> dict[str, Any]:
    """"I'm a member — I go here" / "Not yet — just curious" (§19), answered AFTER the
    join: the sheet is a separate step from the tap, so it posts the answer here.

    'member' → status='confirmed' (counted, named, matched). 'curious' → status='curious'
    (hers to see, excluded everywhere else). Idempotent.

    Raises ValueError('affiliation_not_found' | 'place_required').
    """
    status = "curious" if str(membership or "").strip().lower() == "curious" else "confirmed"
    sb = service_client()
    try:
        res = (
            sb.table("circle_affiliations")
            .select("id, place_ref, status")
            .eq("id", affiliation_id)
            .eq("user_id", user_id)
            .is_("dismissed_at", "null")
            .limit(1)
            .execute()
        )
        rows = [r for r in (res.data or []) if isinstance(r, dict)]
    except Exception:
        logger.exception("membership_lookup_failed aff=%s", affiliation_id)
        raise ValueError("affiliation_not_found") from None
    if not rows:
        raise ValueError("affiliation_not_found")
    row = rows[0]
    place_id = str(row.get("place_ref") or "")
    if not place_id:
        # An ungrounded candidate is not a community yet — nothing to be a member of.
        raise ValueError("place_required")
    if str(row.get("status") or "") != status:
        try:
            sb.table("circle_affiliations").update({"status": status}).eq(
                "id", affiliation_id
            ).execute()
        except Exception:
            logger.exception("membership_write_failed aff=%s", affiliation_id)
            raise ValueError("membership_write_failed") from None
        logger.info(
            "membership_set user=%s place=%s status=%s", user_id, place_id, status
        )
    return {
        "affiliation_id": affiliation_id,
        "place_id": place_id,
        "membership": "member" if status == "confirmed" else "curious",
    }


# ── the chat turn ─────────────────────────────────────────────────────────────

# Nearby communities named in one reply. More than this and the prose stops being
# readable; the cards carry the rest.
_CHAT_NEARBY_MAX = 5

# Roster cards one reply can carry — the wire cap in main._peer_matches_from_ctx. Kept in
# step with it so the count the reply states is the count that ships.
_ROSTER_CARDS_MAX = 8


# Words that carry no place in them, so "the Mizu Sushi community" and "Mizu Sushi" name
# the same spot.
_NAME_NOISE = frozenset(
    {"the", "a", "an", "at", "in", "of", "my", "our", "community", "communities",
     "group", "groups", "place", "spot",
     # Connectors. "and" matters most because _name_tokens CREATES it out of "&":
     # "barnes and nobel" and "Mizu Sushi & Steakhouse" shared the word "and", so a
     # sushi restaurant was offered as a candidate for a bookstore, and the two
     # candidates then triggered a clarifier instead of the single-candidate answer
     # (QA 2026-08-21).
     "and", "or", "y", "e"}
)


def _name_tokens(name: str) -> list[str]:
    """Lowercase alphanumeric words, "&" read as "and".

    People do not type the ampersand: "mizu sushi and steakhouse" against a row named
    "Mizu Sushi & Steakhouse" failed raw containment, and the turn then said it could not
    find a community that the same reply went on to say they were already in
    (QA 2026-08-21)."""
    folded = name.casefold().replace("&", " and ")
    return "".join(ch if ch.isalnum() else " " for ch in folded).split()


def _same_place_name(said: str, row_name: str) -> bool:
    """Do these two name one place? Normalized containment, then a word-subset pass.

    No regex and no fuzzy library ([[no-new-regex-use-ai-signals]]) — the name itself came
    from the AI slot, and this only has to survive punctuation and filler."""
    a, b = _name_tokens(said), _name_tokens(row_name)
    if not a or not b:
        return False
    # Padded, so containment lands on WHOLE words: unpadded, "a" matched "and" and "fit"
    # matched "Fitness", and any short fragment claimed a specific place.
    ja, jb = f" {' '.join(a)} ", f" {' '.join(b)} "
    if ja in jb or jb in ja:
        return True
    # "Mizu Sushi Steakhouse" vs "Mizu Sushi & Steakhouse" — same words, and normalizing
    # left them in a different order than containment can see. Two meaningful words
    # minimum: a lone "fit" would otherwise claim every gym on the list.
    sa = {t for t in a if t not in _NAME_NOISE}
    sb = {t for t in b if t not in _NAME_NOISE}
    if len(sa) < 2 or not sb:
        return False
    return sa <= sb or sb <= sa


def _resolve_named_community(
    user_id: str, name: str, *, locale: str
) -> dict[str, Any] | None:
    """The community the user NAMED — hers first, then the ones near her."""
    if not str(name or "").strip():
        return None
    for pool in (
        _my_communities(user_id),
        discover_communities(user_id, limit=_MAX_LIMIT, locale=locale),
    ):
        for c in pool:
            if _same_place_name(name, str(c.get("place_name") or "")):
                return c
    return None


def _near_name_candidates(
    said: str, pools: list[list[dict[str, Any]]], *, limit: int = 3
) -> list[dict[str, Any]]:
    """Communities that share a meaningful word with what they said.

    A near miss is not a miss. "Fitness CF" against three gyms, or a name typed a little
    wrong, is a question about WHICH one — answering "there is no community by that name"
    is wrong, and picking one for them is a guess."""
    want = {t for t in _name_tokens(said) if t not in _NAME_NOISE}
    if not want:
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for pool in pools:
        for c in pool:
            pid = str(c.get("place_id") or "")
            name = str(c.get("place_name") or "")
            if not pid or pid in seen or not name:
                continue
            if want & {t for t in _name_tokens(name) if t not in _NAME_NOISE}:
                seen.add(pid)
                out.append(c)
                if len(out) >= limit:
                    return out
    return out


# What a "did you mean" chip should ask on their behalf, per side of the question. The
# chip re-asks THEIR question about the place they picked — hard-coding the roster ask
# rewrote "what type of community is this" into "who is in it" (QA 2026-08-21).
_CHIP_ASK = {
    "people": "who is in {place}",
    "about": "what kind of place is {place}",
}


def _did_you_mean_turn(
    *,
    said: str,
    candidates: list[dict[str, Any]],
    message: str,
    session_ctx: dict[str, Any],
    ask: str = "about",
) -> str:
    """"Did you mean this one?" — the honest answer to a near miss, with the real names
    tap-able so the next turn resolves exactly instead of guessing again."""
    from app.reply_compose import compose_reply

    names = [str(c.get("place_name") or "").strip() for c in candidates]
    names = [n for n in names if n]
    # policy_chips is the generic one-turn CTA surface (app/ui_actions.derive_ui_actions):
    # label is what they tap, `send` is posted back verbatim, and the exact row name is
    # what makes the next turn's match exact.
    template = _CHIP_ASK.get(ask, _CHIP_ASK["about"])
    session_ctx["policy_chips"] = [
        {"label": n, "send": template.format(place=n)} for n in names[:3]
    ]
    return compose_reply(
        goal=(
            "They named a place you are not sure about. Say you want to check WHICH one "
            "they mean — never claim it does not exist, and never pick one for them. ONE "
            "short question; the names are tap-able buttons under your message, so do not "
            "spell out more than two of them."
        ),
        facts=[
            f'They said: "{said}"',
            "Closest real communities, which are the buttons under your message: "
            + ", ".join(names),
            "You do NOT know which of these they meant",
        ],
        fallback=(
            f"Did you mean {names[0]}?" if len(names) == 1
            else f"Which one did you mean — {names[0]} or {names[1]}?"
        ),
        session_ctx=session_ctx,
        user_message=message,
    )


def _arm_join(session_ctx: dict[str, Any], place_id: str, place_name: str) -> None:
    """Offer to add them, and make the offer TAP-ABLE.

    Arming the pending state alone left "want me to add you?" with nothing to press —
    the answer had to be typed, which is not what an offer looks like (QA 2026-08-21).
    "Join <name>" is the payload the join lane already reads (read_join_reply), and it is
    what the discovery cards' own Join button has always posted."""
    session_ctx["community_join_pending"] = {
        "places": [{"place_id": place_id, "place_name": place_name}]
    }
    session_ctx["policy_chips"] = [
        {"label": "Add me", "send": f"Join {place_name}"},
        {"label": "Show me others", "send": "what communities are near me"},
    ]


def _community_about_turn(
    user_id: str,
    *,
    community: dict[str, Any],
    message: str,
    session_ctx: dict[str, Any],
    inexact: str | None = None,
) -> str | None:
    """Anything about the PLACE itself — what kind it is, what it has, how big, what is on.

    The community screen renders all of this and chat could not reach any of it, so "what
    type of community is Barnes & Noble" was answered with a roster refusal and then a
    list of other communities (QA 2026-08-21). Reads the same profile the screen does, and
    that read opens for a visitor too ([[visitor-opens-community]]) — you do not have to
    join a bookstore to be told it is a bookstore.

    None when the place cannot be read, so the caller can fall through honestly."""
    from app.community_surface import community_profile
    from app.reply_compose import compose_reply

    pid = str(community.get("place_id") or "")
    try:
        prof = community_profile(user_id, place_id=pid, phone_verified=True)
    except Exception:  # noqa: BLE001 — ValueError('place_not_found') and any read failure
        logger.exception("community_about_turn_failed place=%s", pid)
        return None

    # An about-turn is not a people-turn: no faces, and no scored strip from earlier.
    # activity_previews starts cleared too — it is not turn-scoped, so a browse lane's
    # events would otherwise ride in under a question about a different place.
    session_ctx["peer_matches"] = None
    session_ctx["discovery_surface"] = None
    session_ctx["activity_previews"] = None

    place = str(prof.get("place_name") or "").strip()
    membership = str(prof.get("membership") or "visitor")
    count = int(prof.get("member_count") or 0)
    curious = int(prof.get("curious_count") or 0)
    features = [
        str((f or {}).get("label") or "").strip()
        for f in (prof.get("features") or [])
        if str((f or {}).get("label") or "").strip()
    ]
    events = [
        str((e or {}).get("title") or "").strip()
        for e in (prof.get("upcoming_events") or [])
        if str((e or {}).get("title") or "").strip()
    ]
    facts = [f"Community: {place}"]
    if inexact:
        # They did not name it exactly, so the reply has to say which place it answered
        # about — otherwise a wrong guess reads as a confident answer.
        facts.append(
            f'They said "{inexact}" and this is the one place near them it could be — '
            "name it in your reply so they can correct you"
        )
    if prof.get("relation"):
        facts.append(f"What kind of place it is: {prof['relation']}")
    if prof.get("description"):
        facts.append(f"How it is described: {prof['description']}")
    if prof.get("place_address"):
        facts.append(f"Where it is: {prof['place_address']}")
    facts.append(
        f"People who go here: {count}"
        + (f", plus {curious} curious about it" if curious else "")
    )
    if features:
        facts.append("What members say it has: " + ", ".join(features[:6]))
    if events:
        facts.append("Coming up there: " + "; ".join(events[:3]))
        # The meets themselves, as cards — the community screen shows them and chat only
        # described them, so "there's a Sushi & Social Meetup coming up" arrived with
        # nothing to open (QA 2026-08-21). Same rows the browse lane renders.
        from app.discovery_route import _format_event_when

        session_ctx["activity_previews"] = [
            {
                "activity_id": str(e.get("event_id") or "") or None,
                "title": str(e.get("title") or "").strip(),
                "starts_at": str(e.get("starts_at") or "") or None,
                "has_time": e.get("has_time") is not False,
                "starts_label": _format_event_when(e.get("starts_at")),
                "venue_name": str(e.get("venue_name") or "").strip() or place,
                # They are all at THIS place, which the reply already named.
                "community": None,
                "preview": True,
            }
            for e in (prof.get("upcoming_events") or [])
            if str((e or {}).get("title") or "").strip()
        ][:5]
        facts.append(
            "Their cards are under your message — say it is right below rather than "
            "describing it and leaving them to ask for it"
        )
    else:
        # An empty calendar is an ANSWER, not a failed read. Without saying so she wrote
        # "I can't pull up any events for Barnes & Noble right now", which claims a
        # limitation where there is simply nothing on (QA 2026-08-21).
        facts.append(
            "Nothing is scheduled there at the moment — say that as a fact, never as "
            "something you were unable to look up"
        )
    if not features:
        facts.append("Nobody has said yet what it has — again a fact, not a failed look-up")
    facts.append(
        {
            "member": "They are a member here",
            "curious": "They joined as curious — they have not said they go here",
        }.get(membership, "They are NOT in this community")
    )
    if membership == "visitor":
        # Being let in is a real next step, and a "yes" should mean something.
        _arm_join(session_ctx, pid, place)
    return compose_reply(
        goal=(
            "Answer what they actually asked about this place, using ONLY the facts. If "
            "the facts do not hold what they asked, say that plainly first and then say "
            "what you DO know about it — never answer a different question, and never "
            "list other communities instead. TWO SHORT SENTENCES."
            + (
                " They are not in it, so you may end by offering to add them."
                if membership == "visitor"
                else ""
            )
        ),
        facts=facts,
        fallback=(
            f"{place} — {prof.get('relation') or 'a spot'} near you, with {count} "
            f"{'person' if count == 1 else 'people'} in it."
        ),
        session_ctx=session_ctx,
        user_message=message,
    )


def _roster_chat_turn(
    user_id: str,
    *,
    community: dict[str, Any],
    message: str,
    session_ctx: dict[str, Any],
) -> str:
    """"Who is in <place>" — answered with the people, not the count.

    The roster the community screen renders was reachable only over HTTP, so this ask
    had nothing to read and returned the member COUNT four times in a row while the UI
    showed all seven names one tap away (QA 2026-08-20). Same read, same rows, same
    Nudge the roster offers — served into chat.
    """
    from app.community_surface import community_members
    from app.reply_compose import compose_reply

    pid = str(community.get("place_id") or "")
    place = str(community.get("place_name") or "").strip()
    try:
        roster: dict[str, Any] | None = community_members(
            user_id, place_id=pid, phone_verified=True
        )
    except ValueError:
        # 'not_a_member' — the names here belong to the people who go here.
        roster = None
    except Exception:  # noqa: BLE001
        logger.exception("communities_roster_turn_failed place=%s", pid)
        roster = None

    if roster is None:
        # Say the limit before the offer (constitution §9), and arm the join so a "yes"
        # lands: being let in is the actual answer to what they wanted.
        _arm_join(session_ctx, pid, place)
        return compose_reply(
            goal=(
                "Tell them plainly you cannot show who is in this place because they are "
                "not in it yet — the limit FIRST, in one clause, no apology. Then offer to "
                "add them, which is instant and reversible."
            ),
            facts=[
                f"Community they asked about: {place}",
                f"People in it: {int(community.get('member_count') or 0)}",
                "They are NOT in it, so its people are not theirs to see yet",
            ],
            fallback=(
                f"I can't show you who's in {place} until you're in it yourself — "
                "want me to add you?"
            ),
            session_ctx=session_ctx,
            user_message=message,
        )

    # A roster turn is not a join offer: leaving it armed made the next "yes" join
    # something instead of answering.
    session_ctx["community_join_pending"] = None
    everyone = [m for m in (roster.get("members") or []) if not m.get("me")]
    # main._peer_matches_from_ctx ships at most 8 rows, so a bigger roster would have put
    # "11 cards below" over 8 of them. Cap here instead, and say the remainder out loud —
    # a silent truncation reads as the whole roster.
    others = everyone[:_ROSTER_CARDS_MAX]
    hidden = len(everyone) - len(others)
    rows: list[dict[str, Any]] = []
    for m in others:
        attrs = [str(a).strip() for a in (m.get("attributes") or []) if str(a or "").strip()]
        rows.append(
            {
                "peer_user_id": m.get("peer_user_id"),
                "nickname": m.get("nickname"),
                "avatar_url": m.get("avatar_url"),
                # The tie is the PLACE, and their own threads are what else is true about
                # them ([[truthful-peer-match-model]]). similarity_score stays null and
                # there is no badge: nothing here compared two people.
                "matching_peer_label": attrs[0] if attrs else f"Goes to {place}",
                "similarity_score": None,
                "preview": False,
                "trait_tags": attrs[1:4],
                "actions": m.get("actions") or [],
                "connection": m.get("connection"),
                # "member" | "curious" — the roster screen has always tagged a curious
                # joiner, so the chat card must too, or a watcher reads as someone who
                # actually goes there.
                "membership": m.get("membership"),
                # Already final — keeps stamp_peer_discovery_ctx from re-ranking these and
                # wiping their chips, the same contract tip_rec rows have.
                "community_roster": True,
            }
        )
    if rows:
        session_ctx["peer_matches"] = rows
        # A roster compared nobody: no cosine, no bands, no counts strip. Cleared with
        # None rather than popped ([[ctx-pop-resurrection]]) so a scored summary from an
        # earlier peer search cannot ride in over these cards.
        session_ctx["discovery_surface"] = None

    count = int(roster.get("member_count") or 0)
    curious = int(roster.get("curious_count") or 0)
    named = [str(m.get("nickname") or "").strip() for m in others if m.get("nickname")]
    # Three DIFFERENT numbers, all true, and the reply must not blend them: member_count
    # counts the caller and excludes curious joiners, the cards exclude the caller and
    # include them. Stating only one let "7 people" sit over a different number of cards
    # whenever curious_count was not exactly 1 — the count-vs-roster mismatch this whole
    # fix exists to end.
    facts = [
        f"Community: {place}",
        f"People who go here: {count}, counting them",
        f"Cards under your message: {len(others)} — everyone here except them, each with "
        "a Nudge. Give the count and at most two names; do NOT read the names out one by one",
    ]
    if hidden:
        facts.append(
            f"{hidden} more are here without a card — say there are more rather than "
            "implying the cards are everyone"
        )
    if curious:
        facts.append(
            f"Also here without saying they go: {curious} — curious, not members, and "
            "their cards are below too"
        )
    if named:
        facts.append("Names, for anchoring at most TWO of them: " + ", ".join(named[:6]))
    if not others:
        facts.append("They are the only one here so far")
    return compose_reply(
        goal=(
            "Answer who is in this place. Say how many and that their cards are right "
            "below, anchor with at most TWO names from the facts, and offer an intro to "
            "any of them. TWO SHORT SENTENCES, never a list."
            if others
            else "Tell them they are the only one here so far, and offer to help them "
            "bring someone in or start something here. Never call the place dead."
        ),
        facts=facts,
        fallback=(
            f"{count} people are in {place} — the others are right below. "
            "Want an intro to any of them?"
            if others
            else f"You're the only one in {place} so far — want to invite someone?"
        ),
        session_ctx=session_ctx,
        user_message=message,
    )


def communities_chat_turn(
    user_id: str,
    *,
    message: str,
    session_ctx: dict[str, Any],
    locale: str = "en",
    community_name: str | None = None,
    community_ask: str = "about",
) -> str:
    """Answer a community ask (`discovery.communities`) from real rows.

    Before this existed the ask had no handler, so the classifier sent it to whichever
    arm looked closest — one probe got the area-not-open host bridge ("there aren't any
    local communities to show yet", asserted without counting anything), another got an
    attribute peer search for neighbours "interested in community". Both were wrong
    about the data: the asking account had two communities in its own ZIP.

    Answers with both halves, because either alone is misleading: the ones they are
    already in, and the ones nearby they could join. Every number here is a real read;
    when both halves are genuinely empty the reply says so and offers the one useful
    move (start one), never a claim that the area is too quiet to have any.
    """
    # "who is in <place>" — a named community is a roster ask, not a list ask. Falls
    # through to the list below when the name matches nothing we hold, with the miss
    # stated rather than papered over with a list they did not ask for.
    named_miss: str | None = None
    if community_name:
        said = community_name.strip()[:80]
        hit = _resolve_named_community(user_id, community_name, locale=locale)
        inexact: str | None = None
        if not hit:
            near = _near_name_candidates(
                community_name,
                [
                    _my_communities(user_id),
                    discover_communities(user_id, limit=_MAX_LIMIT, locale=locale),
                ],
            )
            if len(near) == 1:
                # Exactly one place near them shares a word with what they said, so
                # asking "did you mean X?" only stalls — a typo ("barnes and nobel")
                # looped that question three times without ever answering (QA
                # 2026-08-21). Take it and NAME it, so a wrong guess is correctable.
                hit, inexact = near[0], said
            elif near:
                # Genuinely ambiguous: three Lake Nona gyms are a "which one", and
                # picking for them would be a guess.
                session_ctx["peer_matches"] = None
                session_ctx["discovery_surface"] = None
                return _did_you_mean_turn(
                    said=said,
                    candidates=near,
                    message=message,
                    session_ctx=session_ctx,
                    ask=community_ask,
                )
        if hit:
            if community_ask == "people":
                return _roster_chat_turn(
                    user_id, community=hit, message=message, session_ctx=session_ctx
                )
            about = _community_about_turn(
                user_id,
                community=hit,
                message=message,
                session_ctx=session_ctx,
                inexact=inexact,
            )
            if about:
                return about
        named_miss = said

    # Not a roster turn: whatever cards the last one shipped are not these cards.
    # peer_matches is not turn-scoped, and discovery.communities now renders it, so a
    # roster's faces would otherwise re-appear under an unrelated communities reply —
    # the "same card for the third time" shape from the QA report.
    session_ctx["peer_matches"] = None
    session_ctx["discovery_surface"] = None

    mine = _my_communities(user_id)
    nearby = [
        c for c in discover_communities(user_id, limit=_CHAT_NEARBY_MAX * 2, locale=locale)
        if not c.get("is_member")
    ][:_CHAT_NEARBY_MAX]

    session_ctx["community_discovery"] = {
        "communities": nearby,
        "total": len(nearby),
    }
    # Armed for ONE turn so the next message can be read as "Join <place>" — the
    # *_pending twin convention (turn_surfaces.py): the card is turn-scoped, this
    # is not, because the turn that answers it has to still see it.
    session_ctx["community_join_pending"] = (
        {"places": [{"place_id": c["place_id"], "place_name": c["place_name"]} for c in nearby]}
        if nearby
        else None
    )
    if mine:
        from app.community_surface import communities_card

        card = communities_card(user_id, top=len(mine))
        if card:
            session_ctx["communities_card"] = card

    facts: list[str] = []
    if named_miss:
        # Constitution §9: the thing they actually named comes first, even when the
        # answer is that we do not have it.
        facts.append(
            f'They asked about "{named_miss}" and there is NO community by that name '
            "in theirs or near them — say that plainly before anything else"
        )
    if mine:
        # COUNT + one name, not the roll-call. Handing the model six names and four more
        # made it read every one out, and the cards under the message then repeated all
        # ten: a ten-line wall answering a one-line question (QA 2026-08-18).
        facts.append(
            f"Communities they are already in: {len(mine)} "
            f"(one of them: {mine[0]['place_name']}, {_members_phrase(mine[0])})"
        )
    else:
        facts.append("They are not in any community yet")
    if nearby:
        facts.append(
            f"Nearby communities they could join: {len(nearby)} "
            f"(closest: {nearby[0]['place_name']} — {nearby[0]['status_line']}). "
            "The cards under your message list all of them with real member counts, "
            "so your text must NOT name them one by one"
        )
    else:
        facts.append(
            "Nobody nearby has a community yet that they are not already in — do NOT "
            "guess at reasons and do NOT say their area is too quiet to have any"
        )

    if nearby:
        goal = (
            "Answer what they asked: the communities near them. TWO SHORT SENTENCES, and "
            "never a list — the cards under your message carry every name, so a name-by-name "
            "roll-call in the text is the same information twice and unreadable on a phone. "
            "Give the counts, anchor with at most ONE name from the facts, and end by "
            "offering to add them to any — joining is instant and reversible, no warnings "
            "needed."
        )
        fallback = _nearby_fallback(mine, nearby)
    elif mine:
        goal = (
            "Tell them which communities they are already in (from the facts), then say "
            "honestly that nothing NEW has turned up nearby yet, and offer to keep an "
            "ear out or help them start something at a spot they already go to."
        )
        fallback = (
            "Right now you're in "
            + ", ".join(c["place_name"] for c in mine[:3])
            + ". Nothing new nearby yet — want me to keep an ear out?"
        )
    else:
        goal = (
            "Say honestly that no community near them has anyone in it yet — theirs "
            "would be the first — then offer the concrete move: name a spot they go to "
            "and you'll set it up. Never blame them, never call the area dead."
        )
        fallback = (
            "No communities near you have anyone in them yet — yours would be the "
            "first. Tell me a spot you go to and I'll set it up."
        )

    from app.reply_compose import compose_reply

    return compose_reply(
        goal=goal,
        facts=facts,
        fallback=fallback,
        session_ctx=session_ctx,
        user_message=message,
        # Two, not three: the cards are the list, so the text is a summary + an offer.
        max_sentences=2,
    )


_JOIN_VERB_RE = re.compile(
    r"\b(join|add me|sign me up|put me in|i go there|count me in|i'?m in)\b", re.IGNORECASE
)
# A tap posts "Join <place>"; typed replies are short too. Anything longer is a new ask.
_JOIN_REPLY_MAX_LEN = 120


def read_join_reply(user_id: str, message: str, session_ctx: dict[str, Any]) -> dict[str, Any] | None:
    """Read a reply to the "want to join one of these?" offer.

    Returns the join result plus the matched place, or None to fall through to normal
    routing (the offer is consumed either way — an unrelated next message must not stay
    armed, per [[ctx-pop-resurrection]] this clears with None, never pop).

    Matching is deterministic NAME matching, not intent classification: the chips post
    "Join <place>" verbatim, and a typed "join lp fit" names the place too. A bare "yes"
    only joins when exactly ONE community was offered — with three on screen, "yes"
    doesn't say which, and guessing would write the wrong membership.
    """
    pending = session_ctx.get("community_join_pending")
    if not isinstance(pending, dict):
        return None
    session_ctx["community_join_pending"] = None
    text = str(message or "").strip()
    if not text or len(text) > _JOIN_REPLY_MAX_LEN:
        return None
    places = [p for p in (pending.get("places") or []) if isinstance(p, dict)]
    if not places:
        return None

    named = _match_offered_place(text, places)
    wants_join = bool(_JOIN_VERB_RE.search(text)) or _is_bare_yes(text)
    if named is None:
        # "yes" / "join" with nothing named: only unambiguous with a single offer.
        if wants_join and len(places) == 1:
            named = places[0]
        else:
            return None
    if not wants_join and not _looks_like_only_a_name(text, named):
        return None

    try:
        result = join_community(user_id, str(named.get("place_id") or ""))
    except ValueError as exc:
        logger.warning("community_join_reply_failed place=%s err=%s", named.get("place_id"), exc)
        return None
    result["place_name"] = result.get("place_name") or named.get("place_name")
    return result


_BARE_YES_RE = re.compile(r"^(yes|yeah|yep|yup|sure|ok(?:ay)?|please|do it|go ahead)\b[\s!.]*$",
                          re.IGNORECASE)


def _is_bare_yes(text: str) -> bool:
    return bool(_BARE_YES_RE.match(text.strip()))


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def _match_offered_place(text: str, places: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The offered place whose name the message actually contains. Longest name first,
    so "Heroes Community Park" wins over a shorter name nested inside it."""
    hay = _norm(text)
    if not hay:
        return None
    for place in sorted(places, key=lambda p: -len(str(p.get("place_name") or ""))):
        name = _norm(place.get("place_name"))
        if name and name in hay:
            return place
    return None


def _looks_like_only_a_name(text: str, place: dict[str, Any]) -> bool:
    """The user typed just the place name with no verb ("Lp Fit") — still an answer to
    "which one?". Guarded so a sentence ABOUT the place isn't read as a join."""
    return _norm(text) == _norm(place.get("place_name"))


def join_confirm_reply(
    result: dict[str, Any],
    *,
    session_ctx: dict[str, Any],
    message: str,
    member_count: int | None = None,
) -> str:
    """Confirm a join in one warm line, from facts only. Says whether they were already
    in, and offers the ONE next step that is real: seeing who else is there."""
    name = str(result.get("place_name") or "the spot")
    if result.get("already_member"):
        facts = [f"They are already in {name} — nothing changed"]
        goal = (
            "Tell them they're already in this one (no double-join), and offer the real "
            "next step: seeing who else is there."
        )
        fallback = f"You're already in {name}. Want to see who else is there?"
    else:
        facts = [f"They just joined {name}"]
        if member_count and member_count > 1:
            facts.append(f"{member_count} people are in it now, including them")
        if result.get("promoted_from_candidate"):
            facts.append(
                "This is the same place they had mentioned to you before — now it's "
                "confirmed. Do not treat it as new information about them."
            )
        goal = (
            "Confirm the join warmly in one line using the real count if given, then "
            "offer the one real next step: seeing who else is there. Never promise "
            "anyone is waiting for them."
        )
        fallback = f"Done — you're in {name}. Want to see who else is there?"

    from app.reply_compose import compose_reply

    return compose_reply(
        goal=goal,
        facts=facts,
        fallback=fallback,
        session_ctx=session_ctx,
        user_message=message,
    )


def _my_communities(user_id: str) -> list[dict[str, Any]]:
    from app.circles_flow import list_my_circles

    try:
        rows = list_my_circles(user_id)
    except Exception:
        logger.exception("communities_chat_mine_failed user=%s", user_id)
        return []
    return [r for r in rows if r.get("place_name")]


def _members_phrase(community: dict[str, Any]) -> str:
    n = int(community.get("member_count") or 0)
    if n <= 1:
        return "just them so far"
    return f"{n} people"


def _nearby_fallback(mine: list[dict[str, Any]], nearby: list[dict[str, Any]]) -> str:
    """Counts and one anchor name — the cards below carry the full list."""
    n = len(nearby)
    more = f" and {n - 1} more" if n > 1 else ""
    head = (
        f"You're in {len(mine)} already, and there {'is' if n == 1 else 'are'} {n} more "
        f"near you — {nearby[0]['place_name']}{more}."
        if mine
        else (
            f"{n} near you — {nearby[0]['place_name']} ({nearby[0]['status_line']})"
            f"{more}."
        )
    )
    return f"{head} Want me to add you to any of them?"


def _confirmed_member_count(place_id: str) -> int:
    """How many people are confirmed members here — the one fact that makes a "somebody
    joined" mail feel like a community growing. 0 on any failure, and the row is dropped
    rather than showing a wrong number."""
    try:
        res = (
            service_client()
            .table("circle_affiliations")
            .select("id", count="exact")
            .eq("place_ref", place_id)
            .eq("status", "confirmed")
            .is_("dismissed_at", "null")
            .limit(1)
            .execute()
        )
        return int(res.count or 0)
    except Exception:  # noqa: BLE001
        return 0


def mail_join_to_members(place_id: str, place_name: str, joiner_id: str) -> int:
    """Email the community's existing confirmed members that somebody new joined.
    Same roster the community's meets mail. Returns how many were mailed."""
    from app.i18n import t
    from app.notifications import _user_contact, email_html, mail_community_members

    _, nickname = _user_contact(joiner_id)
    members = _confirmed_member_count(place_id)

    def render(lang: str | None) -> tuple[str, str]:
        name = nickname or t("notify.community_join.somebody", lang)
        return (
            t("notify.community_join.subject", lang, name=name, place=place_name),
            email_html(
                t("notify.community_join.title", lang, name=name, place=place_name),
                t("notify.community_join.body", lang, place=place_name),
                t("notify.community_join.cta", lang),
                "/",
                preheader=t("notify.community_join.preheader", lang, name=name),
                badge="👋",
                kicker=t("notify.community_note", lang, name=place_name),
                facts=[
                    (t("notify.facts.community", lang), place_name),
                    (
                        t("notify.facts.members", lang),
                        t("notify.facts.member_count", lang, n=members) if members else "",
                    ),
                ],
            ),
        )

    return mail_community_members(place_id, exclude_user_id=joiner_id, render=render)


def notify_members_of_join(place_id: str, place_name: str, joiner_id: str) -> None:
    """Fire-and-forget wrapper — the join tap never waits on a mail-out, and never
    fails because of one. An unnamed community has nothing to say, so it stays quiet."""
    if not place_id or not place_name:
        return

    def _run() -> None:
        try:
            mail_join_to_members(place_id, place_name, joiner_id)
        except Exception:
            logger.exception("community_join_mail_failed place=%s", place_id)

    threading.Thread(
        target=_run, daemon=True, name=f"circle-join-{str(place_id)[:8]}"
    ).start()


def _after_join(
    user_id: str,
    affiliation_id: str,
    candidate: dict[str, Any] | None,
    place_id: str,
    place_name: str,
) -> None:
    """Best-effort: none of this may cost the user the join they just made."""
    from app.circles_flow import _close_grounding_gap, _flush_parked_features

    if candidate:
        try:
            _flush_parked_features(user_id, candidate, place_id)
        except Exception:
            logger.exception("community_join_feature_flush_failed aff=%s", affiliation_id)
        try:
            _close_grounding_gap(affiliation_id)
        except Exception:
            logger.exception("community_join_gap_close_failed aff=%s", affiliation_id)
    if not place_name:
        return
    try:
        from app.circles_flow import _place_affinity_question
        from app.rapport_gaps import open_semantic_gap

        question, teaser, chips = _place_affinity_question(place_name)
        open_semantic_gap(
            user_id,
            None,
            question,
            label=place_name,
            bucket="interest",
            teaser=teaser,
            place_ref=place_id,
            answer_options=chips,
        )
    except Exception:
        logger.exception("community_join_enrichment_failed place=%s", place_id)
