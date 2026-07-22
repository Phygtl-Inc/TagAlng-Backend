"""AI-tailored quick-setup cards for the in-chat host flow.

Instead of hardcoding "How many moms?" + generic bring chips, this asks the LLM to tailor
the setup cards (name / capacity / sharing / approval / bring) to THIS event — suggested
event names (the first pre-fills the name field, the rest are tap-to-swap chips), the
audience noun ("moms" vs "dads" vs "neighbors"), and bring items that fit the activity (a
stroller coffee walk → stroller + coffee mug; a potluck → a dish to share). All of it comes
back in ONE call so the FE can render them as a single swipeable carousel. Best-effort:
returns sensible defaults on any failure so the flow never breaks.
"""

from __future__ import annotations

import json
from typing import Any

_SYSTEM = """You set up a neighbor's local event. Given the event so far, tailor the four \
quick-setup cards to THIS event and return ONE compact JSON object:
{"title_suggestions": ["...", "...", "..."],
 "capacity_label": "...", "capacity_default": 8,
 "sharing_label": "...", "sharing_hint": "...",
 "approval_label": "...", "approval_hint": "...",
 "bring_label": "...", "bring_hint": "...", "bring_suggestions": ["...", "..."],
 "cover_emoji": "..."}

- title_suggestions: EXACTLY 3 short, specific event names tailored to THIS event — the
  people, theme, or activity mentioned (a gathering of Brazilian moms ->
  ["Brazilian Moms Meetup","Brazil Heritage Mixer","Cafezinho & Chat"]; a toddler park
  meet -> ["Toddler Park Playdate","Morning Playground Meetup","Little Ones at the Park"]).
  Put the best one FIRST — it pre-fills the name field as the default. When almost nothing
  is known yet, still return 3 warm neighborly names ("Neighborhood Meetup").

- capacity_label: a short "how many can come?" question using the RIGHT audience noun for
  this event — a moms meetup -> "How many moms?"; a dads hangout -> "How many dads?"; a
  mixed/family/neighbor event -> "How many neighbors?" or "How many can come?".
- capacity_default: a sensible max headcount for this kind of event (small coffee ~8,
  a park playdate ~12, a big potluck ~20). Integer 2-30.
- sharing_label / sharing_hint: whether attendees can pass the invite link to a friend.
- approval_label / approval_hint: whether the host approves each join request.
- bring_label / bring_hint: a friendly "anything to bring?" prompt.
- bring_suggestions: 0-3 concrete items that fit THIS activity (stroller coffee walk ->
  ["Stroller","Coffee mug"]; picnic -> ["Blanket","A snack to share"]; beach -> ["Sunscreen"]).
  [] if nothing obviously fits. Concrete items ONLY — when the host said there's nothing
  to bring, return []; never emit a none-answer ("nothing", "none") as an item.
- cover_emoji: ONE emoji that captures THIS event's vibe, used as the card's cover art
  (stroller coffee walk -> "☕"; soccer -> "⚽"; potluck -> "🥘"; book club -> "📚";
  playdate -> "🧸"). Pick from the event's actual activity, never a generic default.

Keep every label under ~40 chars, warm and concrete. Never invent event facts."""

_DEFAULTS: dict[str, Any] = {
    "title_suggestions": ["Neighborhood Meetup", "Coffee & Catch-up", "Weekend Get-together"],
    "capacity_label": "How many can come?",
    "capacity_default": 8,
    "sharing_label": "Can attendees pass the link on?",
    "sharing_hint": "They can hand it to a friend who fits.",
    "approval_label": "Want to approve each joiner?",
    "approval_hint": "I'll text you each request · one tap.",
    "bring_label": "Anything to bring?",
    "bring_hint": "I'll add it to the pinned list in chat.",
    "bring_suggestions": [],
    "cover_emoji": None,
}


def _str(val: Any, fallback: str, max_len: int = 60) -> str:
    s = str(val or "").strip()[:max_len]
    return s or fallback


def setup_suggestions(
    *, history: list[dict[str, Any]], user_message: str, draft: dict[str, Any]
) -> dict[str, Any]:
    """Return the tailored setup-card config (see _SYSTEM), or _DEFAULTS on any failure."""
    try:
        from app.orchestrator.llm import llm_configured, llm_json, router_model

        if not llm_configured():
            return dict(_DEFAULTS)
        keep = ("title", "venue_name", "starts_at", "cohort_tags", "description")
        convo = "\n".join(
            f"{m.get('role', '?')}: {str(m.get('content') or '').strip()}"
            for m in (history or [])[-8:]
            if str(m.get("content") or "").strip()
        )
        payload = "\n\n".join(
            [
                "EVENT SO FAR:\n"
                + json.dumps({k: draft.get(k) for k in keep}, ensure_ascii=False),
                "CONVERSATION SO FAR:\n" + (convo or "(none)"),
                f"USER'S LATEST MESSAGE:\n{str(user_message or '').strip()}",
            ]
        )
        # Router-tier model on purpose: this is a tiny fill-a-JSON-template task
        # (labels + chips, ~300 tokens) that the full synth model spent ~6s on —
        # the mini tier does it in a fraction of that with no quality cliff.
        data = llm_json(
            model=router_model(),
            system=_SYSTEM,
            user_payload=payload,
            max_tokens=400,
            temperature=0.3,
        )
        if not isinstance(data, dict):
            return dict(_DEFAULTS)
        cap_default = data.get("capacity_default")
        try:
            cap_default = max(2, min(int(cap_default), 30))
        except (TypeError, ValueError):
            cap_default = _DEFAULTS["capacity_default"]
        from app.lana_ui import is_none_bring_item, sanitize_cover_emoji

        bring = [
            str(b).strip()[:60]
            for b in (data.get("bring_suggestions") or [])
            if isinstance(b, str) and str(b).strip() and not is_none_bring_item(b)
        ][:3]
        titles: list[str] = []
        for t in data.get("title_suggestions") or []:
            name = str(t).strip()[:60] if isinstance(t, str) else ""
            if name and name not in titles:
                titles.append(name)
        return {
            # The first name pre-fills the setup card; the rest render as tap-to-swap chips.
            "title_suggestions": titles[:3] or list(_DEFAULTS["title_suggestions"]),
            "capacity_label": _str(data.get("capacity_label"), _DEFAULTS["capacity_label"], 40),
            "capacity_default": cap_default,
            "sharing_label": _str(data.get("sharing_label"), _DEFAULTS["sharing_label"], 60),
            "sharing_hint": _str(data.get("sharing_hint"), _DEFAULTS["sharing_hint"], 80),
            "approval_label": _str(data.get("approval_label"), _DEFAULTS["approval_label"], 60),
            "approval_hint": _str(data.get("approval_hint"), _DEFAULTS["approval_hint"], 80),
            "bring_label": _str(data.get("bring_label"), _DEFAULTS["bring_label"], 60),
            "bring_hint": _str(data.get("bring_hint"), _DEFAULTS["bring_hint"], 80),
            "bring_suggestions": bring,
            "cover_emoji": sanitize_cover_emoji(data.get("cover_emoji")),
        }
    except Exception:  # noqa: BLE001 - best-effort; deterministic fallback
        import logging

        logging.getLogger(__name__).exception("setup_suggestions_failed")
        return dict(_DEFAULTS)
