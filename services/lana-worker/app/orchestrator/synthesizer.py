import json
from typing import Any

from app.context import build_system_prompt, load_prompt
from app.i18n import session_lang, synth_language_directive, t
from app.profile_intake import apply_profile_stop_rules
from app.lana_ui import merge_event_drafts, parse_event_draft, parse_event_turn_ui, parse_turn_ui, finalize_event_draft, sanitize_assistant_message
from app.orchestrator.llm import llm_json, router_model, synthesizer_model
from app.orchestrator.memory import format_core_block, format_recent_turns, format_recall_memories
from app.turn_timing import TurnTimer
from app.vertex_event import EVENT_BUCKET_GUIDE


def _format_shown_peer_preview(session_ctx: dict[str, Any] | None) -> str | None:
    """What discovery already returned — so Lana can answer pushback honestly."""
    if not session_ctx:
        return None
    peers = session_ctx.get("peer_matches")
    if not isinstance(peers, list) or not peers:
        return None
    rows: list[dict[str, Any]] = []
    for p in peers[:5]:
        if not isinstance(p, dict):
            continue
        rows.append(
            {
                "label": p.get("matching_peer_label"),
                "preview": bool(p.get("preview", True)),
                "nickname": p.get("nickname") if not p.get("preview") else None,
            }
        )
    if not rows:
        return None
    snippet = str(session_ctx.get("identity_snippet") or "").strip()
    block = str(session_ctx.get("preview_block_label") or session_ctx.get("preview_zip") or "")
    parts = [
        "NEIGHBOR PREVIEW ALREADY SHOWN TO USER (backend result — do not invent others):",
        json.dumps(rows, ensure_ascii=False),
    ]
    if snippet:
        parts.append(f"User matching ask stored: {snippet[:200]}")
    if block:
        parts.append(f"Area context (backstage): {block}")
    parts.append(
        "Preview labels (heritage, Mom, interests) are what those neighbors shared nearby — "
        "use them to answer trait questions (e.g. if label includes Brazilian, say yes). "
        "If labels do not match what the user asked for, say so honestly. "
        "Do NOT claim you cannot see heritage when labels list it. "
        "Do NOT repeat the same bullet list unless user asks to see it again."
    )
    return "\n".join(parts)


def _synth_model(outcome: str, tool_result: dict[str, Any] | None, *, purpose: str = "") -> str:
    """Synth model for tool/hero turns; router model for simple R/A."""
    if purpose in ("event_draft", "lana"):
        return router_model()
    if outcome == "T" and tool_result:
        return synthesizer_model()
    if tool_result and tool_result.get("tool") == "recall":
        return synthesizer_model()
    if outcome == "C":
        return synthesizer_model()
    return router_model()


def synthesize_turn(
    *,
    purpose: str,
    utterance: str,
    routing: dict[str, Any],
    core_block: dict[str, Any],
    history: list[dict[str, Any]],
    tool_result: dict[str, Any] | None,
    prev_draft: dict[str, Any] | None = None,
    purpose_ids: list[str] | None = None,
    session_ctx: dict[str, Any] | None = None,
    timer: TurnTimer | None = None,
) -> tuple[str, str, dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    outcome = routing.get("outcome", "R")
    system = build_system_prompt() + "\n\n---\n\n" + load_prompt("orchestrator_synth.md")

    if purpose == "event_draft":
        schema = _event_synth_schema()
    elif purpose == "lana":
        schema = _lana_synth_schema()
    else:
        schema = _profile_synth_schema()

    payload_parts = [
        format_core_block(core_block),
        f"SESSION PURPOSE: {purpose}",
        f"ROUTER OUTCOME: {outcome}",
        f"ROUTING: {json.dumps(routing)}",
        f"TOOL RESULT: {json.dumps(tool_result or {})}",
    ]
    # Session-sticky language mirroring: one directive, applied to every purpose, so a
    # Brazilian/Spanish-speaking mom gets Lana in her own words (event titles stay as
    # authored). Set by app.i18n.resolve_session_lang at pipeline entry.
    lang = session_lang(session_ctx)
    lang_directive = synth_language_directive(lang) if lang else None
    if lang_directive:
        payload_parts.append(lang_directive)
        # The reply comes back already in the session language — tell the
        # final-mile localizer in main not to render it a second time.
        if isinstance(session_ctx, dict):
            session_ctx["_reply_localized"] = True
    if purpose == "profile_intake" and utterance.strip().startswith("(session start"):
        payload_parts.append(
            'OPENING TURN: First chat line after "Meet Lana". '
            'Say something like: "So — *who are you*, right now?" — warm, one question, invite their story.'
        )
    if purpose == "lana":
        phase = str((session_ctx or {}).get("routing_phase") or "listening")
        notes = routing.get("enforce_notes") or []
        fast_chat = "discovery_chat_fast_path" in notes
        payload_parts.append(
            f"LANA UNIFIED · routing_phase={phase} · enforce_notes={notes}\n"
            + (
                "DISCOVERY ROUTER: companionship chat (not a funnel step). "
                "Answer naturally with full context — profile, memories, preview cards if shown.\n"
                if fast_chat
                else ""
            )
            + "You are Lana, local concierge — warm and natural. Short (1-2 sentences) by default, "
            "but MATCH LENGTH TO THE ASK: when the user asks for detail or an overview ('explain "
            "more', 'in detail', 'what can you do'), answer fully from the CAPABILITIES FACT SHEET "
            "with concrete examples — a short structured paragraph, not a re-worded pitch.\n"
            "- Answer what the user actually asked first (are you real, frustration, small talk, a "
            "complaint). If they repeat a question or say your replies sound canned/hardcoded/"
            "robotic, acknowledge that directly first, then answer DIFFERENTLY: never re-summarize "
            "what RECENT TURNS show you already said — go one level deeper with concrete examples, "
            "or ask which part they want.\n"
            "- If routing_phase is need_zip/need_identity but they did not give ZIP/identity, "
            "respond to their question; gently offer ZIP or one identity line only if natural.\n"
            "- Greetings: answer naturally; mention find neighbors / log in when helpful.\n"
            "- NEVER claim you found peers unless tool_result.peer_matches is non-empty.\n"
            "- Do NOT run a long profile interview; discovery = ZIP then one identity line.\n"
            "- If user wants to meet/find people or says stop asking questions, do NOT keep interviewing — "
            "one short line max; discovery code will ask ZIP or show matches.\n"
            "- NEVER ask 'tap That\\'s me' or ready_to_complete — that is legacy profile intake, not Lana unified.\n"
            "- NEVER re-ask life stage, kids, work, or hobbies if RECENT TURNS already cover them.\n"
            "- After phone verify, discovery code shows matches — do not interview; congratulate briefly only.\n"
            "- discovery_need_zip: ask for 5-digit US ZIP (e.g. 32827).\n"
            "- discovery_need_identity: ask one short line (heritage, life stage, or what they want).\n"
            "- discovery_need_display_name: ask what neighbors should call them (first name).\n"
            "- If peer_matches with preview=true: describe labels only, no names.\n"
            "- When NEIGHBOR PREVIEW ALREADY SHOWN is present, use those exact labels to answer "
            "pushback (e.g. user asked for dads but labels say Mom of toddlers — acknowledge the gap).\n"
            "- If USER has no profile photo yet, you may suggest adding one once (warm, optional). "
            "If they agree, tell them to tap Add photo below — do not ask for a URL.\n"
            "- If routing_phase is await_profile_photo, direct them to the Add photo button.\n"
            "- LINGO: context around you is full of backstage words — 'block', 'match', routing "
            "keys. NEVER repeat them to the user: say 'your area', 'your neighborhood', 'near "
            "you', 'someone to meet'. This applies even when describing what you can do. "
            "(Asking for a ZIP code when you need one is fine — 'block' never is.)\n"
        )
        caps = load_prompt("lana_capabilities.md")
        if caps:
            payload_parts.append(
                "CAPABILITIES FACT SHEET (all true today — the material for any 'what can you "
                "do' / 'explain more' answer; never promise beyond it):\n" + caps
            )
        preview_ctx = _format_shown_peer_preview(session_ctx)
        if preview_ctx:
            payload_parts.append(preview_ctx)
        # NOTE: no "display name MISSING → ask" injection here. The name is collected up
        # front on its own clean turn (discovery's need_display_name gate); tacking it onto
        # an unrelated companionship reply ("Rooting for Colombia… by the way, what should
        # neighbors call you") is exactly what we're avoiding.
    if purpose == "event_draft":
        ids = purpose_ids or []
        payload_parts.append(
            "EVENT HOSTING: You MUST fill event_draft from USER words every turn "
            "(title, starts_at ISO8601, venue_name). Merge with CURRENT EVENT DRAFT in core block.\n"
            + EVENT_BUCKET_GUIDE
            + "\nAllowed cohort_tags: "
            + (", ".join(ids) if ids else "see get_event_purposes")
            + "\nDo NOT say the event is ready unless title, starts_at, and venue_name are in event_draft."
        )
    if tool_result and tool_result.get("tool") == "recall":
        memories = tool_result.get("memories") or []
        payload_parts.append("RECALL RESULTS:\n" + format_recall_memories(memories))
    payload_parts.extend(
        [
            "RECENT TURNS:\n" + format_recent_turns(history, limit=6),
            f"USER MESSAGE:\n{utterance.strip()}",
            "Write Lana's reply. Output ONLY JSON:\n" + schema,
        ]
    )
    payload = "\n\n".join(payload_parts)

    model = _synth_model(outcome, tool_result, purpose=purpose)
    attempts_box: list[int] = []
    if timer:
        with timer.stage("llm_synth"):
            raw = llm_json(
                model=model,
                system=system,
                user_payload=payload,
                max_tokens=2048,
                temperature=0.55,
                llm_attempts=attempts_box,
            )
        if attempts_box:
            timer.set_count("llm_synth_attempts", attempts_box[0])
    else:
        raw = llm_json(
            model=model,
            system=system,
            user_payload=payload,
            max_tokens=2048,
            temperature=0.55,
        )

    if purpose == "event_draft":
        return _parse_event_synth(
            raw,
            prev_draft=prev_draft,
            tool_result=tool_result,
            valid_purpose_ids=set(purpose_ids or []),
        )
    if purpose == "lana":
        return _parse_lana_synth(raw, routing=routing, tool_result=tool_result, lang=lang)
    return _parse_profile_synth(raw, history=history)


def synthesize_opening(
    *,
    purpose: str,
    core_block: dict[str, Any],
    purpose_ids: list[str] | None = None,
    timer: TurnTimer | None = None,
) -> tuple[str, str, dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    utterance = "(session start)"
    routing = {"outcome": "R", "intent_class": "companionship", "confidence": 1.0}
    return synthesize_turn(
        purpose=purpose,
        utterance=utterance,
        routing=routing,
        core_block=core_block,
        history=[],
        tool_result=None,
        prev_draft=None,
        purpose_ids=purpose_ids,
        timer=timer,
    )


def _lana_synth_schema() -> str:
    return """{
  "assistant_message": "warm reply — short by default, fuller when the user asked for detail",
  "status": "continue",
  "ui": { "bucket": null, "focus_phrase": null, "highlights": [] }
}

Rules: assistant_message is short (1-2 sentences) UNLESS the user asked for detail or repeated
a question — then give a fuller structured answer (still one JSON string). status is continue."""


def _parse_lana_synth(
    raw: dict[str, Any],
    *,
    routing: dict[str, Any],
    tool_result: dict[str, Any] | None,
    lang: str | None = None,
) -> tuple[str, str, dict[str, Any], dict[str, Any], None]:
    assistant_message = str(raw.get("assistant_message", "")).strip()[:1200]
    notes = list(routing.get("enforce_notes") or [])
    ui = parse_turn_ui(raw)
    if tool_result and tool_result.get("peer_matches") and tool_result.get("summary"):
        assistant_message = str(tool_result["summary"])[:1200]
    elif assistant_message:
        assistant_message = sanitize_assistant_message(assistant_message)
    if not assistant_message:
        if "discovery_need_zip" in notes or (tool_result and tool_result.get("reason") == "need_zip"):
            assistant_message = t("discovery.ask_zip_short", lang)
        elif "discovery_need_identity" in notes or (
            tool_result and tool_result.get("reason") == "need_identity"
        ):
            assistant_message = t("discovery.ask_identity_short", lang)
        elif tool_result and tool_result.get("summary"):
            assistant_message = str(tool_result["summary"])[:1200]
        else:
            assistant_message = (
                "Hey — I'm here for your neighborhood. Ask me to find neighbors like you or say log in."
            )
    status = "continue"
    ctx: dict[str, Any] = {"last_status": status, "unified_mode": True}
    if tool_result and tool_result.get("peer_matches"):
        ctx["peer_matches"] = tool_result["peer_matches"]
    if tool_result and tool_result.get("identity_snippet"):
        ctx["identity_snippet"] = tool_result["identity_snippet"]
    return assistant_message, status, ctx, ui, None


def _profile_synth_schema() -> str:
    return """{
  "assistant_message": "single-line warm reply",
  "status": "continue",
  "topics_covered": [],
  "topics_to_explore": [],
  "ui": { "bucket": null, "focus_phrase": null, "highlights": [] }
}

Rules: status is continue or ready_to_complete. assistant_message ONE line only. Omit core_patch unless session goal changed."""


def _event_synth_schema() -> str:
    return """{
  "assistant_message": "Your warm reply to the host (one line)",
  "status": "continue",
  "event_draft": {
    "title": "short title from user words or null",
    "description": null,
    "venue_name": "place name or null",
    "starts_at": "ISO8601 timestamptz or null",
    "ends_at": null,
    "duration_minutes": null,
    "max_attendees": null,
    "cohort_tags": [],
    "missing": []
  },
  "ui": {
    "bucket": "activity",
    "focus_phrase": null,
    "highlights": [{ "text": "phrase from user", "bucket": "time" }]
  }
}

status: continue until title + starts_at + venue_name are set; then ready_to_complete.
Fill event_draft from conversation — never leave all null if user gave details."""


def _parse_profile_synth(
    raw: dict[str, Any],
    *,
    history: list[dict[str, Any]] | None = None,
) -> tuple[str, str, dict[str, Any], dict[str, Any], None]:
    assistant_message = str(raw.get("assistant_message", "")).strip()[:1200]
    if not assistant_message:
        assistant_message = "Tell me a bit about you — I'd love to hear your story."
    status = str(raw.get("status", "continue")).lower()
    if status not in ("continue", "ready_to_complete"):
        status = "continue"
    ui = parse_turn_ui(raw)
    covered = raw.get("topics_covered") or []
    if not isinstance(covered, list):
        covered = []
    covered = [str(x)[:64] for x in covered[:12]]
    if history is not None:
        assistant_message, status = apply_profile_stop_rules(
            status,
            assistant_message,
            history=history,
            ui=ui,
            topics_covered=covered,
        )
    explore = raw.get("topics_to_explore") or []
    if not isinstance(explore, list):
        explore = []
    explore = [str(x)[:64] for x in explore[:12]]
    core_patch = raw.get("core_patch") if isinstance(raw.get("core_patch"), dict) else None
    ctx = {
        "topics_covered": covered,
        "topics_to_explore": explore,
        "last_status": status,
        "last_ui": ui,
    }
    if core_patch:
        ctx["core_patch"] = core_patch
    return assistant_message, status, ctx, ui, None


def _parse_event_synth(
    raw: dict[str, Any],
    *,
    prev_draft: dict[str, Any] | None,
    tool_result: dict[str, Any] | None,
    valid_purpose_ids: set[str] | None = None,
) -> tuple[str, str, dict[str, Any], dict[str, Any], dict[str, Any]]:
    assistant_message = str(raw.get("assistant_message", "")).strip()[:1200]
    if not assistant_message:
        assistant_message = "What are you thinking of hosting nearby?"
    status = str(raw.get("status", "continue")).lower()
    if status not in ("continue", "ready_to_complete"):
        status = "continue"

    draft_raw = raw.get("event_draft")
    if not isinstance(draft_raw, dict):
        draft_raw = {}
    if tool_result and tool_result.get("event_draft"):
        draft_raw = merge_event_drafts(draft_raw, tool_result["event_draft"])
    parsed = parse_event_draft({"event_draft": draft_raw}, valid_purpose_ids=valid_purpose_ids)
    if prev_draft:
        parsed = merge_event_drafts(prev_draft, parsed)
    parsed = finalize_event_draft(parsed)
    missing = parsed["missing"]

    if missing:
        status = "continue"
    elif status != "ready_to_complete" and not missing:
        status = "ready_to_complete"

    ui = parse_event_turn_ui(raw)
    core_patch = raw.get("core_patch") if isinstance(raw.get("core_patch"), dict) else None
    ctx = {
        "last_status": status,
        "last_ui": ui,
        "event_draft": parsed,
    }
    if core_patch:
        ctx["core_patch"] = core_patch
    if tool_result and tool_result.get("needs_user_confirmation"):
        ctx["pending_confirmation"] = tool_result.get("confirmation_prompt")
    if tool_result and tool_result.get("published"):
        status = "ready_to_complete"
        ctx["event_id"] = tool_result.get("event_id")
        ctx.pop("pending_confirmation", None)

    return assistant_message, status, ctx, ui, parsed
