"""Answer a question from a community's uploaded documents — and prove it.

Two ways to find the answer:
  full      every section the asker may read fits the budget, so the model reads them all
  navigate  it does not: the model reads the section index and opens sections (read /
            find) for up to _MAX_STEPS steps, then answers

Either way the answer is structured — reply, claims (fact + section + verbatim quote),
coverage — and every quote is checked against the section text in code before the
reply goes out. A failed quote gets one retry with the failure named; if it still fails,
a last call writes the reply from the verified claims only. Embedding similarity
decides nothing here.

Visibility: a visitor reads public documents only; members and managers read both.
"""

from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from app.auth import service_client
from app.community_docs import (
    DocsError,
    docs_enabled,
    docs_role,
    visible_visibilities,
)

logger = logging.getLogger(__name__)

# Whole-collection budget for the full mode. Sized for the smallest synth-tier context
# window among the providers llm_json can fall over to (gpt-4o: 128k).
FULL_MODE_TOKENS = 60000
# Navigate mode: how much opened text the model may hold, and how many lookups it gets.
_NAV_OPEN_TOKENS = 30000
_MAX_STEPS = 4
_MAX_READ_PER_STEP = 6
_RECENT_TURNS = 8
_TURN_CHARS = 400
_MIN_QUOTE_CHARS = 3


@dataclass
class DocSection:
    sid: str  # short handle the model sees: S1, S2, …
    id: str
    doc_id: str
    doc_title: str
    heading_path: list[str]
    covers: str
    body: str
    token_count: int


@dataclass
class DocsAnswer:
    reply: str
    coverage: str
    sources: list[dict[str, str]] = field(default_factory=list)
    mode: str = "full"
    claims_total: int = 0
    claims_dropped: int = 0
    retried: bool = False
    sections_read: list[str] = field(default_factory=list)


# ── loading what the asker may read ─────────────────────────────────────────────


def _load_sections(place_id: str, role: str) -> list[DocSection]:
    client = service_client()
    docs = (
        client.table("community_documents")
        .select("id, title")
        .eq("place_id", place_id)
        # No status filter: sections exist only after a successful ingest, so a document
        # being re-processed (or whose new version failed) keeps answering from its last
        # good version.
        .in_("visibility", list(visible_visibilities(role)))
        .execute()
        .data
        or []
    )
    if not docs:
        return []
    titles = {str(d["id"]): str(d.get("title") or "") for d in docs}
    rows = (
        client.table("community_doc_sections")
        .select("id, doc_id, ord, heading_path, covers, body, token_count")
        .eq("place_id", place_id)
        .in_("doc_id", list(titles))
        .order("doc_id")
        .order("ord")
        .execute()
        .data
        or []
    )
    out: list[DocSection] = []
    for r in rows:
        out.append(
            DocSection(
                sid=f"S{len(out) + 1}",
                id=str(r["id"]),
                doc_id=str(r["doc_id"]),
                doc_title=titles.get(str(r["doc_id"]), ""),
                heading_path=list(r.get("heading_path") or []),
                covers=str(r.get("covers") or ""),
                body=str(r.get("body") or ""),
                token_count=int(r.get("token_count") or 0),
            )
        )
    return out


def _live_facts(place_id: str, user_id: str | None) -> list[dict[str, Any]]:
    """Upcoming meets — current data that outranks anything a document says about times
    and places."""
    from app.community_scope import community_events

    try:
        rows = community_events(place_id, limit=8, viewer_id=user_id)
    except Exception:
        logger.warning("community_docs_answer.live_failed place=%s", place_id, exc_info=True)
        return []
    out = []
    for r in rows[:8]:
        out.append(
            {
                "meet": r.get("title"),
                "starts_at": r.get("starts_at") if r.get("has_time", True) else str(r.get("starts_at") or "")[:10],
                "where": r.get("venue_name"),
            }
        )
    return out


# ── quote check ─────────────────────────────────────────────────────────────────

_QUOTE_CHARS = str.maketrans(
    {
        "‘": "'", "’": "'", "‚": "'", "‛": "'",
        "“": '"', "”": '"', "„": '"', "‟": '"',
        "–": "-", "—": "-", "−": "-", " ": " ",
        "…": "...",
    }
)
# Markdown markup the model may drop or keep when it quotes a line.
_MARKUP = re.compile(r"[*_`#>|~]+")
_SPACE = re.compile(r"\s+")


def normalize_for_quote(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "").translate(_QUOTE_CHARS)
    t = _MARKUP.sub(" ", t)
    # A model quoting a list item or table cell may keep or drop the bullet.
    t = re.sub(r"(?m)^\s*(?:[-+]|\d+[.)])\s+", " ", t)
    t = _SPACE.sub(" ", t)
    return t.strip().casefold()


def quote_in_section(quote: str, body: str) -> bool:
    q = normalize_for_quote(quote).strip(" .,;:\"'")
    if len(q) < _MIN_QUOTE_CHARS:
        return False
    return q in normalize_for_quote(body)


def verify_claims(
    claims: list[dict[str, Any]], by_sid: dict[str, DocSection]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    good: list[dict[str, Any]] = []
    bad: list[dict[str, Any]] = []
    for c in claims:
        sec = by_sid.get(str(c.get("section") or "").strip().upper())
        quote = str(c.get("quote") or "")
        if sec is not None and quote_in_section(quote, sec.body):
            good.append(c)
        else:
            bad.append(c)
    return good, bad


# ── model calls ─────────────────────────────────────────────────────────────────


def _system() -> str:
    from app.context import lingo_constitution, load_prompt

    return load_prompt("community_docs_answer.md") + "\n\n---\n\n" + lingo_constitution()


def _section_payload(s: DocSection) -> dict[str, Any]:
    return {"section": s.sid, "document": s.doc_title, "heading": " › ".join(s.heading_path), "text": s.body}


def _index_payload(s: DocSection) -> dict[str, Any]:
    return {"section": s.sid, "document": s.doc_title, "heading": " › ".join(s.heading_path), "covers": s.covers}


def _recent(history: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for m in (history or [])[-_RECENT_TURNS:]:
        role = str(m.get("role") or "")
        content = str(m.get("content") or "")[:_TURN_CHARS]
        if role and content:
            out.append({"role": role, "content": content})
    return out


def _call(payload: dict[str, Any]) -> dict[str, Any]:
    from app.orchestrator.llm import llm_json, synthesizer_model

    return llm_json(
        model=synthesizer_model(),
        system=_system(),
        user_payload=json.dumps(payload, ensure_ascii=False, default=str),
        max_tokens=1500,
        temperature=0.1,
    )


def _parse_answer(data: dict[str, Any]) -> tuple[str, list[dict[str, Any]], str] | None:
    if str(data.get("action") or "answer").strip().lower() != "answer":
        return None
    reply = str(data.get("reply") or "").strip()
    coverage = str(data.get("coverage") or "").strip().lower()
    if coverage not in ("full", "partial", "none"):
        coverage = "none"
    claims = [c for c in (data.get("claims") or []) if isinstance(c, dict)]
    if not reply:
        return None
    return reply, claims, coverage


def _navigate(
    base: dict[str, Any], sections: list[DocSection], place_id: str
) -> tuple[dict[str, Any], set[str]]:
    """The read / find loop. Returns the final answer object and the sections opened."""
    by_sid = {s.sid: s for s in sections}
    by_id = {s.id: s for s in sections}
    opened: dict[str, DocSection] = {}
    found: list[dict[str, Any]] = []
    budget = _NAV_OPEN_TOKENS
    data: dict[str, Any] = {}
    for step in range(_MAX_STEPS + 1):
        last = step == _MAX_STEPS
        payload = {
            **base,
            "mode": "navigate",
            "section_index": [_index_payload(s) for s in sections],
            "opened_sections": [_section_payload(s) for s in opened.values()],
            "find_results": found,
            "steps_left": _MAX_STEPS - step,
            "must_answer_now": last,
        }
        data = _call(payload)
        action = str(data.get("action") or "answer").strip().lower()
        if action == "answer" or last:
            break
        if action == "read":
            for sid in [str(x).strip().upper() for x in (data.get("sections") or [])][:_MAX_READ_PER_STEP]:
                sec = by_sid.get(sid)
                if sec and sid not in opened and sec.token_count <= budget:
                    opened[sid] = sec
                    budget -= sec.token_count
        elif action == "find":
            query = str(data.get("query") or "").strip()[:120]
            if query:
                found = _find(place_id, list({s.doc_id for s in sections}), query, by_id)
        else:
            break
    return data, set(opened)


def _find(place_id: str, doc_ids: list[str], query: str, by_id: dict[str, DocSection]) -> list[dict[str, Any]]:
    try:
        rows = service_client().rpc(
            "community_doc_find",
            {"p_place_id": place_id, "p_doc_ids": doc_ids, "p_query": query, "p_limit": 8},
        ).execute().data or []
    except Exception:
        logger.warning("community_docs_answer.find_failed q=%r", query, exc_info=True)
        return []
    hits = [by_id[str(r["section_id"])] for r in rows if str(r.get("section_id")) in by_id]
    return [{"query": query, "matching_sections": [h.sid for h in hits]}]


# ── entry point ─────────────────────────────────────────────────────────────────


def answer_from_community_docs(
    *,
    user_id: str | None,
    place_id: str,
    question: str,
    history: list[dict[str, Any]] | None = None,
    community_name: str | None = None,
) -> DocsAnswer | None:
    """None = nothing to answer from (flag off, no readable documents, model down): the
    caller falls back to the community's ordinary about-turn."""
    from app.orchestrator.llm import llm_configured

    if not docs_enabled() or not llm_configured() or not (question or "").strip():
        return None
    t0 = time.monotonic()
    try:
        role = docs_role(place_id, user_id)
        sections = _load_sections(place_id, role)
    except DocsError:
        return None
    except Exception:
        logger.exception("community_docs_answer.load_failed place=%s", place_id)
        return None
    if not sections:
        return None

    by_sid = {s.sid: s for s in sections}
    total = sum(s.token_count for s in sections)
    mode = "full" if total <= FULL_MODE_TOKENS else "navigate"
    base: dict[str, Any] = {
        "community": community_name,
        "asker_role": role,
        "question": question.strip(),
        "recent_turns": _recent(history),
        "live_community_data": _live_facts(place_id, user_id),
    }
    try:
        if mode == "full":
            read = {s.sid for s in sections}
            data = _call({**base, "mode": "full", "sections": [_section_payload(s) for s in sections]})
        else:
            data, read = _navigate(base, sections, place_id)
        parsed = _parse_answer(data)
        if parsed is None:
            logger.warning("community_docs_answer.no_answer place=%s mode=%s", place_id, mode)
            return None
        reply, claims, coverage = parsed
        claims_total = len(claims)
        good, bad = verify_claims(claims, by_sid)
        unproven = coverage != "none" and not claims
        retried = False

        if bad or unproven:
            retried = True
            # What it opened plus anything it cited, so the retry can see the text it got wrong.
            cited = {str(c.get("section") or "").strip().upper() for c in claims}
            readable = [s for s in sections if s.sid in read or s.sid in cited]
            retry_payload = {
                **base,
                "mode": "full",
                "sections": [_section_payload(s) for s in readable],
                "previous_answer": {"reply": reply, "claims": claims, "coverage": coverage},
                "verification_failed": [
                    {"section": c.get("section"), "quote": c.get("quote"), "problem": "this exact text is not in that section"}
                    for c in bad
                ]
                or [{"problem": "a factual answer came with no supporting quotes"}],
            }
            retry = _parse_answer(_call(retry_payload))
            if retry is not None:
                r_reply, r_claims, r_cov = retry
                r_good, r_bad = verify_claims(r_claims, by_sid)
                if not r_bad and (r_cov == "none" or r_claims):
                    reply, claims, coverage, good, bad = r_reply, r_claims, r_cov, r_good, []
                    claims_total = len(r_claims)
                else:
                    good, bad = r_good, r_bad
                    claims_total = len(r_claims)
            if bad or (coverage != "none" and not good):
                reply, coverage = _from_verified_only(base, good, by_sid)
                claims = good

        sources = _sources(good, by_sid)
        answer = DocsAnswer(
            reply=reply,
            coverage=coverage,
            sources=sources,
            mode=mode,
            claims_total=claims_total,
            claims_dropped=len(bad),
            retried=retried,
            sections_read=[by_sid[s].id for s in read if s in by_sid],
        )
    except Exception:
        logger.exception("community_docs_answer.failed place=%s", place_id)
        return None

    _audit(place_id, user_id, question, answer, int((time.monotonic() - t0) * 1000))
    logger.info(
        "community_docs_answer place=%s mode=%s coverage=%s claims=%d dropped=%d retried=%s turns=%d",
        place_id, mode, answer.coverage, answer.claims_total, answer.claims_dropped, answer.retried,
        len(base["recent_turns"]),
    )
    return answer


def _from_verified_only(
    base: dict[str, Any], good: list[dict[str, Any]], by_sid: dict[str, DocSection]
) -> tuple[str, str]:
    """Last resort: the reply is rewritten from the claims that passed the check, and
    nothing else. No verified claims → an honest 'the documents don't say'."""
    payload = {
        **base,
        "mode": "verified_only",
        "verified_facts": [
            {"fact": c.get("fact"), "quote": c.get("quote"), "section": c.get("section"),
             "document": by_sid[str(c.get("section")).upper()].doc_title}
            for c in good
            if str(c.get("section") or "").upper() in by_sid
        ],
    }
    parsed = _parse_answer(_call(payload))
    if parsed is None:
        raise RuntimeError("verified_only_failed")
    reply, _, coverage = parsed
    if not good:
        coverage = "none"
    elif coverage == "full":
        coverage = "partial"
    return reply, coverage


def _sources(good: list[dict[str, Any]], by_sid: dict[str, DocSection]) -> list[dict[str, str]]:
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for c in good:
        sec = by_sid.get(str(c.get("section") or "").upper())
        if sec and sec.id not in seen:
            seen.add(sec.id)
            out.append({"document": sec.doc_title, "section": " › ".join(sec.heading_path[1:]) or sec.doc_title})
    return out


def _audit(place_id: str, user_id: str | None, question: str, a: DocsAnswer, latency_ms: int) -> None:
    try:
        service_client().table("community_doc_answers").insert(
            {
                "place_id": place_id,
                "user_id": user_id,
                "question": question[:1000],
                "mode": a.mode,
                "sections_read": a.sections_read,
                "coverage": a.coverage,
                "claims_total": a.claims_total,
                "claims_dropped": a.claims_dropped,
                "retried": a.retried,
                "latency_ms": latency_ms,
            }
        ).execute()
    except Exception:
        logger.warning("community_docs_answer.audit_failed place=%s", place_id, exc_info=True)


def knowledge_gaps(*, user_id: str, place_id: str, limit: int = 50) -> list[dict[str, Any]]:
    """Questions the documents could not fully answer — the owner's to-do list."""
    from app.community_docs import ROLE_MANAGER

    if docs_role(place_id, user_id) != ROLE_MANAGER:
        raise DocsError("not_manager", 403)
    rows = (
        service_client()
        .table("community_doc_answers")
        .select("question, coverage, created_at")
        .eq("place_id", place_id)
        .neq("coverage", "full")
        .order("created_at", desc=True)
        .limit(max(1, min(limit, 200)))
        .execute()
        .data
        or []
    )
    return rows
