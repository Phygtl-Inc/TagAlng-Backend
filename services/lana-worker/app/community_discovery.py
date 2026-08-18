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
    if candidate:
        affiliation_id = str(candidate["id"])
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


def communities_chat_turn(
    user_id: str,
    *,
    message: str,
    session_ctx: dict[str, Any],
    locale: str = "en",
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
