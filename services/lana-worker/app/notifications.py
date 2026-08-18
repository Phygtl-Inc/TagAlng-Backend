"""Web Push (VAPID) + transactional email (Resend) — fire-and-forget.

No-ops cleanly when keys are unset (VAPID_PRIVATE_KEY / RESEND_API_KEY) or pywebpush
isn't installed, so the app runs fine before notifications are configured. Everything
runs on daemon threads so it never adds latency to — or breaks — a request.

Env:
  VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY   base64url keypair (FE uses the public one)
  VAPID_SUBJECT                          contact, e.g. "mailto:hello@tagalng.app"
  RESEND_API_KEY                         Resend transactional-email key
  RESEND_FROM                            verified sender, e.g. "Lana <hi@yourdomain>"
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any

import httpx

from app.auth import service_client

try:  # pywebpush is optional until `pip install -r requirements.txt` runs
    from pywebpush import WebPushException, webpush
except Exception:  # noqa: BLE001
    webpush = None  # type: ignore[assignment]
    WebPushException = Exception  # type: ignore[assignment,misc]

_log = logging.getLogger(__name__)


# ── URL + email helpers ──────────────────────────────────────────────────────
def app_url(path: str = "/") -> str:
    """Absolute app URL for email links (push can use relative paths). APP_BASE_URL env
    sets the PWA origin; falls back to the deployed Vercel host."""
    base = os.environ.get("APP_BASE_URL", "https://tag-alng-backend.vercel.app").strip().rstrip("/")
    if not path.startswith("/"):
        path = "/" + path
    return base + path


def email_html(
    heading: str,
    body: str,
    cta_label: str | None = None,
    cta_path: str | None = None,
    note: str | None = None,
) -> str:
    """Minimal branded email body. cta_* renders a button linking into the app; `note` is
    one muted context line under the body — the meet's community, for event mail."""
    cta = ""
    if cta_label and cta_path:
        cta = (
            f'<a href="{app_url(cta_path)}" style="display:inline-block;margin-top:16px;'
            'padding:10px 20px;background:#c2410c;color:#fff;border-radius:9999px;'
            f'text-decoration:none;font-weight:600">{cta_label}</a>'
        )
    return (
        '<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:480px;'
        'margin:0 auto;padding:24px;color:#1a1a1a">'
        f'<h1 style="font-size:20px;margin:0 0 8px">{heading}</h1>'
        f'<p style="font-size:15px;line-height:1.5;color:#444;margin:0">{body}</p>'
        + (
            f'<p style="font-size:13px;color:#666;margin:10px 0 0">{note}</p>'
            if note
            else ""
        )
        + f"{cta}"
        '<p style="font-size:12px;color:#999;margin-top:28px">Lana · your block concierge</p>'
        "</div>"
    )


# ── Web Push ─────────────────────────────────────────────────────────────────
def _push_one(sub: dict[str, Any], data_json: str, claims: dict[str, str], private_key: str) -> None:
    try:
        webpush(  # type: ignore[misc]
            subscription_info={
                "endpoint": sub["endpoint"],
                "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
            },
            data=data_json,
            vapid_private_key=private_key,
            vapid_claims=dict(claims),  # pywebpush mutates this (adds exp) — pass a copy
        )
    except WebPushException as exc:  # type: ignore[misc]
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status in (404, 410):  # subscription is dead — drop it
            try:
                service_client().table("push_subscriptions").delete().eq(
                    "endpoint", sub["endpoint"]
                ).execute()
            except Exception:  # noqa: BLE001
                pass
        else:
            _log.debug("webpush_failed", exc_info=True)
    except Exception:  # noqa: BLE001
        _log.debug("webpush_failed", exc_info=True)


def send_push(user_id: str | None, *, title: str, body: str, url: str | None = None) -> None:
    """Push to every device the user has subscribed. Best-effort; no-op without keys."""
    private_key = os.environ.get("VAPID_PRIVATE_KEY", "").strip()
    if webpush is None or not private_key or not user_id:
        return
    sb = service_client()
    if sb is None:
        return
    try:
        rows = (
            sb.table("push_subscriptions")
            .select("endpoint,p256dh,auth")
            .eq("user_id", str(user_id))
            .execute()
            .data
            or []
        )
    except Exception:  # noqa: BLE001
        return
    if not rows:
        return
    subject = os.environ.get("VAPID_SUBJECT", "mailto:hello@tagalng.app").strip()
    data_json = json.dumps({"title": title, "body": body, "url": url or "/"})
    for row in rows:
        try:
            threading.Thread(
                target=_push_one,
                args=(row, data_json, {"sub": subject}, private_key),
                daemon=True,
            ).start()
        except Exception:  # noqa: BLE001
            pass


# ── Email (Resend) ───────────────────────────────────────────────────────────
def _email_send(api_key: str, payload: dict[str, Any]) -> None:
    try:
        with httpx.Client(timeout=8.0) as client:
            res = client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {api_key}"},
                json=payload,
            )
        # A rejected send used to be invisible (response ignored, failures at DEBUG):
        # an unverified RESEND_FROM domain 403s every email app-wide and nothing said so.
        if res.status_code >= 400:
            _log.warning(
                "resend_rejected status=%s from=%s detail=%s",
                res.status_code,
                payload.get("from"),
                res.text[:200],
            )
    except Exception:  # noqa: BLE001
        _log.warning("resend_send_failed", exc_info=True)


def send_email(to: str | None, *, subject: str, html: str, text: str | None = None) -> None:
    """Send one transactional email via Resend. Best-effort; no-op without a key."""
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    if not api_key or not to:
        return
    sender = os.environ.get("RESEND_FROM", "Lana <notifications@resend.dev>").strip()
    payload: dict[str, Any] = {"from": sender, "to": [to], "subject": subject, "html": html}
    if text:
        payload["text"] = text
    try:
        threading.Thread(target=_email_send, args=(api_key, payload), daemon=True).start()
    except Exception:  # noqa: BLE001
        pass


def _user_contact(user_id: str | None) -> tuple[str | None, str | None]:
    """(email, nickname) for a user, via the service client. (None, None) on miss."""
    if not user_id:
        return None, None
    sb = service_client()
    if sb is None:
        return None, None
    try:
        row = (
            sb.table("users").select("email,nickname").eq("id", str(user_id)).single().execute()
        )
        data = row.data or {}
        return data.get("email"), data.get("nickname")
    except Exception:  # noqa: BLE001
        return None, None


def recipient_lang(user_id: str | None) -> str | None:
    """The language THIS recipient reads in (users.locale), or None for English.

    Notifications are the one outbound surface that addresses somebody other
    than the person taking the turn, so `session_lang` is the wrong source —
    the host cancelling a meet may be writing in English while an attendee
    reads Spanish. Best-effort: any failure returns None and the caller falls
    back to English rather than dropping the notification.
    """
    if not user_id:
        return None
    sb = service_client()
    if sb is None:
        return None
    try:
        row = (
            sb.table("users").select("locale").eq("id", str(user_id)).single().execute()
        )
        return ((row.data or {}).get("locale") or "").strip() or None
    except Exception:  # noqa: BLE001
        return None


def recipient_langs(user_ids: list[str]) -> dict[str, str | None]:
    """recipient_lang for a roster, in ONE query — fan-outs must not issue a
    lookup per person (the area-open push targets up to 500)."""
    ids = [str(u) for u in user_ids if u]
    if not ids:
        return {}
    sb = service_client()
    if sb is None:
        return {}
    try:
        rows = sb.table("users").select("id,locale").in_("id", ids).execute().data or []
    except Exception:  # noqa: BLE001
        return {}
    return {
        str(r.get("id")): ((r.get("locale") or "").strip() or None)
        for r in rows
        if isinstance(r, dict)
    }


def notify_user(
    user_id: str | None,
    *,
    title: str,
    body: str,
    url: str | None = None,
    email_subject: str | None = None,
    email_html: str | None = None,
    note: str | None = None,
) -> None:
    """Best-effort push + email to one user. Push uses title/body/url; email is sent only
    when subject + html are given and the user has an email on file. `note` is one extra
    context line on the push (the meet's community) — the email carries it via
    ``email_html(note=…)``, which the caller builds. Never raises."""
    try:
        send_push(user_id, title=title, body=f"{body}\n{note}" if note else body, url=url)
    except Exception:  # noqa: BLE001
        pass
    if email_subject and email_html:
        email, _ = _user_contact(user_id)
        if email:
            try:
                send_email(email, subject=email_subject, html=email_html)
            except Exception:  # noqa: BLE001
                pass
