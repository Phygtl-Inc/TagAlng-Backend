"""AI reader for a reply that follows a browse turn (cards shown, or an empty-search offer).

The browse used to read these replies with an accept/widen phrase list and treat anything
it missed as a brand-new search. "no" to "want me to listen, or widen?" therefore listed
every meet in the area, "nah" after "that's all there is" did the same, and "what others"
became the search topic ("Here's what's coming up for what others near you") — each one a
canned dump that read as Lana stuck in a loop (QA 2026-10-08).

AI-first by design (see [[no-new-regex-use-ai-signals]]): the old phrase lists stay only as
the floor when no model is configured.
"""

from __future__ import annotations

import json
import logging

from app.orchestrator.llm import llm_configured, llm_json, router_model

_log = logging.getLogger(__name__)

_SYSTEM = (
    "You read ONE reply a user sent right after Lana (a neighborhood app) showed them meets "
    "or told them none matched. Say what the reply means. Output only valid JSON: "
    '{"verdict":"accept"|"widen"|"decline"|"more"|"new"|"other", '
    '"different_subject": <string or null>}. '
    "different_subject is a kind of thing the reply asks for that is a DIFFERENT subject from "
    "the topic being searched — broader or narrower — in the reply's own words. It is null "
    "when the reply names nothing, names only the searched topic, or only asks to loosen or "
    "broaden the searched topic, even when it repeats the topic's own word. Words that only "
    "point back at the searched topic (similar, related, close to it, like that) are never a "
    "subject. "
    "UNDERSTAND, DO NOT PATTERN-MATCH — any language, any phrasing. "
    "accept = yes to what Lana just offered: keep an ear out / email or notify them when "
    "one appears (yes, sure, please do, go ahead, let me know). "
    "widen = they want related topics or a broader search on the SAME interest (widen it, "
    "anything similar, something close to that). "
    "decline = no to the offer, or they are done looking (no, nah, no thanks, not now, "
    "that's ok, never mind, I'm good). "
    "more = they want to see OTHER or MORE meets than the ones in front of them, with no "
    "new topic of their own (what others?, any other?, anything else?, more, show me more, "
    "what else is there, no others?). A short 'no others' / 'none else' / 'that's it?' "
    "right after Lana listed meets is ASKING whether there are more, punctuation or not — "
    "more, not decline. "
    "new = they name a new or narrower thing to look for: a topic, a day, a time, a place, "
    "a host ('any yoga?', 'this weekend', 'in San Jose', 'for kids'). "
    "other = ANYTHING else: a question about a meet on screen, a different request, "
    "small talk, thanks. "
    "ALWAYS other, whatever the offer: anything about how they FEEL, health, distress, "
    "danger, or an unsafe request — a safety rail owns those turns. "
    "When you genuinely cannot tell, answer other."
)

_VALID = {"accept", "widen", "decline", "more", "new", "other"}


def read_browse_followup(*, lana_said: str, topic: str, msg: str) -> str | None:
    """One verdict from _VALID, or None when no model is configured or the call fails,
    so the caller falls back to its old reading."""
    text = str(msg or "").strip()
    if not text or not llm_configured():
        return None
    payload = json.dumps(
        {
            "lana_said": str(lana_said or "")[:400],
            "topic_being_searched": str(topic or "")[:80] or None,
            "user_reply": text[:300],
        },
        ensure_ascii=False,
    )
    try:
        raw = llm_json(
            model=router_model(),
            system=_SYSTEM,
            user_payload=payload,
            max_tokens=120,
            temperature=0.0,
        )
    except Exception as exc:  # noqa: BLE001 — a follow-up read must never break the turn
        _log.warning("browse_followup_ai failed: %s", exc)
        return None
    if not isinstance(raw, dict):
        return None
    verdict = str(raw.get("verdict") or "").strip().lower()
    if verdict not in _VALID:
        return None
    # "Widen" means loosen the SAME topic. The verdict alone read a fresh subject that is
    # broad ("any informative event" after jazz) as widen, 5/5, and re-ran the jazz search
    # (prod 2026-10-08). The model also names any subject the reply asks for that is not
    # the searched topic; a widen that asks for a different subject is a new search.
    other = str(raw.get("different_subject") or "").strip()
    if verdict == "widen" and other and other.lower() not in ("null", "none"):
        _log.info("browse_followup widen->new subject=%r topic=%r", other[:60], topic[:40])
        return "new"
    return verdict
