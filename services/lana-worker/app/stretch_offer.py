"""Stretch offer (product name: Rapport Reply).

When a topical event search matches nothing nearby, but the matcher rated a nearby event
"closely related", Lana offers that ONE event and says honestly how it differs.

This module is deliberately source-agnostic. It takes rows that already carry the
matcher's per-event judgement (`topic_score`, `topic_mismatch`) and knows nothing about
how they were fetched — radius read, community calendar, or a future semantic search.
The retrieval underneath can be redesigned without touching this.

The honesty rule lives here, structurally: the only reason Lana may give is the
`topic_mismatch` phrase the matcher wrote IN THE SAME CALL that rejected the event. She
reports the judgement; she does not explain it after the fact.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# The writer may quote the description, but only a card's worth of it.
_MAX_DESCRIPTION = 200


@dataclass(frozen=True)
class StretchCandidate:
    """One event offered as a stretch: the row as fetched, its closeness score, and the
    matcher's own difference phrase."""

    event: dict[str, Any]
    score: float
    mismatch: str

    @property
    def title(self) -> str:
        return " ".join(str(self.event.get("title") or "").split())

    @property
    def event_id(self) -> str:
        return str(self.event.get("id") or "")


def pick_stretch(
    rows: list[dict[str, Any]] | None, band: tuple[float, float]
) -> StretchCandidate | None:
    """The best stretch among `rows`, or None.

    A row qualifies only when the matcher actually judged it (not `topic_unchecked`),
    its score sits inside `band` (inclusive), it carries a non-empty difference phrase,
    and it has an id and a title to put on a card. Highest score wins; ties keep input
    order. Callers pass only rows the matcher did NOT match — this never second-guesses
    membership, it only reads what was already rated.
    """
    lo, hi = band
    best: StretchCandidate | None = None
    for row in rows or []:
        if not isinstance(row, dict) or row.get("topic_unchecked"):
            continue
        raw = row.get("topic_score")
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            continue
        # Scores arrive rounded to one decimal; re-round so 0.6000000001 cannot slip a
        # band edge.
        score = round(float(raw), 1)
        if not lo <= score <= hi:
            continue
        mismatch = " ".join(str(row.get("topic_mismatch") or "").split())
        if not mismatch:
            continue
        if not str(row.get("id") or "").strip() or not str(row.get("title") or "").strip():
            continue
        if best is None or score > best.score:
            best = StretchCandidate(event=row, score=score, mismatch=mismatch)
    return best


def stretch_facts(candidate: StretchCandidate) -> list[str]:
    """Facts for the empty-state writer, built ONLY from the event row and the
    matcher's phrase. Nothing here is inferred: every sentence is a field of the row,
    or an instruction about what may not be added."""
    from app.activity_browse import _event_when_parts

    ev = candidate.event
    title = candidate.title
    when = _event_when_parts(ev.get("starts_at"), has_time=ev.get("has_time") is not False)
    facts = [
        f'The closest event near them is "{title}"'
        + (f" on {when}" if when else "")
        + f'. Name it by its exact title, "{title}", unchanged.',
    ]
    desc = " ".join(str(ev.get("description") or "").split())[:_MAX_DESCRIPTION]
    if desc:
        facts.append(f'Its own description: "{desc}".')
    tags = [str(t).strip() for t in (ev.get("cohort_tags") or []) if str(t or "").strip()]
    if tags:
        facts.append(f"Its tags: {', '.join(tags)}.")
    facts.append(
        "It is CLOSELY RELATED to what they asked for, not the same thing — call it the "
        "closest thing, never a match."
    )
    facts.append(
        f'How it differs from what they asked for, as judged when it was rated: '
        f'"{candidate.mismatch}". That difference is the ONLY reason you may give. Add '
        "nothing about who will be there, what happens there, or anything not written "
        "in this event's title, description or tags."
    )
    return facts
