"""MemGPT Tier 1 core block — build, merge, patch, format."""

import copy
from typing import Any

from app.auth import service_client

CORE_BLOCK_VERSION = 1

ALLOWED_CORE_PATCH_KEYS = frozenset(
    {
        "session.current_goal",
        "session.last_topic",
        "session.last_captured_inquiry_id",
        "active_signals.open_marketplace_inquiry",
        "active_signals.pending_joint_moment",
    }
)


def build_core_block(
    *,
    user_id: str,
    session_id: str | None,
    purpose: str,
    ctx_pack: dict[str, Any],
    session_ctx: dict[str, Any] | None = None,
    history: list[dict[str, Any]] | None = None,
    persisted: dict[str, Any] | None = None,
    prefetched: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build fresh core block from DB context; merge persisted patches; attach prefetch."""
    session_ctx = session_ctx or {}
    history = history or []
    claims = ctx_pack.get("existing_claims") or []
    net = ctx_pack.get("block_network") or {}
    tier_map = ctx_pack.get("relationship_tiers") or {}

    fresh: dict[str, Any] = {
        "version": CORE_BLOCK_VERSION,
        "user": {
            "id": user_id,
            "nickname": ctx_pack.get("nickname"),
            "block_id": ctx_pack.get("home_block_id"),
            "block_label": ctx_pack.get("block_display_name") or net.get("block_display_name"),
            "block_state": ctx_pack.get("block_state"),
        },
        "tier_ladder": {
            "tier_with_neighbors": tier_map,
        },
        "session": {
            "id": session_id,
            "purpose": purpose,
            "state": derive_session_state(purpose, session_ctx, history),
            "current_goal": session_ctx.get("goal") or _default_goal(purpose),
            "last_topic": session_ctx.get("last_topic"),
            "last_captured_inquiry_id": session_ctx.get("last_captured_inquiry_id"),
            "last_3_turns": _last_turn_pairs(history, limit=3),
            "topics_covered": session_ctx.get("topics_covered") or [],
            "topics_to_explore": session_ctx.get("topics_to_explore") or [],
            "last_status": session_ctx.get("last_status"),
            "pending_confirmation": session_ctx.get("pending_confirmation"),
            "pattern_hints": _load_pattern_hints(user_id),
        },
        "active_signals": {
            "upcoming_events": (net.get("upcoming_events") or [])[:3],
            "neighbor_hint_count": len(net.get("neighbor_hints") or []),
            "open_marketplace_inquiry": session_ctx.get("open_marketplace_inquiry"),
            "pending_joint_moment": session_ctx.get("pending_joint_moment"),
        },
        "identity": {
            "threads": [
                {
                    "concept": c.get("concept"),
                    "label": c.get("label"),
                    "disclosure": c.get("disclosure", "public"),
                }
                for c in claims[:12]
            ]
        },
        "block": {
            "user_id": user_id,
            "nickname": ctx_pack.get("nickname"),
            "home_block_id": ctx_pack.get("home_block_id"),
            "block_display_name": ctx_pack.get("block_display_name") or net.get("block_display_name"),
            "block_state": ctx_pack.get("block_state"),
            "member_count": net.get("member_count"),
        },
    }

    if purpose == "event_draft":
        fresh["session"]["event_draft"] = session_ctx.get("event_draft") or {}

    merged = merge_core_blocks(persisted or {}, fresh)
    if prefetched:
        merged["_prefetch"] = prefetched
    return merged


def merge_core_blocks(persisted: dict[str, Any], fresh: dict[str, Any]) -> dict[str, Any]:
    """Keep synthesizer-patched session fields from persisted core_block."""
    if not persisted:
        return fresh
    merged = copy.deepcopy(fresh)
    ps = persisted.get("session") or {}
    ms = merged.setdefault("session", {})
    for key in (
        "current_goal",
        "last_topic",
        "last_captured_inquiry_id",
    ):
        if ps.get(key) is not None:
            ms[key] = ps[key]
    pas = persisted.get("active_signals") or {}
    mas = merged.setdefault("active_signals", {})
    for key in ("open_marketplace_inquiry", "pending_joint_moment"):
        if pas.get(key) is not None:
            mas[key] = pas[key]
    return merged


def apply_core_patch(core: dict[str, Any], patch: dict[str, Any] | None) -> dict[str, Any]:
    """Apply whitelisted core_patch from synthesizer (Architecture §4 write-back)."""
    if not patch or not isinstance(patch, dict):
        return core
    out = copy.deepcopy(core)
    flat = _flatten_patch(patch)
    for dotted, value in flat.items():
        if dotted not in ALLOWED_CORE_PATCH_KEYS:
            continue
        if value is None:
            continue
        section, key = dotted.split(".", 1)
        bucket = out.setdefault(section, {})
        if isinstance(bucket, dict):
            bucket[key] = value
    return out


def _flatten_patch(patch: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in patch.items():
        dotted = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict) and key in ("session", "active_signals"):
            out.update(_flatten_patch(value, dotted))
        else:
            out[dotted] = value
    return out


def strip_ephemeral(core: dict[str, Any]) -> dict[str, Any]:
    """Remove per-turn-only fields before persisting to lana_sessions.core_block."""
    return {k: v for k, v in core.items() if not str(k).startswith("_")}


def derive_session_state(
    purpose: str,
    session_ctx: dict[str, Any],
    history: list[dict[str, Any]],
) -> str:
    user_turns = [m for m in history if m.get("role") == "user"]
    if not user_turns:
        return "greeting"
    if purpose == "event_draft":
        if session_ctx.get("pending_confirmation") or session_ctx.get("event_draft"):
            return "acting"
        return "listening"
    if session_ctx.get("last_status") == "ready_to_complete":
        return "matched"
    return "listening"


def _last_turn_pairs(history: list[dict[str, Any]], limit: int = 3) -> list[dict[str, str]]:
    pairs: list[dict[str, str]] = []
    for m in history[-(limit * 2) :]:
        role = "user" if m.get("role") == "user" else "lana"
        content = str(m.get("content", "")).strip()[:400]
        if content:
            pairs.append({"role": role, "content": content})
    return pairs[-limit * 2 :]


def _load_pattern_hints(user_id: str) -> list[str]:
    """Surface behavioral hints after session 3+ (Architecture §4 pattern memory lite)."""
    try:
        sb = service_client()
        sess = (
            sb.table("lana_sessions")
            .select("id", count="exact")
            .eq("user_id", user_id)
            .eq("status", "completed")
            .limit(1)
            .execute()
        )
        completed = int(sess.count or 0)
        if completed < 3:
            return []
        inq = (
            sb.table("inquiry_signals")
            .select("category")
            .eq("user_id", user_id)
            .order("captured_at", desc=True)
            .limit(20)
            .execute()
        )
        categories: dict[str, int] = {}
        for row in inq.data or []:
            cat = str(row.get("category") or "other")
            categories[cat] = categories.get(cat, 0) + 1
        if not categories:
            return []
        top = sorted(categories.items(), key=lambda x: x[1], reverse=True)[:3]
        return [f"returned_to_{cat}" for cat, _ in top]
    except Exception:
        return []


def _default_goal(purpose: str) -> str:
    if purpose == "event_draft":
        return "help host publish a block activity"
    return "learn identity for neighbor matching"


def format_core_block(core: dict[str, Any]) -> str:
    lines = ["CORE MEMORY BLOCK (trusted facts — do not invent beyond this):"]
    user = core.get("user") or {}
    if user.get("nickname"):
        lines.append(f"- User: {user['nickname']}")
    if user.get("block_label"):
        lines.append(f"- Block: {user['block_label']}")

    ident = core.get("identity") or {}
    threads = ident.get("threads") or []
    if threads:
        lines.append("- Identity threads:")
        for t in threads:
            lines.append(f"  · {t.get('label')} ({t.get('disclosure', 'public')})")
    else:
        lines.append("- Identity threads: none yet")

    sess = core.get("session") or {}
    state = sess.get("state", "listening")
    lines.append(
        f"- Session: {sess.get('purpose')} · state={state} · goal: {sess.get('current_goal')}"
    )
    if sess.get("last_topic"):
        lines.append(f"- Last topic: {sess['last_topic']}")
    if sess.get("event_draft"):
        lines.append(f"- Event draft in progress: {sess['event_draft']}")
    if sess.get("pending_confirmation"):
        lines.append(f"- Awaiting user confirmation: {sess['pending_confirmation']}")
    hints = sess.get("pattern_hints") or []
    if hints:
        lines.append(f"- Pattern hints: {', '.join(hints[:5])}")

    tiers = core.get("tier_ladder") or {}
    tier_map = tiers.get("tier_with_neighbors") or {}
    if tier_map:
        lines.append("- Relationship tiers with known neighbors:")
        for uid, tier in list(tier_map.items())[:8]:
            lines.append(f"  · {uid}: {tier}")

    prefetch = core.get("_prefetch") or []
    if prefetch:
        lines.append("- Prefetched archival memories (relevant to this turn):")
        for mem in prefetch[:5]:
            sim = mem.get("similarity")
            pct = f"{round(float(sim) * 100)}%" if sim is not None else "—"
            lines.append(f"  · [{mem.get('source_type')}] {mem.get('content')} (~{pct})")

    return "\n".join(lines)


def format_recent_turns(messages: list[dict[str, Any]], limit: int = 6) -> str:
    """Last N messages — working memory beyond core block last_3_turns."""
    if not messages:
        return "(no prior turns)"
    tail = messages[-limit:]
    lines: list[str] = []
    for m in tail:
        who = "User" if m.get("role") == "user" else "Lana"
        lines.append(f"{who}: {str(m.get('content', '')).strip()}")
    return "\n".join(lines)


def format_recall_memories(memories: list[dict[str, Any]]) -> str:
    if not memories:
        return "(no matching memories)"
    lines: list[str] = []
    for mem in memories[:8]:
        sim = mem.get("similarity")
        pct = f"{round(float(sim) * 100)}%" if sim is not None else "—"
        lines.append(f"- [{mem.get('source_type')}/{mem.get('scope', 'self')}] {mem.get('content')} (~{pct})")
    return "\n".join(lines)
