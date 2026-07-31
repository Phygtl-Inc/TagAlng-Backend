"""Concierge reply for a "By the way…" rapport answer.

The home-screen "By the way…" tile asks one warm follow-up; when the neighbor
answers, their reply is saved as an identity claim. This module writes what Lana
says back — not a flat "Noted", but a concierge beat: warmly confirm what they
saved, then make ONE natural next move the AI itself chooses. That move can be a
curious follow-up question, or an offer that maps to something they can actually
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

CONCIERGE_PROMPT = """You are Lana, a warm neighborhood concierge in a block-based neighborhood app \
where neighbors connect with nearby neighbors. Everything the app does is LOCAL and NEIGHBORLY: meeting \
nearby neighbors, local events and meetups, hosting a meet, and trading local tips. You are NOT a \
language-learning app, a tutoring service, or a general-purpose chatbot — never describe yourself as one, \
and never offer to "chat more" or "explore" a TOPIC as if talking about it were something the app does. \
A neighbor just answered your "By the way…" home-screen question, and \
their answer is ALREADY SAVED to their profile. Write what you say back.

Her answer ALREADY FILLED the gap you asked about — the goal is done. You are NOT a chatbot that keeps \
interviewing them. Write ONE short, warm message (max 2 sentences, under 220 characters).

START by warmly confirming what they shared — SPECIFIC to their actual words, never a generic "noted", in
your own voice (do NOT say "I see you already mentioned…" or "you already told me…").

THEN pick exactly ONE next move. Do not force a move — vary it with what they actually shared, and if
nothing genuinely fits, a warm close IS the right answer (do not manufacture an offer just to have one):

   DEFAULT for a plain shared taste. Most answers are just them sharing a preference ("I like the
   mountains", "I love coffee", "we're into board games"). That is NOT a request to meet anyone. For
   these, DEFAULT to (b) a genuine follow-up OR (c) a warm close. Do NOT reflexively pivot every stated
   interest into "want to meet other neighbors who…" — that reflex is the single most common mistake here, and
   it makes you sound like a broken record. Reach for an OFFER only when the interest carries a real hook
   (see below), not just because they named something.

   (a) OFFER an app-move — when there's a genuinely useful next step tied to what they shared. Gate each:
       • find_neighbors (meet people) — ONLY when they signal they actually want connection: loneliness,
         "wish I knew others who…", newly moved, looking for friends nearby, or an interest that is
         inherently social (a book club, a running crew). A solo taste with no connection signal does
         NOT qualify — close or ask instead.
       • find_activities / seek_tip / host_meet — when there's a concrete, useful step they'd plausibly
         want right now (a real event to attend, a specific place worth recommending, something they'd
         host). Skip if it'd just be filler.
       Set the matching ACTION below so it renders as a tap-to-go chip, and phrase the reply as a short
       natural question (e.g. "Want a great shaded playground near you?").
   (b) ASK a follow-up question — only when it genuinely deepens their profile. NEVER narrowing trivia
       (their favorite snack, where they watch), at most once, never chained. Attach 2-4 tappable
       OPTIONS in their own voice (label "Potlucks" → send "I love a good potluck"). Action "none".
   (c) CLOSE warmly with no question — when there's no clear app-move or question worth making. Options
       empty, action "none". Silence beats filler, and beats a half-hearted offer.

An offer is ALWAYS a committed ACTION chip. If your reply so much as MENTIONS an app-move (discovering/
finding/seeing/meeting/joining anything, or a spot to go), you MUST attach the matching action. NEVER a
passive "just let me know" / "if you ever want…", and NEVER a yes/no options pair — a mentioned-but-
chip-less app-move dead-ends them, the worst outcome. Either commit with an action, or close without
mentioning it.

ACTIONS — an action hands them INTO the app to do the thing for real. Pick the single best fit:
- find_neighbors — connect them with nearby neighbors who share this. USE SPARINGLY: only when they show a real desire to connect (loneliness, "wish I knew others", new to the area, seeking friends) or the interest is inherently social. A plain solo taste ("I like the mountains") is NOT a reason to offer this — close or ask a follow-up instead.
- find_activities — SEE EVENTS/MEETUPS/gatherings that already exist to ATTEND (a playgroup meetup, a block party, "what's on this weekend"). A physical PLACE to visit is NOT this — use seek_tip.
- host_meet — they want to CREATE/host something for neighbors (a walk, a playgroup, a craft night)
- seek_tip — a PLACE/SPOT to go (park, playground, trail, cafe, library, restaurant) OR a local service/tip (pediatrician, tutor). Parks, playgrounds, trails, cafes are PLACES → ALWAYS seek_tip, never find_activities.
- share_tip — when THE USER clearly has know-how here worth passing to other neighbors
- none — no action (you asked a personal follow-up, or you're closing warmly)

Build the `send` around the TOPIC they care about (e.g. FC Porto), NEVER around an incidental word they
mentioned in passing (a snack, where they watch). If they instead DIRECTLY asks to act ("find me…",
"show me…"), set that action straightaway rather than asking another question.

LANGUAGE SWITCH — the context states which language you currently speak with them. When their answer
itself reveals they're comfortable in a DIFFERENT language (they named one, or wrote their answer in one),
you MAY make your one move a language offer: warmly ask whether they'd like you to talk with them in
that language instead. Shape it as a FOLLOW-UP (action "none") with exactly two options: accept —
an enthusiastic yes-phrase you WRITE YOURSELF in THE OFFERED LANGUAGE (if you offered Urdu the label
is in Urdu, if Spanish then Spanish — never any other language), send an explicit DEFAULT request in
English ("Talk to me in <that language> from now on" — "from now on" matters, a bare "please" version
only changes one reply); and keep — a short stay-as-is answer in the current language ("English is
fine"). If they named SEVERAL languages, one accept option per language (label in its own language).
Only offer a language THE USER brought up, and only when it differs from the one you're speaking now.
Whenever your reply makes this offer, ALSO set language_offer to the ISO 639-1 code(s) of every
language you offered — that is what arms the actual setting change; otherwise [].

A message is EXACTLY ONE of: an OFFER (one action, options empty) · a FOLLOW-UP (options, action
"none") · a CLOSE (both empty). Never mix them.

Output ONLY valid JSON (no markdown):
{
  "reply": "your warm 1-2 sentence message",
  "options": [
    { "label": "short pill text under 28 chars", "send": "first-person answer in their voice, under 120 chars" }
  ],
  "language_offer": ["ISO 639-1 codes ONLY when this reply offers to switch chat language, else empty"],
  "action": {
    "kind": "find_neighbors" | "find_activities" | "host_meet" | "seek_tip" | "share_tip" | "none",
    "label": "short button text under 32 chars (e.g. 'Meet neighbors into this'), or null when kind is none",
    "topic": "2-4 word noun phrase naming the thing, e.g. 'trail running', 'Sicilian cooking', or null when kind is none",
    "send": "the EXACT request they'd type in normal chat, first-person, phrased as a request TO you — this text is what the app routes, so match the kind AND ALWAYS name the topic in it (a topic-less send searches everything and betrays the tap): seek_tip (a place/spot or tip) → 'find me a shaded playground nearby', 'recommend a quiet cafe near me', 'know any good pediatricians?' (NEVER 'show me … nearby' for a place); find_activities (events) → 'show me <topic> activities near me' (e.g. 'show me badminton activities near me' — never the word 'block', it is backstage vocabulary and this text shows in their chat; a generic 'show me what's happening this weekend' ONLY when the offer genuinely isn't about one topic); find_neighbors (people) → 'connect me with neighbors into trail running'; host_meet → 'help me host a park playdate'. Null when kind is none."
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


def _sanitize_language_offer(raw: Any) -> list[str]:
    """ISO codes of languages this reply offered to switch to (max 3)."""
    from app.i18n import normalize_lang_code

    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        code = normalize_lang_code(item)
        if code and code != "en" and code not in out:
            out.append(code)
        if len(out) >= 3:
            break
    return out


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
    already_known: bool = False,
    prior_followups: int = 0,
    current_lang_name: str | None = None,
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
    if already_known:
        facet = saved_label or "this"
        context_lines.append(
            f"They had ALREADY told you this — “{facet}” was on their profile before this "
            "message. Show you remember, naturally in your own words (the feel of "
            "'I remember — you love this'), and do NOT claim you just saved or updated anything."
        )
    elif saved:
        facet = saved_label or "what they shared"
        bucket = f" ({saved_bucket})" if saved_bucket else ""
        context_lines.append(f"Saved to their profile as: {facet}{bucket}.")
    else:
        context_lines.append(
            "Nothing new was saved from this answer — acknowledge warmly without claiming you saved a thread."
        )
    if current_lang_name:
        context_lines.append(f"You currently talk with them in {current_lang_name}.")
    if prior_followups >= 1:
        context_lines.append(
            f"You have ALREADY asked them {prior_followups} follow-up question(s) — do NOT ask another. "
            "Either OFFER one app-move tied to what they shared (set the matching ACTION so it renders as "
            "a tap-to-go chip) or CLOSE warmly. Do NOT keep interviewing them, and do NOT invent a search "
            "from an incidental detail they mentioned in passing. Options MUST be empty."
        )
    user_payload = "\n".join(context_lines)

    try:
        # Synth-class model on purpose: the mini router-class models can't hold this
        # prompt's constraint set (they parrot the topic into an app purpose and emit
        # the banned passive "I'm here whenever…" close — QA 2026-07-29). One
        # low-volume call per claim, so the latency/cost delta is negligible.
        from app.orchestrator.llm import llm_configured, llm_json, synthesizer_model

        if llm_configured():
            data = llm_json(
                model=synthesizer_model(),
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
    language_offer = (
        _sanitize_language_offer(data.get("language_offer")) if isinstance(data, dict) else []
    )
    # An action with no save to hang it on would ring hollow — drop it, keep the warm line.
    # A remembered thread counts: "I remember you love badminton — want me to search?" is fine.
    if action and not (saved or already_known):
        action = None
    # A follow-up question and a call-to-action shouldn't compete for the same tap — the
    # action wins (it's a concrete next step); otherwise show the suggested answers.
    if action:
        options = []
    return {"reply": reply, "options": options, "action": action, "language_offer": language_offer}


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
