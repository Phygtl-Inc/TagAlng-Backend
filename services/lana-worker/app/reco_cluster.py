"""The Pareto read of one subject: what most neighbours said, and who said something else.

Standup 2026-09-22. Merging recommendations about one dentist solved *which* rows belong
together and created a new problem — what the card then SAYS. Summarising five people into
one sentence produces "Dr. Sara is a great doctor and her parking space is very big", which
is two unrelated observations welded into a claim nobody made.

Tommaso's answer was the Pareto shape, by way of App Store reviews: most contributions say
roughly the same thing, and a minority say something distinctive.

    aggregate the ~80% that overlap   ->  the themes a reader can trust, with counts
    keep the ~20% that do not         ->  their own voice, at their own (small) count

So this does not produce a summary. It produces THEMES, each carrying how many
contributors expressed it and one real quote. "Gentle with anxious kids 8/10" is a theme
with n=8 of total=10 — the number is the evidence, not decoration. An outlier is simply a
theme with n=1, which is why nothing has to be thrown away to avoid the parking-lot
sentence: the card renders big themes big and small themes small.

RULES OF THE HOUSE, inherited from app/peer_rec_line.py:
- Grounded only. A theme label may never assert something no contributor wrote, and each
  theme's quote is a contributor's OWN words, verbatim, never a paraphrase.
- AI-authored with no canned fallback: a failed call yields no digest, and the card falls
  back to listing contributions, which is exactly what a collection does anyway.
- Cached per (subject, language, contribution fingerprint) so a reload costs no model call
  and a NEW contribution authors a NEW digest rather than serving one that cannot know
  about it.
- COLLECTIONS ARE NEVER CLUSTERED. Two banana breads are two recipes; "themes across
  recipes" is the blend this whole design exists to avoid.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

logger = logging.getLogger("lana.reco_cluster")

# Below this there is nothing to cluster: one contribution IS its own theme, and a model
# call to discover that is a cost with no answer attached.
MIN_FOR_DIGEST = 2

# A card shows a handful of themes; past that the tail is noise a reader scrolls by.
MAX_THEMES = 6
MAX_LABEL = 48
MAX_QUOTE = 160

_SYSTEM = (
    "You group neighbours' recommendations about ONE subject into themes.\n"
    "Input is a JSON list of contributions, each with an id and what that neighbour wrote.\n"
    "Return the THEMES they express, most-shared first.\n"
    "\n"
    "A theme is one quality several people noticed — 'gentle with anxious kids', 'easy "
    "parking', 'quick to answer'. Merge contributions that mean the same thing even in "
    "different words. Do NOT merge different qualities: 'great with kids' and 'big car "
    "park' are two themes, never one sentence.\n"
    "A quality only one person mentioned is still a theme, with n=1. Never drop it and "
    "never fold it into a bigger one to tidy up.\n"
    "\n"
    "Every theme carries the ids it came from, and a quote copied VERBATIM from one of "
    "those contributions — never your own words, never a blend of two.\n"
    "Labels are 2-5 words, lowercase, no trailing punctuation.\n"
    'Reply with JSON only: {"themes": [{"label": "...", "ids": ["..."], "quote": "..."}]}'
)


def basis_sig(contributions: list[dict[str, Any]]) -> str:
    """Fingerprint of what a digest was built from.

    Over the contribution ids AND their text: an author editing their own recommendation
    changes what the themes should say just as much as a new author arriving.
    """
    parts = sorted(
        f"{c.get('signal_id')}:{str(c.get('text') or '')[:400]}" for c in contributions
    )
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]


def contribution_text(row: dict[str, Any]) -> str:
    """What this neighbour actually said about the subject.

    Their description first — it is their own words and the only field guaranteed to be
    opinion rather than fact — then the answered steps, which carry the facets a theme is
    usually made of ("Takes insurance", "Saturday mornings"). detail_text is deliberately
    NOT used: it is a joined recap that leads with the subject's name, so every
    contribution would look alike to the model on the one token they all share.
    """
    parts = [str(row.get("reco_description") or "").strip()]
    for f in row.get("reco_fields") or []:
        if not isinstance(f, dict):
            continue
        label, answer = str(f.get("label") or "").strip(), str(f.get("answer") or "").strip()
        if answer:
            parts.append(f"{label}: {answer}" if label else answer)
    return " · ".join(p for p in parts if p)[:600]


def _norm(text: Any) -> str:
    """Whitespace- and case-insensitive form, for checking a quote against its source."""
    return " ".join(str(text or "").split()).casefold()


def _verbatim(quote: str, sources: list[str]) -> str | None:
    """The quote, but only if a contributor actually wrote it.

    The prompt says VERBATIM; nothing about a prompt makes that true. A quote shown under
    a neighbour's name is the strongest claim this card makes, and a model that smooths two
    people's words into one sentence would be putting words in a real person's mouth —
    so it is checked against the source text and dropped when it is not there.

    Substring rather than equality: a model quoting one clause out of a longer answer is
    doing the right thing, and requiring the whole field back would reject it.
    """
    needle = _norm(quote)
    if len(needle) < 8:
        return None
    return quote if any(needle in _norm(src) for src in sources) else None


def _clean_themes(
    raw: Any, by_id: dict[str, str], total: int
) -> list[dict[str, Any]]:
    """Keep only themes grounded in real contributions.

    An id the model invented is dropped, and a theme left with no ids goes with it — a
    count is the whole claim a theme makes, so one built on nothing may not be shown. A
    quote that no contributor wrote is dropped while the theme survives: the count can
    still be true when the quote is not.
    """
    out: list[dict[str, Any]] = []
    seen_labels: set[str] = set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        label = " ".join(str(item.get("label") or "").split())[:MAX_LABEL].strip(" .")
        ids = sorted({str(i) for i in (item.get("ids") or []) if str(i) in by_id})
        if not label or not ids or label.casefold() in seen_labels:
            continue
        seen_labels.add(label.casefold())
        quote = " ".join(str(item.get("quote") or "").split())[:MAX_QUOTE]
        out.append({
            "label": label,
            "n": len(ids),
            "total": total,
            "signal_ids": ids,
            # Only against the contributions THIS theme claims to come from.
            "quote": _verbatim(quote, [by_id[i] for i in ids]) if quote else None,
        })
    # Deterministic to the last key: two reads of one card must not reshuffle it, and
    # label is the only tiebreak that does not depend on dict ordering.
    out.sort(key=lambda t: (-t["n"], t["label"].casefold()))
    return out[:MAX_THEMES]


def _cached(subject_ref: str, lang: str, sig: str) -> dict[str, Any] | None:
    try:
        from app.db import service_client

        rows = (
            service_client()
            .table("reco_subject_digests")
            .select("themes, total")
            .eq("subject_ref", subject_ref)
            .eq("lang", lang)
            .eq("basis_sig", sig)
            .limit(1)
            .execute()
            .data
            or []
        )
    except Exception:  # noqa: BLE001 — a cache miss and a broken cache are the same thing
        return None
    return rows[0] if rows else None


def _store(subject_ref: str, lang: str, sig: str, themes: list[dict[str, Any]], total: int) -> None:
    try:
        from app.db import service_client

        service_client().table("reco_subject_digests").upsert(
            {
                "subject_ref": subject_ref,
                "lang": lang,
                "basis_sig": sig,
                "themes": themes,
                "total": total,
            },
            on_conflict="subject_ref,lang,basis_sig",
        ).execute()
    except Exception:  # noqa: BLE001 — failing to cache must not fail the turn
        logger.info("reco_cluster.store_failed subject=%s", subject_ref)


def _compose(contributions: list[dict[str, Any]], lang: str) -> list[dict[str, Any]] | None:
    """One model call for one subject. None on any failure — no canned fallback."""
    try:
        from app.i18n import lang_display_name
        from app.orchestrator.llm import composer_model, llm_configured, llm_json

        if not llm_configured():
            return None
        system = _SYSTEM
        if lang and lang != "en":
            system += (
                f"\n- Write every label ENTIRELY in {lang_display_name(lang)}. Quotes stay "
                "in the words the neighbour used, untranslated."
            )
        data = llm_json(
            model=composer_model(),
            system=system,
            user_payload=json.dumps(
                [{"id": c["signal_id"], "said": c["text"]} for c in contributions],
                ensure_ascii=False,
            ),
            max_tokens=120 * min(len(contributions), MAX_THEMES) + 120,
            # Deterministic: the same contributions must not regroup between two reads of
            # the same card.
            temperature=0.0,
        )
    except Exception:  # noqa: BLE001
        logger.exception("reco_cluster.compose_failed")
        return None
    return (data or {}).get("themes") if isinstance(data, dict) else None


def digest_for_subject(
    subject_ref: str,
    contributions: list[dict[str, Any]],
    *,
    lang: str = "en",
    merge_mode: str = "aggregate",
    allow_compose: bool = True,
) -> dict[str, Any] | None:
    """{themes, total} for one subject, or None when there is nothing honest to say.

    None is an ordinary outcome, not a failure: a collection is never clustered, a single
    contribution is its own theme already, and a failed model call leaves the card to list
    contributions — which is what it does for collections regardless.
    """
    # Never cluster a collection: "themes across recipes" is the blend this design exists
    # to avoid, and no amount of prompting makes two banana breads one.
    if merge_mode == "collection":
        return None
    usable = [c for c in contributions if str(c.get("text") or "").strip()]
    if len(usable) < MIN_FOR_DIGEST or not subject_ref:
        return None

    sig = basis_sig(usable)
    hit = _cached(subject_ref, lang, sig)
    if hit is not None:
        return {"themes": hit.get("themes") or [], "total": int(hit.get("total") or 0)}

    # A results LIST must not cost one model call per card. The list asks cache-only and
    # renders without themes when there is no hit; the detail view, which is one subject
    # the reader deliberately opened, is what pays to compose. Without this a five-result
    # turn is five sequential LLM calls in the user's latency budget, every time.
    if not allow_compose:
        return None

    raw = _compose(usable, lang)
    if raw is None:
        return None
    by_id = {str(c["signal_id"]): str(c.get("text") or "") for c in usable}
    themes = _clean_themes(raw, by_id, len(usable))
    if not themes:
        logger.info("reco_cluster.no_grounded_themes subject=%s n=%d", subject_ref, len(usable))
        return None
    logger.info(
        "reco_cluster.composed subject=%s n=%d themes=%d quoted=%d",
        subject_ref, len(usable), len(themes), sum(1 for t in themes if t["quote"]),
    )
    _store(subject_ref, lang, sig, themes, len(usable))
    return {"themes": themes, "total": len(usable)}
