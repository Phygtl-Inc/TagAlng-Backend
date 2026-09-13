"""Text payload for meet embeddings (Vertex text-embedding-005, 20261202120000).

Same lesson as app/tip_embed: what goes IN decides what can be found, and more text is
not better text. A meet's topic lives in its title and its purpose tags; the rest of the
row is logistics. "Bring a chair", "Parking on the street", "RSVP by Friday" pull the
vector toward furniture and scheduling and away from what the meet IS.

So this keeps:
  * the title — the host's own one-line answer to "what is this",
  * the purpose tags — the same vocabulary the ask arrives in ("sports", "parents"),
  * the venue name — often the topic in disguise ("Lake Nona YMCA", "Chess Club"),
  * the description, capped — AI-authored for meets and usually on-topic, but it is the
    longest field and a 500-character blurb should not outweigh an eight-word title.

Deliberately NOT included: bring_items, times, attendee caps, cover emoji. None of them
narrow what the meet is about, and every one of them costs the short ask some cosine.

Mirrors app/claim_embed.py and app/tip_embed.py: a pure string builder with no imports, so
scripts/backfill_event_embeddings.py can use it without pulling the worker's app graph in.
"""

from __future__ import annotations

# Long enough for a real blurb, short enough that it cannot bury an eight-word title.
_MAX_DESCRIPTION = 400


def event_embedding_text(
    *,
    title: str | None,
    description: str | None = None,
    venue_name: str | None = None,
    cohort_tags: list[str] | None = None,
) -> str:
    """One line describing what this meet IS, for the vector."""
    parts: list[str] = []

    def add(value: str | None) -> None:
        text = " ".join(str(value or "").split())
        if not text:
            return
        low = text.lower()
        # Substring both ways: title, venue and description repeat each other constantly
        # ("Sunset Yoga at Lake Nona Park" / venue "Lake Nona Park"), and embedding the
        # same words twice only tells the model the meet is emphatic.
        for i, existing in enumerate(parts):
            if low in existing.lower():
                return
            if existing.lower() in low:
                parts[i] = text
                return
        parts.append(text)

    add(title)
    # Ahead of the prose on purpose: these are the words the ask itself arrives in.
    tags = [str(t).strip() for t in (cohort_tags or []) if str(t or "").strip()]
    if tags:
        add(", ".join(tags[:8]))
    add(venue_name)
    add(str(description or "").strip()[:_MAX_DESCRIPTION])

    return " — ".join(parts)[:2000]
