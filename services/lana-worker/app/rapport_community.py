"""The "By the way…" tile inside a community.

Inside a creator's community the tile kept asking the person's own queue — "Long course
triathlon and race prep" sitting next to a founders' group (Tommaso, 2026-10-01). The
community is also where we learn the most about someone, so the tile there asks about
THEM in relation to the community, and every answer is an ordinary identity claim tagged
to the place (rapport_gaps.place_ref → circles_flow.tag_claim_place_from_gap).

Order, first that yields:
  1. (retired 2026-10-01) the creator's "What might someone ask first?" used to be asked
     verbatim. It is a question MEMBERS ask the community — Etiqueta do Reino's creator
     wrote her own answer into it ("What do I gain from this? I would say personal and
     professional growth."), which then showed as the question. It is context now;
  2. a question already in their queue that bears on this community (the model judges
     relevance; nothing is added, the row is just asked here instead of later);
  3. a new question written from what the community is about — at most _MAX_GENERATED per
     community, and never while another community question is still open;
  4. their normal queue, exactly as outside.

Community rows are ordinary rapport_gaps rows keyed `community:<place_id>:…`, so
unique(user_id, gap_id) makes each one once-per-person, and rapport_ranker never serves
them outside their community (COMMUNITY_GAP_PREFIX).
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

COMMUNITY_GAP_PREFIX = "community:"
_MAX_GENERATED = 3
_RELEVANCE_POOL = 12

_RELEVANCE_PROMPT = """You pick which of a person's queued profile questions fits the \
community they are chatting inside right now.

Output ONLY JSON: {"pick": "<id>"} or {"pick": null}

Pick a question ONLY if asking it inside this community would feel natural — it is about the \
community's TOPIC or something its members share. Otherwise null. Never pick a question \
about health, money, family trouble or anything private. Never pick a question about \
where they live, their neighborhood, how long they have lived somewhere, or which local \
place or spot they go to: a creator's community has no geography, so those belong to \
their neighborhood, not here."""

_GENERATE_PROMPT = """You write ONE short question Lana asks a member inside a community, to \
learn something about THEM that relates to what the community is about.

Output ONLY JSON: {"question": "...", "teaser": "about ..."}

Rules:
- One question, one question mark, at most 14 words, answerable in a few words.
- It must draw out a FACT ABOUT THEM — what they do, make, practise, are working on, their \
experience or level with the community's topic ("Do you make videos yourself?", "How long \
have you been running?", "What are you building right now?"). The answer is saved to their \
profile, and a favourite, a ranking or a "would you rather" tells us nothing about who they \
are — never ask for favourites, opinions of the creator's content, or hypotheticals.
- Never a quiz about the community, never about the creator.
- Never ask where they live, their age, health, money, family, or anything private.
- Never repeat or rephrase a question in `already_asked`.
- `members_might_ask` is what members ask EACH OTHER here (it may even contain the \
creator's own answer) — use it only to understand the topic; never ask it, quote it or \
turn it into your question.
- teaser: 2-5 lowercase words starting with "about", e.g. "about what you're building".
- English only (rendered into their language downstream)."""


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def community_gap_id(place_id: str, suffix: str) -> str:
    return f"{COMMUNITY_GAP_PREFIX}{place_id}:{suffix}"


def is_community_row(row: dict[str, Any] | None, place_id: str | None = None) -> bool:
    gid = str((row or {}).get("gap_id") or "")
    if not gid.startswith(COMMUNITY_GAP_PREFIX):
        return False
    return place_id is None or gid.startswith(community_gap_id(place_id, ""))


def _rows(user_id: str, place_id: str) -> list[dict[str, Any]]:
    """Every community row this person has for this place, any status."""
    from app.auth import service_client

    try:
        res = (
            service_client()
            .table("rapport_gaps")
            .select("*")
            .eq("user_id", user_id)
            .like("gap_id", community_gap_id(place_id, "") + "%")
            .execute()
        )
    except Exception:  # noqa: BLE001 — an unreadable queue falls through to the normal tile
        logger.exception("rapport_community.rows_failed user=%s place=%s", user_id, place_id)
        return []
    return [r for r in (res.data or []) if isinstance(r, dict)]


def _personal_open_rows(user_id: str) -> list[dict[str, Any]]:
    from app.rapport_ranker import _load_open_rows

    return [r for r in _load_open_rows(user_id) if not is_community_row(r)]


def _personal_pending(user_id: str) -> dict[str, Any] | None:
    from app.auth import service_client

    try:
        res = (
            service_client()
            .table("rapport_gaps")
            .select("*")
            .eq("user_id", user_id)
            .eq("status", "asked")
            .not_.like("gap_id", COMMUNITY_GAP_PREFIX + "%")
            .order("asked_at", desc=True)
            .limit(1)
            .execute()
        )
    except Exception:  # noqa: BLE001
        return None
    return (res.data or [None])[0]


def _set_status(gap_row_id: str, status: str, *, place_ref: str | None = None) -> bool:
    from app.auth import service_client
    from app.rapport_ranker import _now

    patch: dict[str, Any] = {"status": status, "updated_at": _now().isoformat()}
    if status == "asked":
        patch["asked_at"] = _now().isoformat()
    if place_ref:
        patch["place_ref"] = place_ref
    try:
        service_client().table("rapport_gaps").update(patch).eq("gap_row_id", gap_row_id).execute()
        return True
    except Exception:  # noqa: BLE001
        logger.exception("rapport_community.status_failed row=%s", gap_row_id)
        return False


def _serve(user_id: str, row: dict[str, Any], *, lang: str, surface: str, kind: str) -> dict[str, Any]:
    from app.analytics import track
    from app.rapport_gap_tree import get_gap
    from app.rapport_ranker import _build, _with_grounding

    track(
        "rapport_gap_shown",
        user_id=user_id,
        event_properties={"gap_id": row.get("gap_id"), "surface": surface, "kind": kind},
    )
    logger.info("rapport_community.serve user=%s kind=%s gap=%s", user_id, kind, row.get("gap_id"))
    return _with_grounding(user_id, row, _build(row, get_gap(str(row.get("gap_id") or "")), lang))


def _pick_relevant(facts: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The queued question that fits this community, judged by the model — or None."""
    # Grounding asks ("which spot do you…?") pin a LOCAL place — never a community topic.
    # "How long have you been in the neighborhood?" was picked as fitting Etiqueta do Reino
    # (2026-10-01); the prompt now rules location out, and these never reach it.
    pool = [
        r for r in rows if _clean(r.get("question")) and not r.get("affiliation_ref")
    ][:_RELEVANCE_POOL]
    if not pool:
        return None
    try:
        from app.orchestrator.llm import llm_configured, llm_json, router_model

        if not llm_configured():
            return None
        data = llm_json(
            model=router_model(),
            system=_RELEVANCE_PROMPT,
            user_payload=json.dumps(
                {
                    "community": {k: facts.get(k) for k in ("name", "about", "members_help")},
                    "questions": [
                        {"id": str(r["gap_row_id"]), "question": _clean(r.get("question"))}
                        for r in pool
                    ],
                },
                ensure_ascii=False,
            ),
            max_tokens=40,
            temperature=0,
        )
    except Exception:  # noqa: BLE001
        logger.exception("rapport_community.relevance_failed")
        return None
    pick = str((data or {}).get("pick") or "")
    return next((r for r in pool if str(r.get("gap_row_id")) == pick), None)


def _generate(facts: dict[str, Any], already: list[str]) -> dict[str, str] | None:
    if not (facts.get("about") or facts.get("members_help") or facts.get("creator_wants")):
        # Nothing true to ground it in — no question beats an invented one.
        return None
    try:
        from app.orchestrator.llm import llm_configured, llm_json, router_model

        if not llm_configured():
            return None
        data = llm_json(
            model=router_model(),
            system=_GENERATE_PROMPT,
            user_payload=json.dumps(
                {
                    "community": {k: facts.get(k) for k in ("name", "about", "members_help")},
                    # lana.help "What might someone ask first?" — what MEMBERS ask the
                    # community. Context for the topic, never a question to repeat.
                    "members_might_ask": facts.get("creator_wants"),
                    "already_asked": already[:10],
                },
                ensure_ascii=False,
            ),
            max_tokens=80,
            temperature=0.5,
        )
    except Exception:  # noqa: BLE001
        logger.exception("rapport_community.generate_failed")
        return None
    question = _clean((data or {}).get("question"))
    if not question:
        return None
    return {"question": question, "teaser": _clean((data or {}).get("teaser")) or "about you here"}


def next_ask_in_community(
    user_id: str,
    session_ctx_like: dict[str, Any],
    *,
    lang: str,
    surface: str = "homescreen",
    cycle: bool = False,
    allow_personal_queue: bool = True,
) -> tuple[bool, dict[str, Any] | None]:
    """(handled, ask). handled=False means "not inside a community — use the normal tile".

    allow_personal_queue=False (guests) stops at step 3: a guest never gets the personal
    queue, only the community's own question."""
    from app.community_opening import active_community_facts
    from app.rapport_gaps import open_semantic_gap

    facts = active_community_facts(session_ctx_like)
    if not facts:
        return False, None
    pid = str(facts["place_id"])
    mine = _rows(user_id, pid)

    # Rows from the retired verbatim step (`…:first`) never show again — one could still be
    # pending on someone's screen, reading as a statement rather than a question.
    for r in mine:
        if str(r.get("gap_id") or "").endswith(":first") and r.get("status") in ("open", "asked"):
            _set_status(str(r["gap_row_id"]), "expired")
            r["status"] = "expired"

    # 0) A community question already on screen: re-show it (idempotent across reloads),
    #    unless they tapped ⟳, which retires it as a skip.
    pending = next((r for r in mine if r.get("status") == "asked"), None)
    if pending and not cycle:
        return True, _serve(user_id, pending, lang=lang, surface=surface, kind="community_pending")
    if pending and cycle:
        _set_status(str(pending["gap_row_id"]), "skipped")
    open_rows = [r for r in mine if r.get("status") == "open"]

    # 1) A community question already opened for them but not yet shown.
    row = open_rows[0] if open_rows else None
    if row and _set_status(str(row["gap_row_id"]), "asked"):
        return True, _serve(user_id, row, lang=lang, surface=surface, kind="community_open")

    # 2) Something already in their queue that fits here. Asked HERE instead of later —
    #    nothing is added. A different personal ask left pending is put back unpenalised,
    #    so leaving the community returns the tile to exactly where it was.
    if allow_personal_queue:
        relevant = _pick_relevant(facts, _personal_open_rows(user_id))
        if relevant:
            prior = _personal_pending(user_id)
            if prior and prior.get("gap_row_id") != relevant.get("gap_row_id"):
                _set_status(str(prior["gap_row_id"]), "open")
            # Asked as THIS community's question, so its answer belongs to it: the claim
            # is tagged from the gap's place_ref, which a personal row never had (QA
            # 2026-10-05 — "Builds apps in Flutter" answered here, tagged nowhere).
            if _set_status(
                str(relevant["gap_row_id"]),
                "asked",
                place_ref=None if relevant.get("place_ref") else pid,
            ):
                return True, _serve(
                    user_id, relevant, lang=lang, surface=surface, kind="community_relevant"
                )

    # 3) Write one from the community itself — capped, one at a time.
    generated = [r for r in mine if ":gen-" in str(r.get("gap_id") or "")]
    if len(generated) < _MAX_GENERATED:
        made = _generate(facts, [_clean(r.get("question")) for r in mine])
        if made:
            suffix = "gen-" + hashlib.sha1(made["question"].lower().encode()).hexdigest()[:10]
            if open_semantic_gap(
                user_id,
                None,
                made["question"],
                bucket="community",
                teaser=made["teaser"],
                place_ref=pid,
                community=str(facts["name"]),
                gap_id=community_gap_id(pid, suffix),
                unlock_score=0.9,
            ):
                row = next(
                    (r for r in _rows(user_id, pid) if r.get("gap_id") == community_gap_id(pid, suffix)),
                    None,
                )
                if row and _set_status(str(row["gap_row_id"]), "asked"):
                    return True, _serve(
                        user_id, row, lang=lang, surface=surface, kind="community_generated"
                    )

    # 4) Nothing community-shaped left: their normal queue (never for a guest).
    if not allow_personal_queue:
        return True, None
    return False, None
