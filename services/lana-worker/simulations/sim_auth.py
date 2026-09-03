"""sim_auth.py — get a JWT for a sim persona account, and FAIL LOUDLY when you can't.

THE PROBLEM THIS SOLVES
-----------------------
Every entry point that talks to real Lana authenticates as one of the six p*-sim accounts by
password grant, using a shared `SIM_PASSWORD`. Two things were wrong with that:

  * The failure was opaque. `resp.raise_for_status()` on a Supabase password grant surfaces
    `400 Bad Request` for THREE completely different mistakes — wrong project, missing/wrong
    password, account not seeded — whose fixes have nothing in common. On a fresh local stack
    all three are live possibilities at once.
  * The password is a shared secret. It is not in anyone's checked-in env, and the whole point
    of the local-stack path (LOCAL_STACK.md) is that you no longer need a team secret: you
    choose the password when you apply `supabase/seed_sim_accounts.sql`.

So: one `sign_in(email)` for the whole suite, with a diagnosed error, plus the service-role
fallback that seed_sim_accounts.sql's own header describes ("OR the service role mints one via
admin.generateLink"), for when SIM_PASSWORD is absent but a service-role key is present — the
normal shape of a local stack.

THE FALLBACK, PRECISELY
-----------------------
    POST /auth/v1/admin/generate_link   (service role)  {"type":"magiclink","email":...}
      -> body carries a `hashed_token` (GoTrue's token_hash)
    POST /auth/v1/verify                (anon key)      {"type":"magiclink","token":<hashed>,
                                                          "email":...}
      -> a normal session: {access_token, refresh_token, user:{id,...}}

This is stock GoTrue admin API — it needs NO product change, no new endpoint, and no edit to
anything under app/. It is available on a hosted project too, but it needs the service-role
key, which is exactly why password auth stays the default where a password exists.

# GUESSED: the response FIELD NAME for the hashed token. GoTrue has returned it both at the
# top level (`hashed_token`) and nested under `properties` across versions, so both are read
# and an unrecognised shape raises with the (key-redacted) body rather than guessing further.
# Unverified end-to-end in this environment — no local stack could be started here (no Docker,
# no supabase CLI, no psql). See LOCAL_STACK.md "What is not verified".
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import httpx

_SIMS = Path(__file__).resolve().parent
if str(_SIMS) not in sys.path:
    sys.path.insert(0, str(_SIMS))

from local_guard import auth_failure_help, describe  # noqa: E402

_TIMEOUT = 30


class SimAuthError(RuntimeError):
    """Authentication failed. The message names WHICH of the plausible causes applies."""


def _env(name: str) -> str:
    # Read at CALL time: entry points call load_dotenv() at import, in an order this module
    # must not depend on.
    return os.environ.get(name, "")


def password_grant(email: str, *, password: str | None = None) -> dict[str, Any]:
    """Supabase password grant. Returns the whole session body (callers want either
    `access_token` or `user.id`). Raises SimAuthError with a diagnosed message."""
    url, anon = _env("SUPABASE_URL").rstrip("/"), _env("SUPABASE_ANON_KEY")
    pwd = _env("SIM_PASSWORD") if password is None else password
    try:
        resp = httpx.post(
            f"{url}/auth/v1/token?grant_type=password",
            headers={"apikey": anon, "Content-Type": "application/json"},
            json={"email": email, "password": pwd},
            timeout=_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise SimAuthError(auth_failure_help(
            email=email, password=pwd, anon_key=anon,
            body=f"could not reach {url}: {exc}")) from exc
    if resp.status_code >= 400:
        raise SimAuthError(auth_failure_help(
            email=email, password=pwd, anon_key=anon,
            status=resp.status_code, body=resp.text))
    return resp.json()


def _hashed_token(body: dict[str, Any]) -> str:
    """Pull GoTrue's token_hash out of an admin generate_link response. Pure — selftested
    against both known shapes and against a shape that carries neither."""
    for holder in (body, body.get("properties") or {}):
        if isinstance(holder, dict):
            for key in ("hashed_token", "token_hash"):
                val = holder.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
    return ""


def magic_link_grant(email: str) -> dict[str, Any]:
    """Service-role fallback: mint a magic link, then redeem its token_hash for a session.

    Used when SIM_PASSWORD is absent. Needs SUPABASE_SERVICE_ROLE_KEY.
    """
    url = _env("SUPABASE_URL").rstrip("/")
    anon, svc = _env("SUPABASE_ANON_KEY"), _env("SUPABASE_SERVICE_ROLE_KEY")
    if not svc:
        raise SimAuthError(
            "magic-link fallback needs SUPABASE_SERVICE_ROLE_KEY (SIM_PASSWORD is empty, so "
            "there is no other way to authenticate). See simulations/LOCAL_STACK.md.")
    with httpx.Client(timeout=_TIMEOUT) as http:
        gen = http.post(
            f"{url}/auth/v1/admin/generate_link",
            headers={"apikey": svc, "Authorization": f"Bearer {svc}",
                     "Content-Type": "application/json"},
            json={"type": "magiclink", "email": email},
        )
        if gen.status_code >= 400:
            raise SimAuthError(
                f"admin generate_link FAILED for {email} (HTTP {gen.status_code}) against "
                f"{describe(url)}.\n"
                f"  server said : {gen.text[:400]}\n"
                f"  most likely : the account does not exist on this target — apply "
                f"supabase/seed_sim_accounts.sql (LOCAL_STACK.md step 3), or the service-role "
                f"key belongs to a different project than SUPABASE_URL.")
        token = _hashed_token(gen.json() if gen.content else {})
        if not token:
            raise SimAuthError(
                "admin generate_link returned no hashed_token/token_hash — this GoTrue version "
                "does not expose one in a shape sim_auth knows (see the # GUESSED note in "
                "sim_auth.py). Set SIM_PASSWORD instead. Keys of the response: "
                + str(sorted((gen.json() if gen.content else {}).keys())))
        ver = http.post(
            f"{url}/auth/v1/verify",
            headers={"apikey": anon, "Content-Type": "application/json"},
            json={"type": "magiclink", "token": token, "email": email},
        )
        if ver.status_code >= 400:
            raise SimAuthError(
                f"redeeming the magic link FAILED for {email} (HTTP {ver.status_code}).\n"
                f"  server said : {ver.text[:400]}\n"
                f"  note        : the link is single-use and short-lived; a second redemption "
                f"of the same token always fails.")
        body = ver.json()
    if not body.get("access_token"):
        raise SimAuthError(
            f"magic-link verify returned no access_token for {email}. Body keys: "
            + str(sorted(body.keys())))
    return body


def sign_in(email: str) -> dict[str, Any]:
    """The one entry point. Password grant when SIM_PASSWORD is set; otherwise the service-role
    magic-link fallback. Either way, a failure explains WHICH thing is wrong."""
    if not email:
        raise SimAuthError("no email to authenticate as (persona.profile.email / SIM_LIVE_EMAIL "
                           "is empty).")
    if not _env("SUPABASE_URL"):
        raise SimAuthError("SUPABASE_URL is unset — nothing to authenticate against. "
                           "See simulations/LOCAL_STACK.md.")
    if _env("SIM_PASSWORD"):
        return password_grant(email)
    if _env("SUPABASE_SERVICE_ROLE_KEY"):
        print(f"  [auth] SIM_PASSWORD is empty — using the service-role magic-link fallback "
              f"for {email} against {describe(_env('SUPABASE_URL')).kind}.")
        return magic_link_grant(email)
    raise SimAuthError(auth_failure_help(
        email=email, password="", anon_key=_env("SUPABASE_ANON_KEY"),
        body="no credential available at all: SIM_PASSWORD and SUPABASE_SERVICE_ROLE_KEY are "
             "both empty, so neither the password grant nor the magic-link fallback can run."))


def jwt_for(email: str) -> str:
    return str(sign_in(email)["access_token"])


__all__ = ["SimAuthError", "sign_in", "jwt_for", "password_grant", "magic_link_grant"]
