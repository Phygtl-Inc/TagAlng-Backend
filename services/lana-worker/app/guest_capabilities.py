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
        return [r for r in raw if isinstance(r, dict)]
    return []


def format_peer_matches(peers: list[dict[str, Any]]) -> str:
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
    return compose_reply(
        goal=(
            "You just found nearby neighbors similar to the user; cards below "
            "the message show their names and shared traits. Give the real "
            "count, point to what they have in common below, and offer to "
            "introduce them to any of these neighbors. Do not invent names."
        ),
        facts=[f"Neighbors found nearby (cards shown below the message): {n}"],
        fallback=(
            f"I found {n} neighbor{'s' if n != 1 else ''} nearby — "
            "here's what you have in common. Want me to introduce you to any of them?"
        ),
    )


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
