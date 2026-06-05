import os
from typing import Any

import httpx
from fastapi import HTTPException

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")


def call_rpc(user_jwt: str, rpc_name: str, payload: dict[str, Any]) -> Any:
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        raise HTTPException(status_code=500, detail="server_misconfigured")
    if not user_jwt:
        raise HTTPException(status_code=401, detail="missing_bearer_token")

    with httpx.Client(timeout=30.0) as client:
        res = client.post(
            f"{SUPABASE_URL}/rest/v1/rpc/{rpc_name}",
            headers={
                "apikey": SUPABASE_ANON_KEY,
                "Authorization": f"Bearer {user_jwt}",
                "Content-Type": "application/json",
            },
            json=payload,
        )

    if res.status_code >= 400:
        detail = res.text[:400]
        code = _map_rpc_error(detail)
        raise HTTPException(status_code=code["status"], detail=code["detail"])

    if not res.text or res.text.strip() in ("", "null"):
        return None
    try:
        return res.json()
    except Exception:
        return res.text


def _map_rpc_error(detail: str) -> dict[str, Any]:
    lower = detail.lower()
    if "phone_not_verified" in lower:
        return {"status": 400, "detail": "phone_not_verified"}
    if "tier_too_low" in lower or "send_nudge_first" in lower:
        return {"status": 400, "detail": "tier_too_low_send_nudge_first"}
    if "candidate_consent_missing" in lower:
        return {"status": 400, "detail": "candidate_consent_missing"}
    if "duplicate_intro" in lower:
        return {"status": 400, "detail": "duplicate_intro_recent"}
    if "nudge_rate_limit" in lower:
        return {"status": 429, "detail": "nudge_rate_limit_daily"}
    if "nudge_cooldown" in lower:
        return {"status": 429, "detail": "nudge_cooldown_pair"}
    if "recipient_not_on_block" in lower or "candidate_not_on_block" in lower:
        return {"status": 400, "detail": "user_not_on_same_block"}
    if "cohost_not_accepted" in lower:
        return {"status": 400, "detail": "cohost_not_accepted"}
    return {"status": 502, "detail": f"rpc_failed:{detail[:200]}"}
