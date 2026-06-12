"""Discovery routing: find peers with ZIP → identity → preview → verify gate → full."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from fastapi import HTTPException

from app.auth import service_client
from app.discovery_slots import (
    ai_parse_discovery_turn,
    discovery_ai_enabled,
    discovery_slots_for_turn,
    slots_want_discovery_handling,
    slots_want_preview_refetch,
)
from app.turn_timing import TurnTimer
from app.guest_capabilities import (
    fetch_peer_matches,
    format_peer_matches,
    wants_host_activity,
    wants_peer_find,
)
from app.guest_login import (
    _exit_login_ctx,
    _logout_ctx,
    extract_otp_code,
    extract_phone_e164,
    handle_guest_login,
    wants_login as wants_login_intent,
    wants_logout as wants_logout_intent,
)
from app.claims_persist import (
    extract_display_name_reply,
    extract_nickname_from_message,
    persist_profile_patch,
    user_needs_display_name,
)
from app.profile_photo import handle_profile_photo_turn, user_profile_photo_url
from app.supabase_rpc import call_rpc

PHASE_NEED_ZIP = "need_zip"
PHASE_NEED_IDENTITY = "need_identity"
PHASE_NEED_DISPLAY_NAME = "need_display_name"
PHASE_PREVIEW = "preview"
PHASE_GATE_VERIFY = "gate_verify"
PHASE_AWAIT_SIGNUP_PHONE = "await_signup_phone"
PHASE_AWAIT_SIGNUP_OTP = "await_signup_otp"
PHASE_AWAIT_PROFILE_PHOTO = "await_profile_photo"

INTENT_FIND_PEERS = "discovery.find_peers"
INTENT_FIND_ACTIVITIES = "discovery.find_activities"
_DISCOVERY_GOALS = frozenset({"peers", "activities", "both"})

_FUNNEL_PHASES = frozenset(
    {PHASE_NEED_ZIP, PHASE_NEED_IDENTITY, PHASE_NEED_DISPLAY_NAME}
)

_MORE_DETAIL_RE = re.compile(
    r"\b(more|names?|introduce|connect|who are they|show me|full|details?|"
    r"see them|meet them|talk to)\b",
    re.I,
)
_VERIFY_HELP_RE = re.compile(
    r"\b(how (?:do|can) i verify|verify (?:my |me|a )?phone|phone verif|get verified|"
    r"unlock (?:names|matches)|need to verify)\b",
    re.I,
)
_RSVP_RE = re.compile(
    r"\b(rsvp|sign up for|join|take part in|attend|going to|i want to go|count me in)\b",
    re.I,
)
_ACTIVITIES_RE = re.compile(
    r"\b(activit\w*|events?|what'?s (?:happening|going on)|things to do)\b",
    re.I,
)
_ZIP_RE = re.compile(r"\b(\d{5})\b")
_META_CHAT_RE = re.compile(
    r"\b(are you (?:real|ai|a bot|human|dumb|stupid)|who are you|what are you)\b|^\s*what\?+\s*$",
    re.I,
)
_NOT_IDENTITY_REPLIES = frozenset(
    {"hello", "hi", "hey", "ok", "okay", "yes", "no", "thanks", "thank you", "yep", "nope"}
)
_AFFIRMATIVE_REPLIES = frozenset(
    {"ok", "okay", "yes", "yeah", "yep", "sure", "done", "ready", "go", "great", "perfect"}
)


def wants_more_peer_detail(text: str) -> bool:
    return bool(_MORE_DETAIL_RE.search(str(text or "").strip()))


def wants_verify_help(text: str) -> bool:
    return bool(_VERIFY_HELP_RE.search(str(text or "").strip()))


def wants_rsvp_intent(text: str) -> bool:
    return bool(_RSVP_RE.search(str(text or "").strip()))


def wants_activities_browse(text: str) -> bool:
    return bool(_ACTIVITIES_RE.search(str(text or "").strip()))


def _active_intent_for_goal(goal: str) -> str | None:
    if goal == "activities":
        return INTENT_FIND_ACTIVITIES
    if goal in ("peers", "both"):
        return INTENT_FIND_PEERS
    return None


def _update_discovery_goal_from_slots(
    session_ctx: dict[str, Any],
    slots: dict[str, Any],
) -> None:
    """Persist browse goal when Flash names peers/activities/both this turn."""
    slot_goal = str(slots.get("goal") or "none").lower()
    conf = float(slots.get("confidence", 0.0))
    if slot_goal in _DISCOVERY_GOALS and conf >= 0.45:
        session_ctx["discovery_goal"] = slot_goal


def _effective_discovery_goal(
    msg: str,
    session_ctx: dict[str, Any],
    slots: dict[str, Any],
) -> str:
    """
    Browse goal for this turn: Flash when explicit; else persisted across ZIP/continue steps.
    Updates session.discovery_goal when user pivots (e.g. peers → activities).
    """
    if wants_activities_browse(msg):
        session_ctx["discovery_goal"] = "activities"
    _update_discovery_goal_from_slots(session_ctx, slots)
    stored = str(session_ctx.get("discovery_goal") or "none")
    slot_goal = str(slots.get("goal") or "none").lower()
    conf = float(slots.get("confidence", 0.0))

    if slot_goal in _DISCOVERY_GOALS and conf >= 0.45:
        return slot_goal
    if slot_goal == "continue" and stored in _DISCOVERY_GOALS:
        return stored
    if extract_zip(msg) and stored in _DISCOVERY_GOALS:
        return stored
    return stored if stored in _DISCOVERY_GOALS else slot_goal


def _zip_prompt(discovery_goal: str) -> str:
    if discovery_goal == "activities":
        return (
            "What ZIP code is your block? That helps me find activities near you."
        )
    if discovery_goal == "both":
        return (
            "What ZIP code is your block? That helps me find neighbors and activities near you."
        )
    return "What ZIP code is your block? That helps me find neighbors near you."


def _show_activities_preview(
    *,
    ctx_base: dict[str, Any],
    block_id: str,
    block_label: str,
) -> tuple[str, dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    events = fetch_preview_events_on_block(block_id)
    reply = format_activities_message(events, block_label)
    ctx = _routing_ctx(
        ctx_base,
        phase=PHASE_PREVIEW,
        preview_block_id=block_id,
        active_intent=INTENT_FIND_ACTIVITIES,
    )
    ctx["last_routing"] = _discovery_routing_stub(PHASE_PREVIEW, "browse_block_activities")
    ctx["activity_previews"] = activity_previews_from_events(events)
    ctx["peer_matches"] = []
    return reply, ctx, ctx["last_routing"], []


def _looks_like_meta_chat(msg: str) -> bool:
    return bool(_META_CHAT_RE.search(str(msg or "").strip()))


def extract_zip(text: str) -> str | None:
    m = _ZIP_RE.search(str(text or ""))
    return m.group(1) if m else None


def invalid_zip_hint(text: str) -> str | None:
    """Explain bad ZIP attempts instead of repeating the same prompt."""
    s = str(text or "").strip()
    if not s or extract_zip(s):
        return None
    digits = "".join(c for c in s if c.isdigit())
    if not digits:
        return None
    if len(digits) < 5:
        return (
            f"That looks like {len(digits)} digits — I need a 5-digit US ZIP code "
            "(e.g. 32827 for Lake Nona). What's yours?"
        )
    if len(digits) != 5:
        return (
            "I need a 5-digit US ZIP code only (e.g. 32827), not a longer number. "
            "Which ZIP is your block?"
        )
    return None


def _is_affirmative(msg: str) -> bool:
    lower = str(msg or "").strip().lower().rstrip(".!")
    return lower in _AFFIRMATIVE_REPLIES or any(lower.startswith(f"{a} ") for a in _AFFIRMATIVE_REPLIES)


def _is_peer_find_command(msg: str) -> bool:
    """Short 'find me people' lines are intent, not identity."""
    s = str(msg or "").strip()
    return bool(s) and wants_peer_find(s) and len(s) < 60


def _user_messages_from_history(history: list[dict[str, Any]] | None) -> list[str]:
    out: list[str] = []
    for turn in history or []:
        if str(turn.get("role") or "") != "user":
            continue
        content = str(turn.get("content") or "").strip()
        if content:
            out.append(content)
    return out


def _fallback_identity_snippet(
    msg: str,
    history: list[dict[str, Any]] | None,
    *,
    phase: str,
    block_just_resolved: bool,
    has_block: bool = False,
    slots: dict[str, Any] | None = None,
) -> str | None:
    """
    Funnel fallback when Flash misses identity — reuse what the user already said.
    Runs during need_identity, right after ZIP, or when user demands peers with block set.
    """
    text = str(msg or "").strip()
    if phase == PHASE_NEED_IDENTITY and text:
        if text.lower() in _NOT_IDENTITY_REPLIES:
            return None
        if (
            not extract_zip(text)
            and not wants_login_intent(text)
            and not _looks_like_meta_chat(text)
            and not _is_peer_find_command(text)
        ):
            return text[:400]

    goal = str((slots or {}).get("goal") or "none")
    late_peer_find = (
        goal in ("peers", "both")
        and float((slots or {}).get("confidence", 0.0)) >= 0.45
        and has_block
        and phase in ("listening", PHASE_PREVIEW)
    )
    if not block_just_resolved and phase != PHASE_NEED_IDENTITY and not late_peer_find:
        return None

    long_parts: list[str] = []
    short_parts: list[str] = []
    for content in _user_messages_from_history(history):
        if extract_zip(content) and len(content.strip()) <= 8:
            continue
        if _is_peer_find_command(content):
            continue
        if wants_activities_browse(content):
            continue
        if _looks_like_meta_chat(content) or wants_login_intent(content):
            continue
        stripped = content.strip()
        if len(stripped) >= 12:
            long_parts.append(stripped[:200])
        elif len(stripped) >= 2:
            short_parts.append(stripped[:80])
    if long_parts:
        merged = long_parts[-3:]
        if short_parts and (block_just_resolved or late_peer_find):
            merged = merged + short_parts[-5:]
        return "; ".join(merged)[:400]
    if short_parts and (block_just_resolved or late_peer_find):
        return "; ".join(short_parts[-6:])[:400]
    return None


def _explicit_funnel_input(msg: str) -> bool:
    """Code-owned structural signals only — peer-find intent is Flash slots, not regex."""
    if extract_zip(msg) or invalid_zip_hint(msg):
        return True
    if wants_verify_help(msg) or wants_more_peer_detail(msg):
        return True
    if wants_rsvp_intent(msg) or wants_activities_browse(msg):
        return True
    return False


def wants_discovery_turn(
    msg: str,
    session_ctx: dict[str, Any],
    history: list[dict[str, Any]] | None = None,
    slots: dict[str, Any] | None = None,
) -> bool:
    """
    Should discovery code handle this turn?
    Code: explicit funnel signals only (ZIP digits, find-peers phrasing).
    AI: every other message — slots decide discovery vs orchestrator.
    """
    if _explicit_funnel_input(msg):
        return True

    phase = str(session_ctx.get("routing_phase") or "")
    if phase in _FUNNEL_PHASES:
        if wants_login_intent(msg):
            return True
        if _looks_like_meta_chat(msg):
            return False
        return True

    if session_ctx.get("pending_post_verify"):
        if wants_login_intent(msg):
            return True
        if _looks_like_meta_chat(msg):
            return False
        return True

    if discovery_ai_enabled():
        if slots is None:
            slots = discovery_slots_for_turn(
                session_ctx,
                msg,
                routing_phase=phase or "listening",
                history=history,
                has_block=bool(resolve_block_id(session_ctx, None)),
                has_identity=bool(session_ctx.get("identity_snippet")),
                phone_verified=bool(session_ctx.get("phone_verified")),
            )
        if slots.get("identity_snippet") and phase == PHASE_NEED_IDENTITY:
            return True
        return slots_want_discovery_handling(slots, routing_phase=phase)

    # AI off (dev/tests): minimal legacy fallback
    if phase == PHASE_NEED_IDENTITY:
        s = str(msg or "").strip()
        if (
            s
            and not extract_zip(s)
            and not wants_login_intent(s)
            and not _looks_like_meta_chat(s)
        ):
            return True
    return wants_peer_find(msg) or wants_activities_browse(msg)


def _identity_refinement(
    slots: dict[str, Any] | None,
    session_ctx: dict[str, Any],
) -> str | None:
    """New identity line in preview phase (user refined who they want)."""
    if not slots:
        return None
    raw = slots.get("identity_snippet")
    if not raw:
        return None
    new_sn = str(raw).strip()[:400]
    if not new_sn:
        return None
    stored = str(session_ctx.get("identity_snippet") or "").strip()
    if stored and new_sn.lower() == stored.lower():
        return None
    return new_sn


def _user_nickname(user_id: str | None) -> str | None:
    if not user_id:
        return None
    try:
        res = (
            service_client()
            .table("users")
            .select("nickname, full_name")
            .eq("id", user_id)
            .limit(1)
            .execute()
        )
        row = (res.data or [None])[0]
        if not isinstance(row, dict):
            return None
        nick = str(row.get("nickname") or row.get("full_name") or "").strip()
        return nick or None
    except Exception:
        return None


def _try_assign_home_block(
    user_jwt: str,
    *,
    session_ctx: dict[str, Any],
    home_block_id: str | None,
) -> str | None:
    """Persist preview block on user after phone verify (required for vector match)."""
    if home_block_id:
        return home_block_id
    bid = resolve_block_id(session_ctx, None)
    if not bid:
        return None
    payload: dict[str, Any] = {"p_block_id": bid}
    zip5 = session_ctx.get("preview_zip")
    if zip5:
        payload["p_home_zip"] = str(zip5)
    try:
        call_rpc(user_jwt, "assign_home_block", payload)
    except HTTPException:
        pass
    return bid


def _should_skip_preview_refetch(
    *,
    phase: str,
    msg: str,
    goal: str,
    slots: dict[str, Any] | None,
    session_ctx: dict[str, Any],
) -> bool:
    """After first preview, let orchestrator handle pushback unless explicit refresh."""
    if phase != PHASE_PREVIEW:
        return False
    if wants_more_peer_detail(msg) or goal in ("verify", "rsvp"):
        return False
    if slots_want_preview_refetch(slots or {}, session_ctx):
        return False
    return True


def resolve_identity_for_turn(
    msg: str,
    session_ctx: dict[str, Any],
    history: list[dict[str, Any]] | None,
    phase: str,
    *,
    block_just_resolved: bool,
    slots: dict[str, Any] | None = None,
) -> str | None:
    stored = str(session_ctx.get("identity_snippet") or "").strip() or None
    if discovery_ai_enabled():
        parsed = slots or discovery_slots_for_turn(
            session_ctx,
            msg,
            routing_phase=PHASE_NEED_IDENTITY if block_just_resolved else phase,
            history=history,
            has_block=True,
            has_identity=bool(stored),
            phone_verified=bool(session_ctx.get("phone_verified")),
        )
        sn = parsed.get("identity_snippet")
        if sn:
            sn_s = str(sn).strip()[:400]
            if sn_s:
                if phase == PHASE_PREVIEW and stored and sn_s.lower() != stored.lower():
                    return sn_s
                if not stored:
                    return sn_s
        if stored:
            return stored
        fallback = _fallback_identity_snippet(
            msg,
            history,
            phase=PHASE_NEED_IDENTITY if block_just_resolved else phase,
            block_just_resolved=block_just_resolved,
            has_block=True,
            slots=slots,
        )
        if fallback:
            return fallback
        return None
    if stored:
        return stored
    fallback = _fallback_identity_snippet(
        msg,
        history,
        phase=phase,
        block_just_resolved=block_just_resolved,
        has_block=bool(resolve_block_id(session_ctx, None)),
        slots=slots,
    )
    if fallback:
        return fallback
    if phase == PHASE_NEED_IDENTITY:
        s = str(msg or "").strip()
        if (
            s
            and s.lower() not in _NOT_IDENTITY_REPLIES
            and not extract_zip(s)
            and not wants_login_intent(s)
            and not _looks_like_meta_chat(s)
            and not _is_peer_find_command(s)
        ):
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
    try:
        raw = call_rpc(
            user_jwt,
            "get_blocks_near_zip",
            {"p_zip": zip5, "p_cluster_id": "lake-nona", "p_limit": 5},
        )
    except HTTPException as exc:
        detail = str(exc.detail or "").lower()
        if "zip_not_found" in detail or "invalid_zip" in detail:
            return []
        raise
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


def _format_event_when(raw: Any) -> str | None:
    s = str(raw or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.strftime("%a %b %d").replace(" 0", " ")
    except ValueError:
        return s[:10] if len(s) >= 10 else s


def activity_previews_from_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for ev in events[:5]:
        if not isinstance(ev, dict):
            continue
        out.append(
            {
                "title": str(ev.get("title") or "Activity"),
                "starts_at": str(ev.get("starts_at") or "") or None,
                "starts_label": _format_event_when(ev.get("starts_at")),
                "venue_name": str(ev.get("venue_name") or "").strip() or None,
                "preview": True,
            }
        )
    return out


def fetch_preview_events_on_block(block_id: str, *, limit: int = 5) -> list[dict[str, Any]]:
    """Upcoming open events on preview block (service role)."""
    try:
        sb = service_client()
        res = (
            sb.table("events")
            .select("title, starts_at, venue_name, cohort_tags")
            .eq("block_id", block_id)
            .eq("status", "open")
            .order("starts_at")
            .limit(limit)
            .execute()
        )
        return [r for r in (res.data or []) if isinstance(r, dict)]
    except Exception:
        return []


def format_activities_message(events: list[dict[str, Any]], block_label: str | None) -> str:
    where = block_label or "your block"
    if not events:
        return (
            f"I don't see open activities on {where} in the next couple weeks yet. "
            "You can verify your phone to host something, or tell me what you're looking for."
        )
    lines = [f"Here's what's coming up near {where}:"]
    for ev in events[:5]:
        title = str(ev.get("title") or "Activity")
        venue = str(ev.get("venue_name") or "").strip()
        when = _format_event_when(ev.get("starts_at"))
        line = f"• {title}"
        if venue:
            line += f" at {venue}"
        if when:
            line += f" ({when})"
        lines.append(line)
    lines.append("Verify your phone to RSVP — or ask me to find neighbors like you.")
    return "\n".join(lines)


def _match_event_title(events: list[dict[str, Any]], msg: str) -> str | None:
    msg_l = str(msg or "").lower()
    for ev in events:
        title = str(ev.get("title") or "").strip()
        if not title:
            continue
        if title.lower() in msg_l:
            return title
        words = [w for w in re.findall(r"[a-z0-9]+", title.lower()) if len(w) > 3]
        if len(words) >= 2 and all(w in msg_l for w in words[:2]):
            return title
    return None


def _verify_gate_reply(
    *,
    session_ctx: dict[str, Any],
    ctx_base: dict[str, Any],
    block_id: str,
    event_label: str | None = None,
) -> tuple[str, dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    if event_label:
        lead = f"To join {event_label}, verify your phone first — I'll text you a code."
    else:
        lead = "I can see neighbors nearby — to show names and connect you, verify your phone first."
    ctx = _routing_ctx(
        ctx_base,
        phase=PHASE_AWAIT_SIGNUP_PHONE,
        preview_block_id=block_id,
    )
    ctx["peer_matches"] = []
    return (
        f"{lead} What's your number?",
        ctx,
        _discovery_routing_stub(PHASE_GATE_VERIFY),
        [],
    )


def format_preview_message(
    peers: list[dict[str, Any]],
    block_label: str | None,
    *,
    phone_verified: bool = False,
) -> str:
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
    if phone_verified:
        lines.append(
            "Tell me more about you for sharper matches — or ask me to introduce you to someone."
        )
    else:
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


def _apply_display_name_gate(
    msg: str,
    *,
    user_id: str | None,
    ctx_base: dict[str, Any],
    block_id: str,
    phase: str,
    snippet: str | None,
) -> tuple[str, dict[str, Any], dict[str, Any], list[dict[str, Any]]] | None:
    """After identity: collect display name before peer preview."""
    if not snippet or not user_needs_display_name(user_id, ctx_base):
        return None

    if phase == PHASE_NEED_DISPLAY_NAME:
        nick = extract_display_name_reply(msg)
        if nick and user_id:
            persist_profile_patch(user_id, {"nickname": nick})
            ctx_base["display_name_saved"] = True
            return None
        return (
            "I didn't catch that — what should neighbors call you? First name is fine.",
            _routing_ctx(
                ctx_base,
                phase=PHASE_NEED_DISPLAY_NAME,
                active_intent=INTENT_FIND_PEERS,
                preview_block_id=block_id,
            ),
            _discovery_routing_stub(PHASE_NEED_DISPLAY_NAME),
            [],
        )

    nick = extract_nickname_from_message(msg)
    if nick and user_id:
        persist_profile_patch(user_id, {"nickname": nick})
        ctx_base["display_name_saved"] = True
        return None

    return (
        "Love that — what should neighbors call you? First name is fine.",
        _routing_ctx(
            ctx_base,
            phase=PHASE_NEED_DISPLAY_NAME,
            active_intent=INTENT_FIND_PEERS,
            preview_block_id=block_id,
        ),
        _discovery_routing_stub(PHASE_NEED_DISPLAY_NAME),
        [],
    )


def handle_discovery_turn(
    user_message: str,
    *,
    session_ctx: dict[str, Any],
    user_jwt: str,
    phone_verified: bool,
    home_block_id: str | None,
    is_anonymous: bool,
    history: list[dict[str, Any]] | None = None,
    user_id: str | None = None,
    timer: TurnTimer | None = None,
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
        ctx["pending_post_verify"] = True
        ctx["auth_action"] = _auth_action(
            type="verify_signup_otp",
            phone=phone,
            token=otp,
            verify_type="phone_change",
        )
        return (
            "Perfect — verifying you now. Once you're verified, tell me your first name "
            "and I'll show neighbors you can connect with.",
            ctx,
            _discovery_routing_stub(PHASE_PREVIEW, "verify_signup_otp"),
            [],
        )

    had_block = bool(resolve_block_id(session_ctx, home_block_id))
    has_profile_photo = bool(user_profile_photo_url(user_id))
    slots: dict[str, Any] = {}
    if discovery_ai_enabled():
        slots = discovery_slots_for_turn(
            session_ctx,
            msg,
            routing_phase=phase or "listening",
            history=history,
            has_block=had_block,
            has_identity=bool(session_ctx.get("identity_snippet")),
            phone_verified=phone_verified,
            has_profile_photo=has_profile_photo,
            timer=timer,
        )

    photo_turn = handle_profile_photo_turn(
        msg,
        session_ctx=session_ctx,
        slots=slots,
        user_id=user_id,
        phone_verified=phone_verified,
        is_anonymous=is_anonymous,
    )
    if photo_turn:
        reply, ctx = photo_turn
        ctx["unified_mode"] = True
        return reply, ctx, {"outcome": "A", "intent_class": "profile_photo", "confidence": 1.0}, []

    if wants_logout_intent(msg):
        if phone_verified or not is_anonymous:
            nick = _user_nickname(user_id)
            farewell = f"Take care{', ' + nick if nick else ''} — signing you out now."
            ctx = _logout_ctx(session_ctx)
            ctx["auth_action"] = _auth_action(type="logout")
            ctx["unified_mode"] = True
            return (
                farewell,
                ctx,
                {"outcome": "A", "intent_class": "auth", "confidence": 1.0},
                [],
            )
        if session_ctx.get("auth_intent") == "login":
            return (
                "No problem — what would you like to do? Find neighbors, plan something, or tell me about yourself.",
                _exit_login_ctx(session_ctx),
                {"outcome": "A", "intent_class": "auth", "confidence": 1.0},
                [],
            )
        return (
            "You're not signed in — nothing to log out of. Ask me to find neighbors or tell me about yourself.",
            _routing_ctx(session_ctx, phase="listening"),
            {"outcome": "A", "intent_class": "auth", "confidence": 1.0},
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
        if phone_verified and not is_anonymous:
            nick = _user_nickname(user_id)
            label = f" as {nick}" if nick else ""
            ctx = _exit_login_ctx(session_ctx)
            return (
                f"You're already signed in{label}! Ask me to find neighbors, plan something, "
                "or tell me what you're looking for.",
                ctx,
                {"outcome": "A", "intent_class": "auth", "confidence": 1.0},
                [],
            )
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

    if not wants_discovery_turn(msg, session_ctx, history, slots=slots):
        return None

    if wants_host_activity(msg) and not wants_peer_find(msg) and slots.get("goal") not in (
        "peers",
        "activities",
        "both",
    ):
        return None

    effective_goal = _effective_discovery_goal(msg, session_ctx, slots)
    active = _active_intent_for_goal(effective_goal) or INTENT_FIND_PEERS
    ctx_base = _routing_ctx(
        session_ctx,
        phase=phase or PHASE_NEED_ZIP,
        active_intent=active,
    )
    if effective_goal in _DISCOVERY_GOALS:
        ctx_base["discovery_goal"] = effective_goal

    # Slot: ZIP / block
    block_id = resolve_block_id(session_ctx, home_block_id)
    zip_from_msg = extract_zip(msg) or slots.get("zip")
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
                _routing_ctx(
                    session_ctx,
                    phase=PHASE_NEED_ZIP,
                    active_intent=active,
                    discovery_goal=ctx_base.get("discovery_goal"),
                ),
                _discovery_routing_stub(PHASE_NEED_ZIP),
                [],
            )
        zip_hint = invalid_zip_hint(msg)
        zip_goal = str(ctx_base.get("discovery_goal") or effective_goal or "peers")
        return (
            zip_hint or _zip_prompt(zip_goal),
            _routing_ctx(
                session_ctx,
                phase=PHASE_NEED_ZIP,
                active_intent=active,
                discovery_goal=ctx_base.get("discovery_goal"),
            ),
            _discovery_routing_stub(PHASE_NEED_ZIP),
            [],
        )

    block_just_resolved = bool(zip_from_msg and not had_block)
    goal = effective_goal

    # Slot: identity snippet (Flash — not chat history heuristics)
    snippet = resolve_identity_for_turn(
        msg,
        ctx_base,
        history,
        phase,
        block_just_resolved=block_just_resolved,
        slots=slots,
    )
    if snippet:
        ctx_base["identity_snippet"] = snippet

    effective_snippet = str(ctx_base.get("identity_snippet") or "").strip() or None

    block_label = str(
        ctx_base.get("preview_block_label")
        or session_ctx.get("preview_block_label")
        or "your block"
    )

    if goal == "activities" or wants_activities_browse(msg):
        return _show_activities_preview(
            ctx_base=ctx_base,
            block_id=block_id,
            block_label=block_label,
        )

    if not effective_snippet:
        in_funnel = phase in _FUNNEL_PHASES or block_just_resolved
        if not in_funnel:
            if goal not in ("continue", "peers", "both") or not slots.get("in_discovery"):
                return None
        return (
            "Tell me one thing about you — life stage, heritage, or what you're looking for — "
            "so I can match you better.",
            _routing_ctx(
                session_ctx,
                phase=PHASE_NEED_IDENTITY,
                active_intent=active,
                preview_block_id=block_id,
                preview_zip=ctx_base.get("preview_zip"),
                preview_block_label=ctx_base.get("preview_block_label"),
                discovery_goal=ctx_base.get("discovery_goal"),
            ),
            _discovery_routing_stub(PHASE_NEED_IDENTITY),
            [],
        )

    if wants_rsvp_intent(msg) or goal == "rsvp":
        events = fetch_preview_events_on_block(block_id)
        event_title = _match_event_title(events, msg)
        if phone_verified:
            return None
        return _verify_gate_reply(
            session_ctx=session_ctx,
            ctx_base=ctx_base,
            block_id=block_id,
            event_label=f'"{event_title}"' if event_title else "that activity",
        )

    if (
        not phone_verified
        and (wants_verify_help(msg) or goal == "verify" or wants_more_peer_detail(msg))
    ):
        return _verify_gate_reply(
            session_ctx=session_ctx,
            ctx_base=ctx_base,
            block_id=block_id,
        )

    # Post-verify funnel: name first, then matches (JWT may lag one turn after OTP).
    if ctx_base.get("pending_post_verify") or phase == PHASE_NEED_DISPLAY_NAME:
        if user_needs_display_name(user_id, ctx_base):
            nick = extract_display_name_reply(msg) or extract_nickname_from_message(msg)
            if nick and user_id:
                persist_profile_patch(user_id, {"nickname": nick})
                ctx_base["display_name_saved"] = True
            else:
                return (
                    "What should neighbors call you? First name is fine.",
                    _routing_ctx(
                        ctx_base,
                        phase=PHASE_NEED_DISPLAY_NAME,
                        active_intent=INTENT_FIND_PEERS,
                        preview_block_id=block_id,
                        pending_post_verify=True,
                    ),
                    _discovery_routing_stub(PHASE_NEED_DISPLAY_NAME),
                    [],
                )
        if not phone_verified:
            nick = str(
                (ctx_base.get("display_name_saved") and extract_display_name_reply(msg))
                or extract_nickname_from_message(msg)
                or ""
            ).strip()
            lead = f"Got it{', ' + nick if nick else ''}! "
            return (
                f"{lead}Finishing verification — send one more message and I'll show your matches.",
                _routing_ctx(
                    ctx_base,
                    phase=PHASE_PREVIEW,
                    active_intent=INTENT_FIND_PEERS,
                    preview_block_id=block_id,
                    pending_post_verify=True,
                ),
                _discovery_routing_stub(PHASE_PREVIEW),
                [],
            )
        _try_assign_home_block(user_jwt, session_ctx=ctx_base, home_block_id=home_block_id)
        try:
            peers = fetch_peer_matches(user_jwt, limit=5)
        except Exception:
            peers = []
        if not peers:
            peers = fetch_preview_peers_on_block(block_id, limit=3)
            reply = format_preview_message(peers, block_label, phone_verified=True)
        else:
            reply = format_peer_matches(peers)
        ctx = _routing_ctx(ctx_base, phase=PHASE_PREVIEW, preview_block_id=block_id)
        ctx.pop("pending_post_verify", None)
        ctx.pop("activity_previews", None)
        ctx["last_routing"] = _discovery_routing_stub(
            PHASE_PREVIEW, "match_peers_by_claim_vectors"
        )
        return reply, ctx, ctx["last_routing"], peers

    # Preview re-search: AI must supply new identity_snippet + goal=peers (not questions).
    if phase == PHASE_PREVIEW and slots_want_preview_refetch(slots, session_ctx):
        refined = _identity_refinement(slots, session_ctx)
        if refined:
            ctx_base["identity_snippet"] = refined
        if phone_verified:
            _try_assign_home_block(user_jwt, session_ctx=ctx_base, home_block_id=home_block_id)
            try:
                peers = fetch_peer_matches(user_jwt, limit=5)
            except Exception:
                peers = []
            if peers:
                reply = format_peer_matches(peers)
                ctx = _routing_ctx(ctx_base, phase=PHASE_PREVIEW, preview_block_id=block_id)
                ctx.pop("activity_previews", None)
                ctx["last_routing"] = _discovery_routing_stub(
                    PHASE_PREVIEW, "match_peers_by_claim_vectors"
                )
                return reply, ctx, ctx["last_routing"], peers
        peers = fetch_preview_peers_on_block(block_id, limit=3)
        reply = format_preview_message(peers, block_label, phone_verified=phone_verified)
        ctx = _routing_ctx(ctx_base, phase=PHASE_PREVIEW, preview_block_id=block_id)
        ctx.pop("activity_previews", None)
        ctx["last_routing"] = _discovery_routing_stub(PHASE_PREVIEW, "preview_peers_on_block")
        return reply, ctx, ctx["last_routing"], peers

    wants_peers = goal in ("peers", "both")
    if not discovery_ai_enabled():
        wants_peers = wants_peers or wants_peer_find(msg)
    if _should_skip_preview_refetch(
        phase=phase,
        msg=msg,
        goal=goal,
        slots=slots,
        session_ctx=session_ctx,
    ):
        return None

    if phase != PHASE_PREVIEW or wants_peers or wants_more_peer_detail(msg):
        effective_home = home_block_id or _try_assign_home_block(
            user_jwt, session_ctx=ctx_base, home_block_id=home_block_id
        )
        if phone_verified and effective_home:
            try:
                peers = fetch_peer_matches(user_jwt, limit=5)
            except Exception:
                peers = []
            if peers:
                reply = format_peer_matches(peers)
                ctx = _routing_ctx(ctx_base, phase=PHASE_PREVIEW, preview_block_id=block_id)
                ctx.pop("activity_previews", None)
                ctx["last_routing"] = _discovery_routing_stub(
                    PHASE_PREVIEW, "match_peers_by_claim_vectors"
                )
                return reply, ctx, ctx["last_routing"], peers

        if wants_peers or phase != PHASE_PREVIEW:
            peers = fetch_preview_peers_on_block(block_id, limit=3)
            reply = format_preview_message(peers, block_label, phone_verified=phone_verified)
            ctx = _routing_ctx(ctx_base, phase=PHASE_PREVIEW, preview_block_id=block_id)
            ctx.pop("activity_previews", None)
            ctx["last_routing"] = _discovery_routing_stub(PHASE_PREVIEW, "preview_peers_on_block")
            return reply, ctx, ctx["last_routing"], peers

    return None
