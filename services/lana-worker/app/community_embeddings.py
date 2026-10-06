"""Community embeddings — what lets "a club about AI ethics" find the Responsible Computing Club.

`places.blurb_embedding` is the vector discover_communities_anywhere's meaning arm reads
(20270119120000). Until now nothing wrote it: 36 blurbs on prod, 0 embeddings, so a
community could only be found by a word in its name.

Blurbs are written from many places — the profile's background author
(community_surface._author_blurb), the settings RPC, lana-help's creator activation, direct
SQL — so this does not try to hook every writer. The database drops a vector whenever the
name or blurb changes (trigger places_drop_stale_blurb_embedding), and this module fills
anything NULL:

  · `embed_place(place_id)`   — one community, now. Called right after a write we own.
  · `embed_missing(limit)`    — a bounded pass over every NULL. The backfill script and the
                                 kick below both use it.
  · `kick()`                  — fire-and-forget `embed_missing`, at most one in flight and
                                 one per _KICK_INTERVAL_S. Community searches call it, so
                                 a community written by any path is searchable by meaning
                                 from the next search on, with no scheduler to forget.

Every failure is logged and swallowed: an embedding is an upgrade to search, never a
reason for a write or a search to fail.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from app.auth import service_client

logger = logging.getLogger(__name__)

# One pass embeds at most this many; a kick that finds more leaves the rest for the next.
_BATCH = 25
_KICK_INTERVAL_S = 300.0

_kick_lock = threading.Lock()
_kick_running = False
_last_kick = 0.0


def embedding_text(name: Any, blurb: Any) -> str:
    """What a community is embedded as: its name, then what it says about itself.

    The name is in the text because it is often the strongest topic word a community has
    ("Responsible Computing Club"); the blurb alone loses it. "" when there is no blurb —
    a name alone is already matched by the name arm, and a vector of a bare name matches
    everything near the word at a confidence it has not earned.
    """
    n = str(name or "").strip()
    b = str(blurb or "").strip()
    if not b:
        return ""
    return f"{n}. {b}" if n else b


def _embed(text: str) -> str | None:
    from app.vec_util import to_pgvector
    from app.vertex_extract import vertex_embed

    return to_pgvector(vertex_embed(text[:2000]))


def _write(row: dict[str, Any]) -> bool:
    """Embed one row and store it — only if name and blurb are still what we embedded.

    The eq filters make the write conditional: a rename between our read and our write
    would otherwise store a vector of the old words, and the trigger only clears vectors
    on updates, not on this one.
    """
    text = embedding_text(row.get("name"), row.get("blurb"))
    if not text:
        return False
    literal = _embed(text)
    if not literal:
        return False
    q = (
        service_client()
        .table("places")
        .update({"blurb_embedding": literal})
        .eq("id", row["id"])
        .eq("blurb", row.get("blurb"))
    )
    q = q.eq("name", row["name"]) if row.get("name") is not None else q.is_("name", "null")
    q.execute()
    return True


def embed_place(place_id: str) -> bool:
    """Embed one community now. True when a vector was written."""
    pid = str(place_id or "").strip()
    if not pid:
        return False
    try:
        res = (
            service_client()
            .table("places")
            .select("id, name, blurb")
            .eq("id", pid)
            .limit(1)
            .execute()
        )
        rows = [r for r in (res.data or []) if isinstance(r, dict)]
        return bool(rows) and _write(rows[0])
    except Exception:  # noqa: BLE001 — search still works by name without it
        logger.exception("community_embed_failed place=%s", pid)
        return False


def missing(limit: int = _BATCH) -> list[dict[str, Any]]:
    """Communities with words to embed and no vector yet."""
    res = (
        service_client()
        .table("places")
        .select("id, name, blurb")
        .not_.is_("blurb", "null")
        .is_("blurb_embedding", "null")
        .limit(max(1, int(limit)))
        .execute()
    )
    return [r for r in (res.data or []) if isinstance(r, dict) and r.get("id")]


def embed_missing(limit: int = _BATCH) -> int:
    """One bounded pass over NULL vectors. Returns how many were written."""
    try:
        rows = missing(limit)
    except Exception:  # noqa: BLE001
        logger.exception("community_embed_missing_read_failed")
        return 0
    written = 0
    for row in rows:
        try:
            if _write(row):
                written += 1
        except Exception:  # noqa: BLE001 — one bad row must not stop the rest
            logger.exception("community_embed_failed place=%s", row.get("id"))
    if rows:
        logger.info("community_embed_missing found=%s written=%s", len(rows), written)
    return written


def kick() -> None:
    """Fill missing vectors in the background, at most once per interval."""
    global _kick_running, _last_kick
    now = time.monotonic()
    with _kick_lock:
        if _kick_running or (_last_kick and now - _last_kick < _KICK_INTERVAL_S):
            return
        _kick_running = True
        _last_kick = now

    def _run() -> None:
        global _kick_running
        try:
            embed_missing()
        finally:
            with _kick_lock:
                _kick_running = False

    threading.Thread(target=_run, daemon=True, name="community-embed").start()


def embed_place_later(place_id: str) -> None:
    """`embed_place` off the request thread — for a writer that should not wait on Vertex."""
    threading.Thread(
        target=embed_place, args=(place_id,), daemon=True, name="community-embed-one"
    ).start()
