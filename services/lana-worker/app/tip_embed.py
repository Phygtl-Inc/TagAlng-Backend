"""Text payload for tip_share embeddings (Vertex text-embedding-005).

MEASURED, not guessed (prod rows, 2026-09-10, ask "art supplies" vs Rifle Paper Co.):

    "stationery store"                                    0.711
    "Rifle Paper Co. - stationery store"                  0.569
    "Rifle Paper Co."                                     0.518
    the whole tip, Q&A dump included  <- what we stored   0.506
    "Known for: Looks good . Cost: Free to browse . ..."  0.471

Storing MORE text matched WORSE. The structured answers are most of a modern tip's
characters and almost none of its topic: "Parking is limited" and "Quiet weekdays" pull the
vector toward parking and scheduling, 0.2 away from what the shop sells, and under the floor.

So this keeps what the recommendation is ABOUT and drops the scaffolding:
  * the ask-shaped tags first (app/tip_tags) — the same vocabulary the ask arrives in,
  * the name, category, place and description,
  * the answers WITHOUT their labels ("Men's haircut", not "Service: Men's haircut") —
    an answer is often topical, its label almost never is,
  * capped, because a long tail of logistics dilutes a short ask no matter how it is framed.

Mirrors app/claim_embed.py: a pure string builder with no imports, so the backfill script
can use it without pulling the worker's app graph in.
"""

from __future__ import annotations

import re

# One "Label: answer" segment of a joined detail_text. Bounded label so a prose sentence
# with a colon in it ("the deal is this: go early") keeps its words.
_LABELLED = re.compile(r"^[^:]{1,28}:\s*(.+)$")

# Enough logistics answers to keep a genuinely topical one ("Men's haircut", "Beard trim"),
# few enough that parking and opening hours cannot outweigh a two-word ask.
_MAX_ANSWERS = 4


def _answers(detail_text: str | None) -> list[str]:
    """Free prose and bare answers from a joined detail_text, labels stripped."""
    out: list[str] = []
    for seg in str(detail_text or "").split("·"):
        seg = seg.strip()
        if not seg:
            continue
        m = _LABELLED.match(seg)
        out.append(m.group(1).strip() if m else seg)
    return out


def tip_embedding_text(
    *,
    detail_text: str | None,
    category: str | None = None,
    reco_name: str | None = None,
    reco_place: str | None = None,
    reco_description: str | None = None,
    affinity_tags: list[str] | None = None,
) -> str:
    """One line describing what this recommendation IS, for the vector."""
    parts: list[str] = []

    def add(value: str | None) -> None:
        text = " ".join(str(value or "").split())
        if not text:
            return
        low = text.lower()
        # Substring both ways: the card fields and detail_text repeat each other, and the
        # old check only caught the case where the NEW part was the shorter one — so every
        # modern tip embedded its own name and category twice.
        for i, existing in enumerate(parts):
            if low in existing.lower():
                return
            if existing.lower() in low:
                parts[i] = text
                return
        parts.append(text)

    add(reco_name)
    add(category)
    # Ahead of the prose on purpose: these are the words the ask itself arrives in.
    tags = [str(t).strip() for t in (affinity_tags or []) if str(t or "").strip()]
    if tags:
        add(", ".join(tags[:8]))
    add(reco_place)
    add(reco_description)

    answers = _answers(detail_text)
    for a in answers[:_MAX_ANSWERS]:
        add(a)
    return " — ".join(parts)[:2000]


def tip_headline(detail_text: str | None, *, max_parts: int = 2) -> str:
    """What a tip LEADS with — the name, and the category if it is there.

    detail_text is "Name · category · description · Label: answer · Label: answer …".
    Everything from the first labelled segment on is card material: the recommendation
    card renders those pairs itself, right under Lana's reply. Handing them to the reply
    composer as well only bought a paraphrase of the card sitting directly below it — and
    a bad one, which read "parking is limited just a minute away" after merging
    "Good to know: Parking is limited" with a "1 min walk" distance.
    """
    head: list[str] = []
    for seg in str(detail_text or "").split("·"):
        seg = seg.strip()
        if not seg or _LABELLED.match(seg):
            break
        head.append(seg)
        if len(head) >= max_parts:
            break
    return " · ".join(head)
