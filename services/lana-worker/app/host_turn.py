"""The LLM 'brain' for a free-text turn in the in-chat host flow.

The flow's STATE stays deterministic — which card to show, stage transitions, publish — because
that has to line up with the UI. But the *understanding* of what a host typed and the *reply*
must NOT be hardcoded or regex-gated: a host can say the same thing a hundred ways ("5-7 people",
"just a handful", "keep it small"), and a keyword gate misfires the moment they phrase it
differently. This one call reads their latest message in whatever form, extracts whatever event
details it carries, and writes a warm, non-repetitive reply. Best-effort: returns None on any
failure so the caller can fall back to a plain nudge (never a loop).
"""

from __future__ import annotations

import json
import logging
from typing import Any

_SYSTEM = """You are Lana, a warm neighborhood concierge helping a neighbor set up a small local \
event in chat. Read their latest message — however they phrased it — and do two things.

The neighbor may write in ANY language — understand it either way. Extracted fields keep the
user's own words exactly as authored (a title or place they named is THEIR content — never
translate it); booleans/integers are language-neutral. Write the reply in the language they
are writing in.

1) EXTRACT any event details they clearly stated (never guess; use null when unsure):
   - title: a name for the event if they gave or proposed one
   - place: ONE clear location (their home, a named venue, a park). null if none — or if they're
     weighing options ("my place or a café"), leave it null; don't pick for them. A bare ZIP
     code ("34786") is an AREA, never a place — leave place null and ask for a real spot.
   - capacity: an integer max headcount if they signalled one ("5-7 people" -> 7, "just a few" or
     "small group" -> a sensible small number like 6)
   - auto_approve: true if anyone can join freely, false if they want to approve joiners
   - allow_share: true if attendees may pass the invite on, false if invite-only

2) REPLY warmly in 1-2 sentences: acknowledge what they just said, answer any question, and gently
   point to what's STILL NEEDED to post it. Rules:
   - You CANNOT poll people, check calendars, or see who's free — never claim you can. If they ask
     you to coordinate availability, suggest they pick a time and you'll post it so neighbors RSVP.
   - If they ask for advice (how many, where, what time), give a concrete suggestion.
   - Never invent a name, date, or place they didn't give. Don't repeat yourself mechanically.
   - Warm and natural, mom-to-mom, under ~55 words. No markdown headers or lists.

Return ONE JSON object:
{"title": <string|null>, "place": <string|null>, "capacity": <int|null>,
 "auto_approve": <bool|null>, "allow_share": <bool|null>, "reply": <string>}"""


def host_turn_brain(
    *,
    history: list[dict[str, Any]],
    user_message: str,
    draft: dict[str, Any],
    needed: list[str],
) -> dict[str, Any] | None:
    """Understand + reply to one free-text host turn. None on any failure (caller falls back)."""
    try:
        from app.orchestrator.llm import llm_configured, llm_json, synthesizer_model

        if not llm_configured():
            return None
        keep = ("title", "description", "starts_at", "venue_name", "max_attendees", "cohort_tags")
        convo = "\n".join(
            f"{m.get('role', '?')}: {str(m.get('content') or '').strip()}"
            for m in (history or [])[-6:]
            if str(m.get("content") or "").strip()
        )
        payload = "\n\n".join(
            [
                "EVENT SO FAR:\n"
                + json.dumps({k: draft.get(k) for k in keep}, ensure_ascii=False),
                "CONVERSATION SO FAR:\n" + (convo or "(none)"),
                f"STILL NEEDED: {', '.join(needed) if needed else '(nothing — ready to post)'}",
                f"USER'S LATEST MESSAGE:\n{str(user_message or '').strip()}",
            ]
        )
        data = llm_json(
            model=synthesizer_model(),
            system=_SYSTEM,
            user_payload=payload,
            max_tokens=280,
            temperature=0.5,
        )
        if not isinstance(data, dict):
            return None
        reply = str(data.get("reply") or "").strip()
        if not reply:
            return None
        out: dict[str, Any] = {
            "reply": reply[:600],
            "title": None,
            "place": None,
            "capacity": None,
            "auto_approve": None,
            "allow_share": None,
        }
        title = str(data.get("title") or "").strip()
        if title:
            out["title"] = title[:80]
        place = str(data.get("place") or "").strip()
        if place:
            out["place"] = place[:80]
        cap = data.get("capacity")
        try:
            if cap is not None:
                out["capacity"] = max(2, min(int(cap), 100))
        except (TypeError, ValueError):
            pass
        if isinstance(data.get("auto_approve"), bool):
            out["auto_approve"] = data["auto_approve"]
        if isinstance(data.get("allow_share"), bool):
            out["allow_share"] = data["allow_share"]
        return out
    except Exception:  # noqa: BLE001 - best-effort; caller falls back to a plain nudge
        logging.getLogger(__name__).exception("host_turn_brain_failed")
        return None
