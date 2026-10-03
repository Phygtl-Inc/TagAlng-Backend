"""How Lana answers when a scope is active and the exact thing isn't there.

TWO PROBLEMS, ONE COMPOSITION LAYER

  1. THE EXIT RAMP (Pouya, standup 2026-10-01)

     Today the fallback has one cut point: found it, or didn't. When it didn't,
     the only door we open leads OUT of the community:

         "I couldn't find this event in Mr Beast. Want me to look beyond?"

     That is correct scoping and bad product. It is an exit ramp offered at exactly
     the moment the community should be proving it is worth being in. And since
     empty is the NORMAL state of a community on day one, every miss becomes a
     reason to leave — the emptier it is, the faster people leave, which keeps it
     empty.

     It also inverts the creator pitch. If a community's reflex under stress is to
     send people elsewhere, the creator's link is a funnel out of their own
     community.

     The fix is a third branch. Before offering to leave, offer what the scope
     actually has:

         "Nothing on Japanese clients specifically — but there's a formal dining
          session Thursday, and Ana's covered cross-cultural table manners."

  2. THE FIRST SENTENCE (Tommaso, standup 2026-10-01)

     Close to 100% of users read only the first sentence, if that. So the first
     sentence must BE the answer, not an introduction to it. A reader who stops
     after it must still have what they asked for.

     This matters most in branch 2, which carries a gap AND an offer. Bury the
     offer in sentence three and nobody sees it.

SCOPE-AGNOSTIC BY DESIGN

  The rule is "exhaust the active scope before offering to leave it." That holds
  for a creator community, a gym, a chapter — anything narrower than the default.
  Only the STAKES are creator-specific: that is the one place where pushing
  somebody out costs a real person their business.

  Building it creator-only would mean building it twice.

Never raises into the request path. Every branch has a deterministic fallback.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Literal

logger = logging.getLogger(__name__)

Band = Literal["strong", "partial", "none"]

# Two cut points on a score retrieval already computes. No new infrastructure.
#
# These are STARTING VALUES, not findings. Start generous on what counts as
# adjacent — a slightly loose branch-2 offer is recoverable, and an exit ramp is
# not — then tighten against how often people actually accept. Move to a config
# table once there is data to tune against.
STRONG_FLOOR = float(os.getenv("LANA_SCOPE_STRONG_FLOOR", "0.62"))
PARTIAL_FLOOR = float(os.getenv("LANA_SCOPE_PARTIAL_FLOOR", "0.38"))

# What widening means depends on where you are. Naming the destination is a real
# improvement on "want me to look wider?" — a chapter widens to its PARENT before
# it ever leaves the community.
_NEXT_SCOPE = {
    "sub_community": "the whole community",
    "creator_community": "nearby",
    "place_community": "nearby",
    "area": "a bit further out",
}

# Openers that mean the first sentence is a preamble rather than the answer.
# Cheap post-check: the model is told the rule, and then held to it.
_PREAMBLE = re.compile(
    r"^\s*(i\s+(looked|checked|searched|found that|can|see|think)|let me|sure[,!.]|"
    r"great question|happy to|of course|absolutely|thanks for|good question|"
    r"here('s| is) what|so[,.]|well[,.]|okay[,.]|it (looks|seems) like)",
    re.IGNORECASE,
)

FRONTLOAD_RULES = """FIRST SENTENCE RULE — this is the most important instruction here.

Almost nobody reads past the first sentence. So the first sentence must BE the
answer, not an introduction to it. Someone who stops reading after it must still
have what they came for.

- Never open with what you did ("I looked", "I checked", "Here's what I found").
- Never open with a pleasantry ("Great question", "Sure", "Happy to help").
- Lead with the thing itself: the name, the date, the fact.
- One short second sentence is allowed ONLY if it adds something the first cannot
  carry. If it merely elaborates, drop it.
- Conversational, not a report. No headings, no bullet lists, no bold.
"""

PARTIAL_PROMPT = """Lana searched inside {scope_name} and did not find the exact thing \
asked for — but {scope_name} does have something near it. Write her reply.

They asked: {question}
What {scope_name} actually has: {alternatives}

{frontload}

ORDER IS NON-NEGOTIABLE. Name the gap first, then make the offer.

  "Nothing on X specifically — but there's Y on Thursday."

Offering the near thing as though it were what they asked for is worse than having
nothing: it reads as a system that did not listen. The gap must be stated before
the alternative, in the same sentence where possible.

- Do NOT offer to look outside {scope_name}. That is a different branch and this
  is not it.
- Do NOT apologise. A young community not having everything is a fact, not a failure.
- Under 30 words. No emoji.

Return ONLY valid JSON: {{"reply": "..."}}
"""

NONE_PROMPT = """Lana searched inside {scope_name} and found nothing relevant at all. \
Write the one line that says so and asks whether to look at {next_scope}.

They asked: {question}

{frontload}

- Name {scope_name}. The boundary has to be FELT — that is the point of being in one.
- Say plainly that nobody here has covered this. Do not soften it into vagueness,
  and do not apologise.
- Then offer, as a question, to look at {next_scope}.
- Under 25 words. No emoji.

Return ONLY valid JSON: {{"reply": "..."}}
"""


def classify(best_score: float | None) -> Band:
    """Which branch a result set falls into.

    None or no results is 'none' — an empty retrieval and a weak retrieval get the
    same treatment, because both mean the scope cannot answer this.
    """
    if best_score is None:
        return "none"
    if best_score >= STRONG_FLOOR:
        return "strong"
    if best_score >= PARTIAL_FLOOR:
        return "partial"
    return "none"


def frontloaded(text: str) -> bool:
    """Does the first sentence carry the answer, or is it a wind-up?

    Deliberately crude. It catches the common preamble openers, which is where
    almost all of the damage is, and does not try to judge meaning.
    """
    first = re.split(r"(?<=[.!?])\s", text.strip(), maxsplit=1)[0]
    return not _PREAMBLE.match(first)


def _generate(system: str, payload: str, max_tokens: int = 140) -> str | None:
    try:
        from app.orchestrator.llm import llm_configured, llm_json, router_model

        if not llm_configured():
            return None
        data = llm_json(
            model=router_model(),
            system=system,
            user_payload=payload,
            max_tokens=max_tokens,
            temperature=0.4,
        )
        if not isinstance(data, dict):
            return None
        reply = str(data.get("reply") or "").strip()
        if not reply:
            return None
        if not frontloaded(reply):
            # One retry with the rule restated. If it fails twice, the deterministic
            # fallback is frontloaded by construction and is the better answer.
            logger.info("scope_response: preamble detected, retrying")
            data = llm_json(
                model=router_model(),
                system=system + "\n\nYour last attempt began with a preamble. "
                "Start with the answer itself.",
                user_payload=payload,
                max_tokens=max_tokens,
                temperature=0.2,
            )
            reply = str((data or {}).get("reply") or "").strip()
            if not reply or not frontloaded(reply):
                return None
        return reply
    except Exception:
        logger.exception("scope_response: generation failed")
        return None


def compose(
    *,
    scope_name: str,
    scope_kind: str = "creator_community",
    question: str,
    best_score: float | None,
    alternatives: list[str] | None = None,
    place_id: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """The reply for one scoped ask.

    Returns {band, reply, offers_widen, next_scope}. `reply` is None for the strong
    band — that branch is answered by the normal path with no fallback copy at all,
    and crucially with NO widening question attached. Asking "want me to look
    wider?" after a good answer is the same exit ramp, just better disguised.
    """
    band = classify(best_score)
    next_scope = _NEXT_SCOPE.get(scope_kind, "a bit further out")
    alts = [a for a in (alternatives or []) if a and a.strip()][:3]

    # A partial band with nothing to actually offer is a 'none' band wearing a hat.
    if band == "partial" and not alts:
        band = "none"

    if band == "strong":
        return {"band": "strong", "reply": None, "offers_widen": False, "next_scope": None}

    if band == "partial":
        reply = _generate(
            PARTIAL_PROMPT.format(
                scope_name=scope_name,
                question=question,
                alternatives="; ".join(alts),
                frontload=FRONTLOAD_RULES,
            ),
            question,
        ) or f"Nothing on that specifically in {scope_name} — but there's {alts[0]}."

        # The gap is real whether or not we covered it in the moment. The creator
        # still needs to know nobody has addressed this.
        _record_gap(place_id, question, session_id, widened=False)

        return {
            "band": "partial",
            "reply": reply,
            "offers_widen": False,
            "next_scope": next_scope,
            # Branch 2 is also where a creator's paid service belongs once that
            # exists: "nothing in the community — but Ana offers exactly this."
            # Deliberately not built here; this is the hook.
            "service_slot": True,
        }

    reply = _generate(
        NONE_PROMPT.format(
            scope_name=scope_name,
            question=question,
            next_scope=next_scope,
            frontload=FRONTLOAD_RULES,
        ),
        question,
    ) or f"Nobody in {scope_name} has covered this yet. Want me to look {next_scope}?"

    _record_gap(place_id, question, session_id, widened=True)

    return {
        "band": "none",
        "reply": reply,
        "offers_widen": True,
        "next_scope": next_scope,
    }


def _record_gap(
    place_id: str | None, question: str, session_id: str | None, *, widened: bool
) -> None:
    """Log the miss. Both non-strong branches, always.

    An unanswered question with a community attached is demand with a named
    audience and nobody serving it — the most valuable row in the product, and
    inquiry_signals has zero of them today.
    """
    if not place_id:
        return
    try:
        from app.auth import service_client

        service_client().rpc(
            "record_community_widen",
            {
                "p_place_id": place_id,
                "p_free_text": question,
                "p_session_id": session_id,
                "p_widened": widened,
            },
        ).execute()
    except Exception:
        logger.exception("scope_response: radar write failed for place=%s", place_id)
