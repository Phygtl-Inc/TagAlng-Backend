"""Community knowledge documents: upload, ingest, list, preview, visibility, review, delete.

Any member uploads MD / DOCX / PDF. The file goes to the private community-docs bucket, a
community_documents row is written, and ingest runs in the background: convert to
Markdown → split at headings → one model call writes a "covers:" line per section and a
document summary → community_doc_replace_sections swaps the sections in one transaction.
Answering lives in app/community_docs_answer.py.

Who reads what is the community's own ladder (20270213120000): visitor < member < the
levels the owner named < owner. A document names the lowest level that may read it; an
upload from someone who cannot approve waits as 'pending' until an approver publishes it.

Everything here runs as the service role; the caller's rights come from
community_docs_access() before any read or write, and what Lana may answer from comes
from community_docs_readable() — both in SQL.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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


VISIBILITIES = ("public", "members", "owners")
RANK_VISITOR = -1
RANK_MEMBER = 0
RANK_OWNER = 1000


@dataclass(frozen=True)
class DocsAccess:
    role: str
    rank: int
    level_id: str | None = None
    level_name: str | None = None
    can_upload: bool = False
    can_approve: bool = False
    can_read_log: bool = False

    @property
    def manager(self) -> bool:
        return self.role == ROLE_MANAGER


def docs_access(place_id: str, user_id: str | None) -> DocsAccess:
    """The caller's rank on the community's ladder and what it lets them do. Raises
    place_not_found."""
    if not _is_uuid(place_id):
        raise DocsError("place_not_found", 404)
    res = service_client().rpc(
        "community_docs_access",
        {"p_place_id": place_id, "p_user_id": user_id if _is_uuid(user_id) else None},
    ).execute()
    data = res.data if isinstance(res.data, dict) else {}
    role = str(data.get("role") or "")
    if role not in (ROLE_MANAGER, ROLE_MEMBER, ROLE_VISITOR):
        raise DocsError("place_not_found", 404)
    return DocsAccess(
        role=role,
        rank=int(data.get("rank") if data.get("rank") is not None else RANK_VISITOR),
        level_id=data.get("level_id"),
        level_name=data.get("level_name"),
        can_upload=bool(data.get("can_upload")),
        can_approve=bool(data.get("can_approve")),
        can_read_log=bool(data.get("can_read_log")),
    )


def docs_role(place_id: str, user_id: str | None) -> str:
    """manager | member | visitor. Raises place_not_found."""
    return docs_access(place_id, user_id).role


def readable_doc_ids(place_id: str, user_id: str | None) -> list[str]:
    """The documents Lana may answer this person from — decided in SQL, never here."""
    res = service_client().rpc(
        "community_docs_readable",
        {"p_place_id": place_id, "p_user_id": user_id if _is_uuid(user_id) else None},
    ).execute()
    out: list[str] = []
    for r in res.data or []:
        v = r.get("community_docs_readable") if isinstance(r, dict) else r
        if v:
            out.append(str(v))
    return out


def _levels(place_id: str) -> dict[str, dict[str, Any]]:
    rows = (
        service_client()
        .table("community_levels")
        .select("id, name, rank")
        .eq("place_id", place_id)
        .order("rank")
        .execute()
        .data
        or []
    )
    return {str(r["id"]): {"id": str(r["id"]), "name": r.get("name"), "rank": int(r.get("rank") or 0)} for r in rows}


def doc_rank(row: dict[str, Any], levels: dict[str, dict[str, Any]]) -> int:
    """Mirrors community_doc_rank(): a level that has gone missing reads as owners-only."""
    vis = row.get("visibility")
    if vis == "public":
        return RANK_VISITOR
    if vis == "owners":
        return RANK_OWNER
    level_id = row.get("min_level_id")
    if not level_id:
        return RANK_MEMBER
    level = levels.get(str(level_id))
    return int(level["rank"]) if level else RANK_OWNER


def _target(
    visibility: str | None, level_id: str | None, levels: dict[str, dict[str, Any]]
) -> tuple[str, str | None]:
    """(visibility, min_level_id) for a requested audience. A level means 'members'."""
    vis = (visibility or "").strip().lower() or "members"
    lvl = (level_id or "").strip() or None
    if lvl:
        if lvl not in levels:
            raise DocsError("level_not_found", 404)
        return "members", lvl
    if vis not in VISIBILITIES:
        raise DocsError("invalid_visibility")
    return vis, None


def _require_manager(place_id: str, user_id: str) -> DocsAccess:
    acc = docs_access(place_id, user_id)
    if not acc.manager:
        raise DocsError("not_manager", 403)
    return acc


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
    "id, place_id, title, source_mime, status, error, visibility, min_level_id, review_status, "
    "uploaded_by, summary, token_count, version, created_at, updated_at, "
    "source_kind, source_url, last_synced_at, sync_error"
)


def _public_row(
    row: dict[str, Any],
    *,
    manager: bool,
    levels: dict[str, dict[str, Any]] | None = None,
    viewer: str | None = None,
    uploader_name: str | None = None,
) -> dict[str, Any]:
    """manager=True adds the processing details — for owners, the uploader of their own
    document, and an approver looking at the review queue."""
    level = (levels or {}).get(str(row.get("min_level_id") or ""))
    out = {
        "doc_id": row.get("id"),
        "title": row.get("title"),
        "summary": row.get("summary"),
        "visibility": row.get("visibility"),
        "level_id": level["id"] if level else None,
        "level_name": level["name"] if level else None,
        "review_status": row.get("review_status") or "approved",
        "mine": bool(viewer) and str(row.get("uploaded_by") or "") == str(viewer),
        "updated_at": row.get("updated_at"),
        "source_kind": row.get("source_kind") or "upload",
    }
    if uploader_name is not None:
        out["uploaded_by_name"] = uploader_name
    if manager:
        out.update(
            {
                "status": row.get("status"),
                "error": row.get("error"),
                "source_mime": row.get("source_mime"),
                "token_count": row.get("token_count"),
                "version": row.get("version"),
                "created_at": row.get("created_at"),
                "source_url": row.get("source_url"),
                "last_synced_at": row.get("last_synced_at"),
                "sync_error": row.get("sync_error"),
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
    level_id: str | None = None,
    replace_doc_id: str | None = None,
) -> dict[str, Any]:
    """Store the file and its row (status 'uploaded'). The caller schedules
    ingest_document(doc_id). With replace_doc_id (owners only) the same row gets the new
    file and version + 1, so the document keeps its id and audience.

    Any member may upload, for their own level or below. Owners' and approvers' uploads
    are live at once; anyone else's wait as 'pending' for an approver."""
    acc = docs_access(place_id, user_id)
    if replace_doc_id and not acc.manager:
        raise DocsError("not_manager", 403)
    if not acc.can_upload:
        raise DocsError("not_member", 403)
    if not raw:
        raise DocsError("empty_file")
    if len(raw) > MAX_BYTES:
        raise DocsError("file_too_large", 413)
    kind = detect_kind(filename, content_type)
    if not kind:
        raise DocsError("type_not_allowed", 415)
    levels = _levels(place_id)
    asked = bool((visibility or "").strip() or (level_id or "").strip())
    vis, min_level = _target(visibility, level_id, levels)
    if doc_rank({"visibility": vis, "min_level_id": min_level}, levels) > acc.rank:
        raise DocsError("level_too_high", 403)
    review = "approved" if acc.can_approve else "pending"
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
        if asked:
            patch["visibility"] = vis
            patch["min_level_id"] = min_level
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
                    "visibility": vis,
                    "min_level_id": min_level,
                    "review_status": review,
                    "version": version,
                }
            )
            .execute()
        )
    row = (res.data or [{}])[0] or _doc(doc_id)
    logger.info(
        "community_docs.uploaded place=%s doc=%s kind=%s bytes=%d v=%d review=%s",
        place_id, doc_id, kind, len(raw), version, row.get("review_status"),
    )
    return _public_row(row, manager=True, levels=levels, viewer=user_id)


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


# ── Google Drive links ──────────────────────────────────────────────────────────

LINK_REFRESH_AFTER = timedelta(hours=24)
# One refresh attempt per document per hour per process, so a busy community's
# list / answer calls never stampede Google with the same download.
_LINK_RETRY_GAP_S = 3600
_link_attempts: dict[str, float] = {}
_link_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def link_document(
    *,
    user_id: str,
    place_id: str,
    url: str,
    title: str | None = None,
    visibility: str | None = None,
    level_id: str | None = None,
) -> dict[str, Any]:
    """A document from a shared Google Drive link. The file goes through upload_document,
    so linking follows every upload rule (who may add, which audience, approval); then the
    row is marked as linked so it is re-checked when stale. The caller schedules
    ingest_document(doc_id)."""
    from app.community_docs_drive import DriveError, fetch_drive_file

    acc = docs_access(place_id, user_id)
    if not acc.can_upload:
        raise DocsError("not_member", 403)
    try:
        got = fetch_drive_file(url, max_bytes=MAX_BYTES)
    except DriveError as exc:
        raise DocsError(exc.code, exc.status) from exc
    row = upload_document(
        user_id=user_id,
        place_id=place_id,
        filename=got.filename,
        content_type=got.content_type,
        raw=got.raw,
        title=title,
        visibility=visibility,
        level_id=level_id,
    )
    drive_fields = {
        "source_kind": "drive",
        "source_url": url.strip()[:2048],
        "content_hash": hashlib.sha256(got.raw).hexdigest(),
        "last_synced_at": _now_iso(),
        "sync_error": None,
    }
    service_client().table("community_documents").update(drive_fields).eq(
        "id", str(row["doc_id"])
    ).execute()
    return {
        **row,
        "source_kind": "drive",
        "source_url": drive_fields["source_url"],
        "last_synced_at": drive_fields["last_synced_at"],
        "sync_error": None,
    }


def _replace_file(row: dict[str, Any], *, filename: str, content_type: str, raw: bytes, digest: str) -> None:
    """A refreshed Drive file as the document's next version. Only the file changes: the
    audience, the approval state and who added it stay exactly as they were."""
    kind = detect_kind(filename, content_type)
    if not kind:
        raise DocsError("type_not_allowed", 415)
    doc_id = str(row["id"])
    version = int(row.get("version") or 1) + 1
    path = f"{row['place_id']}/{doc_id}/v{version}.{kind}"
    client = service_client()
    client.storage.from_(BUCKET).upload(
        path, raw, file_options={"content-type": KIND_MIME[kind], "upsert": "true"}
    )
    client.table("community_documents").update(
        {
            "storage_path": path,
            "source_mime": KIND_MIME[kind],
            "version": version,
            "status": "uploaded",
            "error": None,
            "content_hash": digest,
            "last_synced_at": _now_iso(),
            "sync_error": None,
            "updated_at": _now_iso(),
        }
    ).eq("id", doc_id).execute()


def refresh_document(doc_id: str, *, user_id: str | None = None) -> str:
    """Fetch a Drive document's link again. Returns 'updated' (new version stored, caller
    ingests), 'unchanged', or 'failed' (sync_error written; the last good version keeps
    answering). With user_id the caller must be an owner or the one who added the link;
    without, it is the worker's own scheduled refresh."""
    from app.community_docs_drive import DriveError, fetch_drive_file

    row = _doc(doc_id)
    place_id = str(row["place_id"])
    if user_id is not None:
        acc = docs_access(place_id, user_id)
        if not (acc.manager or str(row.get("uploaded_by") or "") == str(user_id)):
            raise DocsError("not_manager", 403)
    if row.get("source_kind") != "drive" or not row.get("source_url"):
        raise DocsError("not_a_link")
    client = service_client()
    try:
        got = fetch_drive_file(str(row["source_url"]), max_bytes=MAX_BYTES)
    except DriveError as exc:
        client.table("community_documents").update({"sync_error": exc.code}).eq("id", doc_id).execute()
        logger.info("community_docs.link_refresh_failed doc=%s code=%s", doc_id, exc.code)
        return "failed"
    digest = hashlib.sha256(got.raw).hexdigest()
    if digest == row.get("content_hash") and row.get("status") != "failed":
        client.table("community_documents").update(
            {"last_synced_at": _now_iso(), "sync_error": None}
        ).eq("id", doc_id).execute()
        return "unchanged"
    try:
        _replace_file(row, filename=got.filename, content_type=got.content_type, raw=got.raw, digest=digest)
    except DocsError as exc:
        client.table("community_documents").update({"sync_error": exc.code}).eq("id", doc_id).execute()
        return "failed"
    logger.info("community_docs.link_refreshed doc=%s", doc_id)
    return "updated"


def _claim_attempt(doc_id: str) -> bool:
    now = time.monotonic()
    with _link_lock:
        last = _link_attempts.get(doc_id)
        if last is not None and now - last < _LINK_RETRY_GAP_S:
            return False
        _link_attempts[doc_id] = now
        return True


def _stale_links(place_id: str | None, limit: int) -> list[dict[str, Any]]:
    # 'Z', never '+00:00': a '+' in the PostgREST query string reads as a space.
    cutoff = (datetime.now(timezone.utc) - LINK_REFRESH_AFTER).strftime("%Y-%m-%dT%H:%M:%SZ")
    q = (
        service_client()
        .table("community_documents")
        .select("id, last_synced_at")
        .eq("source_kind", "drive")
        .or_(f"last_synced_at.is.null,last_synced_at.lt.{cutoff}")
        .order("last_synced_at", desc=False, nullsfirst=True)
        .limit(limit)
    )
    if place_id:
        q = q.eq("place_id", place_id)
    return q.execute().data or []


def refresh_stale_links(place_id: str | None = None, *, limit: int = 3) -> dict[str, int]:
    """Re-check Drive links older than LINK_REFRESH_AFTER and ingest any that changed."""
    counts = {"updated": 0, "unchanged": 0, "failed": 0, "skipped": 0}
    try:
        rows = _stale_links(place_id, limit)
    except Exception:
        logger.exception("community_docs.stale_links_read_failed place=%s", place_id)
        return counts
    for r in rows:
        doc_id = str(r["id"])
        if not _claim_attempt(doc_id):
            counts["skipped"] += 1
            continue
        try:
            outcome = refresh_document(doc_id)
        except Exception:
            logger.exception("community_docs.link_refresh_crashed doc=%s", doc_id)
            outcome = "failed"
        counts[outcome] += 1
        if outcome == "updated":
            ingest_document(doc_id)
    return counts


def refresh_stale_links_later(place_id: str) -> None:
    """Fire-and-forget: the list or answer that triggered it never waits on Google."""
    if not docs_enabled() or not _is_uuid(place_id):
        return
    threading.Thread(
        target=refresh_stale_links, args=(place_id,), daemon=True, name="docs-link-refresh"
    ).start()


# ── reads for the edit sheet ────────────────────────────────────────────────────


def _names(user_ids: list[str]) -> dict[str, str]:
    ids = sorted({u for u in user_ids if _is_uuid(u)})
    if not ids:
        return {}
    rows = service_client().table("users").select("id, nickname").in_("id", ids).execute().data or []
    return {str(r["id"]): (str(r.get("nickname") or "").strip() or "A member") for r in rows}


def list_documents(*, user_id: str | None, place_id: str) -> dict[str, Any]:
    """Owners see every row with its processing status. Everyone else sees what they may
    be answered from (ready), their own uploads in any state, and — for approvers — the
    pending queue up to their own level."""
    acc = docs_access(place_id, user_id)
    levels = _levels(place_id)
    rows = (
        service_client()
        .table("community_documents")
        .select(_LIST_FIELDS)
        .eq("place_id", place_id)
        .order("created_at", desc=True)
        .execute()
        .data
        or []
    )
    refresh_stale_links_later(place_id)
    readable = set(readable_doc_ids(place_id, user_id)) if not acc.manager else set()
    out: list[dict[str, Any]] = []
    pending_uploaders: list[str] = []
    for r in rows:
        mine = bool(user_id) and str(r.get("uploaded_by") or "") == str(user_id)
        review = r.get("review_status") or "approved"
        queue = acc.can_approve and review == "pending" and doc_rank(r, levels) <= acc.rank
        live = str(r["id"]) in readable and r.get("status") == "ready"
        if not (acc.manager or mine or queue or live):
            continue
        out.append(r)
        if review == "pending" and r.get("uploaded_by"):
            pending_uploaders.append(str(r["uploaded_by"]))
    names = _names(pending_uploaders)
    # The audiences this person may give a document: their own level and below.
    offer = [lv for lv in levels.values() if acc.manager or lv["rank"] <= acc.rank]
    return {
        "you": acc.role,
        "your_rank": acc.rank,
        "your_level_name": acc.level_name,
        "can_manage": acc.manager,
        "can_upload": acc.can_upload,
        "can_approve": acc.can_approve,
        "can_read_log": acc.can_read_log,
        "max_docs": MAX_DOCS_PER_COMMUNITY,
        "levels": sorted(offer, key=lambda lv: lv["rank"]),
        "documents": [
            _public_row(
                r,
                manager=acc.manager
                or str(r.get("uploaded_by") or "") == str(user_id or "")
                or (r.get("review_status") == "pending" and acc.can_approve),
                levels=levels,
                viewer=user_id,
                uploader_name=names.get(str(r.get("uploaded_by") or ""))
                if r.get("review_status") == "pending"
                else None,
            )
            for r in out
        ],
    }


def _may_review(acc: DocsAccess, row: dict[str, Any], levels: dict[str, dict[str, Any]]) -> bool:
    return acc.manager or (acc.can_approve and doc_rank(row, levels) <= acc.rank)


def preview_document(*, user_id: str, doc_id: str) -> dict[str, Any]:
    """What Lana reads: the converted Markdown and its sections. Owners, the uploader, and
    an approver reviewing it — the check for a garbled PDF before anyone is answered."""
    row = _doc(doc_id)
    place_id = str(row["place_id"])
    acc = docs_access(place_id, user_id)
    levels = _levels(place_id)
    mine = str(row.get("uploaded_by") or "") == str(user_id)
    if not (mine or _may_review(acc, row, levels)):
        raise DocsError("not_manager", 403)
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
        **_public_row(row, manager=True, levels=levels, viewer=user_id),
        "markdown": row.get("markdown"),
        "sections": sections,
    }


def set_visibility(
    *, user_id: str, doc_id: str, visibility: str | None, level_id: str | None = None
) -> dict[str, Any]:
    row = _doc(doc_id)
    place_id = str(row["place_id"])
    _require_manager(place_id, user_id)
    levels = _levels(place_id)
    vis, min_level = _target(visibility, level_id, levels)
    res = (
        service_client()
        .table("community_documents")
        .update(
            {
                "visibility": vis,
                "min_level_id": min_level,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        .eq("id", doc_id)
        .execute()
    )
    return _public_row((res.data or [row])[0], manager=True, levels=levels, viewer=user_id)


def review_document(*, user_id: str, doc_id: str, approve: bool) -> dict[str, Any]:
    """Publish (or turn down) a member's upload. Only a pending document, and only by
    someone whose level may approve and may read it."""
    row = _doc(doc_id)
    place_id = str(row["place_id"])
    acc = docs_access(place_id, user_id)
    levels = _levels(place_id)
    if not _may_review(acc, row, levels):
        raise DocsError("not_approver", 403)
    if (row.get("review_status") or "approved") != "pending":
        raise DocsError("not_pending", 409)
    now = datetime.now(timezone.utc).isoformat()
    res = (
        service_client()
        .table("community_documents")
        .update(
            {
                "review_status": "approved" if approve else "rejected",
                "reviewed_by": user_id,
                "reviewed_at": now,
                "updated_at": now,
            }
        )
        .eq("id", doc_id)
        .eq("review_status", "pending")
        .execute()
    )
    logger.info(
        "community_docs.reviewed place=%s doc=%s approve=%s by=%s", place_id, doc_id, approve, user_id
    )
    return _public_row((res.data or [row])[0], manager=True, levels=levels, viewer=user_id)


def delete_document(*, user_id: str, doc_id: str) -> None:
    """Owners delete anything; an uploader may withdraw their own upload until it is live."""
    row = _doc(doc_id)
    place_id = str(row["place_id"])
    acc = docs_access(place_id, user_id)
    mine = str(row.get("uploaded_by") or "") == str(user_id)
    if not (acc.manager or (mine and (row.get("review_status") or "approved") != "approved")):
        raise DocsError("not_manager", 403)
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
    and the answer path re-checks visibility for the asker.

    Only live documents every member may read: a heading from a level-restricted or
    still-pending document ("Applications — E-board") never reaches a model's context."""
    if not docs_enabled() or not _is_uuid(place_id):
        return None
    client = service_client()
    docs = [
        d
        for d in (
            client.table("community_documents")
            .select("id, title, visibility, min_level_id, review_status")
            .eq("place_id", place_id)
            .order("created_at")
            .execute()
            .data
            or []
        )
        if (d.get("review_status") or "approved") == "approved"
        and (d.get("visibility") or "members") in ("public", "members")
        and not d.get("min_level_id")
    ]
    if not docs:
        return None
    rows = (
        client.table("community_doc_sections")
        .select("doc_id, ord, heading_path, covers")
        .eq("place_id", place_id)
        .in_("doc_id", [str(d["id"]) for d in docs])
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
