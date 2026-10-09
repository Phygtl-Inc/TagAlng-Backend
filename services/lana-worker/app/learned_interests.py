"""Learned interests: what Lana picks up from what someone DOES (migration 20270204120000).

Asjid, 2026-10-07: Tommaso searched AI meets twice and "find people into AI" still found
nobody — claims only come from things people say about themselves, and most never do.

  * Each topic search (events or people) is counted; the topic is LEARNED after two
    different days or three searches in all (record_learned_interest decides, in SQL).
  * People search shows learned matches BELOW stated ones, and the card says what they
    did ("Has been checking out AI meetups"), never what they are.
  * The person sees every learned interest on their profile and removes any with a tap;
    a removed topic is never learned again.
  * When one is first learned, Lana may say so in passing — one line, at most once a week.

Never raises into a turn: every write here is a side effect of a search.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_MAX_WORDS = 4


def enabled() -> bool:
    return os.environ.get("LANA_LEARNED_INTERESTS", "1").strip().lower() not in (
        "0", "false", "off",
    )


def topic_label(text: str | None) -> str | None:
    """The topic as the search named it ("AI", "board games"), or None when it reads as
    a sentence rather than a topic. The classifier already reduced the ask to its topic;
    this only refuses what is too long to be one."""
    label = " ".join(str(text or "").split())
    if not (2 <= len(label) <= 40) or len(label.split()) > _MAX_WORDS:
        return None
    return label


def record(user_id: str | None, text: str | None, kind: str) -> str | None:
    """Count one search of `text`. Returns the label to mention in passing when this search
    is the one that taught Lana the interest AND no mention was made this week."""
    label = topic_label(text)
    if not (enabled() and user_id and label):
        return None
    try:
        from app.auth import service_client

        sb = service_client()
        res = sb.rpc(
            "record_learned_interest",
            {"p_user_id": user_id, "p_label": label, "p_kind": kind},
        ).execute()
        row = (res.data or [None])[0] if isinstance(res.data, list) else None
        if not isinstance(row, dict) or not row.get("newly_learned"):
            return None
        ok = sb.rpc(
            "claim_learned_mention", {"p_user_id": user_id, "p_topic": row.get("topic")}
        ).execute()
        return str(row.get("label") or label) if ok.data is True else None
    except Exception:  # noqa: BLE001 — a search must never fail on its side effect
        logger.exception("learned_interest_record_failed user=%s kind=%s", user_id, kind)
        return None


def record_turn(
    session_ctx: dict[str, Any], user_id: str | None, text: str | None, kind: str
) -> None:
    """Count this turn's search once. The same topic searched again straight after (a
    widen tap, a refine that lands on the same topic) is the same search, not a second
    one. A newly learned interest is left on the session for append_mention."""
    label = topic_label(text)
    if not label:
        return
    key = f"{kind}:{label.lower()}"
    if session_ctx.get("_learned_last") == key:
        return
    session_ctx["_learned_last"] = key
    mention = record(user_id, label, kind)
    if mention:
        session_ctx["_learned_mention"] = mention


def append_mention(reply: str, session_ctx: dict[str, Any]) -> str:
    """After the answer, ONE line that an interest was just learned — then cleared with
    None (a popped key is resurrected by the session merge)."""
    label = str(session_ctx.get("_learned_mention") or "").strip()
    session_ctx["_learned_mention"] = None
    if not label or not str(reply or "").strip():
        return reply
    from app.i18n import t

    return f"{reply}\n\n{t('learned.mention', 'en', label=label)}"


def evidence_text(label: str, kind: str | None) -> str:
    """What the card says: what they DID, so nobody is labelled as something they never
    said they were."""
    if kind == "events":
        return f"Has been checking out {label} meetups"
    return f"Has been looking for people into {label}"


def fetch_learned_peers(
    user_jwt: str, terms: list[str], *, limit: int = 5, exclude: set[str] | None = None
) -> list[dict[str, Any]]:
    """Peers whose learned interests carry every term — the lower tier of people search."""
    if not (enabled() and terms):
        return []
    from app.layer1_handlers import _call_peer_rpc

    try:
        raw = _call_peer_rpc(
            user_jwt,
            "find_peers_by_learned_interest",
            {"p_terms": list(terms), "p_limit": limit},
        )
    except Exception:  # noqa: BLE001 — the learned tier is extra; stated results stand
        logger.exception("learned_peers_fetch_failed")
        return []
    out: list[dict[str, Any]] = []
    for r in raw if isinstance(raw, list) else []:
        if not isinstance(r, dict) or not r.get("peer_user_id"):
            continue
        if exclude and str(r["peer_user_id"]) in exclude:
            continue
        row = dict(r)
        row["matching_peer_label"] = evidence_text(
            str(r.get("matching_peer_label") or ""), r.get("learned_kind")
        )
        row["learned"] = True
        out.append(row)
    return out[:limit]


def list_for_user(user_id: str) -> list[dict[str, Any]]:
    from app.auth import service_client

    res = (
        service_client()
        .table("learned_interests")
        .select("id, label, event_searches, people_searches, learned_at, last_seen_at")
        .eq("user_id", user_id)
        .is_("removed_at", "null")
        .not_.is_("learned_at", "null")
        .order("last_seen_at", desc=True)
        .execute()
    )
    return [
        {
            "id": r["id"],
            "label": r["label"],
            "evidence": evidence_text(
                r["label"], "events" if (r.get("event_searches") or 0) > 0 else "people"
            ),
            "learned_at": r.get("learned_at"),
        }
        for r in (res.data or [])
        if isinstance(r, dict)
    ]


def remove(user_id: str, interest_id: str) -> bool:
    """Remove one learned interest for good (it is never learned again)."""
    from datetime import datetime, timezone

    from app.auth import service_client

    res = (
        service_client()
        .table("learned_interests")
        .update({"removed_at": datetime.now(timezone.utc).isoformat()})
        .eq("id", interest_id)
        .eq("user_id", user_id)
        .is_("removed_at", "null")
        .execute()
    )
    return bool(res.data)
