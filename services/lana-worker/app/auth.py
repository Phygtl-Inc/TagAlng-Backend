import os
from dataclasses import dataclass

import httpx
from fastapi import HTTPException
from supabase import create_client

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")


@dataclass(frozen=True)
class AuthSession:
    user_id: str
    is_anonymous: bool
    phone_verified: bool
    home_block_id: str | None


def verify_auth(authorization: str | None) -> AuthSession:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing_bearer_token")
    token = authorization.removeprefix("Bearer ").strip()
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        raise HTTPException(status_code=500, detail="server_misconfigured")

    with httpx.Client(timeout=15.0) as client:
        res = client.get(
            f"{SUPABASE_URL}/auth/v1/user",
            headers={"apikey": SUPABASE_ANON_KEY, "Authorization": f"Bearer {token}"},
        )
    if res.status_code != 200:
        raise HTTPException(status_code=401, detail="invalid_session")
    user = res.json()
    user_id = user.get("id")
    if not user_id:
        raise HTTPException(status_code=401, detail="invalid_session")

    profile = _load_user_profile(str(user_id))
    return AuthSession(
        user_id=str(user_id),
        is_anonymous=bool(user.get("is_anonymous")),
        phone_verified=_resolve_phone_verified(str(user_id), user, profile),
        home_block_id=profile.get("home_block_id"),
    )


def verify_jwt(authorization: str | None) -> str:
    return verify_auth(authorization).user_id


def require_home_block_for_purpose(auth: AuthSession, purpose: str) -> str | None:
    """Return home_block_id when set. profile_intake may run before block assignment."""
    if auth.home_block_id:
        return auth.home_block_id
    if purpose == "profile_intake":
        return None
    raise HTTPException(status_code=400, detail="home_block_required")


def require_home_block(user_id: str) -> str:
    if not SUPABASE_SERVICE_ROLE_KEY:
        raise HTTPException(status_code=500, detail="server_misconfigured")
    profile = _load_user_profile(user_id)
    block_id = profile.get("home_block_id")
    if not block_id:
        raise HTTPException(status_code=400, detail="home_block_required")
    return str(block_id)


def require_verified_neighbor_comms(auth: AuthSession) -> None:
    if auth.is_anonymous:
        raise HTTPException(status_code=403, detail="anonymous_user_comms_blocked")
    if not auth.phone_verified:
        raise HTTPException(status_code=403, detail="phone_not_verified")


def service_client():
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise HTTPException(status_code=500, detail="server_misconfigured")
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


def _resolve_phone_verified(user_id: str, user: dict, profile: dict) -> bool:
    """Auth confirm time is source of truth; public.users may lag the sync trigger."""
    if profile.get("phone_verified_at"):
        return True
    confirmed = user.get("phone_confirmed_at")
    if confirmed and not user.get("is_anonymous"):
        if SUPABASE_SERVICE_ROLE_KEY and not profile.get("phone_verified_at"):
            try:
                sb = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
                sb.table("users").update({"phone_verified_at": confirmed}).eq(
                    "id", user_id
                ).execute()
            except Exception:
                pass
        return True
    return False


def _load_user_profile(user_id: str) -> dict:
    if not SUPABASE_SERVICE_ROLE_KEY:
        raise HTTPException(status_code=500, detail="server_misconfigured")
    sb = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
    profile = sb.table("users").select("home_block_id, phone_verified_at").eq("id", user_id).execute()
    row = profile.data[0] if profile.data else {}
    return row if isinstance(row, dict) else {}
