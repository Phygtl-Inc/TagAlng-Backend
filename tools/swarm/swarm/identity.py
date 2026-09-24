"""Anonymous identities and the service-role DB channel.

Two separate concerns that both talk to Supabase:

  * `AnonymousAuth` — mints the test identities. Agents start anonymous
    (LANA_ZERO_BUG_PROGRAM_FINAL.md §5), which is what the PWA itself does on
    load, so no human provisions a JWT. This is the REST equivalent.
  * `Db` — the service-role channel for assertions and the results sink. Needed
    because 20 of 34 endpoints return untyped `{}` (D-16), so most writes can
    only be asserted at the row; and because `circle_affiliations` has RLS
    enabled with 0 policies (D-22), so a client-scoped read returns nothing even
    when the row exists.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any

import httpx

from .config import Config


class AuthError(RuntimeError):
    pass


def decode_jwt_sub(access_token: str) -> str:
    """Read `sub` out of a JWT without verifying it.

    Verification is the server's job; we only need the uid the token was issued
    for. P0's crown-jewel assertion D04 is `decode(new access_token).sub ==
    uid_pre`, so this has to be exact rather than inferred from a DB lookup.
    """
    try:
        payload = access_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))["sub"]
    except Exception as exc:  # pragma: no cover
        raise AuthError(f"could not decode sub from access token: {exc}") from exc


@dataclass
class Identity:
    user_id: str
    access_token: str
    refresh_token: str
    is_anonymous: bool
    email: str | None = None

    @property
    def jwt(self) -> str:
        return self.access_token


class AnonymousAuth:
    def __init__(self, cfg: Config):
        self._cfg = cfg
        self._http = httpx.Client(
            base_url=f"{cfg.supabase_url}/auth/v1",
            timeout=cfg.request_timeout_s,
            headers={"apikey": cfg.service_role_key, "content-type": "application/json"},
        )

    def close(self) -> None:
        self._http.close()

    def sign_in_anonymously(self) -> Identity:
        """POST /auth/v1/signup with an empty body — the REST form of
        supabase-js `signInAnonymously()`, which is what tagalng-pwa calls on
        load (`lana-conversation.tsx`).
        """
        resp = self._http.post("/signup", json={})
        if resp.status_code >= 400:
            raise AuthError(f"anonymous sign-in failed: HTTP {resp.status_code}: {resp.text[:300]}")
        body = resp.json()
        token = body.get("access_token")
        if not token:
            raise AuthError(f"anonymous sign-in returned no access_token: {body}")
        return Identity(
            user_id=decode_jwt_sub(token),
            access_token=token,
            refresh_token=body.get("refresh_token") or "",
            is_anonymous=True,
        )

    def refresh(self, refresh_token: str) -> Identity:
        """Downstream sections (P5-P8) refresh from the manifest rather than
        re-running signup (SPEC_P0_SIGNUP.md Appendix A).
        """
        resp = self._http.post(
            "/token", params={"grant_type": "refresh_token"}, json={"refresh_token": refresh_token}
        )
        if resp.status_code >= 400:
            raise AuthError(f"token refresh failed: HTTP {resp.status_code}: {resp.text[:300]}")
        body = resp.json()
        token = body["access_token"]
        user = body.get("user") or {}
        return Identity(
            user_id=decode_jwt_sub(token),
            access_token=token,
            refresh_token=body.get("refresh_token") or refresh_token,
            is_anonymous=bool(user.get("is_anonymous")),
            email=user.get("email"),
        )

    def link_email(self, identity: Identity, email: str) -> tuple[int, dict[str, Any]]:
        """PUT the email onto the EXISTING anonymous user (P0 step 10).

        This is an identity link, not a new signup: the whole point of P0 is that
        `uid_post == uid_pre`, i.e. the pre-signup conversation survives.
        """
        resp = self._http.put(
            "/user",
            headers={"authorization": f"Bearer {identity.access_token}"},
            json={"email": email},
        )
        try:
            body = resp.json()
        except Exception:
            body = {"_raw": resp.text}
        return resp.status_code, body

    def verify_email_otp(self, email: str, token: str) -> tuple[int, dict[str, Any]]:
        """POST /auth/v1/verify (P0 step 12)."""
        resp = self._http.post("/verify", json={"type": "email_change", "email": email, "token": token})
        try:
            body = resp.json()
        except Exception:
            body = {"_raw": resp.text}
        return resp.status_code, body


class Db:
    """Service-role PostgREST + RPC. Read-only except for the results sink."""

    def __init__(self, cfg: Config):
        self._cfg = cfg
        self._openapi: dict[str, Any] | None = None
        self._http = httpx.Client(
            base_url=f"{cfg.supabase_url}/rest/v1",
            timeout=cfg.request_timeout_s,
            headers={
                "apikey": cfg.service_role_key,
                "authorization": f"Bearer {cfg.service_role_key}",
                "content-type": "application/json",
            },
        )

    def close(self) -> None:
        self._http.close()

    def select(self, table: str, *, columns: str = "*", **filters: str) -> list[dict[str, Any]]:
        params = {"select": columns, **filters}
        resp = self._http.get(f"/{table}", params=params)
        resp.raise_for_status()
        return resp.json()

    def insert(self, table: str, rows: list[dict[str, Any]], *, upsert: bool = False) -> list[dict[str, Any]]:
        if self._cfg.dry_run:
            return []
        headers = {"prefer": "return=representation" + (",resolution=merge-duplicates" if upsert else "")}
        resp = self._http.post(f"/{table}", headers=headers, json=rows)
        if resp.status_code >= 400:
            raise RuntimeError(f"insert into {table} failed: HTTP {resp.status_code}: {resp.text[:400]}")
        return resp.json()

    def rpc(self, name: str, args: dict[str, Any] | None = None) -> Any:
        resp = httpx.post(
            f"{self._cfg.supabase_url}/rest/v1/rpc/{name}",
            headers={
                "apikey": self._cfg.service_role_key,
                "authorization": f"Bearer {self._cfg.service_role_key}",
                "content-type": "application/json",
            },
            json=args or {},
            timeout=self._cfg.request_timeout_s,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"rpc {name} failed: HTTP {resp.status_code}: {resp.text[:400]}")
        return resp.json()

    # --------------------------------------------------------- typed shortcuts

    def user_row(self, user_id: str) -> dict[str, Any] | None:
        rows = self.select("users", **{"id": f"eq.{user_id}"})
        return rows[0] if rows else None

    def claims(self, user_id: str) -> list[dict[str, Any]]:
        return self.select(
            "user_identity_claims",
            columns="concept,label,bucket,disclosure,confidence,dismissed_at,place_ref",
            **{"user_id": f"eq.{user_id}"},
        )

    def claim_keys(self, user_id: str) -> set[tuple[str, str]]:
        """(concept, lower(label)) — the comparison key P0 E01 and P1 S12 use."""
        return {(c["concept"], (c["label"] or "").lower()) for c in self.claims(user_id)}

    def circle_affiliations(self, user_id: str) -> list[dict[str, Any]]:
        return self.select("circle_affiliations", **{"user_id": f"eq.{user_id}"})

    def local_signals(self, user_id: str) -> list[dict[str, Any]]:
        return self.select("local_signals", **{"user_id": f"eq.{user_id}"})

    def sessions(self, user_id: str) -> list[dict[str, Any]]:
        return self.select("lana_sessions", **{"user_id": f"eq.{user_id}"})

    def columns_of(self, table: str) -> set[str]:
        """Pin a table's column set at run start.

        Both written specs require this. The two code-truth docs disagreed on
        whether `users.role` and `users.grammatical_gender` exist, and an
        assertion against an absent column must be `blocked`, not `fail`.
        (Settled against prod on 2026-07-30: both DO exist. The check stays
        because #109, #112 and #122 each add columns the specs reference.)

        Read from PostgREST's OpenAPI document rather than by sampling a row —
        a `limit 1` select on an empty table returns `[]` and would report a
        table as having no columns at all, turning every assertion against it
        into a spurious `blocked`.
        """
        return set(self._openapi_definitions().get(table, {}).get("properties", {}).keys())

    def _openapi_definitions(self) -> dict[str, Any]:
        if self._openapi is None:
            resp = self._http.get("/")
            resp.raise_for_status()
            self._openapi = resp.json().get("definitions", {}) or {}
        return self._openapi
