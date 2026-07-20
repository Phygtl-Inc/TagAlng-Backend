import os
import re
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from app.orchestrator.audit import log_turn
from app.orchestrator.llm import llm_configured
from app.orchestrator.enforce import enforce_routing, should_execute_tool
from app.orchestrator.guardrails import check_refusal_without_capture, scrub_pii
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
from app.orchestrator.progress import COMPOSING, READING, emit_stage, label_for_routing
from app.orchestrator.recall import prefetch_turn_memories
from app.orchestrator.router import route_turn
from app.orchestrator.synthesizer import synthesize_opening, synthesize_turn
from app.orchestrator.tools import execute_tool
from app.context import load_user_context
from app.turn_surfaces import clear_turn_surfaces
from app.turn_timing import TurnTimer
from app.guest_capabilities import wants_host_activity
from app.orchestrator.slots import has_partial_event_args
from app.vertex_event import reconcile_orchestrator_event_turn


# Background loader that overlaps the turn's DB context reads with the discovery
# classifier's LLM call. Both load_user_context and the memory prefetch depend only
# on user_id + the raw message — neither needs the classifier's verdict — so a turn
# that starts them here has them finished by the time run_turn would have run them
# serially (~5s of DB wall-clock hidden under a ~4s LLM call).
_PRELOAD_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ctx-prefetch")


def start_ctx_prefetch(*, user_id: str, user_message: str) -> Future:
    """Kick off (ctx_pack, prefetched_memories, utterance) in the background.

    The future never raises on DB/embed failure for the memory half (prefetch
    swallows its own errors); a load_user_context failure surfaces on .result(),
    which run_turn catches and retries inline — same failure surface as before.
    """
    utterance = scrub_pii(user_message.strip())

    def _load() -> tuple[dict[str, Any], list[dict[str, Any]], str]:
        ctx_pack = load_user_context(user_id)
        prefetched = prefetch_turn_memories(
            user_id=user_id,
            block_id=ctx_pack.get("home_block_id"),
            utterance=utterance,
        )
        return ctx_pack, prefetched, utterance

    return _PRELOAD_POOL.submit(_load)


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
    ctx_prefetch: Future | None = None,
    host_fast_path: bool = False,
) -> tuple[str, str, dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    timer = timer or TurnTimer()
    # Seed only — the unified pipeline (or the discovery classifier's authored stage)
    # usually already put a label up; never regress from a contextual one to a generic.
    timer.emit_seed(READING)
    clear_turn_surfaces(session_ctx)
    # Session-sticky language (idempotent when the unified pipeline already resolved it
    # this turn; this covers the profile_intake / event_draft purposes called directly).
    from app.i18n import resolve_session_lang

    # Chip-tap language pin (see lana_unified_pipeline): an offered chip payload is
    # app-authored English — never let it flip the session language.
    _offered = session_ctx.get("_offered_chip_msgs") or []
    session_ctx["_lang_pinned_turn"] = bool(
        str(user_message or "").strip() and str(user_message or "").strip() in _offered
    )
    if not session_ctx["_lang_pinned_turn"]:
        resolve_session_lang(session_ctx, user_message)
    utterance = scrub_pii(user_message.strip())
    ctx_pack: dict[str, Any] | None = None
    prefetched: list[dict[str, Any]] | None = None
    if ctx_prefetch is not None:
        # Context was loaded in the background while the discovery classifier ran —
        # this join is near-free on the happy path. On any failure, fall through to
        # the inline loads below (same failure surface as before the overlap).
        with timer.stage("ctx_prefetch_join"):
            try:
                ctx_pack, prefetched, prefetch_utterance = ctx_prefetch.result()
            except Exception:  # noqa: BLE001 — retried inline below
                import logging

                logging.getLogger(__name__).exception("ctx_prefetch_failed")
                ctx_pack, prefetched, prefetch_utterance = None, None, None
        if ctx_pack is not None and prefetch_utterance != utterance:
            # The message was rewritten mid-pipeline (rapport hand-off) — the memories
            # were fetched for the old text. ctx_pack is message-independent; refresh
            # only the recall.
            prefetched = None
    if ctx_pack is None:
        with timer.stage("load_user_context"):
            ctx_pack = load_user_context(user_id)
    block_id = ctx_pack.get("home_block_id")
    purpose_ids = ctx_pack.get("event_purpose_ids") or []
    prev_draft = session_ctx.get("event_draft")

    if host_fast_path:
        # Host turns never reach the router/synthesizer (see below), so the memory
        # prefetch and core rebuild would be dead weight; carry the persisted core
        # through unchanged.
        core = dict(persisted_core) if isinstance(persisted_core, dict) else {}
    else:
        if prefetched is None:
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

    # Crisis (self-harm / DV / emotional distress) is AI-detected upstream — the discovery
    # classifier flags goal=crisis and _respond_crisis answers with empathy + resources
    # before this pipeline runs. The old regex input rail lived here; it was removed because
    # its five keyword patterns missed real distress while the classifier reads meaning.
    capture_fired = False
    tool_result: dict[str, Any] | None = None

    if host_fast_path:
        # The unified pipeline's host stage machine owns this turn end-to-end — it
        # overwrites the routing stamp, the reply, and the UI with its own (see the
        # host block in lana_unified_pipeline). The router + synthesizer calls here
        # were ~4s of discarded wall-clock per host turn, so jump straight to the
        # event reconcile below — the only orchestrator output a host turn consumes.
        routing = {
            "outcome": "R",
            "intent_class": "hosting",
            "confidence": 1.0,
            "tool_to_call": None,
            "enforce_notes": ["host_fast_path"],
        }
        timer.set_count("llm_router_skipped", 1)
        emit_stage(timer, [], 0, "Setting up your meet…")
        reply = ""
        status = "continue"
        synth_ctx: dict[str, Any] = {}
        ui: dict[str, Any] = {"bucket": None, "focus_phrase": None, "highlights": []}
        draft: dict[str, Any] | None = None
    else:
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
                guardrail_flags={"rail": "ok"},
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

        # Routing is decided — the label can now name the real work. The router authors the
        # stage copy itself (grounded in the user's ask, session language); the static map
        # is the fallback when the plan is missing (skip_router fast path, malformed output).
        progress_plan = list(routing.get("progress") or [])
        emit_stage(timer, progress_plan, 0, label_for_routing(routing))

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
            if tool_result and tool_result.get("requires_phone_verification"):
                session_ctx["requires_phone_verification"] = True
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

        emit_stage(timer, progress_plan, 1, COMPOSING)
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
        guardrail_result={"rail": "ok"},
        routing={
            **routing,
            "capture_fired": capture_fired,
            "recall_prefetch_count": len(prefetched or []),
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
    # Sticky host mode: once we're collecting an event in-chat, extract details from
    # every turn ("Friday evening", "the playground") so the draft accumulates even
    # when the utterance has no host keyword and the router calls no tool.
    if session_ctx.get("event_host_active"):
        return True
    if session_ctx.get("pending_confirmation"):
        return True
    if has_partial_event_args(None, session_ctx):
        return True
    if isinstance(prev_draft, dict) and has_partial_event_args(prev_draft, session_ctx):
        return True
    # Below this line every trigger can SPAWN a brand-new draft from heuristics tuned on
    # English (the host regex, the router's intent guess). QA 2026-07-08: "oi Lana, sou
    # brasileira… quero conhecer outras mães" spawned a spurious empty event_draft — the
    # PT text confused classification. With no prior host state, a confidently non-English
    # message must carry an EXPLICIT hosting phrase in its own language to start a draft;
    # otherwise never extract. (English messages are unaffected.)
    from app.i18n import detect_language

    if detect_language(utterance) in ("es", "pt") and not _non_english_hosting_phrase(
        utterance
    ):
        return False
    if wants_host_activity(utterance):
        return True
    if tool_result and tool_result.get("tool") in ("publish_activity", "update_event_draft"):
        return True
    if routing.get("intent_class") == "activity" and routing.get("tool_to_call") in (
        "publish_activity",
        "update_event_draft",
        "propose_cohost",
    ):
        return True
    return False


# Explicit es/pt hosting verbs + event nouns — the deterministic opt-in that lets a
# Spanish/Portuguese speaker still START a host draft ("quero organizar um café",
# "quiero organizar una reunión") while a greeting can't spawn one.
_NON_EN_HOSTING_RE = re.compile(
    r"\b(?:organizar|organizo|hospedar|criar|crear|armar|montar|planear|planejar|hacer|fazer)\b"
    r".{0,60}\b(?:evento|reuni[oó]n|encontro|encuentro|caf[eé]|festa|fiesta|picnic|piquenique|"
    r"churrasco|asado|brunch|meetup|playdate|encontrinho)\b",
    re.IGNORECASE,
)


def _non_english_hosting_phrase(utterance: str) -> bool:
    return bool(_NON_EN_HOSTING_RE.search(str(utterance or "")))


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
        if tool_result.get("requires_phone_verification"):
            ctx["requires_phone_verification"] = True

    outcome = str(routing.get("outcome") or "")
    intent = str(routing.get("intent_class") or "")
    tool = routing.get("tool_to_call")
    peers_this_turn = bool(
        tool_result
        and tool_result.get("peer_matches")
        and tool == "find_peers"
    )
    if outcome == "R" or intent in ("companionship", "meta", "identity"):
        if not ctx.get("pending_intro_offer") and not ctx.get("intro_proposal"):
            ctx["peer_matches"] = []
        ctx.pop("requires_phone_verification", None)
    elif not peers_this_turn and outcome != "T":
        if not ctx.get("pending_intro_offer") and not ctx.get("intro_proposal"):
            ctx["peer_matches"] = []
