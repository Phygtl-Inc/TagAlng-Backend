"""Guest capability gates: peer find + host activity require verify (+ block)."""

from __future__ import annotations

import re
from typing import Any

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
        {"p_limit": limit, "p_min_similarity": 0.55},
    )
    if isinstance(raw, list):
        return [r for r in raw if isinstance(r, dict)]
    return []


def format_peer_matches(peers: list[dict[str, Any]]) -> str:
    if not peers:
        return (
            "I don't see strong matches on your block yet. "
            "Tap Complete to save your profile — that helps me find neighbors like you."
        )
    lines = [f"I found {len(peers)} neighbor{'s' if len(peers) != 1 else ''} on your block:"]
    for p in peers[:5]:
        nick = str(p.get("nickname") or "A neighbor")
        label = str(p.get("matching_peer_label") or "shared interests")
        score = p.get("similarity_score")
        pct = f" ({int(float(score) * 100)}%)" if score is not None else ""
        lines.append(f"• {nick} — {label}{pct}")
    lines.append("Want me to introduce you to any of them?")
    return "\n".join(lines)


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
            "You're verified! I still need your home block before I can show neighbors or host "
            "on the block — share your location when prompted, or finish onboarding.",
            {"requires_phone_verification": False, "guest_step": guest_step},
        )

    if host:
        return (
            "You're set to host on your block. Tell me what you're planning — brunch, playdate, "
            "walk, anything — and I'll help you draft it. (Say something like "
            "\"Sunday coffee for new moms at 10am\".)",
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
