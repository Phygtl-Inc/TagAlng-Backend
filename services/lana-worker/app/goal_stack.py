"""Interrupted-goal stack in session context.

A side-quest that grabs a turn (upfront name capture, a rapport question, a verify
prompt) must not eat the goal the user was pursuing. QA (2026-07-08): tapping Lana's
own "Meet playground-loving neighbors" chip started the name side-quest; after "Jess"
the goal was gone ("how can I help you today?"), and restating it hit the stale
name-ask twice more before matching finally ran.

Contract:
- The handler that interrupts calls ``push_pending_goal`` on the turn ctx it returns,
  so the goal persists in the session context across the side-quest.
- On side-quest completion the handler calls ``pop_pending_goal`` and the caller
  re-drives the stored message through the goal's normal handler, prefixed with a
  ``resume_ack`` that names the stored topic.
- The stack lives under ``session_ctx["goal_stack"]`` (LIFO, bounded). Clearing is a
  ``None`` stamp so the ``{**old, **new}`` session merge deletes it (see
  ``_CTX_NULL_DELETES`` in app/db.py) — a ``pop()`` would silently resurrect the old
  value on merge, which is exactly the bug that kept ``awaiting_upfront_name`` alive.
"""

from __future__ import annotations

from typing import Any

GOAL_STACK_KEY = "goal_stack"
_MAX_DEPTH = 3

# Goal kinds — same vocabulary as the rapport concierge actions / the pipeline's
# _KIND_TO_INTENT forced-slot table, so a stored goal can be re-dispatched without
# re-parsing the noun.
_GOAL_KINDS = frozenset(
    {"find_neighbors", "find_activities", "host_meet", "seek_tip", "share_tip"}
)


def push_pending_goal(ctx: dict[str, Any], goal: dict[str, Any]) -> None:
    """Persist an interrupted goal on the turn ctx (survives the session merge)."""
    if not isinstance(goal, dict) or not str(goal.get("message") or "").strip():
        return
    stack = [g for g in (ctx.get(GOAL_STACK_KEY) or []) if isinstance(g, dict)]
    # The same request re-interrupted is one goal, not two.
    stack = [g for g in stack if g.get("message") != goal.get("message")]
    stack.append(goal)
    ctx[GOAL_STACK_KEY] = stack[-_MAX_DEPTH:]


def pop_pending_goal(ctx: dict[str, Any]) -> dict[str, Any] | None:
    """Take the most recent interrupted goal off the stack (None when empty).

    Stamps the remaining stack — or ``None`` when empty — back on ctx so the
    session merge persists the removal instead of resurrecting the old stack.
    """
    stack = [g for g in (ctx.get(GOAL_STACK_KEY) or []) if isinstance(g, dict)]
    if not stack:
        if GOAL_STACK_KEY in ctx:
            ctx[GOAL_STACK_KEY] = None
        return None
    goal = stack.pop()
    ctx[GOAL_STACK_KEY] = stack or None
    return goal


def goal_kind_from_slots(slots: dict[str, Any] | None) -> str | None:
    """Map this turn's classified slots to a resumable goal kind (or None)."""
    if not isinstance(slots, dict) or not slots:
        return None
    try:
        conf = float(slots.get("confidence") or 0.0)
    except (TypeError, ValueError):
        conf = 0.0
    if conf < 0.45:
        return None
    goal = str(slots.get("goal") or "").strip().lower()
    linear = str(slots.get("linear_intent") or "").strip().lower()
    signal = str(slots.get("signal_intent") or "").strip().lower()
    if goal == "activities" or linear == "discovery.find_activities":
        return "find_activities"
    if (
        goal in ("peers", "both")
        or linear in (
            "discovery.find_peers",
            "discovery.find_by_attrs",
            "discovery.find_in_block",
        )
        or signal == "meet_seek"
        or linear == "looking.meet"
    ):
        return "find_neighbors"
    if signal == "host_meet" or linear == "sharing.host":
        return "host_meet"
    if signal == "tip_seek" or linear == "looking.tip":
        return "seek_tip"
    if signal == "tip_share" or linear == "sharing.tip":
        return "share_tip"
    return None


def _topic_from_slots(slots: dict[str, Any] | None) -> str | None:
    if not isinstance(slots, dict):
        return None
    for key in ("signal_detail", "attr_filter"):
        val = str(slots.get(key) or "").strip()
        if val:
            return val[:80]
    return None


def pending_goal_from_turn(
    msg: str,
    session_ctx: dict[str, Any],
    slots: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Build the goal to stash when a side-quest is about to interrupt this message.

    A structured tap payload (``tapped_goal``, from a suggestion chip's ``goal``
    field) is authoritative — it can't be lost to re-parsing. Otherwise fall back
    to what the classifier read from the message this turn. Returns None when the
    message carries no resumable goal (plain chat never gets "resumed" at them).
    """
    message = str(msg or "").strip()
    if not message:
        return None
    slots_snapshot = dict(slots) if isinstance(slots, dict) and slots else None
    tapped = session_ctx.get("tapped_goal")
    if isinstance(tapped, dict):
        kind = str(tapped.get("kind") or "").strip()
        if kind in _GOAL_KINDS:
            topic = str(tapped.get("topic") or "").strip() or _topic_from_slots(slots)
            return {
                "kind": kind,
                "topic": topic or None,
                "message": message,
                "slots": slots_snapshot,
                "source": "tap",
            }
    kind = goal_kind_from_slots(slots)
    if not kind:
        return None
    return {
        "kind": kind,
        "topic": _topic_from_slots(slots),
        "message": message,
        "slots": slots_snapshot,
        "source": "slots",
    }


def resume_ack(topic: str | None) -> str:
    """Bridge line placed between the side-quest closer and the resumed reply."""
    what = str(topic or "").strip()
    if not what:
        return "Now, back to what you were after —"
    return f"Now, back to {what} —"
