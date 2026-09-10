"""What a shared tip is GOOD FOR, in the words someone would ask for it (§tip tags).

The matcher had one intelligent side. A tip_seek ask goes through the classifier, which
turns "I want to trim my beard" into `barber` — a clean noun in a canonical vocabulary. The
TIP it has to match was never normalized: it is whatever the neighbour typed, plus a Q&A
dump ("Cost: Free to browse", "Parking is limited"). One side spoke vocabulary, the other
prose, and the embedding was the only bridge — measured at 0.506 for "art supplies" vs a
stationery store, under the floor, while the phrase "stationery store" alone scored 0.711.

So this normalizes the other side, once, when the tip is posted:

    Rifle Paper Co. (stationery store)  ->  stationery, art supplies, notebooks, gifts
    Jacas Barber (barber)               ->  barber, haircut, beard trim, shave

Now both sides are the same kind of text. `_tip_match_strength` already has the branch
(20261126120000 shipped it inert, because `local_signals.affinity_tags` has a p_affinity_tags
parameter that no caller has ever passed), and tip_embedding_text feeds the tags to the
vector instead of the parking notes.

WRITE TIME, deliberately. A tip is posted once and searched forever, so one model call here
is amortized over every future ask — where a call per ask is not. It is also the only place
that helps the write-time matcher inside save_local_signal, which runs in SQL where no model
exists and can only ever read columns.

Tags are ASK-SHAPED, not descriptive: the thing a neighbour would type into Lana, never a
quality of the place. "beard trim" earns its place; "friendly staff" does not — nobody
searches for it, and a tag that matches nothing costs a scoring branch for nothing.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Tags are ORed into the score, so a long tail of weak ones is a long tail of ways to be
# wrongly matched. Few and specific beats many and vague.
_MAX_TAGS = 8
_MAX_TAG_LEN = 24

_SYSTEM = """You label a neighbourhood recommendation with the words people would SEARCH \
to find it.

You are given one recommendation a neighbour shared: its name, category, description and \
whatever details they answered. Return the short phrases someone would type into a local \
concierge when they want this kind of place.

Output ONLY JSON: {"tags": ["...", "..."]}

RULES
- 3 to 8 tags. Fewer good ones beats more vague ones.
- Each is 1-3 words, lowercase, under 24 characters, no punctuation.
- ASK-SHAPED: what a person WANTS, not what the place IS LIKE. For a barber: "barber", \
"haircut", "beard trim", "shave" — never "friendly", "clean", "good value", "quick".
- Include the obvious category word AND the specific things it is sought for. A stationery \
shop that sells art materials earns both "stationery" and "art supplies".
- Only what the recommendation actually supports. If nothing says they cut beards, do not \
write "beard trim" — a wrong tag confidently surfaces the wrong place.
- No brand or place names (the name is already stored and searched separately).
- No logistics: not "parking", "wait time", "open late", "cash only", "online".
- English, singular where natural ("notebook" not "notebooks").
- [] if the recommendation is too thin to label honestly."""


def _clean(raw: Any) -> list[str]:
    out: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        tag = " ".join(str(item or "").strip().lower().split())
        tag = tag.strip(" .,;:-")
        if len(tag) < 2 or len(tag) > _MAX_TAG_LEN or tag in out:
            continue
        out.append(tag)
        if len(out) >= _MAX_TAGS:
            break
    return out


def tags_for_tip(
    *,
    name: str | None,
    category: str | None,
    description: str | None = None,
    details: list[str] | None = None,
) -> list[str]:
    """Ask-shaped tags for one recommendation. [] on anything less than a clean answer.

    Best-effort by contract: a tip that posts without tags is findable by words and by
    meaning exactly as it was before, and scripts/backfill_tip_embeddings --tags can fill
    it in later. Never raises — a model outage must not cost somebody their post.
    """
    payload: dict[str, Any] = {
        k: v
        for k, v in (
            ("name", str(name or "").strip()),
            ("category", str(category or "").strip()),
            ("description", str(description or "").strip()),
        )
        if v
    }
    if details:
        payload["details"] = [str(d).strip() for d in details if str(d or "").strip()][:8]
    if not payload:
        return []
    try:
        from app.orchestrator.llm import llm_configured, llm_json, router_model

        if not llm_configured():
            return []
        data = llm_json(
            model=router_model(),
            system=_SYSTEM,
            user_payload=json.dumps(payload, ensure_ascii=False),
            max_tokens=160,
            temperature=0.0,
        )
    except Exception:  # noqa: BLE001
        logger.warning("tip_tags_failed name=%r", str(name or "")[:40], exc_info=True)
        return []
    tags = _clean(data.get("tags")) if isinstance(data, dict) else []
    logger.info("tip_tags name=%r -> %s", str(name or "")[:40], tags)
    return tags
