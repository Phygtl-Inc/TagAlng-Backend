"""What Lana is waiting on: the one question or offer the next message may be answering.

There were about 21 separate notions of "Lana asked something" — each flow parks its own
`*_pending` key and reads replies its own way — and no single place that says which one
is live. The classifier saw roughly half of them (`_active_capture_context`), so a reply
to an out-of-scope clarifier, a community-join offer or an intro offer was classified
cold. And decide_turn runs before most of the engines that read those replies, so a
typed answer could be talked over by the policy while the offer stayed armed.

`pending_ask(ctx, history)` returns the live ask, most specific first. The classifier is
shown it on every turn and returns `reply_to_pending` (answer / decline / new_ask), and
the policy gate steps aside when an engine that runs after it owns the answer.

Every `*pending*` key in the app must be read here or listed in NOT_AN_ASK —
tests/test_pending_ask.py fails otherwise, so a new flow cannot be invisible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from app.signal_capture import PHASE_SIGNAL_CONFIRM


@dataclass(frozen=True)
class PendingAsk:
    kind: str
    # What was asked, in neutral words — shown to the classifier. Never example replies.
    about: str
    # The engine that reads the answer runs AFTER decide_turn and nothing else keeps the
    # policy off the turn, so the gate must step aside for an answer.
    after_policy: bool
    question: str | None = None  # Lana's last message, verbatim
    options: list[str] = field(default_factory=list)


def _d(ctx: dict[str, Any], key: str) -> dict[str, Any]:
    v = ctx.get(key)
    return v if isinstance(v, dict) else {}


def _strs(v: Any) -> list[str]:
    return [str(x).strip() for x in (v or []) if str(x or "").strip()] if isinstance(v, list) else []


def _signal_confirm(ctx: dict[str, Any]) -> bool:
    return _d(ctx, "signal_draft").get("phase") == PHASE_SIGNAL_CONFIRM


def _lang_offer(ctx: dict[str, Any]) -> bool:
    return bool(ctx.get("lang_nudge_pending") or _strs(ctx.get("lang_offer_langs")))


# (kind, armed, about, after_policy, options) — most specific first. The first armed
# entry wins: a confirmation or a concrete offer outranks the flow it sits inside.
_Options = Callable[[dict[str, Any]], list[str]]
_REGISTRY: tuple[tuple[str, Callable[[dict[str, Any]], bool], str, bool, _Options | None], ...] = (
    ("confirmation", lambda c: bool(c.get("pending_confirmation")),
     "a yes-or-no confirmation before Lana carries out an action for them", False, None),
    ("lane_switch", lambda c: bool(c.get("pending_lane_switch")),
     "whether to stop what they were doing and sign in instead", True, None),
    ("heritage_change", lambda c: bool(c.get("pending_heritage_change")),
     "whether to update a heritage already on their profile to the one they just mentioned",
     True, None),
    ("signal_confirm", _signal_confirm,
     "one missing detail of a swap, recommendation or meet they are posting", True, None),
    ("ask_draft", lambda c: bool(c.get("ask_draft_pending")),
     "approving or changing a drafted ask Lana will post to their neighbours", True, None),
    ("tip_tweak", lambda c: bool(c.get("tip_tweak_pending")),
     "how to refine the recommendations just shown", True, None),
    ("tip_ask_offer", lambda c: bool(c.get("tip_ask_offer_pending")),
     "an offer to also ask their neighbours for a recommendation", False, None),
    ("posting_manage", lambda c: bool(c.get("posting_manage_pending")),
     "an offer to take down the ask Lana posted for them", False, None),
    ("peer_seek_offer", lambda c: bool(c.get("peer_seek_offer_pending")),
     "an offer to keep listening for matching neighbours, or to widen the search, after "
     "a people search found nobody", True, None),
    ("community_join", lambda c: bool(c.get("community_join_pending")),
     "an offer to join one of the communities just shown", True,
     lambda c: _strs([p.get("place_name") for p in _d(c, "community_join_pending").get("places") or []
                      if isinstance(p, dict)])),
    ("intro_respond", lambda c: bool(c.get("pending_intro_respond")),
     "whether to accept an introduction another neighbour sent them", True, None),
    ("intro_offer", lambda c: bool(c.get("pending_intro_offer")),
     "an offer to introduce them to a matching neighbour", True, None),
    ("hosting_offer", lambda c: bool(c.get("pending_hosting_offer")),
     "an offer to turn the gathering they described into an event on their block", True, None),
    ("out_of_scope", lambda c: bool(c.get("out_of_scope_pending")),
     "a clarifying question after a request Lana cannot carry out herself, offering what "
     "she can do instead", True, lambda c: _strs(_d(c, "out_of_scope_offer").get("opts"))),
    ("browse_or_meet", lambda c: bool(c.get("browse_or_meet_pending")),
     "whether they want to browse events already happening or be matched with people",
     True, lambda c: _strs(c.get("browse_or_meet_options"))),
    ("clarify", lambda c: bool(c.get("clarify_pending")),
     "a clarifying question about what they meant", True,
     lambda c: _strs(c.get("clarify_options"))),
    ("rapport_offer", lambda c: bool(c.get("rapport_offer_pending")),
     "an offer to act on something they shared about themselves", False, None),
    ("rapport_question", lambda c: bool(c.get("rapport_active")),
     "a getting-to-know-you question", False, None),
    ("tip_share", lambda c: bool(c.get("tip_share_active")),
     "a detail of the recommendation they are sharing", False, None),
    ("community_create", lambda c: bool(c.get("community_create_active")),
     "a detail of the community they are creating", False, None),
    ("look_meet", lambda c: bool(c.get("look_meet_active")),
     "a detail of the meet or group they are looking for", False, None),
    ("pass_along", lambda c: bool(c.get("pass_along_active")),
     "a detail of the item they are passing along", False, None),
    ("activity_browse", lambda c: bool(c.get("activity_browse_active")),
     "narrowing down the events being shown", False, None),
    ("event_host", lambda c: bool(c.get("event_host_active")),
     "a detail of the event they are hosting", False, None),
    ("language_offer", _lang_offer,
     "an offer to switch their default language", False, None),
    # LAST: the policy's own conversational question is the fallback nobody else holds,
    # and the policy itself answers it — so never after_policy.
    ("conversation", lambda c: bool(str(c.get("policy_pending_question") or "").strip()),
     "a question Lana asked in conversation", False, None),
)

# Keys the registry reads (directly or as part of the flow named), for the coverage test.
READ_KEYS: frozenset[str] = frozenset({
    "pending_confirmation", "pending_lane_switch", "pending_heritage_change",
    "ask_draft_pending", "tip_tweak_pending", "tip_ask_offer_pending",
    "posting_manage_pending", "peer_seek_offer_pending", "community_join_pending",
    "pending_intro_respond", "pending_intro_offer", "pending_hosting_offer",
    "out_of_scope_pending", "browse_or_meet_pending", "clarify_pending",
    "rapport_offer_pending", "rapport_pending_action", "lang_nudge_pending",
    "policy_pending_question",
    # Field asks inside a sticky flow — the flow's entry above is what's live.
    "tip_pending_ask", "tip_pending_question", "tip_fork_pending",
    "community_pending_ask", "community_pending_question",
    "look_pending_ask", "pass_along_pending_ask",
})

# Pending keys that are not a question waiting on the next message, with the reason.
NOT_AN_ASK: dict[str, str] = {
    "host_publish_pending": "a stashed draft to publish after verification, not a question",
    "look_seek_pending": "a stashed meet seek to run after verification",
    "signal_pending": "a stashed signal ask to run after verification",
    "tip_seek_pending": "a stashed recommendation ask to run after verification",
    "pending_cohost_id": "part of the host draft",
    "pending_cohost_invite_id": "part of the host draft",
    "pending_intros": "a list of intros read fresh each time",
    "pending_post_verify": "marks a verification in flight; routing_phase owns that turn",
    "pending_signup_gate": "the email/code step; routing_phase owns it and decide_turn is "
    "already gated off",
    "pending_zip": "the ZIP step; routing_phase owns it",
}


def _last_lana_message(history: list[dict[str, Any]] | None) -> str | None:
    for row in reversed(history or []):
        if isinstance(row, dict) and row.get("role") == "assistant":
            text = str(row.get("content") or "").strip()
            return text[:400] or None
    return None


def pending_ask(
    ctx: dict[str, Any] | None, history: list[dict[str, Any]] | None = None
) -> PendingAsk | None:
    """The live ask, or None when Lana is not waiting on anything."""
    if not isinstance(ctx, dict):
        return None
    for kind, armed, about, after_policy, options in _REGISTRY:
        try:
            if not armed(ctx):
                continue
            opts = options(ctx) if options else []
        except Exception:  # noqa: BLE001 — a malformed key must not break the turn
            continue
        if not opts:
            opts = _strs(ctx.get("_offered_chip_msgs"))
        return PendingAsk(
            kind=kind,
            about=about,
            after_policy=after_policy,
            question=_last_lana_message(history),
            options=opts[:6],
        )
    return None


def prompt_line(ask: PendingAsk | None) -> str:
    """The classifier payload line — 'none' when nothing is pending."""
    if ask is None:
        return "none"
    parts = [f"kind={ask.kind}", f"Lana was waiting on {ask.about}."]
    if ask.question:
        parts.append(f'Her last message: "{ask.question}"')
    if ask.options:
        parts.append("Options she offered: " + " | ".join(f'"{o[:80]}"' for o in ask.options))
    return " ".join(parts)


REPLY_VERDICTS = ("answer", "decline", "new_ask")


def reply_verdict(slots: dict[str, Any] | None) -> str | None:
    v = str((slots or {}).get("reply_to_pending") or "").strip().lower()
    return v if v in REPLY_VERDICTS else None
