"""The AI-authored "why this neighbour" line on a fellows row.

The card used to render `trait_tags` as chips ("family oriented", "author talks"), which
is the raw claim vocabulary rather than a reason. This module authors one sentence in
Lana's voice from the SAME proven overlap the chips came from:

    shared: ["Family oriented", "Author talks and interactive programs"]
    → chips ["Family-first", "Author talks"] + "You're both family-oriented, and you both
      turn up for author talks."

The new card leads with the chips ("WHY LANA SEES A FIT") and keeps the sentence under
them, so both come out of the SAME call and the same proven overlap — a chip can never
say more than the line, and neither can say more than the claim.

Rules of the house it inherits:
- Grounded only. The prompt gets the shared claim labels and nothing else — no nicknames,
  no concept slugs (the one field redaction never covers), no private claims. A line can
  therefore never disclose more than the label already on the card.
- AI-authored, never templated (§AI-copy). No canned fallback: when the compose fails the
  row simply carries no line and the card falls back to the tags it always had.
- Authored straight into the reader's language (one call either way) rather than English +
  a second render pass, because a fellows fetch is a screen load, not a chat turn.
- Cached in `peer_rec_lines` per (viewer, neighbour, claim basis, language): a reload costs
  no LLM call, a NEW shared claim authors a NEW line, and the 👍/👎 has a stored row to
  hang off (app/feedback.py snapshots rated text from the DB, never from the client).
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from app.db import service_client
from app.lingo_guard import enforce, find_violations

logger = logging.getLogger("lana.peer_rec_line")

# The card renders this as one small line under the name — same ceiling as the rapport
# why-line, for the same reason.
_MAX_LEN = 140
# Chips per row, and the ceiling on one chip: the card renders them on one or two rows,
# so a facet that doesn't fit in a couple of words isn't a chip.
_MAX_CHIPS = 3
_MAX_CHIP_LEN = 22
# Lines authored per fetch. A 40-row "see all" is one prompt of a dozen, not forty; the
# rest keep their tags until a later fetch (which will hit the cache for these twelve).
_MAX_COMPOSE = 12

_SYSTEM = """You write ONE short line per neighbour for a neighborhood app where a warm \
local concierge (Lana) introduces neighbors to each other. The reader is looking at a list \
of nearby people. Under each name your line says, in her voice, what the two of them \
actually have in common.

You are given, per neighbour, ONLY the things both people have said about themselves that \
overlap. That is your entire evidence. Write from it and nothing else.

Output ONLY JSON: {"lines": [{"chips": ["...", "..."], "line": "..."}, ...]} with EXACTLY \
one entry per input, in the same order.

CHIPS (2-3 per neighbour) are the reader's at-a-glance reasons, shown as small pills above \
the line:
- 1-3 words, under 22 characters, no punctuation, no sentence ("Runs at dawn", "Author \
talks", "Twin toddlers").
- Each names a DIFFERENT thread from the evidence. Never a restatement of another chip, \
never a grade ("Great fit", "Strong match", "Perfect"), never a bare category ("Sports").
- Only what the evidence says. Two overlaps means two chips, not three.
- A kids' claim reads as the kids' ("Kids same age"), never as the adults'.
- [] when you cannot name one honestly.

LINE rules:
- ONE sentence, under 120 characters. No question mark, no greeting, no name.
- Speak TO the reader about the two of them: "You're both…", "You both…".
- Name the actual thing. Weak: "You have a lot in common." Strong: "You're both early \
risers who'd rather run the lake trail than a treadmill."
- NEVER invent a fact, a place, a job, a child's age, or a shared history. If the overlap \
is thin, say the thin truth in a warm way rather than padding it.
- A claim held about a CHILD ("kids_shared") belongs to the kids, not the reader: phrase it \
as "your kids both…", never as something the adults do.
- Never the words "match", "circle", "block", "mom", or "profile".
- Return "" for a neighbour you cannot write honestly from the evidence (chips [] too)."""


def _clean(text: Any) -> str:
    """Trim, de-quote, lexicon-clean (§14) and cap one authored line."""
    out = " ".join(str(text or "").split()).strip().strip('"').strip()
    if out and find_violations(out):
        out = enforce(out).text
    return out[:_MAX_LEN]


def _clean_chips(value: Any) -> list[str]:
    """Trim, cap and de-duplicate the authored facets. Anything sentence-shaped is dropped."""
    out: list[str] = []
    seen: set[str] = set()
    for item in value if isinstance(value, list) else []:
        chip = " ".join(str(item or "").split()).strip().strip('".,;:!?').strip()
        if chip and find_violations(chip):
            chip = enforce(chip).text.strip()
        key = chip.lower()
        if len(chip) < 2 or len(chip) > _MAX_CHIP_LEN or key in seen:
            continue
        seen.add(key)
        out.append(chip)
        if len(out) >= _MAX_CHIPS:
            break
    return out


def _basis(peer: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    """The evidence for one line: the proven overlap, in plain labels."""

    def _labels(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        seen: set[str] = set()
        out: list[str] = []
        for item in value:
            label = " ".join(str(item or "").split()).strip()
            key = label.lower()
            if label and key not in seen:
                seen.add(key)
                out.append(label[:80])
        return out[:6]

    shared = _labels(peer.get("shared_labels")) or _labels(row.get("trait_tags"))
    kids = _labels(peer.get("shared_child_labels"))
    basis: dict[str, Any] = {}
    if shared:
        basis["shared"] = shared
    if kids:
        basis["kids_shared"] = kids
    if not basis:
        # No listed overlap: the two sides' own labels, which is what the templated
        # "You: X · Them: Y" line stood on. Fuzzy, so the prompt keeps them apart.
        mine = " ".join(str(peer.get("matching_my_label") or "").split()).strip()
        theirs = " ".join(str(peer.get("matching_peer_label") or "").split()).strip()
        if mine and theirs:
            basis["you_said"] = mine[:80]
            basis["they_said"] = theirs[:80]
    return basis


def _basis_sig(basis: dict[str, Any]) -> str:
    """Stable fingerprint of the evidence — a new overlap authors a new line."""
    payload = json.dumps(basis, sort_keys=True, ensure_ascii=False).lower()
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def _cached(user_id: str, lang: str, peer_ids: list[str]) -> dict[tuple[str, str], dict]:
    """This viewer's stored lines for these neighbours, keyed (peer_id, basis_sig)."""
    if not peer_ids:
        return {}
    try:
        res = (
            service_client()
            .table("peer_rec_lines")
            .select("id, peer_user_id, basis_sig, line, chips")
            .eq("user_id", user_id)
            .eq("lang", lang)
            .in_("peer_user_id", peer_ids)
            .execute()
        )
    except Exception:  # noqa: BLE001 — a cache miss is a compose, never a failed fetch
        logger.warning("peer-rec-line: cache read failed", exc_info=True)
        return {}
    out: dict[tuple[str, str], dict] = {}
    for row in res.data or []:
        key = (str(row.get("peer_user_id")), str(row.get("basis_sig")))
        if str(row.get("line") or "").strip():
            out[key] = row
    return out


def _compose(basis_list: list[dict[str, Any]], lang: str) -> list[tuple[str, list[str]]]:
    """One batched call for every line this fetch is missing. [] on any failure."""
    try:
        from app.i18n import lang_display_name
        from app.orchestrator.llm import composer_model, llm_configured, llm_json

        if not llm_configured():
            return []
        system = _SYSTEM
        if lang and lang != "en":
            system += (
                f"\n- Write every line ENTIRELY in {lang_display_name(lang)}, "
                "keeping proper nouns as they are."
            )
        data = llm_json(
            model=composer_model(),
            system=system,
            user_payload=json.dumps(basis_list, ensure_ascii=False),
            max_tokens=130 * len(basis_list) + 80,
            temperature=0.5,
        )
    except Exception:  # noqa: BLE001 — no line is a fine outcome; a 500 is not
        logger.exception("peer-rec-line: compose failed")
        return []
    lines = (data or {}).get("lines") if isinstance(data, dict) else None
    if not isinstance(lines, list) or len(lines) != len(basis_list):
        logger.warning("peer-rec-line: compose returned %s lines", len(lines or []))
        return []
    # An item is either the {chips, line} object the prompt asks for or a bare string
    # (older/looser model output) — the sentence is what the card cannot do without.
    return [
        (_clean(item.get("line")), _clean_chips(item.get("chips")))
        if isinstance(item, dict)
        else (_clean(item), [])
        for item in lines
    ]


def _store(
    user_id: str, lang: str, pending: list[tuple[str, str, str, list[str]]]
) -> dict[str, str]:
    """Insert the authored lines, returning {peer_id: rec_id} for the ones that landed."""
    payload = [
        {
            "user_id": user_id,
            "peer_user_id": peer_id,
            "lang": lang,
            "basis_sig": sig,
            "line": line,
            "chips": chips,
        }
        for peer_id, sig, line, chips in pending
    ]
    if not payload:
        return {}
    try:
        res = (
            service_client()
            .table("peer_rec_lines")
            .upsert(payload, on_conflict="user_id,peer_user_id,lang,basis_sig")
            .execute()
        )
    except Exception:  # noqa: BLE001 — show the line even if we could not keep it; the
        # thumb is what needs the row, and the FE hides it without a rec_id.
        logger.warning("peer-rec-line: store failed", exc_info=True)
        return {}
    return {
        str(row.get("peer_user_id")): str(row.get("id"))
        for row in (res.data or [])
        if row.get("id")
    }


def attach_rec_lines(
    user_id: str,
    rows: list[dict[str, Any]],
    peers: list[dict[str, Any]],
) -> None:
    """Set `rec_line`, `rec_chips` (+ `rec_id`, when stored) on each shaped row, in place.

    `rows` and `peers` are the shaped rows and the raw matcher rows they came from, in
    the same order. Best-effort throughout: a row we cannot author for keeps the
    templated label and tags it has always had.
    """
    if not user_id or not rows:
        return
    try:
        from app.lang_pref import get_user_preferred_language

        lang = get_user_preferred_language(user_id) or "en"
    except Exception:  # noqa: BLE001
        lang = "en"

    # (index, peer_id, basis, sig) for every row with real evidence AND a real peer id
    # (the id is the cache key; an unverified viewer's row has it nulled on the wire but
    # the raw match still carries it, so those rows get a line too).
    todo: list[tuple[int, str, dict[str, Any], str]] = []
    for idx, (row, peer) in enumerate(zip(rows, peers)):
        peer_id = str((peer or {}).get("peer_user_id") or "").strip()
        basis = _basis(peer or {}, row)
        if peer_id and basis:
            todo.append((idx, peer_id, basis, _basis_sig(basis)))
    if not todo:
        return

    cached = _cached(user_id, lang, sorted({peer_id for _, peer_id, _, _ in todo}))
    missing: list[tuple[int, str, dict[str, Any], str]] = []
    for idx, peer_id, basis, sig in todo:
        hit = cached.get((peer_id, sig))
        # `chips is None` = a line authored before the card had chips. Re-author it once
        # rather than serving a row the new card renders with an empty facet strip.
        if hit and hit.get("chips") is not None:
            rows[idx]["rec_line"] = str(hit.get("line"))
            rows[idx]["rec_chips"] = _clean_chips(hit.get("chips"))
            rows[idx]["rec_id"] = str(hit.get("id"))
        else:
            missing.append((idx, peer_id, basis, sig))
    if not missing:
        return
    if len(missing) > _MAX_COMPOSE:
        logger.info(
            "peer-rec-line: authoring %d of %d missing lines this fetch",
            _MAX_COMPOSE,
            len(missing),
        )
        missing = missing[:_MAX_COMPOSE]

    composed = _compose([basis for _, _, basis, _ in missing], lang)
    if not composed:
        return
    pending: list[tuple[str, str, str, list[str]]] = []
    for (idx, peer_id, _basis_unused, sig), (line, chips) in zip(missing, composed):
        if not line:
            continue
        rows[idx]["rec_line"] = line
        rows[idx]["rec_chips"] = chips
        pending.append((peer_id, sig, line, chips))
    ids = _store(user_id, lang, pending)
    for idx, peer_id, _basis_unused, _sig in missing:
        rec_id = ids.get(peer_id)
        if rec_id and rows[idx].get("rec_line"):
            rows[idx]["rec_id"] = rec_id
