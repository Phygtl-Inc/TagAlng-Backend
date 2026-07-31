"""Which Lana code path runs per session purpose."""

import os


def _llm_ready() -> bool:
    from app.orchestrator.llm import llm_configured

    return llm_configured()


def event_fast_path_enabled() -> bool:
    """Single-call event hosting (legacy lana_event_turn). Default on."""
    flag = os.environ.get("LANA_EVENT_FAST_PATH", "1").strip().lower()
    return flag not in ("0", "false", "off", "legacy")


def profile_fast_path_enabled() -> bool:
    """Single-call profile intake (heritage + short follow-up). Default on."""
    flag = os.environ.get("LANA_PROFILE_FAST_PATH", "1").strip().lower()
    return flag not in ("0", "false", "off", "legacy")


def _orchestrator_enabled() -> bool:
    flag = os.environ.get("LANA_ORCHESTRATOR", "auto").strip().lower()
    if flag in ("0", "false", "off", "legacy"):
        return False
    if flag in ("1", "true", "on"):
        return _llm_ready()
    return _llm_ready()


def latent_extract_enabled() -> bool:
    """Layer 3 latent-intent collection (entity extractor + capability match).

    Phase 1 = collect, don't surface. Default OFF: enabling adds an LLM call + embeddings
    per turn, so it's a deliberate ops switch (set LANA_LATENT_EXTRACT=1). Requires LLM ready.
    """
    flag = os.environ.get("LANA_LATENT_EXTRACT", "0").strip().lower()
    if flag in ("0", "false", "off"):
        return False
    return _llm_ready()


def rec_personalize_enabled() -> bool:
    """Claim-personalized recommendations on the tip_seek no-neighbor-match path.

    When on, Lana biases the Google Places fallback by the mom's own identity claims and
    names the reasoning ("since you're vegetarian…"). Default OFF: enabling adds one
    gpt-4o-mini call on that path (set LANA_REC_PERSONALIZE=1). Requires LLM ready.
    """
    flag = os.environ.get("LANA_REC_PERSONALIZE", "0").strip().lower()
    if flag in ("0", "false", "off"):
        return False
    return _llm_ready()


def decide_turn_mode() -> str:
    """Unified conversational policy (decide_turn) rollout knob.

      off    — DEFAULT. Legacy decisioning only.
      shadow — legacy path answers; decide_turn runs on a daemon thread and logs
               its would-be decision to lana_audit_log (event decide_turn_shadow)
               for the cutover diff. Zero user-visible change.
      on     — decide_turn answers conversational turns for verified users;
               handoff / any failure falls through to the legacy path untouched.

    Requires LLM ready in shadow/on (silently off otherwise)."""
    mode = os.environ.get("LANA_DECIDE_TURN", "off").strip().lower()
    if mode in ("shadow", "on") and _llm_ready():
        return mode
    return "off"


def unified_rules_first_enabled() -> bool:
    """Run discovery/auth gates before orchestrator on unified lana turns."""
    flag = os.environ.get("LANA_UNIFIED_RULES_FIRST", "1").strip().lower()
    return flag not in ("0", "false", "off")


def use_orchestrator_for_purpose(purpose: str) -> bool:
    """event_draft and profile_intake use fast paths; lana uses orchestrator when LLM ready."""
    if purpose == "event_draft" and event_fast_path_enabled():
        return False
    if purpose == "profile_intake" and profile_fast_path_enabled():
        return False
    if purpose == "lana":
        return _orchestrator_enabled()
    return _orchestrator_enabled()
