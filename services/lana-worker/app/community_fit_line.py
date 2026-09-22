"""The "why Lana sees a fit" block on a community the caller could join (discover).

app/peer_rec_line.py already authors this exact shape — 2-3 chips over one grounded
sentence, cached per (viewer, subject, basis, language) — for a FELLOWS row, from two
people's overlapping claims. app/tip_rec_line.py does it again for a recommendation. A
discovery row wants the same block with the subject swapped for a PLACE:

    shared: ["Trail running", "Sourdough"] · members: 12
    → chips ["Trail running", "Sourdough"] + "Twelve people go here, and a couple of them
      run trails and bake the same bread you do."

So this module is those modules' cleaning, compose and budget with new evidence, a new
prompt and its own cache table (`peer_rec_lines.peer_user_id` is a users FK, so a place
cannot live in it).

Rules of the house it inherits:
- Grounded only. The evidence is app/community_affinity.py's basis — the SAME read the
  0-1 `affinity` is blended from, so the number and the sentence can never disagree. The
  prompt never sees the place name, an address, a member count beyond the integer, or any
  member identity.
- AI-authored, never templated (§AI-copy). No canned fallback: a failed compose leaves the
  row with `fit_line: None` and the card renders without the block.
- Authored straight into the reader's language — a discovery fetch is a screen load, not a
  chat turn, so one call rather than English plus a render pass.
- Cached in `community_fit_lines` per (viewer, place, basis, language): a reload costs no
  LLM call, and a NEW overlap authors a NEW line instead of serving a stale one.
"""

from __future__ import annotations

import logging
from typing import Any

from app.db import service_client
from app.peer_rec_line import (
    _MAX_COMPOSE,
    _basis_sig,
    _clean_chips,
    _compose,
)

logger = logging.getLogger("lana.community_fit_line")

_SYSTEM = """You write ONE short line per community for a neighborhood app where a warm \
local concierge (Lana) helps someone find the places near them worth walking into. The \
reader is looking at a list of nearby communities they have NOT joined. Under each one \
your line says, in her voice, why this one fits them.

Per community you are given ONLY: what the reader and the people there BOTH say about \
themselves ("shared", "kids_shared"), or failing that one thing the reader said \
("you_said") beside one thing a member said ("members_say"), sometimes the kind of place \
the reader already goes to ("you_already_have"), and how many people are there \
("members"). That is your entire evidence. Write from it and nothing else.

Output ONLY JSON: {"lines": [{"chips": ["...", "..."], "line": "..."}, ...]} with EXACTLY \
one entry per input, in the same order.

CHIPS (2-3 per community) are the reader's at-a-glance reasons, shown as small pills above \
the line:
- 1-3 words, under 22 characters, no punctuation, no sentence ("Trail running", "Sunday \
service", "Kids in karate").
- Each names a DIFFERENT thread from the evidence. Never a restatement of another chip, \
never a grade ("Great fit", "Strong match", "Perfect"), never a bare category ("Sports"), \
never a bare count ("12 people" — the card already shows it).
- Only what the evidence says. One overlap means one chip, not three.
- A kids' claim reads as the kids' ("Kids same age"), never as the adults'.
- [] when you cannot name one honestly.

LINE rules:
- ONE sentence, under 120 characters. No question mark, no greeting, no place name.
- Speak TO the reader about the people there: "The people here…", "You'd be…", "A few of \
them…".
- Name the actual thing. Weak: "This could be a good fit." Strong: "A few of the people \
here run the same trails you do."
- NEVER invent a fact about the place — not a schedule, a price, a size, a vibe, a \
speciality, or what happens there. You have not been told any of it.
- Never say or imply the reader has been there, and never name or describe an individual \
member.
- Only "members" licenses a number, and never guess past it: with 1 you may say one \
person, never "a few".
- "you_already_have" alone is a THIN reason — say the thin truth ("it's another gym, and \
the people there are local to you") rather than dressing it up.
- A claim held about a CHILD ("kids_shared") belongs to the kids, not the reader: phrase it \
as "their kids and yours…", never as something the adults do.
- Never the words "match", "circle", "block", "mom", or "profile".
- Return "" for a community you cannot write honestly from the evidence (chips [] too)."""


def _cached(user_id: str, lang: str, place_ids: list[str]) -> dict[tuple[str, str], dict]:
    """This viewer's stored lines for these places, keyed (place_id, basis_sig)."""
    if not place_ids:
        return {}
    try:
        res = (
            service_client()
            .table("community_fit_lines")
            .select("id, place_ref, basis_sig, line, chips")
            .eq("user_id", user_id)
            .eq("lang", lang)
            .in_("place_ref", place_ids)
            .execute()
        )
    except Exception:  # noqa: BLE001 — a cache miss is a compose, never a failed fetch
        logger.warning("community-fit-line: cache read failed", exc_info=True)
        return {}
    out: dict[tuple[str, str], dict] = {}
    for row in res.data or []:
        if str(row.get("line") or "").strip():
            out[(str(row.get("place_ref")), str(row.get("basis_sig")))] = row
    return out


def _store(user_id: str, lang: str, pending: list[tuple[str, str, str, list[str]]]) -> None:
    """Keep the authored lines. Best-effort: the reader gets the line either way."""
    payload = [
        {
            "user_id": user_id,
            "place_ref": place_id,
            "lang": lang,
            "basis_sig": sig,
            "line": line,
            "chips": chips,
        }
        for place_id, sig, line, chips in pending
    ]
    if not payload:
        return
    try:
        service_client().table("community_fit_lines").upsert(
            payload, on_conflict="user_id,place_ref,lang,basis_sig"
        ).execute()
    except Exception:  # noqa: BLE001 — showing it matters more than keeping it
        logger.warning("community-fit-line: store failed", exc_info=True)


def attach_fit_lines(user_id: str, rows: list[dict[str, Any]]) -> None:
    """Set `fit_line` and `fit_chips` on each discovery row, in place.

    Reads the `_fit_basis` app/community_affinity.py::attach_affinity stashed and POPS it,
    so the evidence never reaches the wire. Best-effort throughout: a row we cannot author
    for keeps `fit_line: None`, and the card renders the block only where there is one.
    """
    for row in rows:
        row.setdefault("fit_line", None)
        row.setdefault("fit_chips", [])
    todo: list[tuple[int, str, dict[str, Any], str]] = []
    for idx, row in enumerate(rows):
        basis = row.pop("_fit_basis", None)
        place_id = str(row.get("place_id") or "").strip()
        if isinstance(basis, dict) and basis and place_id:
            todo.append((idx, place_id, basis, _basis_sig(basis)))
    if not user_id or not todo:
        return

    try:
        from app.lang_pref import get_user_preferred_language

        lang = get_user_preferred_language(user_id) or "en"
    except Exception:  # noqa: BLE001
        lang = "en"

    cached = _cached(user_id, lang, sorted({pid for _, pid, _, _ in todo}))
    missing: list[tuple[int, str, dict[str, Any], str]] = []
    for idx, place_id, basis, sig in todo:
        hit = cached.get((place_id, sig))
        # `chips is None` = a line authored before the card had chips. Re-author it once
        # rather than serving a row the card renders with an empty facet strip.
        if hit and hit.get("chips") is not None:
            rows[idx]["fit_line"] = str(hit.get("line"))
            rows[idx]["fit_chips"] = _clean_chips(hit.get("chips"))
        else:
            missing.append((idx, place_id, basis, sig))
    if not missing:
        return
    if len(missing) > _MAX_COMPOSE:
        logger.info(
            "community-fit-line: authoring %d of %d missing lines this fetch",
            _MAX_COMPOSE,
            len(missing),
        )
        missing = missing[:_MAX_COMPOSE]

    composed = _compose([basis for _, _, basis, _ in missing], lang, _SYSTEM)
    if not composed:
        return
    pending: list[tuple[str, str, str, list[str]]] = []
    for (idx, place_id, _unused, sig), (line, chips) in zip(missing, composed):
        if not line:
            continue
        rows[idx]["fit_line"] = line
        rows[idx]["fit_chips"] = chips
        pending.append((place_id, sig, line, chips))
    _store(user_id, lang, pending)
