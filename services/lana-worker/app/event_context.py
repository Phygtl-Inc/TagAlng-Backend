"""Event draft context formatting (no DB / auth deps)."""

from typing import Any

EVENT_HISTORY_MAX = 6


def host_display_name(ctx: dict[str, Any]) -> str | None:
    """Nickname, or first token of full_name, for greetings."""
    nick = str(ctx.get("nickname") or "").strip()
    if nick:
        return nick
    full = str(ctx.get("full_name") or "").strip()
    if full:
        return full.split()[0]
    return None


def format_event_draft_context(ctx: dict[str, Any]) -> str:
    lines = ["HOST CONTEXT (minimal — extract event fields only):"]
    name = host_display_name(ctx)
    if name:
        lines.append(f"- Host name (use in greeting): {name}")
    if ctx.get("block_display_name"):
        lines.append(f"- Block: {ctx['block_display_name']} (block-level venues only)")
    elif ctx.get("home_block_id"):
        lines.append("- Block: assigned (use block-level venue names only)")
    purpose_ids = ctx.get("event_purpose_ids") or []
    if purpose_ids:
        lines.append("- Purpose chip ids (cohort_tags): " + ", ".join(purpose_ids[:12]))
    return "\n".join(lines)


def format_chat_history(
    messages: list[dict[str, Any]],
    *,
    max_messages: int | None = None,
) -> str:
    if max_messages is not None and max_messages > 0 and len(messages) > max_messages:
        messages = messages[-max_messages:]
    if not messages:
        return "(no messages yet)"
    lines: list[str] = []
    for m in messages:
        role = m.get("role", "user")
        who = "User" if role == "user" else "Lana"
        lines.append(f"{who}: {m.get('content', '').strip()}")
    return "\n".join(lines)
