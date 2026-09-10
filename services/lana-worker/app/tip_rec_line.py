"""The "why Lana sees a fit" line on a recent-recommendation row (§43).

app/peer_rec_line.py already authors this exact shape — 2-3 chips over one grounded
sentence, cached per (viewer, subject, basis, language), addressable by `rec_id` so a
thumb rates DB text — for a FELLOWS row, from the two people's overlapping claims. A
recommendation row wants the same block with the evidence swapped:

    the reader's own claims  ×  the recommendation's own fields
    → chips ["Saturday mornings", "Ages 2-3"] + "It runs right after your CF Fitness
      class, and the group is built for 2-3 year olds."

So this module is that module's evidence and prompt, and nothing else new: `_cached`,
`_compose`, `_store` and the cleaning are imported, the `peer_rec_lines` row keys on the
tip AUTHOR (a real user id) with `basis_sig` carrying the tip — so a re-read is free, a
re-captured recommendation authors a new line, and /lana/feedback works unchanged.

The matched claims are also the FIT SCORE (`match_strength`), which is what the For-you
tab orders on. One overlap, both jobs — a row we cannot order honestly is the same row we
cannot write a line for.

Rules of the house, same as the fellows line: grounded only (a line may never name a fact
the tip's fields don't hold), AI-authored with no canned fallback, written straight into
the reader's language, `null` / `[]` when nothing honest can be said.
"""

from __future__ import annotations

import logging
from typing import Any

from app.layer1_intents import attr_filter_tokens
from app.peer_rec_line import (
    _MAX_COMPOSE,
    _basis_sig,
    _cached,
    _clean_chips,
    _compose,
    _store,
)

logger = logging.getLogger("lana.tip_rec_line")

# Claims fed to one line. A reader with thirty claims and a dentist recommendation has at
# most a couple that bear on it; the rest are noise the model would be tempted to reach for.
_MAX_CLAIMS = 4

_SYSTEM = """You write ONE short line per recommendation for a neighborhood app where a \
warm local concierge (Lana) passes on what neighbors recommend. The reader is browsing \
recommendations their neighbors shared. Under each one your line says, in her voice, why \
THIS recommendation fits THIS reader.

Per recommendation you are given ONLY: things the reader has said about themselves that \
touch it ("you_said"), the recommendation's own fields ("recommendation"), and how the \
reader is connected to the person who shared it ("from_neighbor"). That is your entire \
evidence. Write from it and nothing else.

Output ONLY JSON: {"lines": [{"chips": ["...", "..."], "line": "..."}, ...]} with EXACTLY \
one entry per input, in the same order.

CHIPS (2-3 per recommendation) are the reader's at-a-glance reasons, shown as small pills \
above the line:
- 1-3 words, under 22 characters, no punctuation, no sentence ("Saturday mornings", \
"Walk from you", "Ages 2-3").
- Each names a DIFFERENT thread from the evidence. Never a restatement of another chip, \
never a grade ("Great fit", "Perfect match"), never a bare category ("Sports").
- Only what the evidence says. One real reason means one chip, not three.
- [] when you cannot name one honestly.

LINE rules:
- ONE sentence, under 120 characters. No question mark, no greeting, no name.
- Speak TO the reader about the recommendation: "It runs…", "They're…", "You'd…".
- Name the actual thing. Weak: "This could be a good fit." Strong: "It runs Saturday \
mornings, right after your class, and the group is built for 2-3 year olds."
- NEVER invent a fact, a price, a schedule, a distance or a quality the fields do not \
state. If the fit is thin, say the thin truth warmly rather than padding it.
- Never claim the reader has been there, and never speak for the neighbor who shared it \
beyond what "from_neighbor" says.
- Never the words "match", "circle", "block", "mom", or "profile".
- Return "" for a recommendation you cannot write honestly from the evidence (chips [] \
too)."""


def _tip_tokens(row: dict[str, Any]) -> set[str]:
    """Every substantive word the recommendation itself carries.

    Per field rather than on one joined string: attr_filter_tokens caps its output, and a
    long description would otherwise swallow the category and the place.
    """
    parts: list[str] = [
        str(row.get(k) or "")
        for k in ("name", "category", "reco_type", "place", "description", "detail_text")
    ]
    for f in row.get("fields") or []:
        if isinstance(f, dict):
            parts.append(f"{f.get('label') or ''} {f.get('answer') or ''}")
    tokens: set[str] = set()
    for part in parts:
        if part.strip():
            tokens.update(attr_filter_tokens(part))
    return tokens


def _matched(labels: list[str], tokens: set[str]) -> list[tuple[str, float]]:
    """(claim label, how much of it the recommendation touches), best first.

    ponytail: token overlap, not embeddings. It reads "Toddler at home" against "ages 2-3"
    as a miss — swap in the pgvector path claim_search.py already uses if the misses matter.
    """
    out: list[tuple[str, float]] = []
    for label in labels:
        claim = attr_filter_tokens(label)
        if not claim:
            continue
        hit = len([t for t in claim if t in tokens])
        if hit:
            out.append((label, hit / len(claim)))
    out.sort(key=lambda pair: pair[1], reverse=True)
    return out


def _score(matched: list[tuple[str, float]], row: dict[str, Any]) -> float:
    """0-1 fit, which is what the For-you tab sorts on. Two solid claim hits saturate it;
    a shared community or the same block nudges, never carries."""
    fit = sum(ratio for _, ratio in matched[:_MAX_CLAIMS]) / 2.0
    if row.get("shared_circles"):
        fit += 0.1
    if row.get("same_block"):
        fit += 0.05
    return round(min(fit, 1.0), 3)


def _basis(row: dict[str, Any], matched: list[tuple[str, float]]) -> dict[str, Any]:
    """The evidence for one line. Nothing here that isn't already on the card."""
    reco = {
        k: str(row.get(k) or "").strip()
        for k in ("name", "category", "reco_type", "place", "description")
    }
    reco = {k: v for k, v in reco.items() if v}
    if not reco.get("description") and str(row.get("detail_text") or "").strip():
        # Pre-20261120 rows are one " · "-joined sentence and nothing else.
        reco["description"] = str(row["detail_text"]).strip()[:300]
    details = [
        f"{f.get('label')}: {f.get('answer')}"
        for f in (row.get("fields") or [])
        if isinstance(f, dict) and f.get("label") and f.get("answer")
    ][:6]
    if details:
        reco["details"] = details
    basis: dict[str, Any] = {
        "you_said": [label for label, _ in matched[:_MAX_CLAIMS]],
        "recommendation": reco,
    }
    frm: dict[str, Any] = {}
    names = [
        str(c.get("name") or "").strip()
        for c in (row.get("shared_circles") or [])
        if isinstance(c, dict) and str(c.get("name") or "").strip()
    ]
    if names:
        frm["shared_places"] = names[:3]
    if row.get("same_block"):
        frm["same_block"] = True
    if frm:
        basis["from_neighbor"] = frm
    return basis


def attach_fit(
    user_id: str,
    rows: list[dict[str, Any]],
    *,
    tab: str = "recent",
    limit: int = 20,
) -> None:
    """Score every row, order the For-you tab, and author a line where there is evidence.

    In place, best-effort: a row we cannot author for keeps `rec_line: None` and renders
    without the block, which is the frontend's stated position — author it wherever there
    is evidence, on every tab, rather than only under For you.

    Ordering runs BEFORE authoring so the compose budget is spent on the rows the reader
    will actually see (the For-you page is fetched wide and sliced here).
    """
    for row in rows:
        row.setdefault("rec_line", None)
        row.setdefault("rec_chips", [])
        row.setdefault("rec_id", None)
        row.setdefault("match_strength", 0.0)
    if not user_id or not rows:
        return

    try:
        from app.claims_persist import fetch_active_claim_labels

        labels = fetch_active_claim_labels(user_id)
    except Exception:  # noqa: BLE001 — no claims is a feed with no fit lines, not a 500
        logger.warning("tip-rec-line: claim read failed", exc_info=True)
        labels = []

    # Basis rides ON the row while the page is still being ordered — the sort below
    # moves rows, and an index computed before it would author the wrong line.
    for row in rows:
        matched = _matched(labels, _tip_tokens(row)) if labels else []
        row["match_strength"] = _score(matched, row)
        row["_fit_basis"] = _basis(row, matched) if matched else None

    if str(tab or "").lower() == "foryou":
        # Score first, then newest — a tie between two rows the reader fits equally is
        # broken the way the Recent tab would have broken it.
        rows.sort(
            key=lambda r: (r.get("match_strength") or 0.0, str(r.get("created_at") or "")),
            reverse=True,
        )
        del rows[max(1, int(limit)) :]

    todo: list[tuple[int, str, dict[str, Any], str]] = []
    for idx, row in enumerate(rows):
        basis = row.pop("_fit_basis", None)
        author = str(row.get("peer_user_id") or "")
        if basis and author:
            todo.append((idx, author, basis, _basis_sig(basis)))
    if not todo:
        return

    try:
        from app.lang_pref import get_user_preferred_language

        lang = get_user_preferred_language(user_id) or "en"
    except Exception:  # noqa: BLE001
        lang = "en"

    cached = _cached(user_id, lang, sorted({author for _, author, _, _ in todo}))
    missing: list[tuple[int, str, dict[str, Any], str]] = []
    for idx, author, basis, sig in todo:
        hit = cached.get((author, sig))
        if hit and hit.get("chips") is not None:
            rows[idx]["rec_line"] = str(hit.get("line"))
            rows[idx]["rec_chips"] = _clean_chips(hit.get("chips"))
            rows[idx]["rec_id"] = str(hit.get("id"))
        else:
            missing.append((idx, author, basis, sig))
    if not missing:
        return
    if len(missing) > _MAX_COMPOSE:
        logger.info(
            "tip-rec-line: authoring %d of %d missing lines this fetch",
            _MAX_COMPOSE,
            len(missing),
        )
        missing = missing[:_MAX_COMPOSE]

    composed = _compose([basis for _, _, basis, _ in missing], lang, _SYSTEM)
    if not composed:
        return
    pending: list[tuple[str, str, str, list[str]]] = []
    for (idx, author, _unused, sig), (line, chips) in zip(missing, composed):
        if not line:
            continue
        rows[idx]["rec_line"] = line
        rows[idx]["rec_chips"] = chips
        pending.append((author, sig, line, chips))
    ids = _store(user_id, lang, pending)
    for idx, author, _unused, _sig in missing:
        rec_id = ids.get(author)
        if rec_id and rows[idx].get("rec_line"):
            rows[idx]["rec_id"] = rec_id
