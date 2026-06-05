from typing import Any

from fastapi import HTTPException

from app.auth import service_client
from app.event_publish import publish_event
from app.models import EventDraft
from app.orchestrator.recall import execute_recall_tool
from app.orchestrator.slots import event_missing_slots, merged_event_draft, normalize_event_args
from app.supabase_rpc import call_rpc
from app.vertex_extract import vertex_embed


def execute_tool(
    *,
    tool_name: str,
    tool_args: dict[str, Any] | None,
    user_id: str,
    user_jwt: str | None,
    session_id: str,
    block_id: str | None,
    purpose: str,
    session_ctx: dict[str, Any],
    source_module: str,
) -> dict[str, Any]:
    args = tool_args or {}
    if tool_name == "capture_inquiry":
        return _capture_inquiry(
            user_id=user_id,
            session_id=session_id,
            block_id=block_id,
            args=args,
            source_module=source_module,
        )
    if tool_name == "flag_sensitive":
        return {
            "status": "ok",
            "tool": "flag_sensitive",
            "category": args.get("category"),
            "severity": args.get("severity"),
            "suppress_llm": True,
        }
    if tool_name == "update_event_draft":
        return _update_event_draft(session_ctx=session_ctx, args=args)
    if tool_name == "publish_activity":
        return _publish_activity(
            session_ctx=session_ctx,
            args=args,
            purpose=purpose,
            user_id=user_id,
            user_jwt=user_jwt,
        )
    if tool_name == "send_nudge":
        return _send_nudge(user_jwt=user_jwt, args=args)
    if tool_name == "propose_intro":
        return _propose_intro(user_jwt=user_jwt, args=args)
    if tool_name == "propose_cohost":
        return _propose_cohost(
            user_jwt=user_jwt,
            args=args,
            session_id=session_id,
            session_ctx=session_ctx,
        )
    if tool_name == "update_relationship_tier":
        return _update_relationship_tier(user_jwt=user_jwt, args=args)
    if tool_name == "recall":
        return execute_recall_tool(
            user_id=user_id,
            block_id=block_id,
            args=args,
        )
    return {"status": "error", "tool": tool_name, "reason": "unknown_tool"}


def _require_jwt(user_jwt: str | None) -> str:
    if not user_jwt:
        return ""
    return user_jwt


def _send_nudge(*, user_jwt: str | None, args: dict[str, Any]) -> dict[str, Any]:
    jwt = _require_jwt(user_jwt)
    if not jwt:
        return {"status": "error", "tool": "send_nudge", "reason": "auth_required"}
    recipient = args.get("to_user_id") or args.get("recipient_id")
    if not recipient:
        return {"status": "error", "tool": "send_nudge", "reason": "to_user_id_required"}
    context_msg = args.get("context_message")
    try:
        nudge_id = call_rpc(
            jwt,
            "send_nudge",
            {
                "p_recipient_id": str(recipient),
                "p_context_message": context_msg,
            },
        )
        return {
            "status": "ok",
            "tool": "send_nudge",
            "nudge_id": nudge_id,
            "to_user_id": str(recipient),
        }
    except HTTPException as exc:
        return {"status": "error", "tool": "send_nudge", "reason": exc.detail}
    except Exception as exc:
        return {"status": "error", "tool": "send_nudge", "reason": str(exc)}


def _propose_intro(*, user_jwt: str | None, args: dict[str, Any]) -> dict[str, Any]:
    jwt = _require_jwt(user_jwt)
    if not jwt:
        return {"status": "error", "tool": "propose_intro", "reason": "auth_required"}
    candidate = args.get("other_user_id") or args.get("candidate_user_id")
    reason = str(args.get("match_reason") or args.get("reason") or "").strip()
    if not candidate:
        return {"status": "error", "tool": "propose_intro", "reason": "other_user_id_required"}
    if len(reason) < 10:
        return {"status": "error", "tool": "propose_intro", "reason": "match_reason_too_short"}
    dimensions = args.get("shared_dimensions") or []
    if not isinstance(dimensions, list):
        dimensions = []
    try:
        intro_id = call_rpc(
            jwt,
            "propose_intro",
            {
                "p_candidate_id": str(candidate),
                "p_match_reason": reason[:280],
                "p_shared_dimensions": [str(d)[:64] for d in dimensions[:8]],
                "p_match_score": args.get("match_score"),
                "p_joint_moment_id": args.get("joint_moment_id"),
            },
        )
        return {
            "status": "ok",
            "tool": "propose_intro",
            "intro_id": intro_id,
            "candidate_user_id": str(candidate),
        }
    except HTTPException as exc:
        return {"status": "error", "tool": "propose_intro", "reason": exc.detail}
    except Exception as exc:
        return {"status": "error", "tool": "propose_intro", "reason": str(exc)}


def _propose_cohost(
    *,
    user_jwt: str | None,
    args: dict[str, Any],
    session_id: str,
    session_ctx: dict[str, Any],
) -> dict[str, Any]:
    jwt = _require_jwt(user_jwt)
    if not jwt:
        return {"status": "error", "tool": "propose_cohost", "reason": "auth_required"}
    candidate = args.get("candidate_user_id")
    reason = str(args.get("overlap_reason") or args.get("reason") or "").strip()
    if not candidate:
        return {"status": "error", "tool": "propose_cohost", "reason": "candidate_user_id_required"}
    if len(reason) < 10:
        return {"status": "error", "tool": "propose_cohost", "reason": "overlap_reason_too_short"}
    try:
        invite_id = call_rpc(
            jwt,
            "propose_cohost",
            {
                "p_candidate_id": str(candidate),
                "p_overlap_reason": reason[:280],
                "p_event_id": args.get("activity_id") or args.get("event_id"),
                "p_session_id": session_id,
            },
        )
        session_ctx["pending_cohost_id"] = str(candidate)
        session_ctx["pending_cohost_invite_id"] = invite_id
        return {
            "status": "ok",
            "tool": "propose_cohost",
            "invite_id": invite_id,
            "candidate_user_id": str(candidate),
            "note": "candidate_must_accept_before_publish",
        }
    except HTTPException as exc:
        return {"status": "error", "tool": "propose_cohost", "reason": exc.detail}
    except Exception as exc:
        return {"status": "error", "tool": "propose_cohost", "reason": str(exc)}


def _update_relationship_tier(*, user_jwt: str | None, args: dict[str, Any]) -> dict[str, Any]:
    """System-driven tier promotion — requires trigger_event + proof_id."""
    jwt = _require_jwt(user_jwt)
    if not jwt:
        return {"status": "error", "tool": "update_relationship_tier", "reason": "auth_required"}
    other = args.get("other_user_id")
    trigger = args.get("trigger_event") or args.get("new_tier_trigger")
    if not other or not trigger:
        return {
            "status": "blocked",
            "tool": "update_relationship_tier",
            "reason": "trigger_and_other_user_required",
        }
    allowed = {"nudge_sent", "nudge_accepted", "intro_accepted", "rsvp_attended_same_event"}
    if str(trigger) not in allowed:
        return {"status": "blocked", "tool": "update_relationship_tier", "reason": "invalid_trigger"}
    try:
        new_tier = call_rpc(
            jwt,
            "promote_relationship_tier",
            {
                "p_other_user_id": str(other),
                "p_trigger": str(trigger),
                "p_proof_id": args.get("proof_id") or args.get("trigger_event_id"),
            },
        )
        return {
            "status": "ok",
            "tool": "update_relationship_tier",
            "other_user_id": str(other),
            "new_tier": new_tier,
        }
    except HTTPException as exc:
        return {"status": "error", "tool": "update_relationship_tier", "reason": exc.detail}
    except Exception as exc:
        return {"status": "error", "tool": "update_relationship_tier", "reason": str(exc)}


def _capture_inquiry(
    *,
    user_id: str,
    session_id: str,
    block_id: str | None,
    args: dict[str, Any],
    source_module: str,
) -> dict[str, Any]:
    raw = str(args.get("raw_query") or args.get("free_text") or "").strip()
    if not raw:
        return {"status": "error", "tool": "capture_inquiry", "reason": "raw_query_required"}
    category = str(args.get("extracted_category") or args.get("category") or "other")[:120]
    sentiment = str(args.get("sentiment") or "neutral")[:32]
    urgency = str(args.get("urgency") or "low")[:16]
    opt_in = bool(args.get("opt_in_followup", False))
    sensitive = bool(args.get("sensitive_flag", False))

    embedding: list[float] | None = None
    try:
        embedding = vertex_embed(f"{category}: {raw}")
    except Exception:
        embedding = None

    row: dict[str, Any] = {
        "user_id": user_id,
        "block_id": block_id,
        "session_id": session_id,
        "category": category,
        "free_text": raw[:2000],
        "urgency": urgency,
        "sentiment": sentiment,
        "opt_in_followup": opt_in,
        "source_module": source_module[:64],
        "sensitive_flag": sensitive,
        "status": "open",
    }
    if embedding:
        row["embedding"] = embedding

    sb = service_client()
    res = sb.table("inquiry_signals").insert(row).execute()
    inquiry_id = None
    if res.data:
        inquiry_id = res.data[0].get("id")

    return {
        "status": "ok",
        "tool": "capture_inquiry",
        "inquiry_id": inquiry_id,
        "category": category,
    }


def _accepted_cohost_id(user_id: str, candidate_id: str | None) -> str | None:
    if not candidate_id:
        return None
    try:
        sb = service_client()
        res = (
            sb.table("event_cohost_invites")
            .select("id")
            .eq("host_id", user_id)
            .eq("candidate_id", str(candidate_id))
            .eq("status", "accepted")
            .limit(1)
            .execute()
        )
        if res.data:
            return str(candidate_id)
    except Exception:
        return None
    return None


def _draft_to_model(draft: dict[str, Any]) -> EventDraft:
    return EventDraft(
        title=draft.get("title"),
        description=draft.get("description"),
        venue_name=draft.get("venue_name"),
        starts_at=draft.get("starts_at"),
        ends_at=draft.get("ends_at"),
        duration_minutes=draft.get("duration_minutes"),
        max_attendees=draft.get("max_attendees"),
        cohort_tags=draft.get("cohort_tags") or [],
        missing=draft.get("missing") or [],
    )


def _update_event_draft(*, session_ctx: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    merged = merged_event_draft(session_ctx, args)
    missing = event_missing_slots(merged)
    merged["missing"] = missing
    session_ctx["event_draft"] = merged
    return {
        "status": "ok",
        "tool": "update_event_draft",
        "event_draft": merged,
        "missing_slots": missing,
        "ready": len(missing) == 0,
    }


def _publish_activity(
    *,
    session_ctx: dict[str, Any],
    args: dict[str, Any],
    purpose: str,
    user_id: str,
    user_jwt: str | None,
) -> dict[str, Any]:
    if purpose != "event_draft":
        return {"status": "blocked", "tool": "publish_activity", "reason": "wrong_session_purpose"}

    draft = merged_event_draft(session_ctx, args)
    missing = event_missing_slots(draft)
    if missing:
        return {
            "status": "blocked",
            "tool": "publish_activity",
            "reason": "slots_missing",
            "missing_slots": missing,
            "event_draft": draft,
        }

    if args.get("user_confirmed"):
        jwt = _require_jwt(user_jwt)
        if not jwt:
            return {"status": "error", "tool": "publish_activity", "reason": "auth_required"}
        session_ctx["event_draft"] = draft
        try:
            cohost_id = _accepted_cohost_id(user_id, session_ctx.get("pending_cohost_id"))
            event_id = publish_event(
                user_id,
                jwt,
                _draft_to_model(draft),
                cohost_id=cohost_id,
            )
        except HTTPException as exc:
            if exc.detail == "phone_not_verified":
                return {
                    "status": "blocked",
                    "tool": "publish_activity",
                    "reason": "phone_not_verified",
                    "event_draft": draft,
                }
            return {
                "status": "error",
                "tool": "publish_activity",
                "reason": str(exc.detail),
                "event_draft": draft,
            }
        except Exception as exc:
            return {
                "status": "error",
                "tool": "publish_activity",
                "reason": str(exc),
                "event_draft": draft,
            }
        session_ctx.pop("pending_confirmation", None)
        return {
            "status": "ok",
            "tool": "publish_activity",
            "published": True,
            "event_id": event_id,
            "event_draft": draft,
        }

    session_ctx["event_draft"] = draft
    return {
        "status": "ok",
        "tool": "publish_activity",
        "needs_user_confirmation": True,
        "event_draft": draft,
        "confirmation_prompt": _confirmation_echo(draft),
        "pending_cohost_id": session_ctx.get("pending_cohost_id"),
    }


def _confirmation_echo(draft: dict[str, Any]) -> str:
    title = draft.get("title") or "your event"
    when = draft.get("starts_at") or "TBD"
    where = draft.get("venue_name") or "your place"
    return f"Got it: {title} · {when} · {where}. *Publish?*"
