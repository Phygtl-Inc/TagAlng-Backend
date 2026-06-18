"""Hard R/A/T/C decision tree (Tool Routing v1 §2–§3)."""

from typing import Any

from app.orchestrator.slots import (
    KNOWN_TOOLS,
    event_missing_slots,
    has_partial_event_args,
    merged_event_draft,
    next_missing_event_slot,
    normalize_event_args,
    validate_tool_slots,
)

CONF_HIGH = 0.85
CONF_LOW = 0.50
CONF_LOW_STRESSED = 0.40

_AFFIRMATIVE = frozenset(
    {
        "yes",
        "yeah",
        "yep",
        "yup",
        "sure",
        "ok",
        "okay",
        "publish",
        "do it",
        "go ahead",
        "confirm",
        "confirmed",
        "sounds good",
        "let's do it",
        "lets do it",
        "please publish",
        "yes publish",
        "yes please",
    }
)

_NEGATIVE = frozenset(
    {
        "no",
        "nope",
        "not yet",
        "wait",
        "change",
        "edit",
        "hold on",
        "stop",
        "cancel",
    }
)

_STRESSED_SENTIMENTS = frozenset({"frustrated", "urgent", "stressed", "anxious"})


def is_affirmative(utterance: str) -> bool:
    lower = utterance.strip().lower().rstrip(".!")
    lower = lower.replace(",", " ")
    if lower in _AFFIRMATIVE:
        return True
    return any(lower.startswith(f"{a} ") for a in _AFFIRMATIVE)


def is_negative(utterance: str) -> bool:
    lower = utterance.strip().lower().rstrip(".!")
    if lower in _NEGATIVE:
        return True
    return any(lower.startswith(f"{n} ") for n in _NEGATIVE)


def should_execute_tool(routing: dict[str, Any]) -> bool:
    tool = routing.get("tool_to_call")
    if not tool:
        return False
    outcome = routing.get("outcome")
    if outcome == "T":
        return True
    if outcome == "C" and tool == "capture_inquiry":
        return True
    return False


def enforce_routing(
    routing: dict[str, Any],
    *,
    purpose: str,
    utterance: str,
    session_ctx: dict[str, Any],
    history: list[dict[str, Any]] | None = None,
    home_block_id: str | None = None,
) -> dict[str, Any]:
    """Apply confidence + slot rules in code; may override Haiku's outcome."""
    notes: list[str] = []
    base = dict(routing)
    confidence = float(base.get("confidence", 0.5))
    sentiment = str(base.get("sentiment", "neutral")).lower()
    intent = str(base.get("intent_class", "companionship"))
    tool = base.get("tool_to_call")
    if tool is not None:
        tool = str(tool).strip() or None
    tool_args = dict(base.get("tool_args") or {})

    enforced = _check_pending_confirmation(
        base,
        purpose=purpose,
        utterance=utterance,
        session_ctx=session_ctx,
        sentiment=sentiment,
    )
    if enforced is not None:
        return enforced

    if purpose == "lana" and "discovery_chat_fast_path" not in (base.get("enforce_notes") or []):
        discovery = _enforce_lana_discovery(
            base,
            utterance=utterance,
            session_ctx=session_ctx,
            history=history,
            home_block_id=home_block_id,
        )
        if discovery is not None:
            return discovery

    if intent == "off_topic" or base.get("outcome") == "C":
        return _as_capture(base, utterance, notes=["off_topic"])

    if tool and tool not in KNOWN_TOOLS:
        return _as_capture(base, utterance, notes=["unknown_tool"])

    tool, tool_args, blocked = _apply_purpose_guards(purpose, tool, tool_args)
    if blocked:
        return _as_respond(base, notes=["purpose_blocked"])

    if tool == "update_relationship_tier" and not tool_args.get("trigger_event"):
        return _as_respond(base, notes=["tier_system_only"])

    conf_floor = CONF_LOW_STRESSED if sentiment in _STRESSED_SENTIMENTS else CONF_LOW

    if confidence < conf_floor:
        return _as_respond(base, notes=["confidence_low"])

    if confidence < CONF_HIGH:
        missing = _collect_missing_for_ask(tool, tool_args, purpose, session_ctx, base)
        return _as_ask(base, missing_slots=missing, notes=["confidence_medium"])

    if not tool:
        return _as_respond(base, notes=["high_conf_no_tool"])

    missing = validate_tool_slots(tool, tool_args, purpose=purpose, session_ctx=session_ctx)
    if missing:
        if tool == "publish_activity" and purpose in ("event_draft", "lana"):
            if has_partial_event_args(tool_args, session_ctx):
                merged = merged_event_draft(session_ctx, tool_args)
                return _as_tool(
                    base,
                    tool="update_event_draft",
                    tool_args=normalize_event_args(tool_args),
                    missing_slots=event_missing_slots(merged),
                    notes=["publish_downgrade_to_draft"],
                )
        return _as_ask(
            base,
            missing_slots=missing,
            ask_slot=next_missing_event_slot(missing),
            notes=["slots_missing"],
        )

    if tool == "publish_activity" and not tool_args.get("user_confirmed"):
        return _as_tool(
            base,
            tool="publish_activity",
            tool_args=tool_args,
            missing_slots=[],
            needs_confirmation=True,
            notes=["await_confirm"],
        )

    return _as_tool(
        base,
        tool=tool,
        tool_args=tool_args,
        missing_slots=[],
        notes=["tool_ok"],
    )


def _enforce_lana_discovery(
    base: dict[str, Any],
    *,
    utterance: str,
    session_ctx: dict[str, Any],
    history: list[dict[str, Any]] | None,
    home_block_id: str | None,
) -> dict[str, Any] | None:
    """Override router when user is in (or wants) discovery — code owns privacy gates."""
    from app.discovery_route import (
        PHASE_NEED_IDENTITY,
        extract_zip,
        resolve_block_id,
        resolve_identity_for_turn,
        wants_discovery_turn,
    )
    from app.guest_capabilities import wants_host_activity, wants_peer_find

    phase = str(session_ctx.get("routing_phase") or "")
    # Sticky host mode: the orchestrator owns the whole event flow. Never override to
    # find_peers here, or "weekday playground meet with other moms" gets hijacked into
    # a neighbour search via the "moms" term.
    if session_ctx.get("event_host_active"):
        return None
    in_funnel = wants_discovery_turn(utterance, session_ctx, history)
    if not in_funnel:
        return None
    if wants_host_activity(utterance) and not wants_peer_find(utterance):
        return None

    out = {**base, "intent_class": "discovery"}
    block_id = resolve_block_id(session_ctx, home_block_id)
    zip_from = extract_zip(utterance)
    if not block_id and not zip_from:
        return _as_ask(
            out,
            missing_slots=["zip"],
            ask_slot="zip",
            notes=["discovery_need_zip"],
        )

    identity = resolve_identity_for_turn(
        utterance,
        session_ctx,
        history,
        phase,
        block_just_resolved=bool(zip_from and not block_id),
    )
    if block_id and not identity and not zip_from:
        return _as_ask(
            out,
            missing_slots=["identity_snippet"],
            ask_slot="identity_snippet",
            notes=["discovery_need_identity"],
        )

    tool_args: dict[str, Any] = {}
    if zip_from:
        tool_args["zip"] = zip_from
    if identity:
        session_ctx["identity_snippet"] = identity
    return _as_tool(
        out,
        tool="find_peers",
        tool_args=tool_args,
        missing_slots=[],
        notes=["discovery_find_peers"],
    )


def _check_pending_confirmation(
    base: dict[str, Any],
    *,
    purpose: str,
    utterance: str,
    session_ctx: dict[str, Any],
    sentiment: str,
) -> dict[str, Any] | None:
    if not session_ctx.get("pending_confirmation") or purpose not in ("event_draft", "lana"):
        return None

    draft = session_ctx.get("event_draft") or {}
    missing = event_missing_slots(draft)

    if is_affirmative(utterance):
        if missing:
            return _as_ask(
                base,
                missing_slots=missing,
                ask_slot=next_missing_event_slot(missing),
                notes=["confirm_but_incomplete"],
            )
        args = normalize_event_args(dict(draft))
        args["user_confirmed"] = True
        return _as_tool(
            base,
            tool="publish_activity",
            tool_args=args,
            missing_slots=[],
            needs_confirmation=False,
            notes=["user_confirmed_publish"],
        )

    if is_negative(utterance):
        return _as_ask(
            base,
            missing_slots=missing,
            ask_slot=next_missing_event_slot(missing) if missing else None,
            notes=["confirm_declined"],
        )

    return None


def _apply_purpose_guards(
    purpose: str,
    tool: str | None,
    tool_args: dict[str, Any],
) -> tuple[str | None, dict[str, Any], bool]:
    if purpose == "profile_intake" and tool in (
        "publish_activity",
        "update_event_draft",
        "propose_cohost",
        "find_peers",
    ):
        return None, {}, True
    if purpose != "event_draft" and tool == "propose_cohost":
        return None, {}, True
    return tool, tool_args, False


def _collect_missing_for_ask(
    tool: str | None,
    tool_args: dict[str, Any],
    purpose: str,
    session_ctx: dict[str, Any],
    base: dict[str, Any],
) -> list[str]:
    if tool:
        missing = validate_tool_slots(tool, tool_args, purpose=purpose, session_ctx=session_ctx)
        if missing:
            return missing
    existing = base.get("missing_slots")
    if isinstance(existing, list) and existing:
        return [str(s) for s in existing]
    return []


def _capture_args(base: dict[str, Any], utterance: str) -> dict[str, Any]:
    args = dict(base.get("tool_args") or {})
    args.setdefault("raw_query", utterance.strip())
    args.setdefault("extracted_category", base.get("intent_class", "other"))
    args.setdefault("sentiment", base.get("sentiment", "neutral"))
    return args


def _as_capture(base: dict[str, Any], utterance: str, *, notes: list[str]) -> dict[str, Any]:
    out = {**base}
    out["outcome"] = "C"
    out["tool_to_call"] = "capture_inquiry"
    out["tool_args"] = _capture_args(base, utterance)
    out["missing_slots"] = []
    out["needs_confirmation"] = False
    out["enforce_notes"] = notes
    return out


def _as_respond(base: dict[str, Any], *, notes: list[str]) -> dict[str, Any]:
    out = {**base}
    out["outcome"] = "R"
    out["tool_to_call"] = None
    out["tool_args"] = None
    out["needs_confirmation"] = False
    out["enforce_notes"] = notes
    return out


def _as_ask(
    base: dict[str, Any],
    *,
    missing_slots: list[str],
    ask_slot: str | None = None,
    notes: list[str],
) -> dict[str, Any]:
    out = {**base}
    out["outcome"] = "A"
    out["tool_to_call"] = None
    out["tool_args"] = None
    out["missing_slots"] = missing_slots
    out["ask_slot"] = ask_slot or next_missing_event_slot(missing_slots)
    out["needs_confirmation"] = False
    out["enforce_notes"] = notes
    return out


def _as_tool(
    base: dict[str, Any],
    *,
    tool: str,
    tool_args: dict[str, Any],
    missing_slots: list[str],
    needs_confirmation: bool = False,
    notes: list[str],
) -> dict[str, Any]:
    out = {**base}
    out["outcome"] = "T"
    out["tool_to_call"] = tool
    out["tool_args"] = tool_args
    out["missing_slots"] = missing_slots
    out["needs_confirmation"] = needs_confirmation
    out["enforce_notes"] = notes
    return out
