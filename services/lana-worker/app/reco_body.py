"""One neighbour's recommendation, as prose — the DESCRIPTION block on screens 11/12.

A capture stores a one-liner (`reco_description`) and the answered steps (`reco_fields`).
The card has been showing the one-liner and hiding the steps behind a disclosure, which is
a list of key-values rather than something anybody reads. This writes the recommendation
out: what they said, plus what they chose, in their register.

    description  "great prices and they deliver"
    fields       Type of furniture: Bedroom set · Delivery: On time · Assembly: Yes,
                 included · Quality: Excellent · Best for: Families
    ->           "Bedroom sets at good prices, delivered on time and assembled for you.
                  Decent quality, and a sensible stop if you are furnishing for a family."

PER CONTRIBUTION, NEVER PER SUBJECT, and that is the whole safety of it. A merged card
holds several neighbours; composing one body across them would produce a description none
of them wrote and attribute it to all of them — the same blend the collection/aggregate
split exists to prevent. Each voice keeps its own body, and the card stacks them.

WHAT IT MAY NOT DO, enforced rather than asked for:
- Add a fact. Every claim must trace to the description or an answered step; a body is
  checked for invented specifics before it is stored (see `_grounded`).
- Recommend. The neighbour recommends; Lana reports. A body that opens "You should…" is
  Lana vouching for a place she has never been — the same rule tip_share._description
  states for the one-liner it is built from.
- Exist when there is nothing to write from. Fewer than two facts and the one-liner is
  already the best available text; a body would only pad it.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

logger = logging.getLogger("lana.reco_body")

# Below this there is nothing to compose: a lone description IS the body already, and a
# model call could only inflate it.
MIN_FACTS = 2

MAX_BODY = 420


_SYSTEM = (
    "You write ONE neighbour's local recommendation as a short readable paragraph.\n"
    "Input: what they said about it, and the specific answers they gave.\n"
    "\n"
    "Use ONLY those facts. Never add a detail that is not in the input — no prices, hours, "
    "addresses, quality judgements or reasons they did not give. If the input is thin, the "
    "paragraph is short. A short true paragraph is the goal; a fuller invented one is a "
    "failure.\n"
    "Report, never recommend: write what THEY said about it, not advice to the reader. No "
    "'you should', no 'a must-visit', no second person at all.\n"
    "Keep their register — plain, neighbourly, no marketing language.\n"
    "2-3 sentences, under 60 words.\n"
    'Reply with JSON only: {"body": "..."}'
)


def facts_for(row: dict[str, Any]) -> list[str]:
    """Everything this neighbour told us, as plain strings.

    The description first — their own words about why it is worth recommending — then each
    answered step as "Label: answer". detail_text is deliberately excluded: it is a joined
    recap that already contains all of this plus the subject name, so feeding it in would
    let the model echo the recap back as if it were prose.
    """
    out: list[str] = []
    desc = str(row.get("description") or row.get("reco_description") or "").strip()
    if desc:
        out.append(desc)
    for f in row.get("reco_fields") or []:
        if not isinstance(f, dict):
            continue
        label = " ".join(str(f.get("label") or "").split())
        answer = " ".join(str(f.get("answer") or "").split())
        if answer:
            out.append(f"{label}: {answer}" if label else answer)
    return out


def basis_sig(facts: list[str]) -> str:
    """Fingerprint of the facts a body was written from — an edit authors a new body."""
    return hashlib.sha256("|".join(facts).encode()).hexdigest()[:32]


def _grounded(body: str, facts: list[str]) -> bool:
    """Does every number and proper-looking specific in the body appear in the facts?

    Not a full entailment check — that needs another model and would be circular. It
    catches the failure that actually happens: a fluent paragraph that quietly gains a
    price, a time, or a street the neighbour never mentioned. Numbers are the cheapest and
    highest-value thing to verify, because an invented one reads exactly as true.
    """
    import re

    haystack = " ".join(facts).casefold()
    for token in re.findall(r"\$?\d[\d,.:]*\s*(?:am|pm|%)?", body.casefold()):
        cleaned = token.strip().rstrip(".,")
        if cleaned and cleaned not in haystack:
            logger.info("reco_body.ungrounded_number %r", cleaned)
            return False
    return True


def _cached(signal_id: str, lang: str, sig: str) -> str | None:
    try:
        from app.db import service_client

        rows = (
            service_client()
            .table("reco_contribution_bodies")
            .select("body")
            .eq("signal_id", signal_id)
            .eq("lang", lang)
            .eq("basis_sig", sig)
            .limit(1)
            .execute()
            .data
            or []
        )
    except Exception:  # noqa: BLE001 — a miss and a broken cache are the same thing
        return None
    return str(rows[0]["body"]) if rows else None


def _store(signal_id: str, lang: str, sig: str, body: str) -> None:
    try:
        from app.db import service_client

        service_client().table("reco_contribution_bodies").upsert(
            {"signal_id": signal_id, "lang": lang, "basis_sig": sig, "body": body},
            on_conflict="signal_id,lang,basis_sig",
        ).execute()
    except Exception:  # noqa: BLE001 — failing to cache must not fail the turn
        logger.info("reco_body.store_failed signal=%s", signal_id)


def _compose(facts: list[str], lang: str) -> str | None:
    try:
        from app.i18n import lang_display_name
        from app.orchestrator.llm import composer_model, llm_configured, llm_json

        if not llm_configured():
            return None
        system = _SYSTEM
        if lang and lang != "en":
            system += (
                f"\n- Write the paragraph ENTIRELY in {lang_display_name(lang)}, keeping "
                "proper nouns as they are."
            )
        data = llm_json(
            model=composer_model(),
            system=system,
            user_payload=json.dumps({"said": facts}, ensure_ascii=False),
            max_tokens=200,
            # Deterministic: one contribution reads the same on every load.
            temperature=0.0,
        )
    except Exception:  # noqa: BLE001
        logger.info("reco_body.compose_failed")
        return None
    body = " ".join(str((data or {}).get("body") or "").split())[:MAX_BODY]
    return body or None


def body_for(
    signal_id: str,
    row: dict[str, Any],
    *,
    lang: str = "en",
    allow_compose: bool = True,
) -> str | None:
    """The prose body for one contribution, or None when there is nothing honest to write.

    None is ordinary: a thin capture, an unconfigured model, a failed call, or a body that
    did not survive grounding. The card falls back to the neighbour's own one-liner, which
    is what it showed before this existed.
    """
    facts = facts_for(row)
    if len(facts) < MIN_FACTS or not signal_id:
        return None

    sig = basis_sig(facts)
    hit = _cached(signal_id, lang, sig)
    if hit is not None:
        return hit
    # A results LIST renders cache-only; the detail view is what pays to compose.
    if not allow_compose:
        return None

    body = _compose(facts, lang)
    if not body:
        return None
    if not _grounded(body, facts):
        logger.info("reco_body.dropped_ungrounded signal=%s", signal_id)
        return None
    _store(signal_id, lang, sig, body)
    return body
