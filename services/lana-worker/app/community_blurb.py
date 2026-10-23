"""Blurbs: generate the missing ones, refresh the stale ones, embed all of them.

WHY THIS MATTERS MORE THAN IT LOOKS

    `blurb` stopped being decoration the moment two things started depending on it:

      1. DISCOVERY. discover_communities matches a community on its own description when
         it has no members to match on. That is the only arm that works on a community's
         first day, which is every community in the pilot.

      2. LANA'S FIRST QUESTION. The opening for someone arriving from a creator link is
         grounded in first_action or, failing that, the blurb.

    So a missing blurb means invisible AND generic. Three of eleven creator communities
    have no blurb today, and a stale one is worse than none — it grounds a question in a
    community that no longer exists under that name.

Never raises. A failure leaves the row alone for the next pass.
"""

from __future__ import annotations

import logging
from typing import Any

from app.auth import service_client

logger = logging.getLogger(__name__)

BLURB_BATCH = 25

BLURB_PROMPT = """You write one sentence describing a community, so that someone searching \
can tell whether it is for them.

Name: {name}
Type: {place_type}
Based in: {hq_city}
What members say about themselves: {claims}

Rules:
- ONE sentence, under 30 words, third person, present tense.
- Start with the name. "Etiqueta do Reino is a spot focused on social and dining etiquette."
- Say what it is ABOUT and who it is for. Concrete nouns, not "a community for people who
  like community".
- If the member signals are thin or absent, describe it from the name and type alone and
  keep it modest. Never invent activities, member counts, or history.
- No marketing language. No "vibrant", "welcoming", "passionate".

Return ONLY valid JSON: {{"blurb": "..."}}
"""


def _member_signal(place_id: str, limit: int = 12) -> str:
    """A few public self-claims from members, as raw material for the description."""
    try:
        res = (
            service_client()
            .rpc("place_member_public_claims", {"p_place_id": place_id, "p_limit": limit})
            .execute()
        )
        rows = res.data or []
        labels = [str(r.get("label")) for r in rows if r.get("label")]
        return ", ".join(labels[:limit]) if labels else "(nothing yet)"
    except Exception:
        # The RPC may not exist yet — the blurb is still writable from name and type, and
        # a modest description beats no description at all.
        logger.debug("community_blurb: member signal unavailable for %s", place_id)
        return "(nothing yet)"


def generate_blurb(place: dict[str, Any]) -> str | None:
    """One sentence for one community. None on any failure."""
    try:
        from app.orchestrator.llm import llm_configured, llm_json, router_model

        if not llm_configured():
            return None
        data = llm_json(
            model=router_model(),
            system=BLURB_PROMPT.format(
                name=place.get("name") or "",
                place_type=place.get("place_type") or "community",
                hq_city=place.get("hq_city") or place.get("zip") or "unstated",
                claims=_member_signal(str(place["id"])),
            ),
            user_payload=str(place.get("name") or ""),
            max_tokens=140,
            temperature=0.4,
        )
        if not isinstance(data, dict):
            return None
        blurb = str(data.get("blurb") or "").strip()
        return blurb[:600] or None
    except Exception:
        logger.exception("community_blurb: generation failed for %s", place.get("id"))
        return None


def _embed(text: str) -> str | None:
    try:
        from app.vec_util import to_pgvector
        from app.vertex_extract import vertex_embed

        vec = vertex_embed(text)
        return to_pgvector(vec) if vec else None
    except Exception:
        logger.debug("community_blurb: embed failed")
        return None


def refresh_one(place: dict[str, Any]) -> bool:
    """Bring one place's blurb and embedding up to date. True if it was written."""
    place_id = str(place["id"])
    blurb = (place.get("blurb") or "").strip()
    stale = bool(place.get("blurb_stale"))

    if stale or not blurb:
        new_blurb = generate_blurb(place)
        if not new_blurb:
            return False
        blurb = new_blurb

    patch: dict[str, Any] = {"blurb": blurb, "blurb_stale": False}
    emb = _embed(blurb)
    if emb:
        patch["blurb_embedding"] = emb
    else:
        # Writing the blurb without its embedding would leave the row undiscoverable while
        # looking complete. Leave it stale so the next pass retries.
        patch["blurb_stale"] = True

    try:
        service_client().table("places").update(patch).eq("id", place_id).execute()
        logger.info("community_blurb: refreshed %s (embedded=%s)", place_id, bool(emb))
        return True
    except Exception:
        logger.exception("community_blurb: write failed for %s", place_id)
        return False


def sweep(limit: int = BLURB_BATCH) -> dict[str, int]:
    """Every community that is missing a blurb, stale, or unembedded.

    Safe to run on a schedule and safe to run twice. Ordered so the communities that are
    currently invisible get fixed before the ones that are merely out of date.
    """
    done = failed = 0
    try:
        res = (
            service_client()
            .table("places")
            .select("id,name,place_type,zip,hq_city,blurb,blurb_stale,blurb_embedding")
            .neq("governance_state", "suspended")
            .or_("blurb.is.null,blurb_stale.is.true,blurb_embedding.is.null")
            .limit(limit)
            .execute()
        )
        rows = res.data or []
    except Exception:
        logger.exception("community_blurb: sweep query failed")
        return {"done": 0, "failed": 0}

    for row in rows:
        if refresh_one(row):
            done += 1
        else:
            failed += 1

    logger.info("community_blurb: sweep done=%d failed=%d of %d", done, failed, len(rows))
    return {"done": done, "failed": failed}
