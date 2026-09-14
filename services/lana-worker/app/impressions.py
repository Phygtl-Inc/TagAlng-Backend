"""What Lana showed, and what the user did with it (contract v2 §A7).

`recommendation_impressions` has existed since 20260828110000 with zero rows — the
migration's comment points at a `recommendations.py` insert path that was never written.
This module is that path. No DDL: every field below already exists, and the five keys the
consumers need that the table has no column for ride in `metadata` (jsonb).

ONE writer, three surfaces. Peers, meets and tips all render candidates, and three
independent inserts is how "shown" comes to mean three different things by December. The
call site is the response builder in main.py, deliberately: it runs AFTER the ui_intent
drop-filters, so what lands here is what the client actually received rather than what a
lane hoped to send. Order there is render order, which is what makes `position` honest.

The five metadata keys, and who is dead without each:

  request_id      everyone   the rows of ONE answer, grouped. Without it you can count
                             "400 shown / 12 tapped" but cannot ask "when someone asked,
                             did they get anything?" — three candidates for one ask and
                             three failed asks are indistinguishable. Nothing in the
                             contract asks for this; nothing works without it.
  position        Tim        D4 is nDCG@3, which measures ORDER. Rank cannot be rebuilt
                             from `score` after the fact: rows get filtered and re-sorted
                             downstream (PR #151 drops own meets in Python), so a
                             reconstruction would quietly lie to the gate.
  admission_rule  Tim + us   which rule produced the set (contract v2 §A7, verbatim:
                             "or D4 cannot say what it measured"). PR #151 replaced two
                             rules with one; without this, before and after are the same
                             row shape with no way to tell them apart.
  turn_id         analytics  joins an impression to its reply in lana_messages.
  truncated       Pouya      "nothing else matched" vs "the page was full". His flag is
                             the difference between an honest empty and a silent lie.

Two rules about how it writes:
  * It never blocks the turn. Ids are minted here and the insert runs on a daemon thread,
    so the payload carries them immediately and a logging failure can never become an
    outage. A log that can take the product down is worse than no log.
  * The query is redacted. `query` is raw user text and this table is read by people doing
    analytics — redact_pii() is already the repo's answer to that.
"""

from __future__ import annotations

import logging
import threading
import uuid
from typing import Any

from app.auth import service_client

logger = logging.getLogger(__name__)

# Status values the table's CHECK accepts. 'shown' is written here; the rest arrive from
# the client through set_impression_status.
_STATUSES = ("shown", "viewed", "dismissed", "accepted", "converted")

# The table's own CHECK. A surface that is not one of these has no row shape to write.
_TYPES = ("neighbor", "event", "local_signal")

# Cap per turn. The response builder already slices (8 peers, 5 meets); this is the
# backstop so a lane that forgets to slice cannot write 400 rows on one turn.
_MAX_PER_TURN = 20


def _insert_async(rows: list[dict[str, Any]]) -> None:
    """Fire-and-forget. Same pattern as the post-publish event embed (event_publish.py)."""

    def _run() -> None:
        try:
            service_client().table("recommendation_impressions").insert(rows).execute()
        except Exception:  # noqa: BLE001
            # Deliberately swallowed and logged, never raised: the user already has their
            # answer by the time this runs.
            logger.warning("impressions_insert_failed n=%d", len(rows))

    threading.Thread(
        target=_run, daemon=True, name=f"impressions-{len(rows)}"
    ).start()


def _redacted(query: str | None) -> str | None:
    if not query:
        return None
    try:
        from app.pii import redact_pii

        return redact_pii(query[:500])
    except Exception:  # noqa: BLE001
        # A failed redaction must drop the text, never pass it through raw.
        return None


def log_shown(
    *,
    user_id: str,
    session_id: str | None,
    block_id: str | None,
    query: str | None,
    peers: list[Any],
    activities: list[Any],
    ctx: dict[str, Any] | None = None,
    turn_id: str | None = None,
) -> None:
    """Write one row per candidate on screen, and stamp each row's id onto the payload.

    Mutates `peers` / `activities` in place: every model gains `impression_id`, which is
    what the client sends back to /lana/impression when the user acts. Minting the id here
    rather than reading it back from the insert is what lets the write be asynchronous —
    the payload never waits on the database.
    """
    if not user_id or (not peers and not activities):
        return

    ctx = ctx or {}
    request_id = str(uuid.uuid4())
    q = _redacted(query)
    # Whichever rule produced this set. PR #151 writes browse_admission into ctx; absent
    # on every other surface, and null is the honest value for "no rule ran".
    admission_rule = str(ctx.get("browse_admission") or "").strip() or None
    truncated = ctx.get("browse_truncated")
    # Topic scores by event id, stashed by the browse lane. Kept in ctx rather than on
    # ActivityPreviewRow on purpose: it is an internal ranking number with nothing to
    # render, and the wire model is not the place to park telemetry.
    scores = ctx.get("browse_scores")
    scores = scores if isinstance(scores, dict) else {}

    rows: list[dict[str, Any]] = []
    position = 0

    def _row(kind: str, key: str, ident: str | None, score: Any, reasons: Any, action: str) -> None:
        nonlocal position
        if not ident or kind not in _TYPES or position >= _MAX_PER_TURN:
            return
        rows.append(
            {
                "id": str(uuid.uuid4()),
                "user_id": user_id,
                "session_id": session_id,
                "block_id": block_id,
                "recommendation_type": kind,
                key: ident,
                "score": float(score) if isinstance(score, (int, float)) else 0.0,
                "reason_codes": [str(r) for r in reasons][:8] if isinstance(reasons, list) else [],
                "suggested_action": action,
                "query": q,
                "surface": "lana_chat",
                "status": "shown",
                "metadata": {
                    "request_id": request_id,
                    "position": position,
                    "admission_rule": admission_rule,
                    "turn_id": turn_id,
                    "truncated": bool(truncated) if truncated is not None else None,
                },
            }
        )
        position += 1

    for p in peers:
        _row(
            "neighbor",
            "candidate_user_id",
            getattr(p, "peer_user_id", None),
            getattr(p, "similarity_score", None),
            getattr(p, "trait_tags", None),
            "view_peer",
        )
    for a in activities:
        _row(
            "event",
            "event_id",
            getattr(a, "activity_id", None),
            scores.get(str(getattr(a, "activity_id", "") or "")),
            None,
            "view_event",
        )

    if not rows:
        return

    # Stamp before the insert is even attempted: the client's ability to report a tap must
    # not depend on whether our logging thread succeeded.
    minted = iter([r["id"] for r in rows])
    for row_model in list(peers) + list(activities):
        ident = getattr(row_model, "peer_user_id", None) or getattr(
            row_model, "activity_id", None
        )
        if not ident:
            continue
        try:
            row_model.impression_id = next(minted)
        except StopIteration:
            break

    _insert_async(rows)


def set_impression_status(
    *,
    user_id: str,
    impression_id: str,
    status: str,
    converted_action_id: str | None = None,
) -> bool:
    """Record what the user did. Returns False when the row is not this user's.

    Scoped by user_id as well as id so a guessed uuid cannot write to someone else's row —
    the table is service-role write-only, so this function is the whole perimeter.
    """
    if status not in _STATUSES or status == "shown":
        return False
    patch: dict[str, Any] = {"status": status}
    if converted_action_id:
        patch["converted_action_id"] = converted_action_id
    try:
        res = (
            service_client()
            .table("recommendation_impressions")
            .update(patch)
            .eq("id", impression_id)
            .eq("user_id", user_id)
            .execute()
        )
        return bool(res.data)
    except Exception:  # noqa: BLE001
        logger.warning("impression_status_failed id=%s status=%s", impression_id, status)
        return False
