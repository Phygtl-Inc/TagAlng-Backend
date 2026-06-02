import os

import httpx
from fastapi import HTTPException
from supabase import create_client

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")


def verify_jwt(authorization: str | None) -> str:
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
    return user_id


def require_home_block(user_id: str) -> str:
    if not SUPABASE_SERVICE_ROLE_KEY:
        raise HTTPException(status_code=500, detail="server_misconfigured")
    sb = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
    profile = sb.table("users").select("home_block_id").eq("id", user_id).execute()
    row = profile.data[0] if profile.data else None
    if not row or not row.get("home_block_id"):
        raise HTTPException(status_code=400, detail="home_block_required")
    return str(row["home_block_id"])


def service_client():
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise HTTPException(status_code=500, detail="server_misconfigured")
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
