import os
from dataclasses import dataclass

import httpx
from fastapi import HTTPException
from supabase import create_client

def _supabase_url() -> str:
    return os.environ.get("SUPABASE_URL", "")


def _supabase_anon_key() -> str:
    return os.environ.get("SUPABASE_ANON_KEY", "")


def _supabase_service_role_key() -> str:
    return os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")


@dataclass(frozen=True)
class AuthSession:
    user_id: str
    is_anonymous: bool
    # Auth gate flag. Now fed by EMAIL verification (email OTP), not SMS. The name
    # is kept because ~50 downstream call sites in discovery_* read `phone_verified`
    # as the generic "is this a permanent, verified account?" signal.
    phone_verified: bool
    home_block_id: str | None


def verify_auth(authorization: str | None) -> AuthSession:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing_bearer_token")
    token = authorization.removeprefix("Bearer ").strip()
    supabase_url = _supabase_url()
    supabase_anon_key = _supabase_anon_key()
    if not supabase_url or not supabase_anon_key:
        raise HTTPException(status_code=500, detail="server_misconfigured")

    with httpx.Client(timeout=15.0) as client:
        res = client.get(
            f"{supabase_url}/auth/v1/user",
            headers={"apikey": supabase_anon_key, "Authorization": f"Bearer {token}"},
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
        phone_verified=_resolve_verified(str(user_id), user, profile),
        home_block_id=profile.get("home_block_id"),
    )


def verify_jwt(authorization: str | None) -> str:
    return verify_auth(authorization).user_id


def require_home_block_for_purpose(auth: AuthSession, purpose: str) -> str | None:
    """Return home_block_id when set. profile_intake may run before block assignment."""
    if auth.home_block_id:
        return auth.home_block_id
    if purpose in ("profile_intake", "lana"):
        return None
    raise HTTPException(status_code=400, detail="home_block_required")


def require_home_block(user_id: str) -> str:
    if not _supabase_service_role_key():
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
    supabase_url = _supabase_url()
    supabase_service_role_key = _supabase_service_role_key()
    if not supabase_url or not supabase_service_role_key:
        raise HTTPException(status_code=500, detail="server_misconfigured")
    return create_client(supabase_url, supabase_service_role_key)


def email_has_registered_account(email: str) -> bool:
    """
    True when email belongs to a verified non-anonymous account.

    Used during anonymous signup gate to route existing emails to login OTP
    instead of link_email_signup (which 422s on a duplicate email).
    """
    normalized = str(email or "").strip().lower()
    if not normalized:
        return False
    supabase_url = _supabase_url()
    supabase_service_role_key = _supabase_service_role_key()
    if not supabase_url or not supabase_service_role_key:
        return False
    try:
        sb = create_client(supabase_url, supabase_service_role_key)
        res = (
            sb.table("users")
            .select("id, email_verified_at")
            .eq("email", normalized)
            .limit(1)
            .execute()
        )
        if not res.data:
            return False
        row = res.data[0]
        if not row.get("email_verified_at"):
            return False
        user_id = str(row.get("id") or "")
        if not user_id:
            return False
        with httpx.Client(timeout=10.0) as client:
            auth_res = client.get(
                f"{supabase_url}/auth/v1/admin/users/{user_id}",
                headers={
                    "apikey": supabase_service_role_key,
                    "Authorization": f"Bearer {supabase_service_role_key}",
                },
            )
        if auth_res.status_code != 200:
            return True
        user = auth_res.json()
        return bool(user.get("email")) and not user.get("is_anonymous")
    except Exception:
        return False


def registered_user_id_for_email(email: str) -> str | None:
    """User id of the verified non-anonymous account for `email`, else None. Used to
    stash a guest's in-progress event against the account they're logging into."""
    normalized = str(email or "").strip().lower()
    supabase_url = _supabase_url()
    supabase_service_role_key = _supabase_service_role_key()
    if not normalized or not supabase_url or not supabase_service_role_key:
        return None
    try:
        sb = create_client(supabase_url, supabase_service_role_key)
        res = (
            sb.table("users")
            .select("id, email_verified_at")
            .eq("email", normalized)
            .limit(1)
            .execute()
        )
        row = (res.data or [None])[0]
        if not isinstance(row, dict) or not row.get("email_verified_at"):
            return None
        uid = str(row.get("id") or "")
        return uid or None
    except Exception:
        return None


def _resolve_verified(user_id: str, user: dict, profile: dict) -> bool:
    """
    Email confirmation is the source of truth; public.users may lag the sync
    trigger. Falls back to legacy phone verification so any pre-migration
    phone-verified accounts still count as verified.
    """
    if profile.get("email_verified_at") or profile.get("phone_verified_at"):
        return True
    confirmed = user.get("email_confirmed_at")
    column = "email_verified_at"
    if not confirmed:
        # Legacy phone-verified accounts (pre email migration).
        confirmed = user.get("phone_confirmed_at")
        column = "phone_verified_at"
    if confirmed and not user.get("is_anonymous"):
        supabase_url = _supabase_url()
        supabase_service_role_key = _supabase_service_role_key()
        if supabase_service_role_key and not profile.get(column):
            try:
                sb = create_client(supabase_url, supabase_service_role_key)
                sb.table("users").update({column: confirmed}).eq(
                    "id", user_id
                ).execute()
            except Exception:
                pass
        return True
    return False


def _load_user_profile(user_id: str) -> dict:
    supabase_url = _supabase_url()
    supabase_service_role_key = _supabase_service_role_key()
    if not supabase_service_role_key:
        raise HTTPException(status_code=500, detail="server_misconfigured")
    sb = create_client(supabase_url, supabase_service_role_key)
    profile = (
        sb.table("users")
        .select("home_block_id, phone_verified_at, email_verified_at")
        .eq("id", user_id)
        .execute()
    )
    row = profile.data[0] if profile.data else {}
    return row if isinstance(row, dict) else {}
