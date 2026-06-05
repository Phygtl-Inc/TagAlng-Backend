from typing import Any

from app.auth import service_client


def log_turn(
    *,
    session_id: str,
    user_id: str,
    event_type: str,
    module: str | None,
    utterance: str | None,
    response: str | None,
    guardrail_result: dict[str, Any] | None = None,
    routing: dict[str, Any] | None = None,
) -> None:
    try:
        sb = service_client()
        sb.table("lana_audit_log").insert(
            {
                "session_id": session_id,
                "user_id": user_id,
                "event_type": event_type[:64],
                "module": (module or "")[:64] or None,
                "utterance_redacted": (utterance or "")[:2000] or None,
                "response_redacted": (response or "")[:2000] or None,
                "guardrail_result": guardrail_result or {},
                "routing": routing or {},
            }
        ).execute()
    except Exception:
        pass
