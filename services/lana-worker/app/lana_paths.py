"""Which Lana code path runs per session purpose."""

import os


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
        return bool(os.environ.get("GCP_VERTEX_PROJECT", "").strip())
    return bool(os.environ.get("GCP_VERTEX_PROJECT", "").strip())


def use_orchestrator_for_purpose(purpose: str) -> bool:
    """event_draft, profile_intake, and lana use fast paths by default."""
    if purpose == "event_draft" and event_fast_path_enabled():
        return False
    if purpose == "profile_intake" and profile_fast_path_enabled():
        return False
    if purpose == "lana":
        return False
    return _orchestrator_enabled()
