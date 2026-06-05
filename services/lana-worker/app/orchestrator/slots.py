"""Per-tool slot validation for orchestrator routing (Tool Routing v1 §4)."""

from typing import Any

from app.lana_ui import merge_event_drafts, parse_event_draft

PLACEHOLDER_VALUES = frozenset(
    {"tbd", "n/a", "na", "unknown", "none", "null", "...", "?", "todo", "later"}
)

EVENT_SLOT_ORDER = ("starts_at", "venue_name", "title")

KNOWN_TOOLS = frozenset(
    {
        "capture_inquiry",
        "flag_sensitive",
        "update_event_draft",
        "publish_activity",
        "send_nudge",
        "propose_intro",
        "propose_cohost",
        "update_relationship_tier",
        "recall",
    }
)


def is_placeholder(value: Any) -> bool:
    if value is None:
        return True
    text = str(value).strip()
    if not text:
        return True
    lower = text.lower()
    if lower in PLACEHOLDER_VALUES:
        return True
    if lower.startswith("tbd"):
        return True
    return False


def normalize_event_args(args: dict[str, Any]) -> dict[str, Any]:
    out = dict(args)
    if "when" in out and "starts_at" not in out:
        out["starts_at"] = out.pop("when")
    if "where" in out and "venue_name" not in out:
        out["venue_name"] = out.pop("where")
    return out


def merged_event_draft(
    session_ctx: dict[str, Any],
    tool_args: dict[str, Any] | None,
) -> dict[str, Any]:
    prev = session_ctx.get("event_draft") or {}
    patch = normalize_event_args(tool_args or {})
    clean = {
        k: v
        for k, v in patch.items()
        if v is not None and not is_placeholder(v)
    }
    return merge_event_drafts(prev, parse_event_draft(clean))


def event_missing_slots(draft: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    if is_placeholder(draft.get("title")):
        missing.append("title")
    if is_placeholder(draft.get("starts_at")):
        missing.append("starts_at")
    if is_placeholder(draft.get("venue_name")):
        missing.append("venue_name")
    return missing


def next_missing_event_slot(missing: list[str]) -> str | None:
    for slot in EVENT_SLOT_ORDER:
        if slot in missing:
            return slot
    return missing[0] if missing else None


def has_partial_event_args(
    tool_args: dict[str, Any] | None,
    session_ctx: dict[str, Any],
) -> bool:
    merged = merged_event_draft(session_ctx, tool_args)
    return any(not is_placeholder(merged.get(f)) for f in ("title", "starts_at", "venue_name"))


def validate_tool_slots(
    tool_name: str,
    tool_args: dict[str, Any] | None,
    *,
    purpose: str,
    session_ctx: dict[str, Any],
) -> list[str]:
    args = tool_args or {}

    if tool_name == "capture_inquiry":
        missing: list[str] = []
        if is_placeholder(args.get("raw_query")):
            missing.append("raw_query")
        if is_placeholder(args.get("extracted_category")):
            missing.append("extracted_category")
        return missing

    if tool_name == "flag_sensitive":
        missing = []
        if is_placeholder(args.get("category")):
            missing.append("category")
        if is_placeholder(args.get("severity")):
            missing.append("severity")
        return missing

    if tool_name == "update_event_draft":
        if purpose != "event_draft":
            return ["wrong_session_purpose"]
        if not has_partial_event_args(args, session_ctx):
            return ["event_detail"]
        return []

    if tool_name == "publish_activity":
        if purpose != "event_draft":
            return ["wrong_session_purpose"]
        draft = merged_event_draft(session_ctx, args)
        return event_missing_slots(draft)

    if tool_name == "send_nudge":
        if is_placeholder(args.get("to_user_id") or args.get("recipient_id")):
            return ["to_user_id"]
        return []

    if tool_name == "propose_intro":
        missing = []
        if is_placeholder(args.get("other_user_id") or args.get("candidate_user_id")):
            missing.append("other_user_id")
        reason = str(args.get("match_reason") or args.get("reason") or "").strip()
        if len(reason) < 10:
            missing.append("match_reason")
        return missing

    if tool_name == "propose_cohost":
        missing = []
        if purpose != "event_draft":
            missing.append("wrong_session_purpose")
        if is_placeholder(args.get("candidate_user_id")):
            missing.append("candidate_user_id")
        reason = str(args.get("overlap_reason") or args.get("reason") or "").strip()
        if len(reason) < 10:
            missing.append("overlap_reason")
        return missing

    if tool_name == "update_relationship_tier":
        missing = []
        if is_placeholder(args.get("other_user_id")):
            missing.append("other_user_id")
        if is_placeholder(args.get("trigger_event") or args.get("new_tier_trigger")):
            missing.append("trigger_event")
        return missing

    if tool_name == "recall":
        if is_placeholder(args.get("query") or args.get("q")):
            return ["query"]
        scope = str(args.get("scope") or "self").strip().lower()
        if scope not in ("self", "neighbors", "block"):
            return ["scope"]
        return []

    return ["unknown_tool"]
