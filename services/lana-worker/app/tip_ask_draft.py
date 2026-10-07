"""The seek-side ask draft — what Lana understood, before anything is broadcast (§12d).

TipDraft (app/tip_share.py) is the SHARE shape: name / category / trait / locality of a
provider the user is vouching for. A recommendation SEEK has no provider yet, so there was
no draft to render and the frame's confirmation step (C-4-look-tip-P2) had nothing on the
wire: the worker went straight from her words to the verify gate or to the answer.

This builds the missing seek-side draft — a headline, the qualifier line under it, and the
entity chips — so the frontend can show her what was heard and offer "Looks good" /
"Let me tweak that".

AI-first, like every other reader in this lane ([[no-new-regex-use-ai-signals]] and
[[ai-authored-copy-not-canned]]): the headline is a *rewrite* of the user's ask into a
title, which no phrase list can do across languages. The deterministic fallback is not a
matcher — it echoes the extracted detail verbatim, which is always truthful even when it
reads plainly.

NOTHING HERE IS A PROMISE. The draft is a receipt of understanding, not a posting: it is
stamped on the answer turn (and on the verify/ZIP gate turns, so a gated ask at least
arrives with the user's actual words attached). The write still happens only on an explicit
yes to the ask-neighbors offer.
"""

from __future__ import annotations

import json
import logging
from typing import Any

_log = logging.getLogger(__name__)

_SYSTEM = (
    "You turn ONE neighbor's recommendation ask into a compact draft card, in a "
    "neighborhood app. Output only valid JSON: "
    '{"title":"…","detail":"…","category":"…","locality":"…","qualifiers":["…"],'
    '"recommended_by":"…","kind_named":true,"kind_options":[{"label":"…","ask":"…"}]}. '
    "title = the ask as a short noun phrase a person would put on a card, 2-5 words "
    "('Gentle pediatric dentist', 'Weekend dog walker'). NEVER a sentence, never a "
    "question, never 'Looking for…'. "
    "detail = one line of the qualifiers in their own words, joined with ' · ' "
    "('Gentle with toddlers · Lake Nona · open to any insurance'). Empty string if they "
    "gave no qualifiers — do NOT invent any. "
    "category = the kind of provider or place, lowercase ('pediatric dentist', 'plumber', "
    "'vegetarian restaurant'). "
    "locality = the neighborhood/area THEY named, or empty string. Never guess a location "
    "they did not say. "
    "qualifiers = each distinct requirement as its own 1-3 word chip label, at most 4. "
    "Use ONLY what is in their words. "
    "recommended_by = a requirement on the PERSON RECOMMENDING, never on the thing, as one "
    "short chip label in their words ('recommended by someone from Turkey', 'from a Spanish "
    "speaker', 'from a marathon runner'); empty string if they set none. It is NOT also a "
    "qualifier. 'Turkish restaurant' is about the restaurant — the category, not this. "
    "kind_named = false ONLY when they never said what KIND of thing, place or person they "
    "want a recommendation for — 'any recommendations?', 'what do people recommend around "
    "here', or recommendations asked only by WHERE (any school, church, gym, venue, "
    "neighbourhood or town, by full name, initials or nickname). A place or area alone is "
    "not a kind. Anything that names a kind is true, however broad or casual: 'good coffee "
    "near <a place>', 'somewhere to eat', 'a dentist', 'something fun to do', 'gaming "
    "laptop'. "
    "kind_options = when kind_named is false, 3-5 kinds of recommendation that make sense "
    "for where and how they asked (near a campus: food, coffee, study spots; near a "
    "hospital: food, parking, a place to stay…), each as label = 1-2 words for a button and "
    "ask = the whole ask rewritten with that kind, in their words and with the place they "
    "named ('good coffee near <that place>'). When kind_named is true, []. "
    "Reproduce their language — if they wrote in Spanish, "
    "every field is in Spanish. Anything you are unsure of is an empty string, never a guess."
)

_TONE_BY_FIELD = {
    "category": "sky",
    "locality": "green",
    "qualifier": "violet",
    "recommended_by": "violet",
}


def _chip(label: str, field: str) -> dict[str, str]:
    return {
        "label": str(label).strip()[:40],
        "tone": _TONE_BY_FIELD.get(field, "coral"),
        "field": field,
    }


def _fallback_draft(*, detail: str, category: str | None) -> dict[str, Any]:
    """Their own words, unrewritten. Truthful without an LLM — never empty when there is
    a detail, because a card that renders nothing is worse than a plain one."""
    text = str(detail or "").strip()
    if not text:
        return {}
    title = text if len(text) <= 48 else text[:45].rstrip() + "…"
    chips = [_chip(category, "category")] if category else []
    return {
        "title": title[:1].upper() + title[1:],
        "detail": "",
        "category": str(category or "").strip() or None,
        "locality": None,
        "chips": chips,
        "ready": True,
    }


def build_ask_draft(
    *,
    msg: str,
    detail: str,
    category: str | None = None,
    locality: str | None = None,
) -> dict[str, Any]:
    """The ask_draft payload for a looking.tip turn. {} when there is nothing to show."""
    if not str(detail or "").strip():
        return {}

    from app.orchestrator.llm import llm_configured, llm_json, router_model

    if not llm_configured():
        return _fallback_draft(detail=detail, category=category)

    payload = json.dumps(
        {
            "user_message": str(msg or "")[:600],
            "what_they_asked_for": str(detail or "")[:300],
            "category_guess": str(category or ""),
            "area_they_live_in": str(locality or ""),
        },
        ensure_ascii=False,
    )
    try:
        raw = llm_json(
            model=router_model(),
            system=_SYSTEM,
            user_payload=payload,
            max_tokens=220,
            temperature=0.1,
        )
    except Exception as exc:  # noqa: BLE001 — a receipt must never break the answer
        _log.warning("ask_draft_compose_failed: %s", exc)
        return _fallback_draft(detail=detail, category=category)
    if not isinstance(raw, dict):
        return _fallback_draft(detail=detail, category=category)

    def _s(key: str) -> str:
        val = raw.get(key)
        return str(val or "").strip() if isinstance(val, (str, int, float)) else ""

    title = _s("title")[:60] or _fallback_draft(detail=detail, category=category).get("title") or ""
    if not title:
        return {}
    draft_category = _s("category")[:60] or (str(category or "").strip() or None)
    draft_locality = _s("locality")[:60] or None

    chips: list[dict[str, str]] = []
    if draft_category:
        chips.append(_chip(draft_category, "category"))
    if draft_locality:
        chips.append(_chip(draft_locality, "locality"))
    quals = raw.get("qualifiers")
    if isinstance(quals, list):
        for q in quals[:4]:
            label = str(q or "").strip()
            if label and label.lower() not in {c["label"].lower() for c in chips}:
                chips.append(_chip(label, "qualifier"))
    # The requirement on WHO recommends is its own field, so a results card can tell it
    # from the ask's facets about the thing: it is shown there only when a recommender
    # meets it (reco_authority.chips_for_card). Last, so it never displaces a facet.
    # A model that ALSO listed it as a qualifier gets it moved, not duplicated.
    by = _s("recommended_by")[:40]
    if by:
        chips = [c for c in chips if c["label"].lower() != by.lower()][:5]
        chips.append(_chip(by, "recommended_by"))

    # Nothing to search for yet: "do you have recommendations at SJSU" named a place and no
    # kind, so a Places search ran on the bare place and answered with the campus itself
    # (prod 2026-10-06). The model reads that, and offers the kinds as chips. Only an
    # explicit False counts — a missing field, a failed call or the fallback draft all keep
    # today's straight-through answer.
    kind_options: list[dict[str, str]] = []
    if raw.get("kind_named") is False:
        for opt in raw.get("kind_options") or []:
            if not isinstance(opt, dict):
                continue
            label = str(opt.get("label") or "").strip()[:24]
            ask = str(opt.get("ask") or "").strip()[:120]
            if label and ask:
                kind_options.append({"label": label, "ask": ask})
            if len(kind_options) == 5:
                break

    return {
        "title": title,
        "detail": _s("detail")[:200],
        "category": draft_category,
        "locality": draft_locality,
        "chips": chips[:6],
        # The ask is answerable as it stands — the cascade already ran on it. False would
        # mean Lana still needs something before she can look, which this lane handles by
        # asking (ZIP, verification) rather than by shipping a half-draft.
        "ready": True,
        # Present only when the ask named no kind AND the model offered some — the answer
        # turn asks "what kind?" with these instead of searching (discovery_route).
        **({"kind_options": kind_options} if kind_options else {}),
    }


_MERGE_SYSTEM = (
    "A neighbor asked for a recommendation, then corrected the ask. Rewrite it as ONE "
    'searchable ask in their own words. Output only valid JSON: {"ask":"…"}. '
    "The correction WINS wherever the two disagree — if they now want an orthodontist, the "
    "ask is about an orthodontist, not a dentist. Keep every qualifier from the original "
    "that the correction did not overrule. Add nothing they did not say. Stay in their "
    "language. Keep it under 15 words. When part_being_corrected is given, the "
    "correction replaces exactly that part of the ask (it was what they tapped to change) "
    "and everything else stays."
)


def merge_ask_correction(
    *, prior_detail: str, correction: str, field: str | None = None, was: str | None = None
) -> str:
    """The user's tweak folded into the ask. Falls back to joining the two, which reads
    clumsily but never silently drops what they said.

    `field` / `was` come from a chip tap (`fix:<field>`, §30e): which part of the ask she
    is correcting and what it said, so the model replaces THAT part rather than guessing."""
    prior = str(prior_detail or "").strip()
    fix = str(correction or "").strip()
    if not fix:
        return prior
    if not prior:
        return fix[:500]
    joined = f"{prior} — {fix}"[:500]

    from app.orchestrator.llm import llm_configured, llm_json, router_model

    if not llm_configured():
        return joined
    try:
        raw = llm_json(
            model=router_model(),
            system=_MERGE_SYSTEM,
            user_payload=json.dumps(
                {
                    "original_ask": prior[:300],
                    "their_correction": fix[:300],
                    **(
                        {"part_being_corrected": f"{field} (was: {was})" if was else field}
                        if field
                        else {}
                    ),
                },
                ensure_ascii=False,
            ),
            max_tokens=96,
            temperature=0.0,
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning("ask_merge_failed: %s", exc)
        return joined
    merged = str((raw or {}).get("ask") or "").strip() if isinstance(raw, dict) else ""
    return merged[:500] or joined


def stamp_ask_draft(
    ctx: dict[str, Any],
    *,
    msg: str,
    detail: str,
    category: str | None = None,
    locality: str | None = None,
) -> dict[str, Any]:
    """Put the draft on ctx (and arm its one-turn confirm) — returns what was stamped."""
    draft = build_ask_draft(msg=msg, detail=detail, category=category, locality=locality)
    if not draft:
        return {}
    ctx["ask_draft"] = draft
    # Read by the NEXT turn to recognize "Looks good" / "Let me tweak that". None, not pop
    # — a popped key comes back on the session merge ([[ctx-pop-resurrection]]).
    ctx["ask_draft_pending"] = {
        "title": draft.get("title"),
        "detail": str(detail or "")[:300],
        "category": category,
        # The chips as rendered, so a `fix:<field>` tap can name the one being corrected.
        "chips": [
            {"label": c.get("label"), "field": c.get("field")}
            for c in (draft.get("chips") or [])
            if isinstance(c, dict)
        ],
    }
    return draft
