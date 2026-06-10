"""Discovery routing: find peers with ZIP → identity → preview → verify gate → full."""

from __future__ import annotations

import re
from typing import Any

from app.auth import service_client
from app.guest_capabilities import (
    fetch_peer_matches,
    format_peer_matches,
    wants_host_activity,
    wants_peer_find,
)
from app.guest_login import extract_otp_code, extract_phone_e164, handle_guest_login
from app.guest_login import wants_login as wants_login_intent
from app.supabase_rpc import call_rpc

PHASE_NEED_ZIP = "need_zip"
PHASE_NEED_IDENTITY = "need_identity"
PHASE_PREVIEW = "preview"
PHASE_GATE_VERIFY = "gate_verify"
PHASE_AWAIT_SIGNUP_PHONE = "await_signup_phone"
PHASE_AWAIT_SIGNUP_OTP = "await_signup_otp"

INTENT_FIND_PEERS = "discovery.find_peers"

_MORE_DETAIL_RE = re.compile(
    r"\b(more|names?|introduce|connect|who are they|show me|full|details?|"
    r"see them|meet them|talk to)\b",
    re.I,
)
_ZIP_RE = re.compile(r"\b(\d{5})\b")
_IDENTITY_RE = re.compile(
    r"\b(mom|mother|dad|father|parent|latino|latina|hispanic|brazil|mexican|"
    r"kids?|children|new here|new to|heritage|from\s+\w+)\b",
    re.I,
)


def wants_more_peer_detail(text: str) -> bool:
    return bool(_MORE_DETAIL_RE.search(str(text or "").strip()))


def extract_zip(text: str) -> str | None:
    m = _ZIP_RE.search(str(text or ""))
    return m.group(1) if m else None


def extract_identity_snippet(text: str) -> str | None:
    s = str(text or "").strip()
    if not s or len(s) < 8:
        return None
    if wants_peer_find(s) or wants_login_intent(s) or extract_zip(s):
        return None
    if _IDENTITY_RE.search(s) or len(s.split()) >= 4:
        return s[:400]
    return None


def _routing_ctx(
    session_ctx: dict[str, Any],
    *,
    phase: str,
    active_intent: str | None = INTENT_FIND_PEERS,
    **extra: Any,
) -> dict[str, Any]:
    out = {
        **session_ctx,
        "unified_mode": True,
        "active_intent": active_intent,
        "routing_phase": phase,
    }
    out.update(extra)
    return out


def _auth_action(**fields: Any) -> dict[str, Any]:
    return {k: v for k, v in fields.items() if v is not None}


def resolve_block_id(
    session_ctx: dict[str, Any],
    home_block_id: str | None,
) -> str | None:
    if home_block_id:
        return home_block_id
    bid = session_ctx.get("preview_block_id")
    return str(bid) if bid else None


def fetch_blocks_for_zip(user_jwt: str, zip5: str) -> list[dict[str, Any]]:
    raw = call_rpc(
        user_jwt,
        "get_blocks_near_zip",
        {"p_zip": zip5, "p_cluster_id": "lake-nona", "p_limit": 5},
    )
    if isinstance(raw, list):
        return [r for r in raw if isinstance(r, dict)]
    return []


def fetch_preview_peers_on_block(block_id: str, *, limit: int = 3) -> list[dict[str, Any]]:
    """Anonymous-safe preview: labels only, no peer_user_id or nickname."""
    try:
        sb = service_client()
        users = (
            sb.table("users")
            .select("id")
            .eq("home_block_id", block_id)
            .limit(15)
            .execute()
        )
        rows = users.data or []
        out: list[dict[str, Any]] = []
        for u in rows[:limit]:
            uid = u.get("id")
            if not uid:
                continue
            claims = (
                sb.table("user_identity_claims")
                .select("label, bucket")
                .eq("user_id", uid)
                .eq("disclosure", "public")
                .is_("dismissed_at", "null")
                .order("confidence", desc=True)
                .limit(1)
                .execute()
            )
            label = "shared interests on your block"
            if claims.data:
                label = str(claims.data[0].get("label") or label)
            out.append(
                {
                    "peer_user_id": None,
                    "nickname": None,
                    "avatar_url": None,
                    "similarity_score": None,
                    "matching_peer_label": label,
                    "matching_peer_concept": None,
                    "has_exact_concept_match": False,
                    "preview": True,
                }
            )
            if len(out) >= limit:
                break
        return out
    except Exception:
        return []


def redact_peers_for_preview(peers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in peers[:5]:
        out.append(
            {
                **p,
                "peer_user_id": None,
                "nickname": None,
                "avatar_url": None,
                "preview": True,
            }
        )
    return out


def format_preview_message(peers: list[dict[str, Any]], block_label: str | None) -> str:
    where = block_label or "your block"
    if not peers:
        return (
            f"I looked around {where} — no strong matches yet. "
            "Tell me a bit more about yourself, or try a nearby ZIP."
        )
    lines = [f"I found {len(peers)} neighbor{'s' if len(peers) != 1 else ''} near {where}:"]
    for i, p in enumerate(peers[:3], 1):
        label = str(p.get("matching_peer_label") or "shared interests")
        lines.append(f"• Neighbor {i} — {label}")
    lines.append(
        "Verify your phone to see names and connect — or tell me more about you for sharper matches."
    )
    return "\n".join(lines)


def _discovery_routing_stub(phase: str, tool: str | None = None) -> dict[str, Any]:
    return {
        "outcome": "T" if tool else "A",
        "intent_class": "discovery",
        "confidence": 1.0,
        "tool_to_call": tool,
        "capture_fired": False,
        "routing_phase": phase,
    }


def handle_discovery_turn(
    user_message: str,
    *,
    session_ctx: dict[str, Any],
    user_jwt: str,
    phone_verified: bool,
    home_block_id: str | None,
    is_anonymous: bool,
) -> tuple[str, dict[str, Any], dict[str, Any], list[dict[str, Any]]] | None:
    """
    Returns (reply, ctx, routing, peer_matches) or None if not handling this turn.
    Also sets auth_action on ctx when present.
    """
    msg = str(user_message or "").strip()
    phase = str(session_ctx.get("routing_phase") or "")
    active = session_ctx.get("active_intent")

    # Continue signup verify sub-flow
    if phase == PHASE_AWAIT_SIGNUP_PHONE:
        phone = extract_phone_e164(msg)
        if not phone:
            return (
                "What's your phone number? I'll text you a code to verify.",
                _routing_ctx(session_ctx, phase=PHASE_AWAIT_SIGNUP_PHONE),
                _discovery_routing_stub(PHASE_AWAIT_SIGNUP_PHONE),
                [],
            )
        ctx = _routing_ctx(
            session_ctx,
            phase=PHASE_AWAIT_SIGNUP_OTP,
            signup_phone=phone,
        )
        ctx["auth_action"] = _auth_action(
            type="link_phone_signup",
            phone=phone,
            verify_type="phone_change",
        )
        return (
            f"Got it — I sent a 6-digit code to {phone}. Enter it here when it arrives.",
            ctx,
            _discovery_routing_stub(PHASE_AWAIT_SIGNUP_OTP),
            [],
        )

    if phase == PHASE_AWAIT_SIGNUP_OTP:
        otp = extract_otp_code(msg)
        phone = str(session_ctx.get("signup_phone") or "")
        if not otp:
            return (
                f"Enter the 6-digit code we sent to {phone or 'your phone'}.",
                _routing_ctx(session_ctx, phase=PHASE_AWAIT_SIGNUP_OTP, signup_phone=phone or None),
                _discovery_routing_stub(PHASE_AWAIT_SIGNUP_OTP),
                [],
            )
        ctx = _routing_ctx(session_ctx, phase=PHASE_PREVIEW, signup_phone=phone)
        ctx["auth_action"] = _auth_action(
            type="verify_signup_otp",
            phone=phone,
            token=otp,
            verify_type="phone_change",
        )
        return (
            "Perfect — verifying you now. Send me another message once you're verified and I'll show full matches.",
            ctx,
            _discovery_routing_stub(PHASE_PREVIEW, "verify_signup_otp"),
            [],
        )

    # Login delegated to guest_login (maps guest_step from routing or early)
    login_step = session_ctx.get("guest_step") or (
        "await_login_phone"
        if phase == "await_login_phone"
        else "await_login_otp"
        if phase == "await_login_otp"
        else "early_chat"
    )
    if wants_login_intent(msg) or session_ctx.get("auth_intent") == "login":
        login = handle_guest_login(msg, step=str(login_step), session_ctx=session_ctx)
        if login:
            reply, ctx = login
            if ctx.get("login_otp_token") and ctx.get("login_phone"):
                ctx["auth_action"] = _auth_action(
                    type="verify_login_otp",
                    phone=ctx.get("login_phone"),
                    token=ctx.get("login_otp_token"),
                    verify_type="sms",
                )
            elif ctx.get("requires_login_otp") and ctx.get("login_phone"):
                ctx["auth_action"] = _auth_action(
                    type="send_login_otp",
                    phone=ctx.get("login_phone"),
                    verify_type="sms",
                )
            ctx["unified_mode"] = True
            return reply, ctx, {"outcome": "A", "intent_class": "auth", "confidence": 1.0}, []

    in_discovery = active == INTENT_FIND_PEERS or wants_peer_find(msg)
    if not in_discovery and phase not in (PHASE_NEED_ZIP, PHASE_NEED_IDENTITY, PHASE_PREVIEW):
        return None

    if wants_host_activity(msg) and not in_discovery:
        return None

    ctx_base = _routing_ctx(session_ctx, phase=phase or PHASE_NEED_ZIP, active_intent=INTENT_FIND_PEERS)

    # Slot: ZIP / block
    block_id = resolve_block_id(session_ctx, home_block_id)
    zip_from_msg = extract_zip(msg)
    if zip_from_msg and not block_id:
        blocks = fetch_blocks_for_zip(user_jwt, zip_from_msg)
        if blocks:
            block_id = str(blocks[0].get("block_id") or "")
            ctx_base["preview_block_id"] = block_id
            ctx_base["preview_zip"] = zip_from_msg
            ctx_base["preview_block_label"] = str(blocks[0].get("label") or blocks[0].get("name") or zip_from_msg)

    if not block_id:
        if zip_from_msg:
            return (
                f"I couldn't find blocks for ZIP {zip_from_msg}. Try another ZIP (e.g. 32827 for Lake Nona).",
                _routing_ctx(session_ctx, phase=PHASE_NEED_ZIP, active_intent=INTENT_FIND_PEERS),
                _discovery_routing_stub(PHASE_NEED_ZIP),
                [],
            )
        return (
            "What ZIP code is your block? That helps me find neighbors near you.",
            _routing_ctx(session_ctx, phase=PHASE_NEED_ZIP, active_intent=INTENT_FIND_PEERS),
            _discovery_routing_stub(PHASE_NEED_ZIP),
            [],
        )

    # Slot: identity snippet
    snippet = (
        session_ctx.get("identity_snippet")
        or ctx_base.get("identity_snippet")
        or extract_identity_snippet(msg)
    )
    if snippet:
        ctx_base["identity_snippet"] = snippet

    if not snippet:
        return (
                "Tell me one thing about you — life stage, heritage, or what you're looking for — "
                "so I can match you better.",
                _routing_ctx(
                    session_ctx,
                    phase=PHASE_NEED_IDENTITY,
                    active_intent=INTENT_FIND_PEERS,
                    preview_block_id=block_id,
                    preview_zip=ctx_base.get("preview_zip"),
                    preview_block_label=ctx_base.get("preview_block_label"),
                ),
                _discovery_routing_stub(PHASE_NEED_IDENTITY),
                [],
            )

    block_label = str(
        ctx_base.get("preview_block_label")
        or session_ctx.get("preview_block_label")
        or "your block"
    )

    # Gate: user wants full details but not verified
    if wants_more_peer_detail(msg) and not phone_verified:
        return (
            "I can see neighbors nearby — to show names and connect you, verify your phone first. "
            "What's your number?",
            _routing_ctx(
                ctx_base,
                phase=PHASE_AWAIT_SIGNUP_PHONE,
                preview_block_id=block_id,
            ),
            _discovery_routing_stub(PHASE_GATE_VERIFY),
            [],
        )

    # Full matches when verified + block on profile
    if phone_verified and home_block_id:
        try:
            peers = fetch_peer_matches(user_jwt, limit=5)
        except Exception:
            peers = []
        if peers:
            reply = format_peer_matches(peers)
            ctx = _routing_ctx(ctx_base, phase=PHASE_PREVIEW, preview_block_id=block_id)
            ctx["last_routing"] = _discovery_routing_stub(PHASE_PREVIEW, "match_peers_by_claim_vectors")
            return reply, ctx, ctx["last_routing"], peers

    # Preview (anonymous or unverified)
    peers = fetch_preview_peers_on_block(block_id, limit=3)
    reply = format_preview_message(peers, block_label)
    ctx = _routing_ctx(ctx_base, phase=PHASE_PREVIEW, preview_block_id=block_id)
    ctx["last_routing"] = _discovery_routing_stub(PHASE_PREVIEW, "preview_peers_on_block")
    return reply, ctx, ctx["last_routing"], peers
