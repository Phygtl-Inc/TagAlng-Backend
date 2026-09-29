"""The first thing Lana says to someone who arrived from a community.

THE PROBLEM (2026-09-29)

    Someone taps a creator's link, lands on "I'm the concierge at Etiqueta do Reino",
    taps Continue — and Lana opens ZIP-scoped, asking about their "active lifestyle".
    Nothing about the arrival survives the tap.

    Worse, the greeting CANNOT name the community even when the scope is set, because
    `POST /lana/sessions` composes `opening` BEFORE apply_community_selection runs
    (frontend backend-asks §56a). The ordering is the bug; this module is what the
    corrected ordering calls.

THE MODEL — TWO INPUTS, NEVER A TEMPLATE

    1. SOURCE   — what this community is actually for.
                  first_action (what the operator says people should do first) when set,
                  else blurb (Lana's own generated description, populated on 8 of 11
                  creator communities today).

    2. MATURITY — who is arriving. A visitor with no claims gets one light, answerable,
                  community-grounded question. An existing user with thirty claims gets
                  something that uses what we already know about them.

    Contextual on turn one, governed by maturity thereafter. A template with the
    community's name slotted into it is not this and will read as one.

WHAT THIS DOES NOT DO
    It does not replace the rapport engine. It owns the FIRST question of a
    community-entry session. From the second turn the existing engine resumes, now with
    the community as standing context.

Defensive by contract: never raises into the request path. A failure returns the
existing generic opening rather than an error.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from app.auth import service_client

logger = logging.getLogger(__name__)

Maturity = Literal["visitor", "new", "engaged"]

# A visitor who has said nothing is not asked about themselves. They are asked something
# about the SUBJECT they arrived for, which costs them nothing to answer.
VISITOR_CLAIM_CEILING = 0
ENGAGED_CLAIM_FLOOR = 8

INTRO_PROMPT = """You write the FIRST thing Lana says to someone who just arrived at a \
community through its creator's link. One short question. Nothing else.

Lana is a concierge: warm, specific, never bubbly, never a form.

What this community is for:
{purpose}

Who just arrived:
{audience}

Rules:
- ONE question. It must be answerable in a sentence, by someone standing in a queue.
- Ground it in what this community is ACTUALLY about. A community about dining etiquette
  does not get asked about "your interests" — it gets asked something an etiquette person
  would enjoy answering.
- Do NOT ask them to introduce themselves, describe themselves, or list interests. That is
  a form wearing a question mark.
- Do NOT welcome them, thank them, or explain what Lana is. They just read that.
- Do NOT name the community. The header already does, and repeating it is what makes a
  greeting read as generated.
- No emoji. No exclamation marks.
- Under 18 words.

Return ONLY valid JSON: {{"question": "...", "why": "one clause, internal, not shown"}}
"""

PURPOSE_DRAFT_PROMPT = """You propose what a community's operator would most likely want a \
new person to DO first. The operator will edit this, so give them something worth editing.

Community: {name}
Description: {blurb}

Rules:
- One sentence, an ACTION, in the operator's voice, addressed to a new arrival.
- Concrete to this community. "Connect with others" is worthless — it fits anything.
- Under 20 words.
- Not a welcome message. What should they DO.

Return ONLY valid JSON: {{"first_action": "..."}}
"""

WIDEN_PROMPT = """Lana looked inside one community for an answer and found none. Write the \
one line asking whether to look wider.

Community: {community}
They asked: {question}

Rules:
- Name the community. The boundary has to be FELT — that is the point of being in one.
- State plainly that nobody here has answered this. Do not apologise, do not soften it
  into vagueness. It is not a failure, it is a fact about a young community.
- Then offer, as a question, to look beyond it.
- Under 25 words. No emoji.

Return ONLY valid JSON: {{"ask": "..."}}
"""


def community_purpose(place_id: str) -> dict[str, Any] | None:
    """What a community is for, and how sure we are.

    first_action is the operator's own words and always wins. blurb is Lana's generated
    description and is the floor — it exists on 8 of 11 creator communities and needs no
    operator effort, which is why it is the default rather than the fallback of last
    resort.
    """
    try:
        res = service_client().rpc("resolve_place_handle_by_id", {"p_place_id": place_id}).execute()
        row = res.data
    except Exception:
        row = None

    if not row:
        try:
            res = (
                service_client()
                .table("places")
                .select("id,name,blurb,first_action,place_type,hq_city")
                .eq("id", place_id)
                .limit(1)
                .execute()
            )
            rows = res.data or []
            row = rows[0] if rows else None
        except Exception:
            logger.exception("community_intro: could not read place %s", place_id)
            return None

    if not row:
        return None

    first_action = (row.get("first_action") or "").strip()
    blurb = (row.get("blurb") or "").strip()
    if not first_action and not blurb:
        # Nothing describes this community. Better to fall back to the generic opening
        # than to invent a purpose and ask a question grounded in fiction.
        return None

    return {
        "place_id": place_id,
        "name": row.get("name"),
        "purpose": first_action or blurb,
        "source": "first_action" if first_action else "blurb",
        "hq_city": row.get("hq_city"),
    }


def user_maturity(user_id: str | None, *, is_anonymous: bool = False) -> Maturity:
    """How much Lana already knows about this person.

    Drives how much the first question may assume. Cheap: one count.
    """
    if is_anonymous or not user_id:
        return "visitor"
    try:
        res = (
            service_client()
            .table("user_identity_claims")
            .select("id", count="exact")
            .eq("user_id", user_id)
            .is_("dismissed_at", "null")
            .eq("transient", False)
            .limit(1)
            .execute()
        )
        n = res.count or 0
    except Exception:
        logger.debug("community_intro: maturity lookup failed, treating as new")
        return "new"

    if n <= VISITOR_CLAIM_CEILING:
        return "visitor"
    return "engaged" if n >= ENGAGED_CLAIM_FLOOR else "new"


_AUDIENCE = {
    "visitor": (
        "A stranger. Lana knows nothing about them and has no right to assume anything. "
        "Ask about the community's subject, not about them — something they can answer "
        "without disclosing anything."
    ),
    "new": (
        "Someone who has used Lana a little. A light question that connects this "
        "community's subject to their own experience of it."
    ),
    "engaged": (
        "An established user with a real profile. Lana may be direct and specific, and "
        "may assume competence. Ask something only a regular would bother answering."
    ),
}


def first_question(
    *, place_id: str, user_id: str | None, is_anonymous: bool = False
) -> dict[str, Any] | None:
    """The opening question for a community-entry session.

    Returns {question, source, maturity} or None — and None means "use the existing
    generic opening", never an error.
    """
    purpose = community_purpose(place_id)
    if not purpose:
        return None

    maturity = user_maturity(user_id, is_anonymous=is_anonymous)

    try:
        from app.orchestrator.llm import llm_configured, llm_json, router_model

        if not llm_configured():
            return None
        data = llm_json(
            model=router_model(),
            system=INTRO_PROMPT.format(
                purpose=purpose["purpose"], audience=_AUDIENCE[maturity]
            ),
            user_payload=purpose["purpose"],
            max_tokens=160,
            temperature=0.6,
        )
    except Exception:
        logger.exception("community_intro: generation failed for place=%s", place_id)
        return None

    if not isinstance(data, dict):
        return None
    q = str(data.get("question") or "").strip()
    if not q or len(q) > 200:
        return None

    logger.info(
        "community_intro: place=%s maturity=%s source=%s", place_id, maturity, purpose["source"]
    )
    return {
        "question": q,
        "source": purpose["source"],
        "maturity": maturity,
        "place_id": place_id,
    }


def draft_first_action(place_id: str) -> str | None:
    """Propose the operator's purpose so settings is never a blank box.

    `first_action` is filled on 1 of 39 places today, and that one answer is a reply to a
    different question. A blank text field in settings is the most-skipped element in any
    product; correcting a draft is a far smaller ask than composing one.
    """
    try:
        res = (
            service_client()
            .table("places")
            .select("name,blurb")
            .eq("id", place_id)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        if not rows:
            return None
        name = rows[0].get("name") or ""
        blurb = rows[0].get("blurb") or ""
        if not blurb:
            return None

        from app.orchestrator.llm import llm_configured, llm_json, router_model

        if not llm_configured():
            return None
        data = llm_json(
            model=router_model(),
            system=PURPOSE_DRAFT_PROMPT.format(name=name, blurb=blurb),
            user_payload=blurb,
            max_tokens=120,
            temperature=0.5,
        )
        if not isinstance(data, dict):
            return None
        draft = str(data.get("first_action") or "").strip()
        return draft[:280] or None
    except Exception:
        logger.exception("community_intro: purpose draft failed for %s", place_id)
        return None


def widen_ask(*, community_name: str, question: str) -> str:
    """Ask before looking beyond the community. Never widen silently.

    Decision H: this is a conversational step, asked EVERY time, and the answer is not
    remembered. Remembering it would quietly dissolve the boundary, and the boundary is
    what being in a community means.
    """
    try:
        from app.orchestrator.llm import llm_configured, llm_json, router_model

        if llm_configured():
            data = llm_json(
                model=router_model(),
                system=WIDEN_PROMPT.format(community=community_name, question=question),
                user_payload=question,
                max_tokens=100,
                temperature=0.4,
            )
            if isinstance(data, dict):
                ask = str(data.get("ask") or "").strip()
                if ask:
                    return ask[:220]
    except Exception:
        logger.exception("community_intro: widen ask generation failed")

    # Deterministic fallback. Still names the community, still asks.
    return (
        f"Nobody in {community_name} has answered this yet. "
        "Want me to look wider?"
    )


def record_widen(
    *, place_id: str, question: str, session_id: str | None = None, widened: bool = True
) -> str | None:
    """Write the unanswered ask into the radar.

    THE ASK IS THE SIGNAL. A question a community could not answer, with the community
    attached, is the most valuable row in the product — it is demand with a named audience
    and nobody serving it. `inquiry_signals` exists for exactly this and has zero rows.
    """
    try:
        res = service_client().rpc(
            "record_community_widen",
            {
                "p_place_id": place_id,
                "p_free_text": question,
                "p_session_id": session_id,
                "p_widened": widened,
            },
        ).execute()
        return res.data if isinstance(res.data, str) else None
    except Exception:
        logger.exception("community_intro: radar write failed for place=%s", place_id)
        return None
