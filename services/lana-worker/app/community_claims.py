"""Claiming a community that already exists on Lana, from her profile (backend-asks §62).

The SQL owns the rules (20270205120000): who may claim, one open claim per place, one
open claim per claimant, and only-decided-claims-close. This module authenticates
nothing — main.py does — and turns the SQL's refusal words into ClaimError codes the
routes hand straight to the client as `detail`.

The rows are ordinary place_claims rows (origin='in_app'), so the admin portal's review
queue and approve_place_claim / reject_place_claim decide them unchanged. The decision
is made in SQL by a reviewer, so the worker learns of it afterwards: the portal calls
/hooks/claim-decision, a token sweep catches anything that call missed, and her own
/claims/mine settles whatever is still owed. decision_notified_at makes all three safe
to run together — each decision is delivered once.
"""

from __future__ import annotations

import html
import logging
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from app.auth import service_client

logger = logging.getLogger(__name__)

PROOF_BUCKET = "community-claim-proofs"
_PROOF_TYPES = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/heic": "heic",
    "image/heif": "heif",
}
_PROOF_MAX_BYTES = 5 * 1024 * 1024

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_NOTE_MAX = 2000
_PHONE_MAX = 32

# SQL refusal → the code a client branches on. Anything unlisted is 'claim_failed'.
_SQL_CODES = (
    "already_claimed",
    "already_yours",
    "claim_open",
    "no_such_place",
    "no_such_claim",
    "not_decided",
)

_DECIDED = ("verified", "rejected")


class ClaimError(ValueError):
    """A refusal with the word the client reads, and the HTTP status it travels as."""

    def __init__(self, code: str, status: int = 400) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


_STATUS_FOR = {
    "already_claimed": 409,
    "already_yours": 409,
    "claim_open": 409,
    "no_such_place": 404,
    "no_such_claim": 404,
    "not_decided": 409,
}


def _sql_error(exc: Exception) -> ClaimError:
    text = str(getattr(exc, "message", "") or exc)
    code = next((c for c in _SQL_CODES if c in text), "claim_failed")
    return ClaimError(code, _STATUS_FOR.get(code, 500))


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except (ValueError, TypeError):
        return False
    return True


# ── proof photo ─────────────────────────────────────────────────────────────────
def upload_proof(user_id: str, raw: bytes, content_type: str) -> str:
    """Store one proof photo under {user_id}/ in the private bucket; returns its path.
    The path is only ever accepted back from the same user (see submit_claim)."""
    if not raw:
        raise ClaimError("proof_empty")
    if len(raw) > _PROOF_MAX_BYTES:
        raise ClaimError("proof_too_large", 413)
    ext = _PROOF_TYPES.get(content_type)
    if not ext:
        raise ClaimError("proof_type_not_allowed", 415)
    path = f"{user_id}/{uuid.uuid4().hex}.{ext}"
    service_client().storage.from_(PROOF_BUCKET).upload(
        path, raw, file_options={"content-type": content_type, "upsert": "false"}
    )
    return path


# ── the row her profile reads ───────────────────────────────────────────────────
def _with_glyphs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add circle_type + emoji the way her communities list draws them: the emoji a
    member already gave this place, else the type's glyph. A read only — never asks the
    model to mint one (that happens on join)."""
    from app.circles_flow import place_relation_emoji

    place_ids = sorted({str(r.get("place_id") or "") for r in rows if r.get("place_id")})
    stored: dict[str, str] = {}
    if place_ids:
        try:
            res = (
                service_client()
                .table("circle_affiliations")
                .select("place_ref, emoji")
                .in_("place_ref", place_ids)
                .not_.is_("emoji", "null")
                .limit(200)
                .execute()
            )
            for a in res.data or []:
                stored.setdefault(str(a.get("place_ref") or ""), str(a.get("emoji") or ""))
        except Exception:  # noqa: BLE001 — the glyph is decoration
            logger.warning("claim_glyph_read_failed", exc_info=True)
    out = []
    for r in rows:
        circle_type = str(r.get("place_type") or "").strip().lower() or "other"
        out.append(
            {
                **{k: v for k, v in r.items() if k != "place_type"},
                "circle_type": circle_type,
                "emoji": place_relation_emoji(circle_type, stored.get(str(r.get("place_id")))),
            }
        )
    return out


# ── submit / list / close ───────────────────────────────────────────────────────
def submit_claim(
    user_id: str,
    *,
    place_id: str,
    contact_email: str,
    contact_phone: str | None = None,
    note: str | None = None,
    proof_path: str | None = None,
) -> dict[str, Any]:
    place_id = (place_id or "").strip()
    if not _is_uuid(place_id):
        raise ClaimError("place_required")
    email = (contact_email or "").strip()
    if not _EMAIL_RE.match(email) or len(email) > 254:
        raise ClaimError("invalid_email")
    phone = (contact_phone or "").strip() or None
    if phone and len(phone) > _PHONE_MAX:
        raise ClaimError("invalid_phone")
    note = (note or "").strip() or None
    if note and len(note) > _NOTE_MAX:
        raise ClaimError("note_too_long")
    proof = (proof_path or "").strip() or None
    # Only a file she uploaded: a path is a pointer into a private bucket, and attaching
    # someone else's would put their proof in front of a reviewer as hers.
    if proof and (not proof.startswith(f"{user_id}/") or ".." in proof):
        raise ClaimError("invalid_proof")

    try:
        res = service_client().rpc(
            "submit_community_claim_for",
            {
                "p_user_id": user_id,
                "p_place_id": place_id,
                "p_contact_email": email,
                "p_contact_phone": phone,
                "p_note": note,
                "p_proof_path": proof,
            },
        ).execute()
    except Exception as exc:  # noqa: BLE001
        err = _sql_error(exc)
        if err.code == "claim_failed":
            logger.exception("community_claim_submit_failed place=%s", place_id)
        raise err from None
    row = res.data if isinstance(res.data, dict) else None
    if not row:
        raise ClaimError("claim_failed", 500)
    logger.info("community_claim_submitted claim=%s place=%s", row.get("claim_id"), place_id)
    return _with_glyphs([row])[0]


def my_claims(user_id: str) -> list[dict[str, Any]]:
    """Her in-app claims that are pending or decided-and-not-closed, newest first.

    Settles any decision still owed to her first (silently — she is looking at it), so
    an approval the portal never reported still makes her a member of the community."""
    deliver_owed_decisions(user_id=user_id, notify=False)
    try:
        res = service_client().rpc("my_community_claims_for", {"p_user_id": user_id}).execute()
    except Exception as exc:  # noqa: BLE001
        logger.exception("community_claims_list_failed")
        raise ClaimError("claim_failed", 500) from exc
    rows = res.data if isinstance(res.data, list) else []
    return _with_glyphs([r for r in rows if isinstance(r, dict)])


def close_claim(user_id: str, claim_id: str) -> None:
    claim_id = (claim_id or "").strip()
    if not _is_uuid(claim_id):
        raise ClaimError("no_such_claim", 404)
    try:
        service_client().rpc(
            "close_community_claim_for", {"p_user_id": user_id, "p_claim_id": claim_id}
        ).execute()
    except Exception as exc:  # noqa: BLE001
        err = _sql_error(exc)
        if err.code == "claim_failed":
            logger.exception("community_claim_close_failed claim=%s", claim_id)
        raise err from None


# ── the decision, delivered once ────────────────────────────────────────────────
def is_reviewer(user_id: str) -> bool:
    """The same standing approve_place_claim demands (users.founder_role='internal')."""
    try:
        res = (
            service_client()
            .table("users")
            .select("founder_role")
            .eq("id", user_id)
            .limit(1)
            .execute()
        )
    except Exception:  # noqa: BLE001
        return False
    rows = res.data or []
    return bool(rows) and rows[0].get("founder_role") == "internal"


def _take_decision(claim_id: str) -> dict[str, Any] | None:
    """Mark one decided in-app claim as delivered and return it — or None when it is not
    decided, not in-app, or already delivered. The conditional update is the lock: two
    callers racing on one claim, exactly one gets the row back."""
    res = (
        service_client()
        .table("place_claims")
        .update({"decision_notified_at": datetime.now(UTC).isoformat()})
        .eq("id", claim_id)
        .eq("origin", "in_app")
        .in_("status", list(_DECIDED))
        .is_("decision_notified_at", "null")
        .execute()
    )
    rows = res.data or []
    return rows[0] if rows else None


def deliver_decision(claim_id: str, *, notify: bool = True) -> bool:
    """Act on one decision: an approval makes her a member of the community (operator
    standing already came with places.claimed_by), and either way she is told — push to
    her devices, email to the address she gave on the claim. True when this call
    delivered it; False when it was not owed. Never raises."""
    try:
        claim = _take_decision(claim_id)
    except Exception:  # noqa: BLE001
        logger.exception("claim_decision_take_failed claim=%s", claim_id)
        return False
    if not claim:
        return False
    user_id = str(claim.get("requested_by") or "")
    place_id = str(claim.get("place_id") or "")
    approved = claim.get("status") == "verified"

    if approved:
        # Without a membership row the community never reaches her list, and her list is
        # where "created communities" are drawn from.
        from app.community_discovery import join_community

        try:
            join_community(user_id, place_id)
        except Exception:  # noqa: BLE001
            logger.exception("claim_approved_join_failed claim=%s", claim_id)

    if notify:
        try:
            _notify(claim, approved=approved)
        except Exception:  # noqa: BLE001
            logger.exception("claim_decision_notify_failed claim=%s", claim_id)
    logger.info(
        "claim_decision_delivered claim=%s approved=%s notified=%s", claim_id, approved, notify
    )
    return True


def deliver_owed_decisions(
    *, user_id: str | None = None, limit: int = 100, notify: bool = True
) -> int:
    """Every decided in-app claim nobody has delivered yet (optionally only hers)."""
    try:
        q = (
            service_client()
            .table("place_claims")
            .select("id")
            .eq("origin", "in_app")
            .in_("status", list(_DECIDED))
            .is_("decision_notified_at", "null")
        )
        if user_id:
            q = q.eq("requested_by", user_id)
        rows = q.order("resolved_at").limit(int(limit)).execute().data or []
    except Exception:  # noqa: BLE001
        logger.exception("claim_decisions_owed_read_failed")
        return 0
    return sum(1 for r in rows if deliver_decision(str(r["id"]), notify=notify))


def _notify(claim: dict[str, Any], *, approved: bool) -> None:
    from app.i18n import t
    from app.notifications import (
        _user_contact,
        email_html,
        recipient_lang,
        send_email,
        send_push,
    )

    user_id = str(claim.get("requested_by") or "")
    lang = recipient_lang(user_id)
    place = _place_name(str(claim.get("place_id") or "")) or t("notify.claim.place_fallback", lang)
    reference = f"CL-{claim.get('reference_no')}" if claim.get("reference_no") else None
    key = "notify.claim_approved" if approved else "notify.claim_declined"

    send_push(
        user_id,
        title=t(f"{key}.title", lang, place=place),
        body=t(f"{key}.body", lang, place=place),
        url="/",
    )

    to = str(claim.get("contact_email") or "").strip() or (_user_contact(user_id)[0] or "")
    if not to:
        return
    facts: list[tuple[str, str]] = []
    if reference:
        facts.append((t("notify.claim.reference", lang), html.escape(reference)))
    note = str(claim.get("review_notes") or "").strip()
    if note and not approved:
        facts.append((t("notify.claim.reviewer_note", lang), html.escape(note)))
    send_email(
        to,
        subject=t(f"{key}.subject", lang, place=place),
        html=email_html(
            t(f"{key}.title", lang, place=html.escape(place)),
            t(f"{key}.email", lang, place=html.escape(place)),
            t("notify.claim.cta", lang),
            "/",
            badge="✅" if approved else "📋",
            facts=facts or None,
            footer_note=t("notify.claim.footer", lang),
        ),
    )


def _place_name(place_id: str) -> str | None:
    if not place_id:
        return None
    try:
        rows = (
            service_client().table("places").select("name").eq("id", place_id).limit(1).execute()
        ).data or []
    except Exception:  # noqa: BLE001
        return None
    if not rows:
        return None
    return str(rows[0].get("name") or "").strip() or None
