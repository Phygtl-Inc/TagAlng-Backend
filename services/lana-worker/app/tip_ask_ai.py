"""AI reader for the one-turn offers around a recommendation ask.

Two offers use this: "want me to ask your neighbors too?" (armed under a rec answer)
and "want me to take that posting down?" (armed after a posting was made). Both are
one-turn and both need the same question answered — did the user say yes, no, take it
down, or something else entirely?

AI-first by design (see [[no-new-regex-use-ai-signals]]): an accept/decline lexicon is
exactly the kind of phrase list that mis-fires across languages and paraphrases. The
deterministic floor in the caller is not a phrase matcher either — it compares the
message to the chip payload Lana herself offered, which is an exact known string.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.orchestrator.llm import llm_configured, llm_json, router_model

_log = logging.getLogger(__name__)

_SYSTEM = (
    "You read ONE reply to ONE offer Lana just made in a neighborhood app, and say what the "
    "reply means. Output only valid JSON: {\"verdict\":\"accept\"|\"decline\"|\"remove\"|\"other\"}. "
    "UNDERSTAND, DO NOT PATTERN-MATCH — any language, any phrasing. "
    "offer=ask_neighbors: Lana answered a recommendation ask and offered to also ask the "
    "user's neighbors for their own recommendation. "
    "accept = they want her to ask the neighbors (yes, please do, go ahead, sure, ask them, "
    "that'd be great, keep listening for me, keep an ear out). "
    "Asking her to look FURTHER OUT or show MORE PEOPLE is not accept — that is a wider "
    "search, not permission to post: answer other. "
    "decline = they do NOT want it, now or at all (no, no thanks, not now, maybe later, "
    "don't post anything, I'd rather you didn't, just wanted the list). "
    "remove = they are asking her to take down / undo / delete a posting she already made. "
    "other = ANYTHING else: a new question, a refinement of the recommendation "
    "('kid-friendly ones', 'show me all restaurants', 'somewhere closer'), a different "
    "request, thanks-and-nothing-more, or an unrelated remark. "
    "offer=manage_posting: the posting already exists and Lana offered to remove it. "
    "accept and remove BOTH mean take it down here — prefer remove. decline = leave it up. "
    "A refinement of the recommendation is NEVER accept or decline — it is other. "
    "The user telling you what they originally wanted ('I didn't want anything posted', "
    "'I just wanted encouragement') is a decline when a posting is pending or exists — they "
    "are telling you not to do it / to undo it, not asking a new question. "
    "ALWAYS other, no matter how the offer reads: anything about how they FEEL, a symptom or "
    "health worry, distress, danger, or an unsafe request. Those turns belong to a safety "
    "rail, and answering them as an offer reply would bury them. "
    "When you genuinely cannot tell, answer other — falling through to normal routing is "
    "always safer than acting on a guess."
)

_VALID = {"accept", "decline", "remove", "other"}


_DRAFT_SYSTEM = (
    "You read ONE reply to a draft card Lana showed a neighbor. The card says, in short "
    "form, what she understood their recommendation ask to be, and offers two choices: "
    "keep it as it is, or change it. Output only valid JSON: "
    '{"verdict":"confirm"|"tweak"|"other"}. '
    "UNDERSTAND, DO NOT PATTERN-MATCH — any language, any phrasing. "
    "confirm = the card is right as written (looks good, that's it, yep exactly, perfect). "
    "tweak = they want the ASK ITSELF changed — a wrong or missing qualifier, wrong area, "
    "wrong kind of provider ('let me tweak that', 'actually make it evenings', 'not "
    "dentist, orthodontist', 'closer to home'). "
    "other = ANYTHING else: a new question, a reply about the recommendations she gave, an "
    "answer to a different question she asked, or an unrelated remark. "
    "When you cannot tell, answer other — normal routing is always safer than a guess."
)

_DRAFT_VALID = {"confirm", "tweak", "other"}


def interpret_ask_draft_reply(*, title: str, detail: str, msg: str) -> str | None:
    """Read a reply to the ask-draft card. "confirm" | "tweak" | "other", or None when the
    LLM is unavailable so the caller can fall back to its chip-payload comparison."""
    text = str(msg or "").strip()
    if not text or not llm_configured():
        return None
    payload = json.dumps(
        {
            "card_title": str(title or "")[:120],
            "card_ask": str(detail or "")[:200],
            "user_reply": text,
        },
        ensure_ascii=False,
    )
    try:
        raw = llm_json(
            model=router_model(),
            system=_DRAFT_SYSTEM,
            user_payload=payload,
            max_tokens=64,
            temperature=0.0,
        )
    except Exception as exc:  # noqa: BLE001 — a draft read must never break the turn
        _log.warning("ask_draft_read failed: %s", exc)
        return None
    if not isinstance(raw, dict):
        return None
    verdict = str(raw.get("verdict") or "").strip().lower()
    return verdict if verdict in _DRAFT_VALID else None


def interpret_offer_reply(
    *,
    offer: str,
    detail: str,
    msg: str,
) -> str | None:
    """Read a reply to the ask-neighbors / manage-posting offer.

    Returns "accept" | "decline" | "remove" | "other", or None when the LLM is
    unavailable so the caller can fall back to its chip-payload comparison.
    """
    text = str(msg or "").strip()
    if not text or not llm_configured():
        return None
    payload = json.dumps(
        {
            "offer": str(offer or "ask_neighbors"),
            "what_the_ask_was": str(detail or "")[:200],
            "user_reply": text,
        },
        ensure_ascii=False,
    )
    try:
        raw = llm_json(
            model=router_model(),
            system=_SYSTEM,
            user_payload=payload,
            max_tokens=64,
            temperature=0.0,
        )
    except Exception as exc:  # noqa: BLE001 — an offer read must never break the turn
        _log.warning("tip_ask_ai failed: %s", exc)
        return None
    if not isinstance(raw, dict):
        return None
    verdict = str(raw.get("verdict") or "").strip().lower()
    return verdict if verdict in _VALID else None
