"""Community knowledge documents: upload, ingest, list, preview, visibility, delete.

An owner or co-owner uploads MD / DOCX / PDF. The file goes to the private
community-docs bucket, a community_documents row is written, and ingest runs in the
background: convert to Markdown → split at headings → one model call writes a "covers:"
line per section and a document summary → community_doc_replace_sections swaps the
sections in one transaction. Answering lives in app/community_docs_answer.py.

Everything here runs as the service role; the caller's role comes from
community_docs_role() before any read or write.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from app.auth import service_client
from app.community_docs_convert import (
    KIND_MIME,
    ConvertError,
    Section,
    approx_tokens,
    detect_kind,
    split_sections,
    to_markdown,
)

logger = logging.getLogger(__name__)

BUCKET = "community-docs"
MAX_BYTES = 10 * 1024 * 1024
MAX_DOCS_PER_COMMUNITY = 25
# Per-section text the summariser sees, and how much it sees per call.
_SUMMARY_SECTION_CHARS = 2400
_SUMMARY_BATCH_CHARS = 60000

ROLE_MANAGER = "manager"
ROLE_MEMBER = "member"
ROLE_VISITOR = "visitor"


def docs_enabled() -> bool:
    return os.environ.get("LANA_COMMUNITY_DOCS", "0").strip().lower() not in {"", "0", "false", "off"}


class DocsError(Exception):
    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code = code
        self.status = status


def _is_uuid(value: Any) -> bool:
    try:
        uuid.UUID(str(value))
    except (ValueError, TypeError):
        return False
    return True


def docs_role(place_id: str, user_id: str | None) -> str:
    """manager | member | visitor. Raises place_not_found."""
    if not _is_uuid(place_id):
        raise DocsError("place_not_found", 404)
    res = service_client().rpc(
        "community_docs_role",
        {"p_place_id": place_id, "p_user_id": user_id if _is_uuid(user_id) else None},
    ).execute()
    role = str(res.data or "")
    if role == "place_not_found" or role not in (ROLE_MANAGER, ROLE_MEMBER, ROLE_VISITOR):
        raise DocsError("place_not_found", 404)
    return role


def visible_visibilities(role: str) -> tuple[str, ...]:
    return ("members", "public") if role in (ROLE_MANAGER, ROLE_MEMBER) else ("public",)


def _require_manager(place_id: str, user_id: str) -> None:
    if docs_role(place_id, user_id) != ROLE_MANAGER:
        raise DocsError("not_manager", 403)


def _doc(doc_id: str) -> dict[str, Any]:
    if not _is_uuid(doc_id):
        raise DocsError("doc_not_found", 404)
    res = (
        service_client()
        .table("community_documents")
        .select("*")
        .eq("id", doc_id)
        .limit(1)
        .execute()
    )
    rows = res.data or []
    if not rows:
        raise DocsError("doc_not_found", 404)
    return rows[0]


_LIST_FIELDS = (
    "id, place_id, title, source_mime, status, error, visibility, summary, token_count, "
    "version, created_at, updated_at"
)


def _public_row(row: dict[str, Any], *, manager: bool) -> dict[str, Any]:
    out = {
        "doc_id": row.get("id"),
        "title": row.get("title"),
        "summary": row.get("summary"),
        "visibility": row.get("visibility"),
        "updated_at": row.get("updated_at"),
    }
    if manager:
        out.update(
            {
                "status": row.get("status"),
                "error": row.get("error"),
                "source_mime": row.get("source_mime"),
                "token_count": row.get("token_count"),
                "version": row.get("version"),
                "created_at": row.get("created_at"),
            }
        )
    return out


# ── upload ──────────────────────────────────────────────────────────────────────


def upload_document(
    *,
    user_id: str,
    place_id: str,
    filename: str | None,
    content_type: str | None,
    raw: bytes,
    title: str | None = None,
    visibility: str | None = None,
    replace_doc_id: str | None = None,
) -> dict[str, Any]:
    """Store the file and its row (status 'uploaded'). The caller schedules
    ingest_document(doc_id). With replace_doc_id the same row gets the new file and
    version + 1, so the document keeps its id and visibility."""
    _require_manager(place_id, user_id)
    if not raw:
        raise DocsError("empty_file")
    if len(raw) > MAX_BYTES:
        raise DocsError("file_too_large", 413)
    kind = detect_kind(filename, content_type)
    if not kind:
        raise DocsError("type_not_allowed", 415)
    vis = (visibility or "").strip().lower() or None
    if vis is not None and vis not in ("members", "public"):
        raise DocsError("invalid_visibility")
    name = (title or "").strip() or _title_from_filename(filename)

    client = service_client()
    if replace_doc_id:
        old = _doc(replace_doc_id)
        if old.get("place_id") != place_id:
            raise DocsError("doc_not_found", 404)
        doc_id = str(old["id"])
        version = int(old.get("version") or 1) + 1
    else:
        count = (
            client.table("community_documents")
            .select("id", count="exact")
            .eq("place_id", place_id)
            .execute()
        )
        if (count.count or 0) >= MAX_DOCS_PER_COMMUNITY:
            raise DocsError("too_many_docs", 409)
        doc_id = str(uuid.uuid4())
        version = 1

    path = f"{place_id}/{doc_id}/v{version}.{kind}"
    client.storage.from_(BUCKET).upload(
        path, raw, file_options={"content-type": KIND_MIME[kind], "upsert": "true"}
    )
    if replace_doc_id:
        patch: dict[str, Any] = {
            "storage_path": path,
            "source_mime": KIND_MIME[kind],
            "version": version,
            "status": "uploaded",
            "error": None,
            "uploaded_by": user_id,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if title and title.strip():
            patch["title"] = name
        if vis:
            patch["visibility"] = vis
        res = client.table("community_documents").update(patch).eq("id", doc_id).execute()
    else:
        res = (
            client.table("community_documents")
            .insert(
                {
                    "id": doc_id,
                    "place_id": place_id,
                    "title": name,
                    "source_mime": KIND_MIME[kind],
                    "storage_path": path,
                    "uploaded_by": user_id,
                    "visibility": vis or "members",
                    "version": version,
                }
            )
            .execute()
        )
    row = (res.data or [{}])[0] or _doc(doc_id)
    logger.info(
        "community_docs.uploaded place=%s doc=%s kind=%s bytes=%d v=%d",
        place_id, doc_id, kind, len(raw), version,
    )
    return _public_row(row, manager=True)


def _title_from_filename(filename: str | None) -> str:
    base = os.path.basename((filename or "").strip()) or "Document"
    stem = base.rsplit(".", 1)[0] if "." in base else base
    stem = stem.replace("_", " ").replace("-", " ").strip()
    return (stem or "Document")[:200]


# ── ingest ──────────────────────────────────────────────────────────────────────


def _set_status(doc_id: str, status: str, error: str | None = None) -> None:
    service_client().table("community_documents").update(
        {"status": status, "error": error}
    ).eq("id", doc_id).execute()


def ingest_document(doc_id: str) -> bool:
    """Background job. Never raises: a failure is written onto the row as status
    'failed' + an error code the edit sheet can show."""
    try:
        row = _doc(doc_id)
    except Exception:
        logger.exception("community_docs.ingest_missing doc=%s", doc_id)
        return False
    expected_path = row.get("storage_path")
    try:
        _set_status(doc_id, "processing")
        raw = service_client().storage.from_(BUCKET).download(expected_path)
        kind = detect_kind(expected_path, row.get("source_mime"))
        markdown = to_markdown(raw, kind or "")
        sections = split_sections(markdown, str(row.get("title") or ""))
        if not sections:
            raise ConvertError("no_text_found")
        summary = summarize_sections(str(row.get("title") or ""), sections)
        # A newer upload replaced the file while this one ran: leave the row to that job.
        if _doc(doc_id).get("storage_path") != expected_path:
            logger.info("community_docs.ingest_superseded doc=%s", doc_id)
            return False
        n = service_client().rpc(
            "community_doc_replace_sections",
            {
                "p_doc_id": doc_id,
                "p_sections": [s.as_row() for s in sections],
                "p_markdown": markdown,
                "p_summary": summary,
                "p_token_count": approx_tokens(markdown),
            },
        ).execute()
        logger.info(
            "community_docs.ingested doc=%s sections=%s tokens=%d",
            doc_id, n.data, approx_tokens(markdown),
        )
        return True
    except ConvertError as exc:
        logger.warning("community_docs.ingest_convert_failed doc=%s code=%s", doc_id, exc.code)
        _safe_fail(doc_id, exc.code)
    except Exception:
        logger.exception("community_docs.ingest_failed doc=%s", doc_id)
        _safe_fail(doc_id, "ingest_failed")
    return False


def _safe_fail(doc_id: str, code: str) -> None:
    try:
        _set_status(doc_id, "failed", code)
    except Exception:
        logger.exception("community_docs.ingest_status_write_failed doc=%s", doc_id)


def _summary_system() -> str:
    from app.context import load_prompt

    return load_prompt("community_docs_summarize.md")


def _summary_batches(sections: list[Section]) -> list[list[Section]]:
    batches: list[list[Section]] = []
    cur: list[Section] = []
    size = 0
    for s in sections:
        n = min(len(s.body), _SUMMARY_SECTION_CHARS) + 200
        if cur and size + n > _SUMMARY_BATCH_CHARS:
            batches.append(cur)
            cur, size = [], 0
        cur.append(s)
        size += n
    if cur:
        batches.append(cur)
    return batches


def summarize_sections(title: str, sections: list[Section]) -> str:
    """Fills each section's .covers in place; returns the document summary. A failed
    call leaves covers as the heading path, so a document is never lost to the model."""
    from app.orchestrator.llm import llm_configured, llm_json, router_model

    for s in sections:
        s.covers = " › ".join(s.heading_path[1:]) or s.heading_path[0]
    if not llm_configured():
        return ""
    summaries: list[str] = []
    batches = _summary_batches(sections)
    for i, batch in enumerate(batches):
        payload = {
            "document_title": title,
            "part": f"{i + 1} of {len(batches)}",
            "sections": [
                {
                    "ord": s.ord,
                    "heading_path": s.heading_path,
                    "text": s.body[:_SUMMARY_SECTION_CHARS],
                }
                for s in batch
            ],
        }
        try:
            data = llm_json(
                model=router_model(),
                system=_summary_system(),
                user_payload=json.dumps(payload, ensure_ascii=False),
                max_tokens=4096,
                temperature=0.1,
            )
        except Exception:
            logger.warning("community_docs.summarize_failed title=%r part=%d", title, i, exc_info=True)
            continue
        by_ord = {s.ord: s for s in batch}
        for item in data.get("sections") or []:
            if not isinstance(item, dict):
                continue
            try:
                ord_ = int(item.get("ord"))
            except (TypeError, ValueError):
                continue
            covers = str(item.get("covers") or "").strip()
            if ord_ in by_ord and covers:
                by_ord[ord_].covers = covers[:400]
        part_summary = str(data.get("summary") or "").strip()
        if part_summary:
            summaries.append(part_summary)
    return " ".join(summaries)[:1200]


# ── reads for the edit sheet ────────────────────────────────────────────────────


def list_documents(*, user_id: str | None, place_id: str) -> dict[str, Any]:
    role = docs_role(place_id, user_id)
    q = (
        service_client()
        .table("community_documents")
        .select(_LIST_FIELDS)
        .eq("place_id", place_id)
        .order("created_at", desc=True)
    )
    if role != ROLE_MANAGER:
        q = q.eq("status", "ready").in_("visibility", list(visible_visibilities(role)))
    rows = q.execute().data or []
    return {
        "you": role,
        "can_manage": role == ROLE_MANAGER,
        "max_docs": MAX_DOCS_PER_COMMUNITY,
        "documents": [_public_row(r, manager=role == ROLE_MANAGER) for r in rows],
    }


def preview_document(*, user_id: str, doc_id: str) -> dict[str, Any]:
    """What Lana reads: the converted Markdown and its sections. Managers only — the
    owner checks a garbled PDF here before members ask anything."""
    row = _doc(doc_id)
    _require_manager(str(row["place_id"]), user_id)
    sections = (
        service_client()
        .table("community_doc_sections")
        .select("id, ord, heading_path, covers, token_count")
        .eq("doc_id", doc_id)
        .order("ord")
        .execute()
        .data
        or []
    )
    return {
        **_public_row(row, manager=True),
        "markdown": row.get("markdown"),
        "sections": sections,
    }


def set_visibility(*, user_id: str, doc_id: str, visibility: str) -> dict[str, Any]:
    vis = (visibility or "").strip().lower()
    if vis not in ("members", "public"):
        raise DocsError("invalid_visibility")
    row = _doc(doc_id)
    _require_manager(str(row["place_id"]), user_id)
    res = (
        service_client()
        .table("community_documents")
        .update({"visibility": vis, "updated_at": datetime.now(timezone.utc).isoformat()})
        .eq("id", doc_id)
        .execute()
    )
    return _public_row((res.data or [row])[0], manager=True)


def delete_document(*, user_id: str, doc_id: str) -> None:
    row = _doc(doc_id)
    place_id = str(row["place_id"])
    _require_manager(place_id, user_id)
    client = service_client()
    # Every version of the file, not just the current one.
    try:
        listed = client.storage.from_(BUCKET).list(f"{place_id}/{doc_id}") or []
        paths = [f"{place_id}/{doc_id}/{o['name']}" for o in listed if o.get("name")]
        if paths:
            client.storage.from_(BUCKET).remove(paths)
    except Exception:
        logger.exception("community_docs.delete_storage_failed doc=%s", doc_id)
    client.table("community_documents").delete().eq("id", doc_id).execute()
    logger.info("community_docs.deleted place=%s doc=%s", place_id, doc_id)


# ── what routing sees ───────────────────────────────────────────────────────────


DOCS_HINT_CHARS = 1000


def _fit(items: list[str], sep: str, budget: int) -> str:
    """Join whole items until the next one would not fit — never a cut word."""
    out = ""
    for item in items:
        nxt = f"{out}{sep}{item}" if out else item
        if len(nxt) > budget:
            break
        out = nxt
    return out


def _doc_hint(title: str, sections: list[dict[str, Any]], budget: int) -> str:
    """One document's share of the hint. The rich form (each section's heading with
    its covers line) when it fits; otherwise the headings alone, which name every topic
    in a fraction of the space; otherwise as many headings as fit. A long document can
    never push the others out (2026-10-19: one 400-char cap dropped the whole handbook
    and its questions went unanswered)."""
    head = f"{title}: "
    room = max(budget - len(head), 0)
    headings: list[str] = []
    rich: list[str] = []
    for s in sections:
        path = [p for p in (s.get("heading_path") or []) if p]
        name = path[-1] if len(path) > 1 else ""
        covers = str(s.get("covers") or "").strip()
        if name and name not in headings:
            headings.append(name)
        if name or covers:
            rich.append(f"{name} ({covers})" if name and covers else name or covers)
    full = "; ".join(rich)
    if len(full) <= room:
        return head + full if full else title
    plain = ", ".join(headings)
    if plain and len(plain) <= room:
        # Every heading fits: give sections their covers line, in order, while it still
        # fits — the topics stay complete and the detail goes as far as the room allows.
        items = list(headings)
        details = {}
        for s in sections:
            path = [p for p in (s.get("heading_path") or []) if p]
            name = path[-1] if len(path) > 1 else ""
            covers = str(s.get("covers") or "").strip()
            if name and covers and name not in details:
                details[name] = covers
        for i, name in enumerate(items):
            if name in details:
                trial = items[:i] + [f"{name} ({details[name]})"] + items[i + 1:]
                if len(", ".join(trial)) <= room:
                    items = trial
        return head + ", ".join(items)
    fitted = _fit(headings, ", ", room) if headings else _fit(rich, "; ", room)
    return head + fitted if fitted else title[:budget]


def docs_cover_line(place_id: str, *, max_chars: int = DOCS_HINT_CHARS) -> str | None:
    """What the community's documents cover, for the routing model's context: each
    document's title with its section headings and covers lines, every document given
    an equal share of max_chars. A hint about WHAT they cover — never their content,
    and the answer path re-checks visibility for the asker."""
    if not docs_enabled() or not _is_uuid(place_id):
        return None
    client = service_client()
    docs = (
        client.table("community_documents")
        .select("id, title")
        .eq("place_id", place_id)
        .order("created_at")
        .execute()
        .data
        or []
    )
    if not docs:
        return None
    rows = (
        client.table("community_doc_sections")
        .select("doc_id, ord, heading_path, covers")
        .eq("place_id", place_id)
        .order("ord")
        .limit(600)
        .execute()
        .data
        or []
    )
    by_doc: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_doc.setdefault(str(r.get("doc_id")), []).append(r)
    # Only documents with sections (a last good ingest) are anything Lana can answer from.
    live = [d for d in docs if str(d["id"]) in by_doc]
    if not live:
        return None
    sep = " | "
    room = max_chars - len(sep) * (len(live) - 1)
    # Equal shares, smallest need first: a document that needs less than its share
    # hands the rest to the ones still waiting, so short documents never waste space a
    # long one could use.
    need = {str(d["id"]): len(_doc_hint(str(d.get("title") or ""), by_doc[str(d["id"])], 10**6)) for d in live}
    budget: dict[str, int] = {}
    left, waiting = room, sorted(live, key=lambda d: need[str(d["id"])])
    for i, d in enumerate(waiting):
        share = max(left // (len(waiting) - i), 40)
        budget[str(d["id"])] = min(need[str(d["id"])], share)
        left -= budget[str(d["id"])]
    parts = [_doc_hint(str(d.get("title") or ""), by_doc[str(d["id"])], budget[str(d["id"])]) for d in live]
    return sep.join(parts)[:max_chars] or None
