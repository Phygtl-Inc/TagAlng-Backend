"""The "Help Lana learn more" round — reco_aspects, wired into the capture flow.

reco_aspects.py owns the method (split the statement, one question per section they
raised, band the words). This module owns the ROUND: the session state that the invite
card (screen 07) and the one-question-at-a-time card (screen 08) render, and the actions
that move it (start / answer / skip one / skip all).

LIFECYCLE
    1. The tip posts (tip_share, the `tip_listed_now` turn). open_after_post() splits
       the author's own words and stores `aspect_round` on the session, status 'offered'.
       Every section is persisted as an OPEN reco_aspect row at that moment, so an
       abandoned round is recoverable (decision F in reco_aspects).
    2. The turn response carries the round ONLY on that posting turn. It is not sticky:
       if they move on, the next turn has no round and the app shows nothing. The open
       rows remain, and reoffer_round() brings them back in a later session.
    3. The app drives it through POST /lana/sessions/{id}/aspect-answer, which calls
       apply_action(). A finished or skipped round is cleared from the session with None
       (not popped — a popped key resurrects through the {**old, **new} merge).

OFF BY DEFAULT (LANA_ASPECTS). The reco_aspect migrations (20261229120000-02) must be
applied before this is switched on; without them every write fails into a log line and
the answers are lost, which is worse than never asking.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

CTX_KEY = "aspect_round"

# Terminal round states. 'done' = every question answered or skipped one by one;
# 'skipped' = they declined the whole round (invite Skip, or "skip the rest").
_TERMINAL = frozenset({"done", "skipped"})


def aspects_enabled() -> bool:
    return os.environ.get("LANA_ASPECTS", "0").strip().lower() not in {"", "0", "false", "off"}


def _statement(draft: dict[str, Any]) -> str:
    """The author's own words about the thing: the trait + details, then what they said
    on the card. Never the joined recap (_detail_text), which carries the name, category
    and locality — splitting that would produce "aspects" they never commented on."""
    from app.tip_share import _description, _reco_fields

    parts: list[str] = []
    desc = str(_description(draft) or "").strip()
    if desc:
        parts.append(desc)
    # The answered steps, as the draft actually stores them (step_set + answers) —
    # _reco_fields is the one reader of that shape.
    for step in _reco_fields(draft) or []:
        ans = str(step.get("answer") or "").strip()
        # The consent toggle / agree row are the server's wording, and a picked place is a
        # name, not something they said about it.
        if ans and step.get("kind") not in {"toggle", "agree", "place"}:
            parts.append(ans)
    return ". ".join(parts)


def open_after_post(
    session_ctx: dict[str, Any],
    *,
    draft: dict[str, Any],
    signal_id: str | None,
    subject_ref: str | None,
    user_id: str | None,
) -> dict[str, Any] | None:
    """Split the just-posted recommendation into its round. Never raises.

    Returns the stored round, or None when there is nothing to ask (flag off, no id, or
    they did not comment on anything specific — an empty split is a valid outcome)."""
    if not aspects_enabled() or not signal_id or not user_id:
        return None
    try:
        from app.reco_aspects import open_aspect_questions

        questions = open_aspect_questions(
            signal_id=str(signal_id),
            subject_ref=subject_ref,
            author_id=str(user_id),
            statement=_statement(draft),
            subject_name=str(draft.get("name") or "").strip() or None,
        )
    except Exception:  # noqa: BLE001 — a posted tip must never fail on its follow-ups
        logger.exception("aspect_round_open_failed signal=%s", signal_id)
        return None
    if not questions:
        return None
    rnd = {
        "round_id": str(signal_id),
        "mode": "post",
        "subject_name": str(draft.get("name") or "").strip() or None,
        "status": "offered",
        "items": [
            {
                "aspect_key": q["aspect_key"],
                "aspect_label": q["aspect_label"],
                "source_span": q.get("source_span"),
                "question": q["question"],
                "signal_id": str(signal_id),
                "subject_ref": subject_ref,
                "state": "open",
                "answer": None,
            }
            for q in questions
        ],
    }
    session_ctx[CTX_KEY] = rnd
    return rnd


def reoffer_round(
    session_ctx: dict[str, Any], *, user_id: str, lang: str | None = None
) -> dict[str, Any] | None:
    """A later session: the sections this person raised and never graded. Never raises.

    One recommendation per round — the invite names ONE subject, and a round that hops
    between Dr. Sarah and a bakery mid-way reads as a form, not a conversation."""
    if not aspects_enabled() or not user_id:
        return None
    try:
        from app.reco_aspects import reoffer_open_aspects

        rows = reoffer_open_aspects(str(user_id))
    except Exception:  # noqa: BLE001
        logger.exception("aspect_round_reoffer_failed user=%s", user_id)
        return None
    if not rows:
        return None
    first = rows[0]["signal_id"]
    rows = [r for r in rows if r["signal_id"] == first]
    name = _subject_name(first)
    questions = [r["question"] for r in rows]
    if lang:
        try:
            from app.i18n import localize_text

            questions = [localize_text(q, lang) for q in questions]
        except Exception:  # noqa: BLE001 — English beats nothing
            logger.debug("aspect_round_localize_failed", exc_info=True)
    rnd = {
        "round_id": str(first),
        "mode": "reoffer",
        "subject_name": name,
        "status": "offered",
        "items": [
            {
                "aspect_key": r["aspect_key"],
                "aspect_label": r["aspect_label"],
                "source_span": r.get("source_span"),
                "question": q,
                "signal_id": str(r["signal_id"]),
                "subject_ref": r.get("subject_ref"),
                "state": "open",
                "answer": None,
            }
            for r, q in zip(rows, questions)
        ],
    }
    session_ctx[CTX_KEY] = rnd
    return rnd


def _subject_name(signal_id: str) -> str | None:
    try:
        from app.auth import service_client

        res = (
            service_client()
            .table("local_signals")
            .select("reco_name")
            .eq("id", signal_id)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        return (str(rows[0].get("reco_name") or "").strip() or None) if rows else None
    except Exception:  # noqa: BLE001
        return None


def public_round(rnd: dict[str, Any] | None) -> dict[str, Any] | None:
    """What the client sees. Internal ids (signal_id, subject_ref, source_span) stay
    server-side; there is no sentiment here because there is none on the round at all."""
    if not isinstance(rnd, dict) or not rnd.get("items"):
        return None
    items = [
        {
            "aspect_key": it["aspect_key"],
            "label": it["aspect_label"],
            "question": it["question"],
            "state": it.get("state") or "open",
            "answer": it.get("answer"),
        }
        for it in rnd["items"]
    ]
    current = next((i for i, it in enumerate(items) if it["state"] == "open"), None)
    return {
        "round_id": rnd.get("round_id"),
        "mode": rnd.get("mode") or "post",
        "subject_name": rnd.get("subject_name"),
        "status": rnd.get("status") or "offered",
        "current": current,
        "total": len(items),
        "answered": sum(1 for it in items if it["state"] == "answered"),
        "items": items,
    }


class RoundError(Exception):
    """A client action that does not fit the round (unknown key, finished round)."""


def apply_action(
    session_ctx: dict[str, Any],
    *,
    action: str,
    author_id: str,
    aspect_key: str | None = None,
    answer: str | None = None,
    source: str = "voice",
) -> dict[str, Any]:
    """Advance the round. Returns the updated (internal) round.

    Only keys that belong to THIS session's round are accepted — the same intersection
    tip-setup uses, and what stops a client writing arbitrary aspects onto someone's
    recommendation. The row's author is always the caller."""
    from app.reco_aspects import record_aspect

    rnd = session_ctx.get(CTX_KEY)
    if not isinstance(rnd, dict) or not rnd.get("items"):
        raise RoundError("no_aspect_round")
    if rnd.get("status") in _TERMINAL:
        raise RoundError("aspect_round_closed")

    if action == "start":
        rnd["status"] = "active"
        return rnd

    if action == "skip_all":
        # A decision, not an abandoned round: every question still owed is stored as
        # skipped, so Lana does not re-offer what they explicitly declined.
        for it in rnd["items"]:
            if it.get("state") == "open":
                _record(record_aspect, it, author_id, None, "skipped")
                it["state"] = "skipped"
        rnd["status"] = "skipped"
        return rnd

    if action not in {"answer", "skip"}:
        raise RoundError("unknown_action")
    item = next(
        (it for it in rnd["items"] if it["aspect_key"] == (aspect_key or "")), None
    )
    if item is None:
        raise RoundError("unknown_aspect")

    if action == "skip":
        _record(record_aspect, item, author_id, None, "skipped")
        item["state"], item["answer"] = "skipped", None
    else:
        text = str(answer or "").strip()[:600]
        if not text:
            raise RoundError("empty_answer")
        src = source if source in {"voice", "text"} else "text"
        _record(record_aspect, item, author_id, text, src)
        item["state"], item["answer"] = "answered", text

    rnd["status"] = (
        "done" if all(it.get("state") != "open" for it in rnd["items"]) else "active"
    )
    return rnd


def _record(record_aspect, item: dict[str, Any], author_id: str, text: str | None, src: str):
    record_aspect(
        signal_id=item["signal_id"],
        subject_ref=item.get("subject_ref"),
        author_id=author_id,
        aspect_key=item["aspect_key"],
        aspect_label=item["aspect_label"],
        source_span=item.get("source_span"),
        answer_verbatim=text,
        answer_source=src,
    )


def settle(session_ctx: dict[str, Any]) -> None:
    """Clear a finished round. None, not pop: the session merge is {**old, **new}, so a
    popped key comes straight back from the stored context."""
    rnd = session_ctx.get(CTX_KEY)
    if isinstance(rnd, dict) and rnd.get("status") in _TERMINAL:
        session_ctx[CTX_KEY] = None


# ── reader side ─────────────────────────────────────────────────────────────

# Cards per results page that get an aspect read: one RPC each, and the list is short.
ASPECT_CARDS = 5
ASPECTS_PER_CARD = 3


def attach_aspects(cards: list[dict[str, Any]], *, user_jwt: str | None) -> None:
    """"5 of 8 mentioned the wait: 'over an hour, every time'" on each subject card.

    Runs as the READER (subject_aspects reads auth.uid() for block and circle visibility).
    Only aspects someone put words to are shown — a section that was only ever skipped
    has nothing to say to a reader. Best-effort: a card without aspects is today's card."""
    if not aspects_enabled() or not user_jwt:
        return
    from app.supabase_rpc import call_rpc

    for card in cards[:ASPECT_CARDS]:
        ref = str(card.get("subject_ref") or "").strip()
        if not ref:
            continue
        try:
            rows = call_rpc(user_jwt, "subject_aspects", {"p_subject_ref": ref}) or []
        except Exception:  # noqa: BLE001
            logger.debug("aspects_attach_failed subject=%s", ref, exc_info=True)
            continue
        out = []
        for r in rows if isinstance(rows, list) else []:
            quotes = [q for q in (r.get("sample_quotes") or []) if str(q or "").strip()]
            if not quotes:
                continue
            out.append({
                "aspect_key": str(r.get("aspect_key") or ""),
                "label": str(r.get("aspect_label") or r.get("aspect_key") or ""),
                "n_people": int(r.get("n_people") or 0),
                "n_shared_community": int(r.get("n_shared_community") or 0),
                "quotes": quotes[:2],
            })
            if len(out) >= ASPECTS_PER_CARD:
                break
        if out:
            card["aspects"] = out


def rerank_tips_by_aspects(
    tips: list[dict[str, Any]], *, request: str, user_jwt: str | None
) -> list[dict[str, Any]]:
    """Re-order tip rows by how many parts of a multi-part ask their subject satisfies.

    "a pediatrician great with toddlers AND no wait" is two requirements; a subject that
    meets both outranks one that meets one brilliantly. Stable: rows with no aspect match
    keep their existing order, so this can only promote, never scramble. Stamps
    `_aspect_match` on matched rows for the card's "why". Never raises."""
    if not aspects_enabled() or not user_jwt or not tips:
        return tips
    scope = sorted({str(t.get("subject_ref")) for t in tips if t.get("subject_ref")})
    if not scope:
        return tips
    try:
        from app.reco_aspects import find_by_aspects

        hits = find_by_aspects(request=request, user_jwt=user_jwt, subject_scope=scope)
    except Exception:  # noqa: BLE001
        logger.debug("aspects_rerank_failed", exc_info=True)
        return tips
    by_ref: dict[str, dict[str, Any]] = {}
    for h in hits or []:
        ref = str(h.get("subject_ref") or "")
        if not ref:
            continue
        quotes = [
            str(m.get("quote")) for m in (h.get("matched_aspects") or [])
            if isinstance(m, dict) and str(m.get("quote") or "").strip()
        ]
        by_ref[ref] = {
            "clauses_matched": int(h.get("clauses_matched") or 0),
            "clauses_total": int(h.get("clauses_total") or 0),
            "quotes": quotes[:3],
        }
    if not by_ref:
        return tips
    for t in tips:
        m = by_ref.get(str(t.get("subject_ref") or ""))
        if m:
            t["_aspect_match"] = m
    return sorted(
        tips, key=lambda t: -int((t.get("_aspect_match") or {}).get("clauses_matched") or 0)
    )
