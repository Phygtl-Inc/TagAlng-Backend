"""Unified Lana: discovery gates first (code), orchestrator for everything else (AI)."""

from __future__ import annotations

from typing import Any

from app.discovery_route import handle_discovery_turn
from app.lana_dispatch import lana_unified_turn
from app.lana_ui import sanitize_assistant_message
from app.lana_paths import unified_rules_first_enabled
from app.orchestrator.pipeline import run_turn
from app.turn_timing import TurnTimer


def run_lana_unified_pipeline(
    *,
    user_id: str,
    session_id: str,
    history: list[dict[str, Any]],
    user_message: str,
    session_ctx: dict[str, Any],
    user_jwt: str,
    phone_verified: bool,
    home_block_id: str | None,
    is_anonymous: bool,
    persisted_core: dict[str, Any] | None = None,
    timer: TurnTimer | None = None,
    use_orchestrator: bool = True,
) -> tuple[str, str, dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    """
    1. Discovery/auth gates (code) — ZIP, identity, preview, verify; orchestrator off.
    2. Orchestrator (AI) — companionship, hosting; enforce still applies discovery overrides.
    """
    timer = timer or TurnTimer()
    session_ctx = {
        **session_ctx,
        "phone_verified": phone_verified,
        "unified_mode": True,
    }

    if unified_rules_first_enabled():
        discovery = handle_discovery_turn(
            user_message,
            session_ctx=session_ctx,
            user_jwt=user_jwt,
            phone_verified=phone_verified,
            home_block_id=home_block_id,
            is_anonymous=is_anonymous,
            history=history,
            user_id=user_id,
            timer=timer,
        )
        if discovery is not None:
            reply, ctx, routing, peers = discovery
            reply = sanitize_assistant_message(reply)
            ctx["last_routing"] = routing
            ctx["_orchestrator_turn"] = False
            ctx["timing_ms"] = timer.to_dict()
            if ctx.get("activity_previews"):
                ctx["peer_matches"] = []
            elif peers:
                ctx["peer_matches"] = peers
                ctx.pop("activity_previews", None)
            elif "peer_matches" not in ctx:
                ctx["peer_matches"] = []
            ui = {
                "bucket": None,
                "focus_phrase": None,
                "highlights": [],
            }
            status = "ready_to_complete" if ctx.get("ready_to_complete") else "continue"
            return reply, status, ctx, ui, ctx.get("event_draft")

    if use_orchestrator:
        reply, status, turn_ctx, ui, draft = run_turn(
            user_id=user_id,
            session_id=session_id,
            purpose="lana",
            history=history,
            user_message=user_message,
            session_ctx=session_ctx,
            user_jwt=user_jwt,
            persisted_core=persisted_core,
            timer=timer,
        )
        turn_ctx["_orchestrator_turn"] = True
        return reply, status, turn_ctx, ui, draft

    reply, status, turn_ctx, ui_raw, draft_raw = lana_unified_turn(
        history=history,
        user_message=user_message,
        session_ctx=session_ctx,
        user_jwt=user_jwt,
        phone_verified=phone_verified,
        home_block_id=home_block_id,
        is_anonymous=is_anonymous,
    )
    turn_ctx["_orchestrator_turn"] = False
    peers = turn_ctx.pop("peer_matches", None)
    if peers:
        turn_ctx["peer_matches"] = peers
    return reply, status, turn_ctx, ui_raw, draft_raw
