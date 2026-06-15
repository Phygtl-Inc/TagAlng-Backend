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
