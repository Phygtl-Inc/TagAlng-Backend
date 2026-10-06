"""How well a meet fits the viewer — the score, the proven threads, and one authored line.

The map draws a meet's fit three ways and they must never disagree: the marker's meter
(`fit_score`), the card's chips (`affinity_matched_tags`) and the card's sentence
(`rec_line`). All three come from ONE intersection — public.event_viewer_fit
(20270110120000), the viewer's public claims against the meet's cohort_tags — which the
radius read (get_nearby_activities_authed) and the preview (get_event_preview_authed)
call in SQL and this module reads for the meets the worker lists itself
(/lana/circles/profile's upcoming_events) through score_events_fit_for_user.

`fit_score` is a rank over proven shared threads (0 / 0.6 / 0.8 / 1.0 for 0 / 1 / 2 / 3+),
the ladder the peer card's badge uses; `None` means UNSCORED — the viewer holds no public
claims, the meet has no tags, or the read failed — and is never coerced to 0.

The line follows app/community_fit_line.py: the same cleaning, compose and budget
(app/peer_rec_line.py), a meet-shaped prompt, and its own cache table `event_fit_lines`
per (viewer, meet, basis, language). It is authored ONLY over the matched threads —
never the meet's title or its other tags — so the sentence can name nothing the chips
do not show. No canned fallback: a failed compose returns `rec_line: None` and the card
renders its chips without a sentence (§AI-copy).
"""

from __future__ import annotations

import logging
from typing import Any

from app.auth import service_client
from app.peer_rec_line import _basis_sig, _clean_chips, _compose

logger = logging.getLogger("lana.event_fit")

# Meets scored in one read. The profile shows a handful; this guards any future caller.
_MAX_EVENTS = 50
# Threads fed to one line. More and the composer reaches past the strongest for filler.
_MAX_BASIS_LABELS = 4


def _labels(value: Any) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in value if isinstance(value, list) else []:
        label = " ".join(str(item or "").split()).strip()
        if label and label.lower() not in seen:
            seen.add(label.lower())
            out.append(label[:80])
    return out


def _score(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return round(max(0.0, min(1.0, float(value))), 2)
    except (TypeError, ValueError):
        return None


def fetch_event_fit(user_id: str, event_ids: list[str]) -> dict[str, dict[str, Any]]:
    """{event_id: {affinity_matched_tags, affinity_match_count, affinity_total_count,
    fit_score}} for this viewer. {} on any failure — an unscored list is still a list."""
    ids = [str(e).strip() for e in event_ids if str(e or "").strip()][:_MAX_EVENTS]
    if not user_id or not ids:
        return {}
    try:
        res = service_client().rpc(
            "score_events_fit_for_user", {"p_user_id": user_id, "p_event_ids": ids}
        ).execute()
        rows = res.data if isinstance(res.data, list) else []
    except Exception:  # noqa: BLE001 — no score is a plain row, never a 500
        logger.exception("event_fit.read_failed user=%s n=%d", user_id, len(ids))
        return {}
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        if not isinstance(r, dict) or not str(r.get("event_id") or "").strip():
            continue
        out[str(r["event_id"])] = {
            "affinity_matched_tags": _labels(r.get("affinity_matched_tags")),
            "affinity_match_count": int(r.get("affinity_match_count") or 0),
            "affinity_total_count": int(r.get("affinity_total_count") or 0),
            "fit_score": _score(r.get("fit_score")),
        }
    return out


def attach_event_fit(user_id: str, rows: list[dict[str, Any]], *, id_key: str = "event_id") -> None:
    """Set `fit_score` on each meet row, in place — None where unscored."""
    for row in rows:
        row.setdefault("fit_score", None)
    scored = fetch_event_fit(user_id, [str(r.get(id_key) or "") for r in rows])
    for row in rows:
        hit = scored.get(str(row.get(id_key) or ""))
        if hit is not None:
            row["fit_score"] = hit["fit_score"]


# ── the line ─────────────────────────────────────────────────────────────────

_SYSTEM = """You write ONE short line per meet for a neighborhood app where a warm local \
concierge (Lana) helps someone find meets near them worth turning up to. The reader is \
looking at one meet a neighbour is hosting. Your line says, in her voice, why this meet \
fits them.

Per meet you are given ONLY "shared": the things the reader has said about themselves \
that this meet is about. That is your entire evidence. Write from it and nothing else.

Output ONLY JSON: {"lines": [{"chips": ["...", "..."], "line": "..."}, ...]} with EXACTLY \
one entry per input, in the same order.

CHIPS (1-3 per meet) are the reader's at-a-glance reasons, shown as small pills:
- 1-3 words, under 22 characters, no punctuation, no sentence.
- Each names a DIFFERENT thread from "shared". Only what "shared" says: one thread means \
one chip. Never a grade ("Great fit", "Perfect"), never a bare category ("Sports").
- [] when you cannot name one honestly.

LINE rules:
- ONE sentence, under 120 characters. No question mark, no greeting, no meet title.
- Speak TO the reader: "You're into…, and that's what this one is about", "It's built \
around…, which you've told me you love".
- Name ONLY threads in "shared". Never mention any other topic the meet might have.
- NEVER invent a fact about the meet — not who is going, how many, a schedule, a price, a \
vibe, or what happens there. You have not been told any of it.
- Never say or imply the reader has been before, and never describe any attendee.
- Never the words "match", "circle", "block", "mom", or "profile".
- Return "" when you cannot write it honestly from the evidence (chips [] too)."""


def _cached(user_id: str, event_id: str, lang: str, sig: str) -> dict[str, Any] | None:
    try:
        res = (
            service_client()
            .table("event_fit_lines")
            .select("id, line, chips")
            .eq("user_id", user_id)
            .eq("event_id", event_id)
            .eq("lang", lang)
            .eq("basis_sig", sig)
            .limit(1)
            .execute()
        )
    except Exception:  # noqa: BLE001 — a cache miss is a compose, never a failed fetch
        logger.warning("event-fit-line: cache read failed", exc_info=True)
        return None
    for row in res.data or []:
        if str(row.get("line") or "").strip() and row.get("chips") is not None:
            return row
    return None


def _store(user_id: str, event_id: str, lang: str, sig: str, line: str, chips: list[str]) -> str | None:
    """Keep the authored line; its id. Best-effort: the reader gets the line either way."""
    try:
        res = (
            service_client()
            .table("event_fit_lines")
            .upsert(
                {
                    "user_id": user_id,
                    "event_id": event_id,
                    "lang": lang,
                    "basis_sig": sig,
                    "line": line,
                    "chips": chips,
                },
                on_conflict="user_id,event_id,lang,basis_sig",
            )
            .execute()
        )
    except Exception:  # noqa: BLE001 — showing it matters more than keeping it
        logger.warning("event-fit-line: store failed", exc_info=True)
        return None
    for row in res.data or []:
        if row.get("id"):
            return str(row["id"])
    return None


def event_fit_line(user_id: str, event_id: str) -> dict[str, Any]:
    """The "why Lana sees a fit" block for one meet, for this viewer.

    {event_id, affinity_matched_tags, fit_score, rec_line, rec_chips, rec_id}. The line is
    None when nothing is shared (nothing true to say), when the compose fails, or when the
    meet cannot be read — never a templated stand-in.
    """
    out: dict[str, Any] = {
        "event_id": event_id,
        "affinity_matched_tags": [],
        "fit_score": None,
        "rec_line": None,
        "rec_chips": [],
        "rec_id": None,
    }
    hit = fetch_event_fit(user_id, [event_id]).get(event_id)
    if hit is None:
        return out
    shared = hit["affinity_matched_tags"]
    out["affinity_matched_tags"] = shared
    out["fit_score"] = hit["fit_score"]
    if not shared:
        return out

    basis = {"shared": shared[:_MAX_BASIS_LABELS]}
    sig = _basis_sig(basis)
    try:
        from app.lang_pref import get_user_preferred_language

        lang = get_user_preferred_language(user_id) or "en"
    except Exception:  # noqa: BLE001
        lang = "en"

    cached = _cached(user_id, event_id, lang, sig)
    if cached:
        out["rec_line"] = str(cached.get("line"))
        out["rec_chips"] = _clean_chips(cached.get("chips"))
        out["rec_id"] = str(cached.get("id") or "") or None
        return out

    composed = _compose([basis], lang, _SYSTEM)
    if not composed or not composed[0][0]:
        return out
    line, chips = composed[0]
    out["rec_line"] = line
    out["rec_chips"] = chips
    out["rec_id"] = _store(user_id, event_id, lang, sig, line, chips)
    return out
