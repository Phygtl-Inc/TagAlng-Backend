"""Directed recommendation asks — Lana picks the few neighbors who can actually answer.

The old "Yes, ask my neighbors" wrote one `tip_seek` row and contacted nobody; the reply
said otherwise. This module is the mechanism that makes the sentence true, and its shape is
chosen so the sentence CANNOT drift from it again: `route_tip_ask` returns an outcome, and
the caller composes the receipt from that outcome rather than from its own intention.

Selection, in one pass:

    radius-bounded peers whose identity claims match the ask   (fetch_peers_semantic)
      -> domain standing on the ask's concepts, with the quote  (authority.best_authority)
      -> drop anyone below MIN_EXPLICIT_SCORE                   (§A4 anti-gaming floor)
      -> drop muted / recently-asked / never-answering          (tip_ask_recipients)
      -> Lana reads the shortlist and picks at most 3, or none

The last step is a judgement, not a threshold, because the scores only prove that someone
has standing on a nearby concept — not that they can answer THIS question. Returning none
is a first-class result: emailing nobody costs one turn, while emailing five people who
cannot help costs the channel permanently. That asymmetry is the whole design.

Nothing here decides what the user meant; it runs only after they have explicitly said yes.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from app.auth import service_client

_log = logging.getLogger(__name__)

MAX_RECIPIENTS = 3
_SHORTLIST = 8
_COOLDOWN_DAYS = 7
_FATIGUE_LIMIT = 2  # consecutive unanswered asks before we stop asking someone


def _int_env(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def enabled() -> bool:
    """Off by default until T7 coverage says the authority tier can carry it.

    A flag rather than a gradual rollout because the two states are behaviourally
    different, not differently tuned: off, the ask is a passive listen and the receipt says
    so; on, real people get mail.

    The flag is ANDed with a working unsubscribe on purpose. mute_link needs a signing
    secret and the worker's public URL, and without either it returns None — so a
    half-configured deploy would otherwise start emailing people about strangers' asks
    with no way to opt out. Make that state unreachable rather than documented.
    """
    if os.environ.get("LANA_ASK_ROUTING", "0").strip() in {"", "0", "false", "off"}:
        return False
    if not _unsubscribe_ready():
        _log.error("tip_ask_routing_disabled: no unsubscribe link (secret or public URL unset)")
        return False
    return True


def _unsubscribe_ready() -> bool:
    return bool(_signing_key() and os.environ.get("LANA_WORKER_PUBLIC_URL", "").strip())


# ---------------------------------------------------------------------------
# Eligibility — the three anti-spam rules, all predicates on tip_ask_recipients
# ---------------------------------------------------------------------------
def eligible_recipients(candidate_ids: list[str]) -> set[str]:
    """Which of these people may be asked right now.

    Split out and pure-ish so the rules can be tested without a router run: the failure we
    are guarding against (one helpful neighbor becoming the block's answering service) is
    invisible in any single turn and only shows up across weeks.
    """
    ids = [str(c) for c in candidate_ids if str(c or "").strip()]
    if not ids:
        return set()
    sb = service_client()
    if sb is None:
        return set()

    allowed = set(ids)
    try:
        muted = sb.table("tip_ask_mutes").select("user_id").in_("user_id", ids).execute()
        allowed -= {str(r["user_id"]) for r in (muted.data or []) if r.get("user_id")}
    except Exception:  # noqa: BLE001 — a mute we cannot read must not become a send
        _log.warning("tip_ask_mutes_read_failed", exc_info=True)
        return set()

    if not allowed:
        return allowed

    cutoff = datetime.now(timezone.utc) - timedelta(
        days=_int_env("LANA_ASK_COOLDOWN_DAYS", _COOLDOWN_DAYS)
    )
    try:
        rows = (
            sb.table("tip_ask_recipients")
            .select("recipient_user_id,status,created_at")
            .in_("recipient_user_id", sorted(allowed))
            .order("created_at", desc=True)
            .limit(200)
            .execute()
        ).data or []
    except Exception:  # noqa: BLE001
        _log.warning("tip_ask_history_read_failed", exc_info=True)
        return set()

    history: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        uid = str(row.get("recipient_user_id") or "")
        if uid:
            history.setdefault(uid, []).append(row)

    fatigue_limit = _int_env("LANA_ASK_FATIGUE_LIMIT", _FATIGUE_LIMIT)
    for uid, entries in history.items():
        # Cooldown: asked at all inside the window.
        if any(_after(e.get("created_at"), cutoff) for e in entries):
            allowed.discard(uid)
            continue
        # Fatigue: the last N asks all went unanswered, so this is not their thing.
        if fatigue_limit and len(entries) >= fatigue_limit:
            recent = entries[:fatigue_limit]
            if all(str(e.get("status") or "") != "answered" for e in recent):
                allowed.discard(uid)
    return allowed


def _after(value: Any, cutoff: datetime) -> bool:
    if not value:
        return False
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return False
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp > cutoff


# ---------------------------------------------------------------------------
# The pick
# ---------------------------------------------------------------------------
_PICK_SYSTEM = (
    "A neighbor asked for a local recommendation. You choose which of their neighbors is "
    "worth emailing about it — people who are about to be interrupted on someone else's "
    "behalf, so the bar is high. Each candidate comes with evidence: something they said "
    "about themselves, in their own words. "
    "Pick ONLY those whose evidence shows they would actually know the answer to THIS "
    "ask. Shared topic is not enough — someone who runs marathons cannot recommend a "
    "pediatric dentist. Choosing nobody is the right answer more often than not, and an "
    "empty list is always acceptable. Never pick more than 3. "
    'Output only valid JSON: {"picks":[{"user_id":"…","reason":"…"}]}. '
    "reason = one warm sentence, addressed to the CANDIDATE, saying why you thought of "
    "them, grounded in their own evidence ('you mentioned you've been going to the "
    "farmers market for years'). Never invent evidence. Never mention scores or ranking."
)


def _pick(ask_text: str, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Lana's judgement over the shortlist. [] on any failure — never a blind fallback
    to 'email everyone', because the failure mode we refuse is sending too much."""
    from app.orchestrator.llm import llm_configured, llm_json, router_model

    if not candidates or not llm_configured():
        return []
    payload = json.dumps(
        {
            "the_ask": ask_text[:300],
            "candidates": [
                {
                    "user_id": c["user_id"],
                    "evidence": (c.get("quote") or c.get("label") or "")[:200],
                    "how_far": c.get("distance_label") or "nearby",
                }
                for c in candidates
            ],
        },
        ensure_ascii=False,
    )
    try:
        raw = llm_json(
            model=router_model(),
            system=_PICK_SYSTEM,
            user_payload=payload,
            max_tokens=400,
            temperature=0.1,
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning("tip_ask_pick_failed: %s", exc)
        return []
    picks = raw.get("picks") if isinstance(raw, dict) else None
    if not isinstance(picks, list):
        return []
    by_id = {c["user_id"]: c for c in candidates}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for p in picks:
        if not isinstance(p, dict):
            continue
        uid = str(p.get("user_id") or "").strip()
        # Only ever someone we actually shortlisted: a hallucinated id must not become mail.
        if uid not in by_id or uid in seen:
            continue
        seen.add(uid)
        out.append(
            {
                "user_id": uid,
                "name": by_id[uid].get("name") or "a neighbor",
                "reason": str(p.get("reason") or "").strip()[:280],
            }
        )
        if len(out) >= MAX_RECIPIENTS:
            break
    return out


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------
def route_tip_ask(
    user_jwt: str,
    *,
    signal_id: str,
    asker_user_id: str,
    ask_text: str,
    asker_name: str | None = None,
) -> dict[str, Any]:
    """Pick and record the neighbors to ask. Returns the OUTCOME the receipt is built from:

        {"recipients": [{user_id, name, reason}], "none_qualified": bool, "error": bool}

    Recording is synchronous (the caller's own turn must know what it is about to claim);
    the mail goes out on a daemon thread, because nobody's chat should wait on SMTP.
    """
    outcome: dict[str, Any] = {"recipients": [], "none_qualified": False, "error": False}
    ask = str(ask_text or "").strip()
    if not (enabled() and ask and signal_id):
        outcome["none_qualified"] = True
        return outcome

    try:
        from app.authority import MIN_EXPLICIT_SCORE, best_authority, concepts_for_ask
        from app.layer1_handlers import fetch_peers_semantic

        peers = fetch_peers_semantic(user_jwt, ask, limit=_SHORTLIST)
        concepts = concepts_for_ask(ask)
        if not (peers and concepts):
            outcome["none_qualified"] = True
            return outcome

        candidates: list[dict[str, Any]] = []
        for peer in peers:
            uid = str(peer.get("peer_user_id") or "").strip()
            if not uid or uid == str(asker_user_id):
                continue
            # ponytail: one authority RPC per candidate, N<=8. Batch into a single
            # `attester_authority(p_user_ids[])` if this ever shows up in turn latency.
            standing = best_authority(uid, concepts)
            if not standing or standing["score"] < MIN_EXPLICIT_SCORE:
                continue
            candidates.append(
                {
                    "user_id": uid,
                    "name": str(peer.get("peer_nickname") or peer.get("nickname") or "").strip(),
                    "label": str(peer.get("matching_peer_label") or "").strip(),
                    "quote": standing.get("quote"),
                    "score": standing["score"],
                    "distance_label": _distance_label(peer.get("distance_meters")),
                    "distance": peer.get("distance_meters"),
                }
            )

        allowed = eligible_recipients([c["user_id"] for c in candidates])
        candidates = [c for c in candidates if c["user_id"] in allowed]
        # Strongest standing first, nearer neighbor breaking the tie — the order Lana reads.
        candidates.sort(key=lambda c: (-c["score"], c.get("distance") or 1e9))

        picks = _pick(ask, candidates[:_SHORTLIST])
        if not picks:
            outcome["none_qualified"] = True
            return outcome

        _record(signal_id=signal_id, asker_user_id=asker_user_id, picks=picks)
        outcome["recipients"] = picks
        _send_async(
            signal_id=signal_id,
            ask=ask,
            asker_name=_asker_name(asker_name, asker_user_id),
            picks=picks,
        )
        return outcome
    except Exception:  # noqa: BLE001 — a failed routing must be reported, never narrated as success
        _log.exception("tip_ask_route_failed signal=%s", signal_id)
        outcome["error"] = True
        return outcome


def _asker_name(from_session: str | None, asker_user_id: str) -> str:
    """Who to say is asking. The session context carries a nickname only sometimes, and
    the difference is visible in the mail: "A neighbor nearby is looking for…" reads like
    a mailshot, "Dom nearby is looking for…" reads like a neighbor. The name is already
    the asker's own — they are the one who chose to ask — so fall back to the profile
    rather than to anonymity."""
    name = str(from_session or "").strip()
    if name:
        return name
    try:
        from app.notifications import _user_contact

        return str((_user_contact(asker_user_id) or (None, None))[1] or "").strip() or "A neighbor"
    except Exception:  # noqa: BLE001
        return "A neighbor"


def _distance_label(meters: Any) -> str | None:
    try:
        m = float(meters)
    except (TypeError, ValueError):
        return None
    if m < 800:
        return "a few minutes' walk away"
    if m < 3000:
        return "a short walk away"
    return "nearby"


def _record(*, signal_id: str, asker_user_id: str, picks: list[dict[str, Any]]) -> None:
    sb = service_client()
    if sb is None:
        return
    sb.table("tip_ask_recipients").upsert(
        [
            {
                "signal_id": signal_id,
                "asker_user_id": asker_user_id,
                "recipient_user_id": p["user_id"],
                "reason": p.get("reason") or None,
                "status": "queued",
            }
            for p in picks
        ],
        on_conflict="signal_id,recipient_user_id",
    ).execute()


# ---------------------------------------------------------------------------
# The mail
# ---------------------------------------------------------------------------
def _send_async(*, signal_id: str, ask: str, asker_name: str, picks: list[dict[str, Any]]) -> None:
    try:
        threading.Thread(
            target=_send,
            kwargs={"signal_id": signal_id, "ask": ask, "asker_name": asker_name, "picks": picks},
            daemon=True,
        ).start()
    except Exception:  # noqa: BLE001
        _log.debug("tip_ask_send_spawn_failed", exc_info=True)


def _send(*, signal_id: str, ask: str, asker_name: str, picks: list[dict[str, Any]]) -> None:
    from app.i18n import localize_text
    from app.notifications import email_html, notify_user, recipient_langs

    sb = service_client()
    langs = recipient_langs([p["user_id"] for p in picks])
    for pick in picks:
        uid = pick["user_id"]
        lang = langs.get(uid)
        heading = localize_text(f"{asker_name} nearby is looking for a recommendation", lang)
        body = localize_text(pick.get("reason") or f"Do you know: {ask}?", lang)
        # The unsubscribe belongs in the FOOTER, under the CTA — as a short anchor, not a
        # bare URL. Passed raw it auto-links in the client and renders as three wrapped
        # lines of visible link text, louder than the thing we are actually asking for.
        # Only offered when it works (see mute_link): an opt-out we cannot honour is the
        # same false promise this whole change is about.
        opt_out = mute_link(uid)
        footer = (
            localize_text("Not your thing?", lang)
            + f' <a href="{opt_out}" style="color:inherit;text-decoration:underline">'
            + localize_text("Stop emailing me about neighbors' asks", lang)
            + "</a>"
            if opt_out
            else None
        )
        try:
            notify_user(
                uid,
                title=heading,
                body=body,
                url="/chat",
                email_subject=localize_text("A neighbor could use your recommendation", lang),
                email_html=email_html(
                    heading,
                    body,
                    localize_text("Share what you know", lang),
                    "/chat",
                    facts=[(localize_text("They asked for", lang), ask[:140])],
                    footer_note=footer,
                ),
            )
            status, stamp = "sent", datetime.now(timezone.utc).isoformat()
        except Exception:  # noqa: BLE001 — one bad address must not stop the batch
            _log.warning("tip_ask_send_failed user=%s", uid, exc_info=True)
            status, stamp = "failed", None
        if sb is not None:
            try:
                sb.table("tip_ask_recipients").update(
                    {"status": status, "sent_at": stamp}
                ).eq("signal_id", signal_id).eq("recipient_user_id", uid).execute()
            except Exception:  # noqa: BLE001
                _log.debug("tip_ask_status_update_failed", exc_info=True)


# ---------------------------------------------------------------------------
# Opt-out link — signed, no frontend route, no session needed
# ---------------------------------------------------------------------------
def _signing_key() -> str:
    """Reuses the sweeper's server-only secret rather than adding a deploy variable.
    Unset means no key, which means no link — never an unsigned one."""
    return os.environ.get("SIGNAL_SWEEP_TOKEN", "").strip()


def mute_token(user_id: str) -> str | None:
    import hashlib
    import hmac

    key = _signing_key()
    if not (key and user_id):
        return None
    return hmac.new(key.encode(), str(user_id).encode(), hashlib.sha256).hexdigest()[:32]


def mute_link(user_id: str) -> str | None:
    token = mute_token(user_id)
    # The WORKER's public origin, not APP_BASE_URL — /asks/mute is served here, not by the
    # PWA. Unset means no link, which (via enabled()) means no mail either.
    base = os.environ.get("LANA_WORKER_PUBLIC_URL", "").strip().rstrip("/")
    if not (token and base):
        return None
    return f"{base}/asks/mute?u={user_id}&t={token}"


def verify_mute(user_id: str, token: str) -> bool:
    import hmac

    expected = mute_token(user_id)
    return bool(expected and hmac.compare_digest(expected, str(token or "")))


def mute_asks(user_id: str) -> bool:
    sb = service_client()
    if sb is None:
        return False
    try:
        sb.table("tip_ask_mutes").upsert({"user_id": user_id}, on_conflict="user_id").execute()
        return True
    except Exception:  # noqa: BLE001
        _log.warning("tip_ask_mute_failed user=%s", user_id, exc_info=True)
        return False


def mark_answered(user_jwt: str, *, responder_user_id: str) -> None:
    """A recipient posted a tip — retire their open asks so fatigue never counts them.

    Called from the tip_share save path: the answer is what the whole outreach was for, and
    an un-retired row would make a neighbor who DID help look like someone who never does.
    """
    sb = service_client()
    if sb is None or not responder_user_id:
        return
    try:
        sb.table("tip_ask_recipients").update(
            {"status": "answered", "answered_at": datetime.now(timezone.utc).isoformat()}
        ).eq("recipient_user_id", responder_user_id).in_("status", ["queued", "sent"]).execute()
    except Exception:  # noqa: BLE001
        _log.debug("tip_ask_mark_answered_failed", exc_info=True)


# ---------------------------------------------------------------------------
# The return path — Lana raises the ask when the neighbor arrives
# ---------------------------------------------------------------------------
def pending_ask_for(user_id: str) -> dict[str, Any] | None:
    """The oldest unanswered ask this person has never been told about in chat.

    Without this the outreach was a dead end: the email's button opened an ordinary chat
    that said nothing about the ask, so the one neighbor who agreed to help had to
    remember the mail and bring it up themselves. Raised ONCE (surfaced_at), and only
    while the asker is still listening — answering a question somebody already withdrew
    wastes the goodwill this whole feature is spending.
    """
    sb = service_client()
    if sb is None or not user_id:
        return None
    try:
        rows = (
            sb.table("tip_ask_recipients")
            .select("id,signal_id,reason,asker_user_id")
            .eq("recipient_user_id", str(user_id))
            .is_("surfaced_at", "null")
            .in_("status", ["queued", "sent"])
            .order("created_at", desc=True)
            .limit(5)
            .execute()
        ).data or []
    except Exception:  # noqa: BLE001 — a missing column (pre-migration) is not an error
        _log.debug("pending_ask_lookup_failed", exc_info=True)
        return None

    for row in rows:
        try:
            sig = (
                sb.table("local_signals")
                .select("detail_text,status")
                .eq("id", row["signal_id"])
                .single()
                .execute()
            ).data or {}
        except Exception:  # noqa: BLE001
            continue
        if str(sig.get("status") or "") != "listening":
            continue  # taken down since — nothing to answer
        ask = str(sig.get("detail_text") or "").strip()
        if not ask:
            continue
        from app.notifications import _user_contact

        return {
            "row_id": row["id"],
            "ask": ask,
            "reason": str(row.get("reason") or "").strip(),
            "asker_name": str((_user_contact(row.get("asker_user_id")) or (None, None))[1] or "")
            or "A neighbor",
        }
    return None


def mark_surfaced(row_id: str) -> None:
    sb = service_client()
    if sb is None or not row_id:
        return
    try:
        sb.table("tip_ask_recipients").update(
            {"surfaced_at": datetime.now(timezone.utc).isoformat()}
        ).eq("id", row_id).execute()
    except Exception:  # noqa: BLE001
        _log.debug("mark_surfaced_failed", exc_info=True)


def opening_for_pending_ask(user_id: str, session_ctx: dict[str, Any]) -> str | None:
    """Lana's first line when someone arrives owing a neighbor an answer. None when there
    is nothing pending, which is the overwhelmingly common case and costs one query."""
    pending = pending_ask_for(user_id)
    if not pending:
        return None
    from app.reply_compose import compose_reply

    reply = compose_reply(
        goal=(
            "Open the conversation by passing on ONE neighbor's request, warmly and in two "
            "sentences at most. Name who is asking and what they asked for, say briefly why "
            "you thought of this person, and ask what they would tell them. They have not "
            "agreed to anything yet, so do not thank them and do not imply they already "
            "said yes — and never promise to pass on an answer they have not given."
        ),
        facts=[
            f"Who is asking: {pending['asker_name']}",
            f"What they asked for: {pending['ask'][:140]}",
            f"Why you thought of this person: {pending['reason'][:200]}"
            if pending["reason"]
            else "",
        ],
        session_ctx=session_ctx,
        fallback=(
            f"{pending['asker_name']} nearby is looking for {pending['ask'][:80]} — you came "
            "to mind. Anywhere you'd point them?"
        ),
    )
    mark_surfaced(str(pending["row_id"]))
    return reply
