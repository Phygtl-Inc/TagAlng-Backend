"""LLM-authored replies for the out-of-scope lane.

Routing stays exactly as it was — the classifier still owns *whether* a turn is a clarify,
a decline, or a graceful close (goal / clarify / confidence / abandon). This module only turns
the CHOSEN branch into Lana's own words for the specific situation, instead of returning a
canned template string.

Best-effort by design: if the LLM isn't configured or the call fails, this returns None and the
caller falls back to its template — so a reply is always produced, never blocked.
"""

import json
import logging
from typing import Any

_log = logging.getLogger(__name__)

_CAPABILITIES = (
    "connect neighbors on the same block: finding people, local activities and meetups, "
    "swapping/borrowing/giving things, and sharing or asking for a local TIP (a recommendation "
    "of a nearby place, person, or service)"
)

_SYSTEM = (
    "You are Lana, a warm, concise neighborhood assistant. Author ONE short reply (1–2 "
    "sentences, plain prose, no lists, at most one light emoji) in Lana's voice for the given "
    "SITUATION. TagAlng can only " + _CAPABILITIES + ". Never claim you can do the unsupported "
    "thing, and never invent a capability or a specific place/name. Ground the reply in the "
    "user's actual words.\n"
    "SITUATIONS:\n"
    "- clarify: you can't do the ask directly, but there is a neighborhood angle (almost always "
    "a local recommendation / tip). Honestly say you can't do it yourself, then offer that "
    "SPECIFIC angle as a question, naming the concrete subject (e.g. a local tax preparer). Use "
    "offer_angle if provided.\n"
    "- decline: the ask has NO neighborhood angle. Warmly and honestly say it's not something "
    "you can do yet, mention you've noted the request, and invite them back to what you can help "
    "with. Do NOT offer a recommendation that doesn't exist.\n"
    "- close: the user has accepted your 'no' and is disengaging. Give a brief, warm sign-off. "
    "Do NOT repeat the refusal or re-list capabilities — just leave the door open lightly.\n"
    'Return JSON: {"assistant_message": "<the reply>"}.'
)


def author_out_of_scope_reply(
    *,
    mode: str,
    user_msg: str = "",
    subject: str = "",
    offer_angle: str = "",
) -> str | None:
    """Return Lana's reply for an out-of-scope turn, or None to fall back to a template.

    mode: "clarify" | "decline" | "close".
    subject: the thing the user asked about (e.g. "taxes").
    offer_angle: the specific in-scope angle to offer on a clarify (e.g. "a local tax preparer
    recommendation"). Never raises.
    """
    if mode not in ("clarify", "decline", "close"):
        return None
    try:
        from app.orchestrator.llm import llm_configured, llm_json, router_model

        if not llm_configured():
            return None
        payload = json.dumps(
            {
                "situation": mode,
                "user_message": str(user_msg or "").strip(),
                "subject": str(subject or "").strip(),
                "offer_angle": str(offer_angle or "").strip(),
            },
            ensure_ascii=False,
        )
        from app.context import self_disclosure_rule

        data = llm_json(
            model=router_model(),
            # G8: the decline lane is the single most likely place for a
            # sympathy performance ("Aw, I'm sorry I can't help with that!").
            # This composer builds its own system string, so the rule is
            # appended here rather than inherited.
            system=_SYSTEM + "\n\n---\n\n" + self_disclosure_rule(),
            user_payload=payload,
            max_tokens=160,
            temperature=0.5,
        )
        reply = str((data or {}).get("assistant_message") or "").strip()
        return reply or None
    except Exception:  # noqa: BLE001 — best-effort; caller falls back to its template
        _log.exception("out_of_scope_reply.error mode=%s", mode)
        return None
