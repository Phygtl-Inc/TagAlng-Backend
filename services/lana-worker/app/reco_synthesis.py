"""The line that reconciles agreement AND disagreement.

    "Six say it freezes well, two say it went watery — both froze it cooked."

The themes (app/reco_cluster.py) say what most neighbours mentioned and how many. What
they cannot say is that some neighbours DISAGREED, or what the dissenters had in common.
That sentence is the most useful thing on the card: it is the difference between a rating
and an explanation, and it is the one line a reader would actually repeat to someone.

It is also the easiest thing here to get wrong, because it makes a COUNTED CLAIM ABOUT
DISAGREEMENT between identifiable neighbours. "Two say it went watery" names two real
people by implication. So this module is built the other way round from a normal compose:

    the model proposes the reading, and the DATA decides whether it may be shown.

WHAT IS CHECKED, in code, before a line is ever returned:

  1. Both sides cite signal_ids that exist in this subject's contributions. An invented id
     kills the line.
  2. The counts in the structured claim match the number of DISTINCT ids cited. The model
     does not get to assert "six" over four rows.
  3. Any number written in the prose matches one of those verified counts. A line whose
     sentence says a different number from its own evidence is a line that reads true and
     is not.
  4. The shared trait is traceable: the phrase must actually appear in the minority's own
     contributions. "Both froze it cooked" is only sayable if both of them said so.
  5. The two sides are disjoint. One neighbour cannot be both the agreement and the
     dissent.

Any failure drops the line entirely rather than repairing it — a half-verified sentence
about what neighbours disagreed on is worse than no sentence, because the reader has no
way to tell which half survived.

NO DISAGREEMENT MEANS NO LINE. If everyone said much the same thing there is nothing to
reconcile, and a synthesis that manufactures tension to have something to say is inventing
the most damaging thing it could. `None` is the common and correct outcome.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger("lana.reco_synthesis")

# Below this there is no "most people" to speak of, and a two-row card can simply show
# both rows. The line earns its place only once a reader cannot hold every voice at once.
MIN_CONTRIBUTIONS = 4

# Both sides must be real groups. A single dissenter is that person — naming them as a
# counted faction ("one says…") invites the reader to work out who, exactly as with
# cohorts.
MIN_SIDE = 2

MAX_LINE = 180

_NUM_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}

_SYSTEM = (
    "Neighbours recommended the same thing. Some of them disagreed about it.\n"
    "Find the real disagreement and say what the dissenters had in common.\n"
    "\n"
    "You get the contributions, each with an id and what that neighbour wrote.\n"
    "Return the MAJORITY view, the MINORITY view that contradicts it, and — only if it is "
    "actually there in their words — what the minority shared that might explain it.\n"
    "\n"
    "Cite the ids on each side. Never cite an id twice across the two sides.\n"
    "The counts you give must equal the number of ids you cite.\n"
    "The shared trait must be a phrase that genuinely appears in the minority's own "
    "words — if they have nothing in common, leave it empty.\n"
    "\n"
    "If nobody contradicts anybody, return nothing. Do NOT manufacture a disagreement to "
    "have something to say: inventing a dispute between real neighbours is the worst "
    "thing you can do here.\n"
    "\n"
    "The line reads like one neighbour telling another. Under 25 words.\n"
    'Reply with JSON only: {"majority": {"label": "...", "n": 0, "ids": []}, '
    '"minority": {"label": "...", "n": 0, "ids": []}, "shared_trait": "...", "line": "..."}'
)


def _norm(text: Any) -> str:
    return " ".join(str(text or "").split()).casefold()


def _side(raw: Any, valid: dict[str, str]) -> dict[str, Any] | None:
    """One side of the disagreement, with its cited rows checked against reality."""
    if not isinstance(raw, dict):
        return None
    ids = sorted({str(i) for i in (raw.get("ids") or []) if str(i) in valid})
    label = " ".join(str(raw.get("label") or "").split())[:60]
    if not label or len(ids) < MIN_SIDE:
        return None
    # The model's own count must equal the rows it cited. A claim of "six" over four
    # citations is the failure this exists to catch.
    try:
        claimed = int(raw.get("n") or 0)
    except (TypeError, ValueError):
        return None
    if claimed != len(ids):
        logger.info("reco_synthesis.count_mismatch claimed=%s cited=%d", claimed, len(ids))
        return None
    return {"label": label, "n": len(ids), "signal_ids": ids}


def _numbers_agree(line: str, counts: set[int]) -> bool:
    """Every number the sentence states must be one of the verified counts.

    Both digits and number words, because "Six say it freezes well" is the shape the line
    actually takes. A sentence whose prose disagrees with its own evidence is precisely the
    thing a reader cannot detect.
    """
    said: set[int] = set()
    for token in re.findall(r"\b\d+\b", line):
        said.add(int(token))
    for word, value in _NUM_WORDS.items():
        if re.search(rf"\b{word}\b", line, re.I):
            said.add(value)
    stray = said - counts
    if stray:
        logger.info("reco_synthesis.number_not_in_evidence %s (have %s)", stray, counts)
    return not stray


def _trait_traceable(trait: str, minority_ids: list[str], texts: dict[str, str]) -> bool:
    """"Both froze it cooked" is sayable only if both of them said so.

    Word-level rather than whole-phrase: the model paraphrases lightly ("froze it cooked"
    for "I froze mine after cooking"), and requiring an exact substring would reject
    honest readings. Requiring every content word to appear in EVERY cited contribution
    keeps it from being a free pass.
    """
    words = [w for w in re.findall(r"[a-z]{4,}", trait.casefold())]
    if not words:
        return False
    return all(
        all(w in _norm(texts.get(sid, "")) for w in words) for sid in minority_ids
    )


def synthesis_for(
    contributions: list[dict[str, Any]], *, lang: str = "en"
) -> dict[str, Any] | None:
    """{line, majority, minority, shared_trait} — or None, which is the usual answer.

    `contributions` is [{signal_id, text}], the same shape reco_cluster uses.
    """
    usable = [c for c in contributions if str(c.get("text") or "").strip()]
    if len(usable) < MIN_CONTRIBUTIONS:
        return None

    texts = {str(c["signal_id"]): str(c["text"]) for c in usable}
    try:
        from app.i18n import lang_display_name
        from app.orchestrator.llm import composer_model, llm_configured, llm_json

        if not llm_configured():
            return None
        system = _SYSTEM
        if lang and lang != "en":
            system += f"\n- Write the line ENTIRELY in {lang_display_name(lang)}."
        data = llm_json(
            model=composer_model(),
            system=system,
            user_payload=json.dumps(
                [{"id": k, "said": v} for k, v in texts.items()], ensure_ascii=False
            ),
            max_tokens=260,
            temperature=0.0,
        )
    except Exception:  # noqa: BLE001
        logger.info("reco_synthesis.compose_failed")
        return None
    if not isinstance(data, dict):
        return None

    majority = _side(data.get("majority"), texts)
    minority = _side(data.get("minority"), texts)
    line = " ".join(str(data.get("line") or "").split())[:MAX_LINE]
    if not majority or not minority or not line:
        return None

    # One neighbour cannot be both the consensus and the dissent.
    if set(majority["signal_ids"]) & set(minority["signal_ids"]):
        logger.info("reco_synthesis.overlapping_sides")
        return None

    if not _numbers_agree(line, {majority["n"], minority["n"]}):
        return None

    trait = " ".join(str(data.get("shared_trait") or "").split())[:80]
    if trait and not _trait_traceable(trait, minority["signal_ids"], texts):
        # The reconciliation is the weakest link, so it is dropped ON ITS OWN — the
        # disagreement can still be true when the explanation for it is not. But the line
        # goes with it, because the line is what stated the explanation.
        logger.info("reco_synthesis.trait_untraceable %r", trait)
        return None

    logger.info(
        "reco_synthesis.ok majority=%d minority=%d trait=%s",
        majority["n"], minority["n"], bool(trait),
    )
    return {
        "line": line,
        "majority": majority,
        "minority": minority,
        "shared_trait": trait or None,
    }


# ── Caching, in the row the themes already live in ────────────────────────────────────
#
# Keyed with reco_cluster.basis_sig so the synthesis and the themes for one subject share
# a single reco_subject_digests row: they are two readings of the SAME contributions, and
# keying them apart would let a card show themes built from five voices beside a synthesis
# built from four.

# Stored when the check ran and found nothing to reconcile. Neighbours AGREEING is the
# common outcome, and without a negative entry every warm re-asks the model the question it
# has already answered — forever, because a null is indistinguishable from "not yet asked".
_ABSENT: dict[str, Any] = {"absent": True}


def _cached(subject_ref: str, lang: str, sig: str) -> dict[str, Any] | None:
    """The stored reading, or None for a miss. `_ABSENT` is a HIT meaning "nothing to say"."""
    try:
        from app.db import service_client

        rows = (
            service_client()
            .table("reco_subject_digests")
            .select("synthesis")
            .eq("subject_ref", subject_ref)
            .eq("lang", lang)
            .eq("basis_sig", sig)
            .limit(1)
            .execute()
            .data
            or []
        )
    except Exception:  # noqa: BLE001 — a miss and a broken cache are the same thing
        return None
    if not rows:
        return None
    value = rows[0].get("synthesis")
    return value if isinstance(value, dict) else None


def _store(subject_ref: str, lang: str, sig: str, payload: dict[str, Any]) -> None:
    """UPDATE, never upsert: the themes own this row.

    An upsert here would create a digest row carrying a synthesis and no themes, and
    reco_cluster._cached reads that as a hit — so the subject would render its synthesis
    and permanently lose its themes. When no digest row exists yet the update no-ops and
    the synthesis is simply recomposed on the next warm, which is bounded and rare.
    """
    try:
        from app.db import service_client

        service_client().table("reco_subject_digests").update({"synthesis": payload}).eq(
            "subject_ref", subject_ref
        ).eq("lang", lang).eq("basis_sig", sig).execute()
    except Exception:  # noqa: BLE001 — failing to cache must not fail the turn
        logger.info("reco_synthesis.store_failed subject=%s", subject_ref)


def synthesis_for_subject(
    subject_ref: str,
    contributions: list[dict[str, Any]],
    *,
    lang: str = "en",
    merge_mode: str = "aggregate",
    allow_compose: bool = True,
) -> dict[str, Any] | None:
    """Cache-first synthesis for one subject.

    THREE outcomes, and the caller needs all three apart:
      a reading   — a dict with `line`, show it;
      `_ABSENT`   — decided, and there was nothing to reconcile. Do NOT warm this again;
      None        — not decided yet (cache miss on a list render, or too few voices).

    Collapsing the last two is what would make a card whose neighbours agree re-ask the
    model on every single render for the rest of its life.
    """
    # A collection is never reconciled across, for the reason it is never clustered: three
    # banana breads that disagree are three recipes, not a dispute.
    if merge_mode == "collection" or not subject_ref:
        return None
    usable = [c for c in contributions if str(c.get("text") or "").strip()]
    if len(usable) < MIN_CONTRIBUTIONS:
        return None

    from app.reco_cluster import basis_sig

    sig = basis_sig(usable)
    hit = _cached(subject_ref, lang, sig)
    if hit is not None:
        return hit
    # A results LIST renders cache-only, as with themes: the detail view pays to compose.
    if not allow_compose:
        return None

    out = synthesis_for(usable, lang=lang)
    _store(subject_ref, lang, sig, out or _ABSENT)
    return out or _ABSENT
