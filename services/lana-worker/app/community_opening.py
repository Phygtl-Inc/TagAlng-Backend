"""The first line Lana says when a chat opens inside a community.

Someone taps a creator's link, joins, and lands in /chat with the community already
selected (app/community_scope.py). The opening used to be composed BEFORE that selection
was applied, so it could not know the community existed: the follower of Etiqueta do
Reino was greeted like any neighbour and asked about their area. Nobody who arrived
through a creator link ever stayed.

This composes the replacement from the community's own words — its description (`blurb`)
and the operator's stated purpose (`first_action`) — and asks one easy question about
the community's SUBJECT. Not about the person: a visitor who just tapped a link from a
bio has told us nothing yet and should not have to disclose anything to answer.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from app.reply_compose import compose_reply

logger = logging.getLogger(__name__)

# An affiliation this recent means they joined on the way in (the creator page's Join),
# so the opening welcomes them rather than greeting a regular.
JUST_JOINED_WINDOW = timedelta(minutes=30)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _community_row(place_id: str) -> dict[str, Any]:
    from app.auth import service_client

    for fields in ("name, place_type, blurb, first_action", "name, place_type"):
        try:
            res = (
                service_client()
                .table("places")
                .select(fields)
                .eq("id", place_id)
                .limit(1)
                .execute()
            )
        except Exception:  # noqa: BLE001 — step down to the narrower read
            continue
        rows = res.data if isinstance(res.data, list) else []
        return rows[0] if rows else {}
    return {}


def _creator_answers(place_id: str) -> dict[str, str]:
    """What the creator wrote on lana.help's "Community fit" step.

    "What connects your followers?" and "What can they help each other with?" are stored
    as place_features (shared_context / member_value), never as places.blurb — so a new
    creator community has a creator-written description that nothing here read, and
    Lana said "its creator hasn't described it yet" over the creator's own words.
    """
    from app.auth import service_client

    try:
        res = (
            service_client()
            .table("place_features")
            .select("key, value, confidence, created_at")
            .eq("place_id", place_id)
            .in_("key", ["shared_context", "member_value"])
            .order("confidence", desc=True)
            .order("created_at", desc=True)
            .limit(10)
            .execute()
        )
    except Exception:  # noqa: BLE001 — missing answers only cost facts
        return {}
    out: dict[str, str] = {}
    for r in res.data if isinstance(res.data, list) else []:
        key, value = str(r.get("key") or ""), _clean(r.get("value"))
        if value and key not in out:
            out[key] = value
    return out


def _creator_name(place_id: str) -> str:
    """Same source as place_claim_card: what the creator typed to be found."""
    from app.auth import service_client

    try:
        res = (
            service_client()
            .table("place_features")
            .select("value")
            .eq("place_id", place_id)
            .eq("key", "creator_name")
            .order("confidence", desc=True)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
    except Exception:  # noqa: BLE001 — a missing name only costs one fact
        return ""
    rows = res.data if isinstance(res.data, list) else []
    named = _clean(rows[0].get("value")) if rows else ""
    return named or _operator_name(place_id)


def _operator_name(place_id: str) -> str:
    """The person who runs it, when the claim form never stored a creator_name.

    "who created this?" was answered "I don't have info on who created Tommaso" inside
    Tommaso's own community (2026-10-01): the feature row was missing, the operator was
    not. Same fallback resolve_place_handle v2 uses."""
    from app.auth import service_client

    try:
        res = (
            service_client()
            .table("place_managers")
            .select("user_id, created_at, users(nickname)")
            .eq("place_id", place_id)
            .eq("role", "operator")
            .is_("removed_at", "null")
            .order("created_at")
            .limit(1)
            .execute()
        )
    except Exception:  # noqa: BLE001 — pre-place_managers environments simply have none
        return ""
    rows = res.data if isinstance(res.data, list) else []
    user = (rows[0].get("users") if rows else None) or {}
    return _clean(user.get("nickname") if isinstance(user, dict) else "")


def _just_joined(user_id: str, place_id: str) -> bool:
    from app.community_surface import caller_affiliation_at

    row = caller_affiliation_at(user_id, place_id, statuses=("confirmed", "curious"))
    raw = _clean((row or {}).get("created_at"))
    if not raw:
        return False
    try:
        joined = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    if joined.tzinfo is None:
        joined = joined.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - joined <= JUST_JOINED_WINDOW


_FACTS_KEY = "_active_community_facts"
_FACTS_TTL = timedelta(minutes=10)


def active_community_facts(session_ctx: dict[str, Any] | None) -> dict[str, Any] | None:
    """What the chat's community IS, for every model that reads this turn.

    Selecting a community only scoped the SEARCHES. Neither the router nor the policy was
    told the conversation was inside one, so "what is this community?" or "what do people
    do here?" meant nothing to them — the router filed it as "show me communities near me"
    and the guest got the verify-your-email gate three times running (2026-09-30).

    Cached on the session per place for a few minutes: this is read on every turn, and the
    description changes only when the operator edits it.
    """
    from app.community_scope import active_community

    comm = active_community(session_ctx)
    if not comm or session_ctx is None:
        return None
    place_id = _clean(comm.get("place_id"))
    cached = session_ctx.get(_FACTS_KEY)
    if isinstance(cached, dict) and cached.get("place_id") == place_id:
        try:
            at = datetime.fromisoformat(str(cached.get("at")))
            if datetime.now(timezone.utc) - at <= _FACTS_TTL:
                return cached
        except ValueError:
            pass
    try:
        row = _community_row(place_id)
    except Exception:  # noqa: BLE001 — a failed read leaves the turn unscoped, never broken
        logger.exception("active_community_facts_failed place=%s", place_id)
        row = {}
    is_creator = row.get("place_type") == "creator"
    answers = _creator_answers(place_id) if is_creator else {}
    facts = {
        "place_id": place_id,
        "name": _clean(row.get("name")) or _clean(comm.get("name")),
        "kind": "creator community" if is_creator else _clean(row.get("place_type")) or "community",
        # The creator's own "what connects your followers?" outranks a stored blurb: the
        # blurb on a creator row may be a generated line from before 2026-10-01.
        "about": answers.get("shared_context") or _clean(row.get("blurb")) or None,
        "members_help": answers.get("member_value") or None,
        # lana.help stores "What might someone ask first?" here.
        "creator_wants": _clean(row.get("first_action")) or None,
        "creator": (_creator_name(place_id) or None) if is_creator else None,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    if not facts["name"]:
        return None
    session_ctx[_FACTS_KEY] = facts
    return facts


def active_community_prompt_line(session_ctx: dict[str, Any] | None) -> str:
    """One line for a model's context block. 'none' when the chat is not in a community."""
    facts = active_community_facts(session_ctx)
    if not facts:
        return "none"
    parts = [f'"{facts["name"]}" ({facts["kind"]})']
    if facts.get("creator"):
        parts.append(f'run by {facts["creator"]}')
    if facts.get("about"):
        parts.append(f'about: {facts["about"][:200]}')
    if facts.get("members_help"):
        parts.append(f'members help each other with: {facts["members_help"][:160]}')
    if facts.get("creator_wants"):
        parts.append(f'a first question its creator expects: {facts["creator_wants"][:160]}')
    return " — ".join(parts)


def community_opening(
    community: dict[str, Any] | None,
    *,
    user_id: str | None,
) -> str | None:
    """The opening line for a chat that starts inside `community`, or None to keep the
    generic one. Never raises: a failed read falls back to the generic opening, never to
    a broken session."""
    place_id = _clean((community or {}).get("place_id"))
    if not place_id:
        return None
    try:
        row = _community_row(place_id)
        name = _clean(row.get("name")) or _clean((community or {}).get("name"))
        if not name:
            return None
        answers = _creator_answers(place_id) if row.get("place_type") == "creator" else {}
        blurb = answers.get("shared_context") or _clean(row.get("blurb"))
        helps = answers.get("member_value", "")
        purpose = _clean(row.get("first_action"))
        creator = _creator_name(place_id) if row.get("place_type") == "creator" else ""
        joined_now = bool(user_id) and _just_joined(str(user_id), place_id)

        facts = [f"Community name: {name}"]
        if blurb:
            facts.append(f"What the community is about: {blurb}")
        elif not purpose:
            facts.append(
                "It has no description yet — never guess what it is about from its name"
            )
        if helps:
            facts.append(f"What members help each other with: {helps}")
        if purpose:
            facts.append(f"A first question its creator expects people to ask: {purpose}")
        if creator:
            facts.append(f"Run by: {creator} (refer to them by name — never he/she)")
        if row.get("place_type") == "creator":
            facts.append(
                "It is a group run by one person around a topic — never call it local, "
                "nearby, a spot, a place or a venue, and call its people members, never "
                "neighbors"
            )
        facts.append(
            "They just joined this community from its link."
            if joined_now
            else "They are already a member and are opening a new chat inside it."
        )

        opening = compose_reply(
            goal=(
                "Open the chat inside this community. "
                + (
                    "Welcome them to it warmly in a few words. "
                    if joined_now
                    else "Greet them as a returning member in a few words — never "
                    "'welcome to', they already belong here. "
                )
                + "Then ask ONE easy question about the community's SUBJECT, drawn from "
                "what it is about or what its creator wants people to do — something a "
                "stranger could answer on the spot without sharing anything personal. "
                "Do not ask for their ZIP, neighborhood or location. Do not ask them to "
                "tell you about themselves. Do not list what you can do."
            ),
            facts=facts,
            fallback=(
                f"Welcome to **{name}**! Ask me anything about it, or tell me what "
                "you're looking for."
                if joined_now
                else f"You're in **{name}**. What are you looking for today?"
            ),
            max_sentences=2,
        )
        logger.info(
            "community_opening place=%s just_joined=%s grounded=%s",
            place_id,
            joined_now,
            bool(blurb or purpose),
        )
        return opening or None
    except Exception:  # noqa: BLE001 — the generic opening is always a safe floor
        logger.exception("community_opening_failed place=%s", place_id)
        return None
