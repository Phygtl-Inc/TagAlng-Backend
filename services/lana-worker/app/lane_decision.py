"""Shared "should this sticky lane keep the turn?" decision.

The sticky capture lanes (look_meet, activity_browse, event_host) must NOT trap the
user: every turn we re-read the AI classifier and CONTINUE the lane only when the
current message is genuinely an answer / refine / confirm for the in-flight capture (or
an explicit cancel, which the lane's own run-turn turns into a graceful exit). Anything
else — a pivot to another intent, a vague switch, an abandon, or a low-confidence /
off-lane classification — RELEASES, so normal routing handles the new request fresh.

This inverts the old "stay-by-default, release on a tiny whitelist" logic that pinned
users whenever the classifier was unsure or returned goal in ("","continue","none").
The lane-specific judgement of "is this a valid answer to my current step?" lives in each
lane's ``is_valid_answer`` predicate; the helper only wires the universal exits.
"""

from __future__ import annotations

import re
from typing import Any, Callable

# Cancel is a graceful IN-FLOW exit (the lane's run-turn emits "No problem…"), never a
# reroute — identical across lanes, so it lives here.
_CANCEL_RE = re.compile(
    r"\b(cancel|never\s*mind|nvm|stop|forget it|not now|skip this|exit|quit)\b",
    re.IGNORECASE,
)

# A confident classification is one we trust enough to act on as a pivot.
_CONFIDENT = 0.6
_VAGUE_GOALS = ("", "continue", "none")

# Predicate: given (message, session_ctx, slots) decide whether the message is a valid
# answer/refine/confirm for the lane's CURRENT step (so the lane keeps the turn).
IsValidAnswer = Callable[[str, dict[str, Any], "dict[str, Any] | None"], bool]


def is_meta_or_chat(slots: dict[str, Any] | None) -> bool:
    """A question / companionship / meta turn (goal=chat) — e.g. "what's my zip?",
    "who's coming?", "why these?". It is never an ANSWER to a capture step, so a lane that
    can't answer questions (browse, look_meet) should release and let normal routing /
    the orchestrator answer it, instead of mistaking the question for a refinement."""
    return bool(slots) and str(slots.get("goal") or "") == "chat"


# Universal exits — these release EVERY sticky lane, at every step, no matter what the lane
# owns. An errand Lana can't run (out_of_scope), abusive content (unsafe), or a user in
# emotional distress (crisis) must never be swallowed as a field answer; the decline /
# refuse / empathy handlers downstream must get the turn.
_ALWAYS_OFF_LANE_GOALS = ("out_of_scope", "unsafe", "crisis")
_ALWAYS_OFF_LANE_LINEARS = ("system.out_of_scope", "system.unsafe", "system.crisis")


def is_confident_off_lane(
    slots: dict[str, Any] | None,
    *,
    native_goals: frozenset[str] = frozenset(),
    native_linears: frozenset[str] = frozenset(),
    native_signals: frozenset[str] = frozenset(),
    ignore_goal: bool = False,
) -> bool:
    """True when the classifier confidently reads this turn as an intent OUTSIDE this lane.

    Self-maintaining inverse of the old "enumerate every foreign intent" approach: a lane
    declares only the small, stable set of intents it OWNS (native_*). ANY confident
    classification that is not native — peers, login, out_of_scope, unsafe, or any intent
    added in the future — counts as off-lane and releases the sticky flow. We never list the
    open-ended "everything else", so a newly added intent can't silently become an
    unhandled escape hatch that traps the user (the bug this replaces).

    ignore_goal: when True the goal alone never decides — only a concrete foreign
    linear_intent / signal_intent releases (the universal exits still apply). Used by the
    host naming step, where a bare title ("Soccer in the park") reads as goal=activities and
    must STAY, while an explicit action pivot ("search a meet") must release.
    """
    if not slots:
        return False
    from app.layer1_intents import normalize_linear_intent

    conf = float(slots.get("confidence", 0.0))
    goal = str(slots.get("goal") or "")
    if conf < _CONFIDENT or goal in _VAGUE_GOALS:
        return False
    linear = normalize_linear_intent(slots.get("linear_intent")) or ""
    signal_intent = str(slots.get("signal_intent") or "")

    # Universal exits first — out_of_scope / unsafe / crisis always release, every lane, every step.
    if goal in _ALWAYS_OFF_LANE_GOALS or linear in _ALWAYS_OFF_LANE_LINEARS:
        return True

    # A concrete action this lane owns (its linear/signal) → definitively an answer; stay.
    if (linear and linear in native_linears) or (signal_intent and signal_intent in native_signals):
        return False
    # A concrete action this lane does NOT own → off-lane pivot; release.
    if (linear and linear not in native_linears) or (signal_intent and signal_intent not in native_signals):
        return True
    # No concrete action named. Fall back to the goal (unless the caller suppresses it).
    if ignore_goal:
        return False
    return goal not in native_goals


def lane_should_continue(
    message: str,
    session_ctx: dict[str, Any],
    slots: dict[str, Any] | None,
    *,
    is_valid_answer: IsValidAnswer,
    pivot_re: "re.Pattern[str] | None" = None,
) -> bool:
    """Return True to STAY in the lane this turn, False to RELEASE to normal routing.

    Decision order (first match wins):
      1. empty message      -> stay  (no-op turn)
      2. cancel words       -> stay  (run-turn emits the graceful exit copy)
      3. slots.abandon      -> release
      4. cross-lane regex   -> release
      5. is_valid_answer    -> stay  (answer / refine / confirm / chip edit)
      6. default            -> RELEASE  (the inversion — nothing keeps a user trapped)
    """
    msg = str(message or "").strip()
    if not msg:
        return True
    if _CANCEL_RE.search(msg):
        return True
    if slots and slots.get("abandon"):
        return False
    if pivot_re is not None and pivot_re.search(msg):
        return False
    return bool(is_valid_answer(msg, session_ctx, slots))
