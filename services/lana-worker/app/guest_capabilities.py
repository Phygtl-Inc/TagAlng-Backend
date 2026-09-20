"""Guest capability gates: peer find + host activity require verify (+ block)."""

from __future__ import annotations

import re
from typing import Any

from app.reply_compose import compose_reply
from app.supabase_rpc import call_rpc

_HOST_RE = re.compile(
    r"\b(host|plan|create|start|organize|set up)\b.*\b(activity|event|meetup|gathering|"
    r"playdate|brunch|coffee)\b"
    r"|\b(host an activity|plan something|host something)\b",
    re.I,
)
_PEER_FIND_RE = re.compile(
    r"\b(find|show|match|meet|connect with|introduce me to|people|neighbors?|neighbours?|fellows|"
    r"moms?|dads?|parents?|users)\b.*\b(like me|similar|same|my vibe|on the block|nearby|near me|in my block)\b"
    r"|\b(find people|find neighbors|find neighbours|who else|others like|similar people)\b"
    r"|\b(want|wanna|looking|trying)\b.*\b(similar|like-minded|new users|new people|neighbors|neighbours|people|peers)\b"
    r"|\b(find|meet|connect with?|show)\b.*\b(new users|new people|neighbors|neighbours|people|users)\b"
    r"|\bnew here\b.*\b(find|meet|people|neighbors|neighbours|users)\b"
    r"|\b(find|show)\b.*\b(me )?(people|users|neighbors|neighbours)\b"
    r"|\b(i wanna|i want to)\b.*\bmeet\b.*\b(neighbors|neighbours|people)\b",
    re.I,
)


def wants_peer_find(text: str) -> bool:
    return bool(_PEER_FIND_RE.search(str(text or "").strip()))


def wants_host_activity(text: str) -> bool:
    return bool(_HOST_RE.search(str(text or "").strip()))


def fetch_peer_matches(user_jwt: str, *, limit: int = 5) -> list[dict[str, Any]]:
    raw = call_rpc(
        user_jwt,
        "match_peers_by_claim_vectors",
        {"p_limit": limit, "p_min_similarity": 0.70},
    )
    if isinstance(raw, list):
        from app.auth import jwt_user_id
        from app.peer_discovery_surface import drop_connected_peers

        return drop_connected_peers(
            [r for r in raw if isinstance(r, dict)], user_id=jwt_user_id(user_jwt)
        )
    return []


def format_peer_matches(
    peers: list[dict[str, Any]], session_ctx: dict[str, Any] | None = None
) -> str:
    """The reply over a neighbours list.

    `session_ctx` only carries the community filter's state: which community the
    list is scoped to, or — when that community had nobody — which one came up
    empty before this wider list was fetched. Both are FACTS for the composer, so
    a scoped search never reads as if it swept the whole neighbourhood.
    """
    scoped = str((peers[0] if peers else {}).get("community_name") or "").strip()
    widened = str((session_ctx or {}).get("community_widened_from") or "").strip()
    if session_ctx is not None and widened:
        session_ctx["community_widened_from"] = None  # one turn only
    if not peers:
        return compose_reply(
            goal=(
                "The user asked to find neighbors like them but no strong fits "
                "were found near them yet. Say so honestly, and tell them to tap "
                "the Complete button (exact button name) to save their profile so "
                "you can find neighbors like them."
            ),
            fallback=(
                "I don't see strong fits near you yet. "
                "Tap Complete to save your profile — that helps me find neighbors like you."
            ),
            cache=True,
        )
    # The match cards below the message carry names + shared traits — don't
    # narrate the same list twice; keep the text to the count and the next step.
    n = len(peers)
    facts = [f"Neighbors found nearby (cards shown below the message): {n}"]
    if scoped:
        facts.append(f"Every one of them is at {scoped}, the community they are filtered to")
    if widened:
        facts.append(
            f"Nobody at {widened} — the community they are filtered to — matched yet, "
            "so this list is the wider area instead. Say that before the count."
        )
    # WHAT THEY ACTUALLY SHARE. Without these the composer was told to "point to what
    # they have in common" and handed nothing but a count, so it invented the overlap:
    # "5 neighbors nearby who share some interests with you, like being parents and
    # enjoying local parks" over cards reading "your gym" and "your sushi spot", for a
    # user whose claims are English/Urdu, gym, grandparent, gaming zone (prod
    # 2026-09-18). A composer can only be truthful about what it is given.
    shared = _shared_labels_across(peers)
    if shared:
        # WITH COUNTS, because the union alone reads as if everyone holds everything:
        # one neighbour shares the gym and Italian food, four share the sushi spot, and
        # "5 neighbors who share your gym, love Italian food and frequent your sushi
        # spot" is four small untruths dressed as one summary.
        facts.append(
            "What they actually share with the user, and how many of the "
            f"{n} hold each: " + " · ".join(f"{label} ({count})" for label, count in shared)
        )
        if any(count < n for _label, count in shared):
            facts.append(
                "No trait is held by all of them — never write the list as if it were. "
                "Name the one or two most common, or say they share different things."
            )
    else:
        facts.append(
            "No shared trait was proven for these rows — they are neighbors near the "
            "user, nothing more. Do not claim shared interests."
        )
    # Unscored rows carry no similarity: "on your block", never "a match" (truthful
    # peer-match model, 20260820120000).
    unscored = sum(1 for p in peers if isinstance(p, dict) and p.get("similarity_score") is None)
    if unscored == n:
        facts.append("None of them were scored for similarity — call them neighbors near the user, not matches")

    # WHO CAN STILL BE INTRODUCED. Every row on this list can already be "✓ Sent" — an
    # intro is out and the card has no Nudge button — and the old goal offered an intro
    # anyway, for all five (prod 2026-09-18). The single-featured-peer path owns up to
    # this state (format_intro_sent_offer_turn); this one, the list reply, never knew
    # about it. An offer the cards contradict is worse than no offer.
    open_names = [
        str(p.get("nickname") or "").strip()
        for p in peers
        if isinstance(p, dict) and not str(p.get("connection") or "").strip()
        and p.get("can_nudge") is not False
    ]
    open_names = [nm for nm in open_names if nm]
    if not open_names:
        facts.append(
            "You have ALREADY introduced them to every neighbor on this list — each card "
            "says Sent and has no button. There is no intro left to offer here."
        )
        offer_line = (
            "NEVER offer an intro or a nudge: it is the one thing already done for all of "
            "them, and the cards say so. End by offering the move that is left — looking "
            "further out for someone new."
        )
        fallback_tail = (
            "Your intros to all of them are already out and waiting on them. "
            "Want me to look further out for someone new?"
        )
    elif len(open_names) < n:
        facts.append(
            "Intros are already out to the others (their cards say Sent); only these can "
            "still be introduced: " + ", ".join(open_names)
        )
        offer_line = (
            "Offer an intro ONLY to the ones named as still introducible — never to the "
            "ones whose intro is already out."
        )
        fallback_tail = f"Want me to introduce you to {open_names[0]}?"
    else:
        offer_line = "Offer to introduce them to any of these neighbors."
        fallback_tail = "Want me to introduce you to any of them?"

    return compose_reply(
        goal=(
            "You just found nearby neighbors for the user; cards below the message show "
            "their names and shared traits. Give the real count. Name what they have in "
            "common ONLY from the facts — if a shared trait is not listed there, it does "
            "not exist, so say nothing about interests rather than guessing one. Do not "
            "invent names. " + offer_line
        ),
        facts=facts,
        fallback=(
            f"I found {n} neighbor{'s' if n != 1 else ''} nearby. {fallback_tail}"
        ),
    )


def _shared_labels_across(peers: list[dict[str, Any]]) -> list[tuple[str, int]]:
    """Every proven shared label on the rows with how many rows hold it, commonest first.

    `shared_labels` is what the matcher proved BOTH sides hold; matching_peer_label is
    the fuzzy-pair fallback and is one-sided, so it is deliberately not read here — a
    one-sided trait stated as shared is the bug this function exists to prevent.
    """
    counts: dict[str, int] = {}
    for p in peers:
        if not isinstance(p, dict):
            continue
        for label in dict.fromkeys(str(x or "").strip() for x in (p.get("shared_labels") or [])):
            if label:
                counts[label] = counts.get(label, 0) + 1
    return sorted(counts.items(), key=lambda kv: -kv[1])[:6]


def handle_guest_capability(
    user_message: str,
    *,
    phone_verified: bool,
    home_block_id: str | None,
    user_jwt: str,
    guest_step: str,
) -> tuple[str, dict[str, Any]] | None:
    """Return scripted reply when user asks for peers or hosting; None if not matched."""
    msg = str(user_message or "").strip()
    if not msg:
        return None

    if not phone_verified:
        return None

    peer = wants_peer_find(msg)
    host = wants_host_activity(msg)
    if not peer and not host:
        return None

    if not home_block_id:
        return (
            compose_reply(
                goal=(
                    "The user is verified but you don't know their home area yet, "
                    "so you can't show neighbors or set up hosting. Congratulate "
                    "them on verifying, and ask them to share their location when "
                    "prompted or finish onboarding."
                ),
                fallback=(
                    "You're verified! I still need your home area before I can show neighbors "
                    "or host nearby — share your location when prompted, or finish onboarding."
                ),
                cache=True,
            ),
            {"requires_phone_verification": False, "guest_step": guest_step},
        )

    if host:
        return (
            compose_reply(
                goal=(
                    "The user wants to host something for neighbors and is all set "
                    "to do it. Ask what they're planning — brunch, playdate, walk, "
                    "anything — and offer to help draft it, including one concrete "
                    "example ask like \"Sunday coffee for new parents at 10am\"."
                ),
                fallback=(
                    "You're set to host for neighbors nearby. Tell me what you're planning — "
                    "brunch, playdate, walk, anything — and I'll help you draft it. "
                    "(Say something like \"Sunday coffee for new parents at 10am\".)"
                ),
                cache=True,
            ),
            {"requires_phone_verification": False, "guest_step": guest_step, "intent": "host_activity"},
        )

    try:
        peers = fetch_peer_matches(user_jwt)
    except Exception:
        peers = []

    extra: dict[str, Any] = {
        "requires_phone_verification": False,
        "guest_step": guest_step,
        "intent": "peer_find",
        "peer_matches": peers[:5],
    }
    return format_peer_matches(peers), extra
