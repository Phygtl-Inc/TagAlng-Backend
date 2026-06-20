"""AI-tailored title suggestions + affinity question for the in-chat event-host flow.

The host flow's chips used to be hardcoded ("Playdate at the park", "Same kid-stage"),
which felt generic for, say, a Brazilian moms meetup. This proposes event-aware options
from the conversation — same idea as the pass-along / tip `ask` mechanism. Best-effort:
returns {} on any failure so the flow falls back to the deterministic chips.
"""

from __future__ import annotations

import json
from typing import Any

_SYSTEM = """You help a neighbor host a local event. Given the event captured so far and \
the conversation, return ONE compact JSON object:
{"title_suggestions": [...], "affinity": {"question": "...", "options": [...]}}

- title_suggestions: 2-3 short, specific event titles tailored to THIS event — the
  people, theme, or activity mentioned. e.g. a gathering of Brazilian moms →
  ["Brazilian Moms Meetup","Brazil Heritage Mixer","Latina Moms Hangout"]; a toddler
  park meet → ["Toddler Park Playdate","Morning Playground Meetup"]. [] if too little is known.
- affinity: the single "who's it for?" question for THIS event, with 2-4 tappable options
  tailored to it. e.g. Brazilian moms event → {"question":"Who's it for?","options":
  ["Brazilian moms only","All Latina moms","Open to everyone"]}; generic playdate →
  {"question":"Who's it for?","options":["Same kid-stage","Any toddler mom","Open · all moms"]}.

Tailor everything to the actual event. Never invent facts the conversation doesn't imply."""


def event_suggestions(
    *, history: list[dict[str, Any]], user_message: str, draft: dict[str, Any]
) -> dict[str, Any]:
    """Returns {"title_suggestions": [...], "affinity": {"question","options"}} — any
    subset the model could produce, or {} on failure."""
    try:
        from app.orchestrator.llm import llm_configured, llm_json, synthesizer_model

        if not llm_configured():
            return {}
        convo = "\n".join(
            f"{m.get('role', '?')}: {str(m.get('content') or '').strip()}"
            for m in (history or [])[-8:]
            if str(m.get("content") or "").strip()
        )
        keep = ("title", "venue_name", "starts_at", "cohort_tags", "description")
        payload = "\n\n".join(
            [
                "EVENT SO FAR:\n"
                + json.dumps({k: draft.get(k) for k in keep}, ensure_ascii=False),
                "CONVERSATION SO FAR:\n" + (convo or "(none)"),
                f"USER'S NEW MESSAGE:\n{str(user_message or '').strip()}",
            ]
        )
        data = llm_json(
            model=synthesizer_model(),
            system=_SYSTEM,
            user_payload=payload,
            max_tokens=300,
            temperature=0.3,
        )
        if not isinstance(data, dict):
            return {}
        out: dict[str, Any] = {}
        titles = [
            str(t).strip()
            for t in (data.get("title_suggestions") or [])
            if isinstance(t, str) and str(t).strip()
        ][:3]
        if titles:
            out["title_suggestions"] = titles
        aff = data.get("affinity")
        if isinstance(aff, dict):
            q = str(aff.get("question") or "").strip()
            opts = [
                str(o).strip()
                for o in (aff.get("options") or [])
                if isinstance(o, str) and str(o).strip()
            ][:4]
            if q and len(opts) >= 2:
                out["affinity"] = {"question": q, "options": opts}
        return out
    except Exception:  # noqa: BLE001 - suggestions are best-effort
        import logging

        logging.getLogger(__name__).exception("event_suggestions_failed")
        return {}
