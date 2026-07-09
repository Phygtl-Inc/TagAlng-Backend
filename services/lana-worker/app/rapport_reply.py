"""Concierge reply for a "By the way…" rapport answer.

The home-screen "By the way…" tile asks one warm follow-up; when the neighbor
answers, her reply is saved as an identity claim. This module writes what Lana
says back — not a flat "Noted", but a concierge beat: warmly confirm what she
saved, then make ONE natural next move the AI itself chooses. That move can be a
curious follow-up question, or an offer that maps to something she can actually
do here (meet neighbors who share it, host a meet, trade a tip) — or nothing,
when silence beats filler.

No sticky flow: the AI re-decides the next move every time from the answer in
hand. One Flash-class call, with a warm static fallback so the path never breaks.
"""

import logging
import os
from typing import Any

log = logging.getLogger(__name__)

# The next-move kinds the frontend knows how to dispatch. Anything else the model
# returns collapses to "none" (a conversational follow-up with no action chip).
_ACTION_KINDS = frozenset(
    {"find_neighbors", "find_activities", "host_meet", "seek_tip", "share_tip", "none"}
)

_FALLBACK_REPLY = "Love that — I've saved it to your profile. Tell me more anytime."

CONCIERGE_PROMPT = """You are Lana, a warm neighborhood concierge in TagAlng, a block-based app where \
moms connect with nearby moms. A neighbor just answered your "By the way…" home-screen question, and \
her answer is ALREADY SAVED to her profile. Write what you say back.

Her answer ALREADY FILLED the gap you asked about — the goal is done. You are NOT a chatbot that keeps \
interviewing her. Write ONE short, warm message (max 2 sentences, under 220 characters).

START by warmly confirming what she shared — SPECIFIC to her actual words, never a generic "noted", in
your own voice (do NOT say "I see you already mentioned…" or "you already told me…").

THEN pick exactly ONE next move. Do not force a move — vary it with what she actually shared, and if
nothing genuinely fits, a warm close IS the right answer (do not manufacture an offer just to have one):
   (a) OFFER an app-move — when there's a genuinely useful next step tied to what she shared (meet
       neighbors who share it, see related meetups/events, get a local tip or spot, host something).
       Set the matching ACTION below so it renders as a tap-to-go chip, and phrase the reply as a short
       natural question (e.g. "Want to meet other FC Porto fans on your block?" / "Want a great shaded
       playground near you?").
   (b) ASK a follow-up question — only when it genuinely deepens her profile. NEVER narrowing trivia
       (her favorite snack, where she watches), at most once, never chained. Attach 2-4 tappable
       OPTIONS in her own voice (label "Potlucks" → send "I love a good potluck"). Action "none".
   (c) CLOSE warmly with no question — when there's no clear app-move or question worth making. Options
       empty, action "none". Silence beats filler, and beats a half-hearted offer.

An offer is ALWAYS a committed ACTION chip. If your reply so much as MENTIONS an app-move (discovering/
finding/seeing/meeting/joining anything, or a spot to go), you MUST attach the matching action. NEVER a
passive "just let me know" / "if you ever want…", and NEVER a yes/no options pair — a mentioned-but-
chip-less app-move dead-ends her, the worst outcome. Either commit with an action, or close without
mentioning it.

ACTIONS — an action hands her INTO the app to do the thing for real. Pick the single best fit:
- find_neighbors — connect her with nearby neighbors who share this (an interest, heritage, life stage)
- find_activities — SEE EVENTS/MEETUPS/gatherings that already exist to ATTEND (a playgroup meetup, a block party, "what's on this weekend"). A physical PLACE to visit is NOT this — use seek_tip.
- host_meet — she wants to CREATE/host something for neighbors (a walk, a playgroup, a craft night)
- seek_tip — a PLACE/SPOT to go (park, playground, trail, cafe, library, restaurant) OR a local service/tip (pediatrician, tutor). Parks, playgrounds, trails, cafes are PLACES → ALWAYS seek_tip, never find_activities.
- share_tip — when SHE clearly has know-how here worth passing to other moms
- none — no action (you asked a personal follow-up, or you're closing warmly)

Build the `send` around the TOPIC she cares about (e.g. FC Porto), NEVER around an incidental word she
mentioned in passing (a snack, where she watches). If she instead DIRECTLY asks to act ("find me…",
"show me…"), set that action straightaway rather than asking another question.

A message is EXACTLY ONE of: an OFFER (one action, options empty) · a FOLLOW-UP (options, action
"none") · a CLOSE (both empty). Never mix them.

Output ONLY valid JSON (no markdown):
{
  "reply": "your warm 1-2 sentence message",
  "options": [
    { "label": "short pill text under 28 chars", "send": "first-person answer in her voice, under 120 chars" }
  ],
  "action": {
    "kind": "find_neighbors" | "find_activities" | "host_meet" | "seek_tip" | "share_tip" | "none",
    "label": "short button text under 32 chars (e.g. 'Meet neighbors into this'), or null when kind is none",
    "topic": "2-4 word noun phrase naming the thing, e.g. 'trail running', 'Sicilian cooking', or null when kind is none",
    "send": "the EXACT request she'd type in normal chat, first-person, phrased as a request TO you — this text is what the app routes, so match the kind: seek_tip (a place/spot or tip) → 'find me a shaded playground nearby', 'recommend a quiet cafe near me', 'know any good pediatricians?' (NEVER 'show me … nearby' for a place); find_activities (events) → 'show me what's happening this weekend'; find_neighbors (people) → 'connect me with moms into trail running'; host_meet → 'help me host a park playdate'. Null when kind is none."
  }
}

Rules:
- reply: warm, concrete, complete sentences — never trail off. Do NOT prefix with "By the way".
- NEVER propose an action OR options for a sensitive topic (health, grief, divorce, money, legal, mental health) — reply gently, options empty, action kind "none".
- Keep JSON compact, no commentary outside JSON."""


def _clean(val: Any, limit: int) -> str:
    return str(val or "").strip()[:limit]


def _sanitize_options(raw: Any) -> list[dict[str, str]]:
    """Up to 4 suggested answers, each {label, send}. Drops anything malformed or empty."""
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        label = _clean(item.get("label"), 28)
        send = _clean(item.get("send"), 120) or label
        if label and send:
            out.append({"label": label, "send": send})
        if len(out) >= 4:
            break
    return out


def _sanitize_action(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    kind = _clean(raw.get("kind"), 32).lower()
    if kind not in _ACTION_KINDS or kind == "none":
        return None
    label = _clean(raw.get("label"), 32)
    topic = _clean(raw.get("topic"), 60)
    send = _clean(raw.get("send"), 120)
    if not label:
        return None
    return {"kind": kind, "label": label, "topic": topic or None, "send": send or None}


def _fallback(saved_label: str | None) -> dict[str, Any]:
    if saved_label:
        return {
            "reply": f"Love that — I've saved “{saved_label}” to your profile. Tell me more anytime.",
            "options": [],
            "action": None,
        }
    return {"reply": _FALLBACK_REPLY, "options": [], "action": None}


def rapport_concierge_reply(
    *,
    answer_text: str,
    question: str | None = None,
    saved_label: str | None = None,
    saved_bucket: str | None = None,
    saved: bool = True,
    prior_followups: int = 0,
) -> dict[str, Any]:
    """Author Lana's concierge reply to a rapport tile answer.

    Returns ``{"reply": str, "action": {kind,label,topic} | None}``. Never raises —
    on any model or config failure it returns a warm static acknowledgement so the
    home tile always closes gracefully.
    """
    answer = _clean(answer_text, 500)
    if not answer:
        return _fallback(saved_label)

    context_lines = [f'The neighbor answered: "{answer}"']
    if question:
        context_lines.insert(0, f'Your "By the way…" question was: "{_clean(question, 300)}"')
    if saved:
        facet = saved_label or "what she shared"
        bucket = f" ({saved_bucket})" if saved_bucket else ""
        context_lines.append(f"Saved to her profile as: {facet}{bucket}.")
    else:
        context_lines.append(
            "Nothing new was saved from this answer — acknowledge warmly without claiming you saved a thread."
        )
    if prior_followups >= 1:
        context_lines.append(
            f"You have ALREADY asked her {prior_followups} follow-up question(s) — do NOT ask another. "
            "Either OFFER one app-move tied to what she shared (set the matching ACTION so it renders as "
            "a tap-to-go chip) or CLOSE warmly. Do NOT keep interviewing her, and do NOT invent a search "
            "from an incidental detail she mentioned in passing. Options MUST be empty."
        )
    user_payload = "\n".join(context_lines)

    try:
        from app.orchestrator.llm import llm_configured, llm_json, router_model

        if llm_configured():
            data = llm_json(
                model=router_model(),
                system=CONCIERGE_PROMPT,
                user_payload=user_payload,
                max_tokens=512,
                temperature=0.5,
            )
        else:
            data = _vertex_concierge_reply(user_payload)
    except Exception:
        log.exception("rapport_concierge_reply_failed")
        return _fallback(saved_label)

    reply = _clean(data.get("reply") if isinstance(data, dict) else None, 320)
    if not reply:
        return _fallback(saved_label)
    action = _sanitize_action(data.get("action")) if isinstance(data, dict) else None
    options = _sanitize_options(data.get("options")) if isinstance(data, dict) else []
    # An action with no save to hang it on would ring hollow — drop it, keep the warm line.
    if action and not saved:
        action = None
    # A follow-up question and a call-to-action shouldn't compete for the same tap — the
    # action wins (it's a concrete next step); otherwise show the suggested answers.
    if action:
        options = []
    return {"reply": reply, "options": options, "action": action}


def _vertex_concierge_reply(user_payload: str) -> Any:
    """Direct Vertex Gemini fallback when the orchestrator LLM isn't configured."""
    from app.orchestrator.json_util import parse_json_object
    from app.vertex_extract import _vertex_client

    client = _vertex_client()
    model = os.environ.get("VERTEX_EXTRACT_MODEL", "gemini-2.5-flash")
    from google.genai import types

    response = client.models.generate_content(
        model=model,
        contents=CONCIERGE_PROMPT + "\n\n" + user_payload,
        config=types.GenerateContentConfig(
            temperature=0.5,
            response_mime_type="application/json",
        ),
    )
    return parse_json_object(response.text or "")
