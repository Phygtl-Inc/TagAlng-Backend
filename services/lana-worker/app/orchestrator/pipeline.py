import os
from typing import Any

from app.orchestrator.audit import log_turn
from app.orchestrator.llm import llm_configured
from app.orchestrator.enforce import enforce_routing, should_execute_tool
from app.orchestrator.guardrails import check_refusal_without_capture, run_input_rails, scrub_pii
from app.orchestrator.memory import (
    apply_core_patch,
    build_core_block,
    strip_ephemeral,
)
from app.orchestrator.lana_chat_fast_path import (
    lana_chat_routing_from_discovery,
    should_skip_lana_router,
    discovery_slots_for_message,
)
from app.orchestrator.recall import prefetch_turn_memories
from app.orchestrator.router import route_turn
from app.orchestrator.synthesizer import synthesize_opening, synthesize_turn
from app.orchestrator.tools import execute_tool
from app.context import load_user_context
from app.turn_timing import TurnTimer
from app.guest_capabilities import wants_host_activity
from app.orchestrator.slots import has_partial_event_args
from app.vertex_event import reconcile_orchestrator_event_turn


def orchestrator_enabled() -> bool:
    flag = os.environ.get("LANA_ORCHESTRATOR", "auto").strip().lower()
    if flag in ("0", "false", "off", "legacy"):
        return False
    if flag in ("1", "true", "on"):
        return llm_configured()
    return llm_configured()


def run_opening(
    *,
    user_id: str,
    purpose: str,
    session_id: str,
) -> tuple[str, str, dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    ctx_pack = load_user_context(user_id)
    core = build_core_block(
        user_id=user_id,
        session_id=session_id,
        purpose=purpose,
        ctx_pack=ctx_pack,
    )
    purpose_ids = ctx_pack.get("event_purpose_ids") or []
    reply, status, session_ctx, ui, draft = synthesize_opening(
        purpose=purpose,
        core_block=core,
        purpose_ids=purpose_ids if purpose == "event_draft" else None,
    )
    core = apply_core_patch(core, session_ctx.pop("core_patch", None))
    session_ctx["core_block"] = strip_ephemeral(core)
    log_turn(
        session_id=session_id,
        user_id=user_id,
        event_type="opening",
        module="companionship",
        utterance=None,
        response=reply,
        routing={"outcome": "R"},
    )
    return reply, status, session_ctx, ui, draft


def run_turn(
    *,
    user_id: str,
    session_id: str,
    purpose: str,
    history: list[dict[str, Any]],
    user_message: str,
    session_ctx: dict[str, Any],
    user_jwt: str | None = None,
    persisted_core: dict[str, Any] | None = None,
    timer: TurnTimer | None = None,
) -> tuple[str, str, dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    timer = timer or TurnTimer()
    with timer.stage("load_user_context"):
        ctx_pack = load_user_context(user_id)
    block_id = ctx_pack.get("home_block_id")
    purpose_ids = ctx_pack.get("event_purpose_ids") or []
    prev_draft = session_ctx.get("event_draft")
    utterance = scrub_pii(user_message.strip())

    with timer.stage("prefetch_memories"):
        prefetched = prefetch_turn_memories(
            user_id=user_id,
            block_id=block_id,
            utterance=utterance,
        )
    with timer.stage("build_core_block"):
        core = build_core_block(
            user_id=user_id,
            session_id=session_id,
            purpose=purpose,
            ctx_pack=ctx_pack,
            session_ctx=session_ctx,
            history=history,
            persisted=persisted_core if isinstance(persisted_core, dict) else None,
            prefetched=prefetched,
        )

    rails = run_input_rails(utterance)
    capture_fired = False
    tool_result: dict[str, Any] | None = None

    if rails.safety_triggered:
        routing = {
            "outcome": "T",
            "intent_class": "companionship",
            "confidence": 1.0,
            "tool_to_call": "flag_sensitive",
            "tool_args": {"category": rails.category, "severity": rails.severity},
        }
        tool_result = execute_tool(
            tool_name="flag_sensitive",
            tool_args=routing["tool_args"],
            user_id=user_id,
            user_jwt=user_jwt,
            session_id=session_id,
            block_id=block_id,
            purpose=purpose,
            session_ctx=session_ctx,
            source_module="guardrails",
        )
        reply = rails.template_response or "I'm here with you."
        status = "continue"
        ui = {"bucket": None, "focus_phrase": None, "highlights": []}
        core = strip_ephemeral(core)
        out_ctx = {**session_ctx, "last_status": status, "core_block": core}
        log_turn(
            session_id=session_id,
            user_id=user_id,
            event_type="safety_gate",
            module="guardrails",
            utterance=utterance,
            response=reply,
            guardrail_result={"safety": True, "category": rails.category},
            routing=routing,
        )
        return reply, status, out_ctx, ui, prev_draft if purpose == "event_draft" else None

    skip_router = should_skip_lana_router(
        purpose=purpose,
        utterance=utterance,
        session_ctx=session_ctx,
    )
    if skip_router:
        slots = discovery_slots_for_message(session_ctx, utterance) or {}
        routing = lana_chat_routing_from_discovery(slots)
        timer.set_count("llm_router_skipped", 1)
    else:
        routing = route_turn(
            purpose=purpose,
            utterance=utterance,
            core_block=core,
            history=history,
            guardrail_flags=rails.flags,
            timer=timer,
        )
    routing = enforce_routing(
        routing,
        purpose=purpose,
        utterance=utterance,
        session_ctx=session_ctx,
        history=history,
        home_block_id=block_id,
    )
    if skip_router:
        prior = list(routing.get("enforce_notes") or [])
        if "discovery_chat_fast_path" not in prior:
            routing = {**routing, "enforce_notes": prior + ["discovery_chat_fast_path"]}

    if should_execute_tool(routing):
        with timer.stage("execute_tool"):
            tool_result = execute_tool(
                tool_name=str(routing["tool_to_call"]),
                tool_args=routing.get("tool_args"),
                user_id=user_id,
                user_jwt=user_jwt,
                session_id=session_id,
                block_id=block_id,
                purpose=purpose,
                session_ctx=session_ctx,
                source_module=str(routing.get("intent_class", "companionship")),
            )
        if routing["tool_to_call"] == "capture_inquiry":
            capture_fired = True
            if tool_result and tool_result.get("inquiry_id"):
                session_ctx["last_captured_inquiry_id"] = tool_result["inquiry_id"]
        if tool_result and tool_result.get("needs_user_confirmation"):
            session_ctx["pending_confirmation"] = tool_result.get("confirmation_prompt")
        if tool_result and tool_result.get("published"):
            session_ctx.pop("pending_confirmation", None)
            session_ctx["event_id"] = tool_result.get("event_id")
            session_ctx["last_status"] = "ready_to_complete"
        if tool_result and tool_result.get("peer_matches"):
            session_ctx["peer_matches"] = tool_result["peer_matches"]
        if tool_result and tool_result.get("routing_phase"):
            session_ctx["routing_phase"] = tool_result["routing_phase"]
        if tool_result and tool_result.get("block_id"):
            session_ctx["preview_block_id"] = tool_result["block_id"]
        if (
            routing.get("tool_to_call") == "propose_intro"
            and tool_result
            and tool_result.get("status") == "ok"
            and tool_result.get("intro_id")
        ):
            from app.intro_proposal import stamp_intro_proposal_ctx

            candidate_id = str(tool_result.get("candidate_user_id") or "")
            peer = next(
                (
                    p
                    for p in (session_ctx.get("peer_matches") or [])
                    if isinstance(p, dict)
                    and str(p.get("peer_user_id") or "") == candidate_id
                ),
                {"peer_user_id": candidate_id},
            )
            stamp_intro_proposal_ctx(session_ctx, intro=tool_result, peer=peer)
        if (
            routing.get("tool_to_call") == "list_my_intros"
            and tool_result
            and tool_result.get("status") == "ok"
        ):
            from app.intro_list import stamp_pending_intros_ctx

            raw_rows = tool_result.get("intros") or []
            stamp_pending_intros_ctx(
                session_ctx,
                [r for r in raw_rows if isinstance(r, dict)],
            )

    reply, status, synth_ctx, ui, draft = synthesize_turn(
        purpose=purpose,
        utterance=utterance,
        routing=routing,
        core_block=core,
        history=history,
        tool_result=tool_result,
        prev_draft=prev_draft,
        purpose_ids=purpose_ids if purpose == "event_draft" else None,
        session_ctx=session_ctx,
        timer=timer,
    )

    if not check_refusal_without_capture(reply, capture_fired):
        routing_retry = {**routing, "outcome": "C", "tool_to_call": "capture_inquiry"}
        if not capture_fired:
            tool_result = execute_tool(
                tool_name="capture_inquiry",
                tool_args={
                    "raw_query": utterance,
                    "extracted_category": "refusal_repair",
                    "sentiment": routing.get("sentiment", "neutral"),
                },
                user_id=user_id,
                user_jwt=user_jwt,
                session_id=session_id,
                block_id=block_id,
                purpose=purpose,
                session_ctx=session_ctx,
                source_module="guardrails",
            )
            capture_fired = True
            if tool_result and tool_result.get("inquiry_id"):
                session_ctx["last_captured_inquiry_id"] = tool_result["inquiry_id"]
        reply, status, synth_ctx, ui, draft = synthesize_turn(
            purpose=purpose,
            utterance=utterance,
            routing=routing_retry,
            core_block=core,
            history=history,
            tool_result=tool_result,
            prev_draft=prev_draft,
            purpose_ids=purpose_ids if purpose == "event_draft" else None,
            session_ctx=session_ctx,
            timer=timer,
        )

    if _should_reconcile_event_turn(
        purpose=purpose,
        utterance=utterance,
        session_ctx=session_ctx,
        routing=routing,
        tool_result=tool_result,
        prev_draft=prev_draft,
    ):
        status, ui, draft = reconcile_orchestrator_event_turn(
            ctx_pack=ctx_pack,
            history=history,
            utterance=utterance,
            prev_draft=prev_draft,
            synth_draft=draft,
            ui=ui,
            status=status,
            tool_result=tool_result,
            pending_confirmation=bool(
                synth_ctx.get("pending_confirmation") or session_ctx.get("pending_confirmation")
            ),
            timer=timer,
        )
        synth_ctx["event_draft"] = draft
        synth_ctx["last_status"] = status

    core = apply_core_patch(core, synth_ctx.pop("core_patch", None))
    merged_ctx = {
        **session_ctx,
        **synth_ctx,
        "core_block": strip_ephemeral(core),
        "last_routing": routing,
        "timing_ms": timer.to_dict(),
    }
    if draft:
        merged_ctx["event_draft"] = draft

    if purpose == "lana":
        _stamp_lana_unified_fields(merged_ctx, routing=routing, tool_result=tool_result)

    log_turn(
        session_id=session_id,
        user_id=user_id,
        event_type="turn",
        module=str(routing.get("intent_class")),
        utterance=utterance,
        response=reply,
        guardrail_result=rails.flags,
        routing={
            **routing,
            "capture_fired": capture_fired,
            "recall_prefetch_count": len(prefetched),
        },
    )

    return reply, status, merged_ctx, ui, draft


def _should_reconcile_event_turn(
    *,
    purpose: str,
    utterance: str,
    session_ctx: dict[str, Any],
    routing: dict[str, Any],
    tool_result: dict[str, Any] | None,
    prev_draft: dict[str, Any] | None,
) -> bool:
    if purpose == "event_draft":
        return True
    if purpose != "lana":
        return False
    if wants_host_activity(utterance):
        return True
    if session_ctx.get("pending_confirmation"):
        return True
    if tool_result and tool_result.get("tool") in ("publish_activity", "update_event_draft"):
        return True
    if has_partial_event_args(None, session_ctx):
        return True
    if isinstance(prev_draft, dict) and has_partial_event_args(prev_draft, session_ctx):
        return True
    if routing.get("intent_class") == "activity" and routing.get("tool_to_call") in (
        "publish_activity",
        "update_event_draft",
        "propose_cohost",
    ):
        return True
    return False


def _stamp_lana_unified_fields(
    ctx: dict[str, Any],
    *,
    routing: dict[str, Any],
    tool_result: dict[str, Any] | None,
) -> None:
    notes = list(routing.get("enforce_notes") or [])
    if routing.get("intent_class") == "discovery" or "discovery_find_peers" in notes:
        ctx["active_intent"] = "discovery.find_peers"
        ctx.setdefault("unified_mode", True)
    if "discovery_need_zip" in notes or (tool_result and tool_result.get("reason") == "need_zip"):
        ctx["routing_phase"] = "need_zip"
    elif "discovery_need_identity" in notes or (
        tool_result and tool_result.get("reason") == "need_identity"
    ):
        ctx["routing_phase"] = "need_identity"
    elif tool_result and tool_result.get("tool") == "find_peers" and tool_result.get("status") == "ok":
        ctx["routing_phase"] = "preview"
