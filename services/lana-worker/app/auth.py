import os
from dataclasses import dataclass
from functools import lru_cache

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
    # Auth gate flag. Now fed by EMAIL verification (email OTP), not SMS. The name
    # is kept because ~50 downstream call sites in discovery_* read `phone_verified`
    # as the generic "is this a permanent, verified account?" signal.
    phone_verified: bool
    home_block_id: str | None
    # Lingo §3.3/§4 (migration 20260909): inferred household role + grammatical
    # gender, read from the same users row this loader already fetches. None means
    # unspecified/unknown — composers stay neutral.
    role: str | None = None
    grammatical_gender: str | None = None


def jwt_user_id(jwt: str | None) -> str | None:
    """The JWT's `sub` (user id), decoded locally — no crypto: the token was
    verified upstream this request. For read-side gates only, never for auth."""
    try:
        import base64
        import json

        payload = str(jwt or "").split(".")[1]
        payload += "=" * (-len(payload) % 4)
        sub = json.loads(base64.urlsafe_b64decode(payload)).get("sub")
        return str(sub) if sub else None
    except Exception:  # noqa: BLE001
        return None


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
        phone_verified=_resolve_verified(str(user_id), user, profile),
        home_block_id=profile.get("home_block_id"),
        role=profile.get("role") or None,
        grammatical_gender=profile.get("grammatical_gender") or None,
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


def _supabase(url: str, key: str):
    """A Supabase client whose pooled connections survive going idle.

    postgrest hardcodes `http2=True`, and httpcore's HTTP/2 `has_expired()` only checks
    the keepalive timer — unlike HTTP/1.1, which probes the socket ("idle but readable"
    means the server hung up) and evicts it. So a connection Supabase closed while we were
    busy with a slow LLM turn stayed in the pool and failed instantly on reuse:
    `ReadError: [Errno 35] Resource temporarily unavailable`, ~7ms in, losing a reply that
    had already cost 20s+ to compute.

    HTTP/1.1 fixes it at the source with no retry — retrying a POST insert could
    double-write, and neither httpx's `retries=` (connect errors only) nor postgrest's
    send_with_retry (GET/HEAD on 503/520) covers this case anyway. We issue a handful of
    sequential REST calls per turn, so h2 multiplexing was buying nothing here.
    """
    from supabase import ClientOptions

    return create_client(
        url, key, options=ClientOptions(httpx_client=httpx.Client(http2=False))
    )


@lru_cache(maxsize=1)
def _cached_service_client():
    # Built once and reused so the underlying httpx session keeps the connection
    # alive (HTTP keepalive) — otherwise every DB call pays a fresh TCP + TLS
    # handshake to Supabase, which dominates per-call latency.
    return _supabase(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


def service_client():
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise HTTPException(status_code=500, detail="server_misconfigured")
    return _cached_service_client()


def email_has_registered_account(email: str) -> bool:
    """
    True when email belongs to a verified non-anonymous account.

    Used during anonymous signup gate to route existing emails to login OTP
    instead of link_email_signup (which 422s on a duplicate email).
    """
    normalized = str(email or "").strip().lower()
    if not normalized:
        return False
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return False
    try:
        sb = _supabase(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
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
                f"{SUPABASE_URL}/auth/v1/admin/users/{user_id}",
                headers={
                    "apikey": SUPABASE_SERVICE_ROLE_KEY,
                    "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
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
    if not normalized or not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return None
    try:
        sb = _supabase(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
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
        if SUPABASE_SERVICE_ROLE_KEY:
            try:
                sb = _supabase(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
                sb.table("users").update({column: confirmed}).eq(
                    "id", user_id
                ).execute()
            except Exception:
                pass
        # Account-creation completion for the funnel. This branch runs on the FIRST
        # authenticated turn after the OTP lands (the stamp above makes later calls
        # take the early return), so it is the one place a guest becomes a real
        # account regardless of which client verified.
        from app.analytics import track

        track(
            "signup_complete",
            user_id=user_id,
            event_properties={"method": "phone" if column.startswith("phone") else "email"},
        )
        return True
    return False


def _load_user_profile(user_id: str) -> dict:
    if not SUPABASE_SERVICE_ROLE_KEY:
        raise HTTPException(status_code=500, detail="server_misconfigured")
    sb = _supabase(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
    try:
        profile = (
            sb.table("users")
            .select(
                "home_block_id, phone_verified_at, email_verified_at, "
                "role, grammatical_gender"
            )
            .eq("id", user_id)
            .execute()
        )
    except Exception:  # noqa: BLE001 — pre-20260909 environments miss role/gender
        profile = (
            sb.table("users")
            .select("home_block_id, phone_verified_at, email_verified_at")
            .eq("id", user_id)
            .execute()
        )
    row = profile.data[0] if profile.data else {}
    return row if isinstance(row, dict) else {}
