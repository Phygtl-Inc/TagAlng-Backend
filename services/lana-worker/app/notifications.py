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
from collections.abc import Callable
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
    sets the PWA origin; the fallback is the public production domain, since a link in a
    mail somebody already received cannot be fixed later — a preview host in an inbox is
    a dead link the day that preview rotates."""
    base = os.environ.get("APP_BASE_URL", "https://get.lana.help").strip().rstrip("/")
    if not path.startswith("/"):
        path = "/" + path
    return base + path


_BRAND = "#c2410c"       # Lana's warm orange — the button and the rule under the header
_INK = "#1f2937"
_MUTED = "#6b7280"
_CARD = "#ffffff"
_PAGE = "#f6f4f1"        # warm paper, so the card has an edge in light mode
_HAIR = "#e7e2dc"


def email_html(
    heading: str,
    body: str,
    cta_label: str | None = None,
    cta_path: str | None = None,
    note: str | None = None,
    *,
    preheader: str | None = None,
    badge: str | None = None,
    kicker: str | None = None,
    facts: list[tuple[str, str]] | None = None,
) -> str:
    """One branded transactional email. The only email layout in the app — every
    notification renders through here, so it is worth the inline CSS.

    heading/body are the message. Everything else is optional and stays out of the way
    when unused, which is what keeps the older callers unchanged:
      preheader  the grey line the inbox shows next to the subject. Unset and the client
                 grabs the first words of the body instead, which reads like a leak.
      badge      one emoji in the header disc — the thing the eye lands on first.
      kicker     small caps line above the heading ("Community · Lake Nona YMCA").
      facts      (label, value) rows in a bordered block — when, where, who. A meet
                 invite that makes someone open the app to learn the date has failed.
      note       one muted line under the body.

    Layout is a single centred table (Outlook ignores max-width on divs) holding one
    card. Colors are set explicitly on every block: a client that flips to dark mode
    inverts what it can guess and leaves the rest, and half-inverted mail is unreadable.
    """
    def _row(label: str, value: str) -> str:
        return (
            f'<tr><td style="padding:7px 0;font-size:13px;color:{_MUTED};'
            f'white-space:nowrap;vertical-align:top">{label}</td>'
            f'<td style="padding:7px 0 7px 14px;font-size:14px;color:{_INK};'
            f'font-weight:600;vertical-align:top">{value}</td></tr>'
        )

    hidden_preheader = (
        f'<div style="display:none;max-height:0;overflow:hidden;opacity:0;'
        f'mso-hide:all">{preheader}</div>' if preheader else ""
    )
    disc = (
        f'<td width="40" style="width:40px;padding-right:12px"><div style="width:40px;'
        f'height:40px;line-height:40px;border-radius:20px;background:#fdf1ea;'
        f'text-align:center;font-size:20px">{badge}</div></td>' if badge else ""
    )
    kicker_html = (
        f'<p style="margin:0 0 6px;font-size:11px;letter-spacing:.09em;'
        f'text-transform:uppercase;color:{_BRAND};font-weight:700">{kicker}</p>'
        if kicker else ""
    )
    facts_html = (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'width="100%" style="margin:18px 0 0;border-top:1px solid {_HAIR};'
        f'border-bottom:1px solid {_HAIR}">'
        + "".join(_row(label, value) for label, value in facts if value)
        + "</table>"
        if facts and any(v for _, v in facts) else ""
    )
    note_html = (
        f'<p style="margin:16px 0 0;font-size:13px;line-height:1.5;color:{_MUTED}">'
        f'{note}</p>' if note else ""
    )
    cta_html = (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'style="margin:22px 0 0"><tr><td style="border-radius:9999px;'
        f'background:{_BRAND}"><a href="{app_url(cta_path)}" '
        f'style="display:inline-block;padding:13px 30px;font-size:15px;font-weight:600;'
        f'color:#ffffff;text-decoration:none">{cta_label} &nbsp;&rarr;</a>'
        f"</td></tr></table>" if cta_label and cta_path else ""
    )
    return (
        f'<div style="background:{_PAGE};padding:28px 12px;'
        'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,Helvetica,'
        'Arial,sans-serif">'
        + hidden_preheader
        + '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'align="center" width="100%" style="max-width:520px;margin:0 auto">'
        f'<tr><td style="background:{_CARD};border:1px solid {_HAIR};'
        'border-radius:16px;padding:28px 26px 30px">'
        # Header: badge disc + wordmark, over a hairline rule.
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'width="100%"><tr>'
        + disc
        + f'<td style="vertical-align:middle"><span style="font-size:17px;'
        f'font-weight:700;color:{_INK};letter-spacing:-.01em">Lana</span>'
        f'<span style="font-size:13px;color:{_MUTED}"> · your block concierge</span>'
        "</td></tr></table>"
        f'<div style="height:1px;background:{_HAIR};margin:18px 0 20px"></div>'
        + kicker_html
        + f'<h1 style="margin:0 0 10px;font-size:21px;line-height:1.3;'
        f'color:{_INK};font-weight:700;letter-spacing:-.01em">{heading}</h1>'
        + f'<p style="margin:0;font-size:15px;line-height:1.6;color:#4b5563">{body}</p>'
        + facts_html
        + note_html
        + cta_html
        + "</td></tr>"
        f'<tr><td style="padding:18px 26px 0;font-size:12px;line-height:1.5;'
        f'color:{_MUTED};text-align:center">You are getting this because you joined a '
        f'community in Lana.<br>'
        f'<a href="{app_url("/")}" style="color:{_MUTED};text-decoration:underline">'
        "Open Lana</a> to change what you hear about.</td></tr>"
        "</table></div>"
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


# How many of a community's members one event may email. Big communities are rare
# today; the cap is here so one publish or join can never become an unbounded mail-out.
# ponytail: hard cap, move to a queued digest if communities outgrow it.
COMMUNITY_MAIL_CAP = 200


def mail_community_members(
    place_id: str,
    *,
    exclude_user_id: str | None = None,
    render: Callable[[str | None], tuple[str, str]],
    cap: int = COMMUNITY_MAIL_CAP,
) -> int:
    """Email a community's confirmed members, each in their own language. Returns how
    many were mailed.

    `render(lang)` builds (subject, html) for one recipient's locale. Two queries for the
    whole roster — a fan-out must never issue a lookup per person. Best-effort: any
    failure mails nobody rather than raising into the caller's flow.
    """
    pid = str(place_id or "").strip()
    sb = service_client()
    if not pid or sb is None:
        return 0
    try:
        q = (
            sb.table("circle_affiliations")
            .select("user_id")
            .eq("place_ref", pid)
            .eq("status", "confirmed")
            .is_("dismissed_at", "null")
        )
        if exclude_user_id:
            q = q.neq("user_id", str(exclude_user_id))
        rows = q.limit(cap).execute().data or []
    except Exception:  # noqa: BLE001
        _log.warning("community_mail_roster_failed place=%s", pid, exc_info=True)
        return 0
    ids = list(dict.fromkeys(str(r["user_id"]) for r in rows if r.get("user_id")))
    if not ids:
        return 0
    try:
        contacts = sb.table("users").select("id,email,locale").in_("id", ids).execute().data or []
    except Exception:  # noqa: BLE001
        _log.warning("community_mail_contacts_failed place=%s", pid, exc_info=True)
        return 0
    sent = 0
    for row in contacts:
        email = str(row.get("email") or "").strip()
        if not email:
            continue
        subject, html = render(str(row.get("locale") or "").strip() or None)
        send_email(email, subject=subject, html=html)
        sent += 1
    _log.info("community_mailed place=%s roster=%s sent=%s", pid, len(ids), sent)
    return sent


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
